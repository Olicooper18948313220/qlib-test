from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
from filelock import FileLock, Timeout

from .config import ROOT, config_warnings, effective_config, validate_config
from .data.db import iter_stock_frames, trading_dates, validate_database
from .factors.technical import add_technical_features
from .reports import write_report
from .evaluation import create_factor_store, panel_factor_names, store_factor_frame, write_evaluation
from .signals.legacy_v1 import score_signals, signal_lookbacks as legacy_lookbacks
from .signals.zff0708 import score_0708cao, signal_lookbacks as cao_lookbacks
from .backtest.simple import run_backtest
from .experiments import ENGINE_VERSION, atomic_json, set_status
from .jobs import start_job, cancel_job  # compatibility with the original UI imports


class CancelledError(Exception):
    pass


def resolve_request(request):
    config = copy.deepcopy(request["config"]) if request.get("resolved") else effective_config(request.get("config"))
    bt = config["backtest"]
    bt["signal_version"] = config["signals"].get("version", "legacy_v1")
    if bt["signal_version"] == "0708cao":
        # Legacy thresholds do not apply to this rule family.
        config["signals"]["thresholds"] = {}
    config["data"]["database"] = str(Path(request.get("database") or config["data"]["database"]).resolve())
    benchmark_path = config.get("evaluation", {}).get("benchmark", {}).get("path")
    if benchmark_path and not Path(benchmark_path).is_absolute():
        config["evaluation"]["benchmark"]["path"] = str((ROOT / benchmark_path).resolve())
    for key in ("start", "end"):
        bt[key] = str(request.get(key) or bt.get(key) or config["data"][key])
    for key in ("max_stocks", "max_rows"):
        bt[key] = request[key] if request.get(key) is not None else bt.get(key, 0)
    validate_config(config)
    return config


def allocate_run_dir(base=None):
    base = Path(base) if base else ROOT / "runs" / ("run_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not base.is_absolute():
        base = ROOT / base
    # Even failed or empty runs must not overwrite a prior experiment.
    if base.exists():
        base = base.with_name(base.name + "_" + uuid.uuid4().hex[:8])
    base.mkdir(parents=True, exist_ok=False)
    return base


def code_digest():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/qlib_quant").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def warmup_window(config):
    enabled = set(config["signals"]["enabled"])
    lookbacks = cao_lookbacks if config["signals"].get("version") == "0708cao" else legacy_lookbacks
    return max([20, *config["factors"]["windows"],
                *[window for name, window in lookbacks.items() if name in enabled]])


def load_prepared(config, check_cancel, tell, calendar=None, factor_db=None):
    data, bt = config["data"], config["backtest"]
    frames, statistics = [], []
    warmup = warmup_window(config)
    evaluation = config["evaluation"]
    selected_factors = panel_factor_names(evaluation, config["signals"]["enabled"]) if evaluation["enabled"] else []
    selected_signals = list(config["signals"]["enabled"]) if evaluation["enabled"] else []
    calendar_index = {str(pd.Timestamp(day).date()): index for index, day in enumerate(calendar or [])}
    factor_con = create_factor_store(factor_db, selected_factors, selected_signals) if factor_db and evaluation["enabled"] else None
    for frame, count, index, total in iter_stock_frames(
            data["database"], bt["start"], bt["end"], warmup,
            bt["max_stocks"], bt["max_rows"], check_cancel):
        check_cancel()
        code = str(frame.iloc[0]["code"])
        statistics.append({"code": code, "warmup_rows": count, "first_loaded": frame.iloc[0]["date"]})
        features = add_technical_features(frame, windows=config["factors"]["windows"])
        if config["signals"].get("version") == "0708cao":
            scored = score_0708cao(features, config["signals"]["enabled"])
        else:
            scored = score_signals(features, config["signals"]["thresholds"], config["signals"]["enabled"])
        scored = scored[scored["date"].between(bt["start"], bt["end"])]
        if factor_con is not None:
            store_factor_frame(factor_con, scored, calendar_index, selected_factors, selected_signals)
        # Discard warmup and large intermediate feature matrices per stock.
        frame_columns = ["date", "code", "open", "close", "trade_status", "signal", "signal_score"]
        if selected_factors:
            frame_columns.extend(name for name in selected_factors if name in scored and name not in frame_columns)
        frames.append(scored[frame_columns])
        if index % 10 == 0 or index == total or index == 1:
            tell("features", f"已处理 {index}/{total} 只股票（含预热）", 20 + int(40 * index / total))
    if factor_con is not None:
        factor_con.commit()
        factor_con.close()
    if not frames:
        raise ValueError("回测区间没有行情")
    return pd.concat(frames, ignore_index=True), statistics, warmup


def run_request(request, progress=None, run_dir=None, job_dir=None):
    out = Path(run_dir) if run_dir else allocate_run_dir(request.get("output"))
    out.mkdir(parents=True, exist_ok=True)
    cancel_path = Path(job_dir) / "cancel.request" if job_dir else None
    log_path = out / "execution.log"
    metadata = {"engine_version": ENGINE_VERSION, "code_digest": code_digest(),
                "created_at": datetime.now().isoformat(), "python": sys.version, "pid": os.getpid(),
                "request": request, "research_approximation": True}
    atomic_json(out / "run_metadata.json", metadata)
    set_status(out, "queued", "等待运行", 0)

    def check_cancel():
        if cancel_path and cancel_path.exists():
            raise CancelledError("用户取消任务")

    def tell(stage, message, percent):
        check_cancel()
        set_status(out, "running", message, percent, stage=stage)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat(timespec='seconds')} {stage} {message}\n")
        if progress:
            progress(stage, message, percent)

    # This OS-backed lock also covers direct CLI runs. It is released by the OS
    # if a process crashes, with no stale-PID deletion required.
    (ROOT / "runs").mkdir(exist_ok=True)
    lock = FileLock(str(ROOT / "runs/.execution.lock"))
    try:
        with lock.acquire(timeout=0):
            config = resolve_request(request)
            bt, database = config["backtest"], config["data"]["database"]
            metadata.update(effective_config=config, database=database, start=bt["start"], end=bt["end"],
                            config_warnings=config_warnings(config), price_adjustment="hfq")
            atomic_json(out / "run_metadata.json", metadata)
            tell("validate", "检查数据库结构、日期、重复行情和价格", 5)
            manifest = validate_database(database, check_cancel)
            atomic_json(out / "data_manifest.json", manifest.__dict__)
            metadata["data_manifest"] = manifest.__dict__
            problems = {k: getattr(manifest, k) for k in ("duplicate_rows", "invalid_price_rows", "invalid_date_rows")
                        if getattr(manifest, k)}
            if problems:
                raise ValueError(f"数据库质量检查未通过: {problems}")
            if manifest.adjustment != "hfq":
                raise ValueError("数据库复权口径不是 hfq")
            if request.get("action") == "validate":
                atomic_json(out / "run_metadata.json", metadata)
                check_cancel()
                set_status(out, "success", "数据检查完成", 100, output=str(out))
                return out
            if bt["start"] < manifest.first_date or bt["end"] > manifest.last_date:
                raise ValueError(f"回测区间必须位于数据范围 {manifest.first_date} 至 {manifest.last_date} 内")
            calendar, source = trading_dates(database, bt["start"], bt["end"], check_cancel)
            tell("features", "按股票加载历史并计算信号", 20)
            factor_db = out / "evaluation" / ".factor_panel.sqlite" if config["evaluation"]["enabled"] else None
            frame, stocks, warmup = load_prepared(config, check_cancel, tell, calendar, factor_db)
            metadata.update(rows_loaded=len(frame), stocks_loaded=len(stocks), stock_sample=stocks,
                            warmup_rows_required=warmup, calendar_source=source,
                            query_start=min(s["first_loaded"] for s in stocks))
            if any(s["warmup_rows"] < warmup for s in stocks):
                metadata["config_warnings"].append("部分股票预热历史不足，相应长窗口信号暂不可用")
            if manifest.null_trade_status_rows:
                metadata["config_warnings"].append("存在未知交易状态：不推断停牌；尚未模拟涨跌停与真实成交限制")
            metadata["config_warnings"].append("后复权价格用于成交是研究近似；100 股整数手不代表真实交易股数")
            atomic_json(out / "run_metadata.json", metadata)
            tell("backtest", "执行逐日账本", 65)
            equity, trades = run_backtest(
                frame, **{k: bt[k] for k in ("initial_cash", "max_positions", "commission_rate",
                         "stamp_duty_rate", "slippage_bps", "lot_size", "rebalance_weekday")},
                start_date=bt["start"], end_date=bt["end"], calendar=calendar, check_cancel=check_cancel,
                progress=lambda done, total: tell("backtest", f"账本 {done}/{total} 个交易日", 65 + int(20 * done / total)))
            tell("report", "写入报告、逐日持仓和跳过订单", 90)
            write_report(equity, trades, out)
            if config["evaluation"]["enabled"]:
                tell("evaluation", "计算账户绩效、交易与因子诊断", 94)
                positions = equity.attrs.get("positions", pd.DataFrame())
                write_evaluation(equity, trades, positions, bt["initial_cash"], config["evaluation"],
                                 factor_db, config["signals"]["enabled"], check_cancel=check_cancel,
                                 progress=lambda percent, message: tell("evaluation", message, percent))
                Path(factor_db).unlink(missing_ok=True)
            check_cancel()
            set_status(out, "success", "回测完成", 100, output=str(out))
            return out
    except CancelledError:
        set_status(out, "cancelled", "任务已取消", 100)
        return out
    except Exception as exc:
        message = "已有回测或数据检查正在运行" if isinstance(exc, Timeout) else str(exc)
        (out / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        set_status(out, "failed", message, 100, error_type=type(exc).__name__)
        raise
