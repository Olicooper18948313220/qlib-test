"""Local research workbench; all execution uses the same CLI worker service."""
from __future__ import annotations

import io
import hashlib
import sys
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qlib_quant.config import effective_config, config_warnings
from qlib_quant.evaluation import supported_factors
from qlib_quant.experiments import read_json, read_csv, discover_runs, run_id, run_classification, config_diff
from qlib_quant.jobs import start_job, cancel_job, job_status
from qlib_quant.signals.legacy_v1 import signal_names as legacy_signal_names, signal_descriptions as legacy_signal_descriptions
from qlib_quant.signals.zff0708 import (signal_names as cao_signal_names,
                                        signal_descriptions as cao_signal_descriptions,
                                        signal_trigger_descriptions as cao_signal_triggers,
                                        signal_lookbacks as cao_signal_lookbacks)

st.set_page_config(page_title="qlib_quant 研究工作台", layout="wide")
st.title("qlib_quant 本地量化研究工作台")
st.caption("V1 修复版 · 后复权研究近似 · 每次实验保存参数、账本与报告")
cfg = effective_config()
page = st.sidebar.radio("页面", ["首页", "新建回测", "结果与实验", "学习路线", "编辑导航"], key="page")


def navigate(page):
    st.session_state["page"] = page


def jobs():
    return sorted((ROOT / "runs/jobs").glob("*"), reverse=True)


def launch(request):
    try:
        job, _ = start_job(request)
        st.success(f"任务已提交：{job.name}。在首页查看进度，完成后到结果与实验查看。")
    except (ValueError, RuntimeError, OSError) as exc:
        st.error(str(exc))


if page == "首页":
    c1, c2, c3 = st.columns(3)
    c1.metric("Python", f"{sys.version_info.major}.{sys.version_info.minor}")
    c2.metric("数据库", "存在" if Path(cfg["data"]["database"]).exists() else "未找到")
    c3.metric("可查看实验", len(discover_runs(ROOT / "runs")))
    st.caption(cfg["data"]["database"])
    left, middle, right = st.columns(3)
    if left.button("检查数据"):
        launch({"action": "validate"})
    middle.button("新建回测", on_click=navigate, args=("新建回测",))
    right.button("查看结果", on_click=navigate, args=("结果与实验",))
    st.info("第一次使用：检查数据 → 学习路线 → 选择短区间和少量股票 → 启动回测 → 查看交易与持仓 → 比较实验。")
    if st.button("打开全流程学习手册", key="open_learning"):
        navigate("学习路线")

    @st.fragment(run_every="2s")
    def task_panel():
        st.subheader("任务进度")
        if st.button("刷新状态"):
            st.rerun()
        recent = [j for j in jobs() if j.is_dir()][:10]
        if not recent:
            st.write("暂无后台任务")
        for job in recent:
            status = job_status(job)
            if not status:
                continue
            label = {"queued": "等待", "running": "运行中", "cancelling": "取消中",
                     "cancelled": "已取消", "success": "成功", "failed": "失败"}.get(status["state"], status["state"])
            st.write(f"{job.name} · {label} · {status.get('message', '')}")
            st.progress(min(100, max(0, status.get("progress", 0))) / 100)
            if status["state"] in {"queued", "running"} and st.button("取消任务", key=f"cancel_{job.name}"):
                cancel_job(job)
                st.rerun()
            with st.expander(f"日志与数据检查 · {job.name}"):
                for filename in ("result/execution.log", "result/error.log", "process.log"):
                    path = job / filename
                    if path.exists():
                        st.code(path.read_text(encoding="utf-8", errors="replace")[-12000:], language="text")
                manifest = read_json(job / "result/data_manifest.json")
                if manifest:
                    st.json(manifest)
    task_panel()

elif page == "新建回测":
    st.subheader("设置并启动回测")
    bt = cfg["backtest"]
    with st.form("new_backtest"):
        c1, c2 = st.columns(2)
        start = c1.date_input("开始日期", pd.Timestamp(bt.get("start", "2020-01-01")).date())
        end = c2.date_input("结束日期", pd.Timestamp(bt.get("end", "2024-12-31")).date())
        c1, c2, c3 = st.columns(3)
        initial_cash = c1.number_input("初始资金", min_value=1000.0, value=float(bt["initial_cash"]), step=10000.0)
        max_positions = c2.number_input("最多持仓数", min_value=1, value=int(bt["max_positions"]))
        max_stocks = c3.number_input("试跑股票数（0=全部）", min_value=0, value=10,
                                    help="按股票代码排序取固定子集，用于流程验收，不代表全市场。")
        c1, c2, c3 = st.columns(3)
        commission = c1.number_input("佣金率", min_value=0.0, max_value=0.99, value=float(bt["commission_rate"]), format="%.6f")
        stamp = c2.number_input("卖出印花税率", min_value=0.0, max_value=0.99, value=float(bt["stamp_duty_rate"]), format="%.6f")
        slippage = c3.number_input("滑点（基点）", min_value=0.0, max_value=9999.0, value=float(bt["slippage_bps"]))
        weekday = st.selectbox("调仓日", range(5), index=int(bt["rebalance_weekday"]),
                               format_func=lambda d: ["周一", "周二", "周三", "周四", "周五"][d])
        signal_version = st.selectbox("策略信号系列", ["legacy_v1", "0708cao"],
                                      index=0 if cfg["signals"].get("version", "legacy_v1") == "legacy_v1" else 1,
                                      format_func=lambda value: "现有信号（legacy_v1）" if value == "legacy_v1"
                                      else "Excel 新信号（0708cao）")
        if signal_version == "0708cao":
            active_signal_names, active_descriptions = cao_signal_names, cao_signal_descriptions
            default_signals = cfg["signals"]["enabled"] if cfg["signals"].get("version") == signal_version else []
            st.caption("T日收盘后计算信号，沿用下一交易日开盘执行。各信号内部条件取交集，多条信号取并集并按命中数评分。")
            st.dataframe(pd.DataFrame([
                {"信号": name, "名称": active_descriptions[name], "触发条件": cao_signal_triggers[name],
                 "最少有效样本（含T）": cao_signal_lookbacks[name]}
                for name in active_signal_names]), hide_index=True, use_container_width=True)
        else:
            active_signal_names, active_descriptions = legacy_signal_names, legacy_signal_descriptions
            default_signals = cfg["signals"]["enabled"] if cfg["signals"].get("version") == signal_version else []
        enabled = st.multiselect("选择信号", active_signal_names, default=default_signals,
                                format_func=lambda name: active_descriptions[name], key=f"signals_{signal_version}")
        if signal_version == "0708cao":
            st.warning("MACD 的 ±0.25 是对后复权价格的绝对阈值，复权尺度变化会影响信号命中。")
            if "0708cao_sig5" in enabled:
                st.info("Sig5 按 Excel F 列执行：昨日成交量至少为今日的 1.1 倍，实际表示缩量。")
            if "0708cao_sig10" in enabled:
                st.info("Sig10 使用独立换手率字段；某日缺少换手率时，该日信号不会触发。")
        thresholds = cfg["signals"].get("thresholds", {})
        if signal_version == "legacy_v1":
            c1, c2 = st.columns(2)
            ratio1 = c1.number_input("强放量：较昨日增加倍数", min_value=0.0, value=float(thresholds.get("volume_ratio_20230111", 2)),
                                     help="填 2 代表今日成交量至少是昨日的 3 倍。")
            ratio2 = c2.number_input("较弱放量：较昨日增加倍数", min_value=0.0, value=float(thresholds.get("volume_ratio_20230112", 1)))
            learning_gap = st.number_input("教学因子阈值：20 期均线偏离率", min_value=0.0,
                                           value=float(thresholds.get("learning_ma_gap_min", 0.02)),
                                           format="%.4f",
                                           help="仅在选择教学信号时生效；0.02 表示收盘价高于均线2%。")
        st.markdown("**评价设置**")
        evaluation = cfg["evaluation"]
        risk_free = st.number_input("年化无风险收益率", min_value=-0.99, max_value=10.0,
                                    value=float(evaluation["risk_free_rate"]), format="%.4f")
        sortino_target = st.number_input("Sortino 最低目标年化收益率", min_value=-0.99, max_value=10.0,
                                         value=float(evaluation["sortino_target_return"]), format="%.4f")
        eval_factors = st.multiselect("因子诊断", supported_factors(cfg["factors"]["windows"]),
                                     default=evaluation["factors"])
        horizons = st.multiselect("远期收益期限（交易日）", [1, 5, 20], default=evaluation["horizons"])
        st.markdown("**可选基准 CSV**（必需列：`date,close`）")
        benchmark_file = st.file_uploader("导入基准 CSV", type=["csv"], key="benchmark_csv")
        benchmark_name = st.text_input("基准名称", value=evaluation["benchmark"].get("name", ""))
        benchmark_source = st.text_input("基准来源说明", value=evaluation["benchmark"].get("source", ""))
        benchmark_return_type = st.selectbox("基准收益口径", ["total", "price"],
                                             index=0 if evaluation["benchmark"].get("return_type", "total") == "total" else 1,
                                             format_func=lambda x: "全收益指数" if x == "total" else "价格指数")
        st.caption("未勾选的信号不会参与选股。所有信号均关闭时，回测保留现金，仍会生成可查看、可下载的空交易报告。")
        with st.expander("默认配置说明"):
            for warning in config_warnings(cfg):
                st.write(warning)
        submitted = st.form_submit_button("启动回测")
    if submitted:
        benchmark_config = {"path": cfg["evaluation"]["benchmark"].get("path", ""),
                            "name": benchmark_name, "source": benchmark_source,
                            "return_type": benchmark_return_type}
        if benchmark_file is not None:
            payload = benchmark_file.getvalue()
            digest = hashlib.sha256(payload).hexdigest()
            benchmark_dir = ROOT / "runs" / "benchmarks"
            benchmark_dir.mkdir(parents=True, exist_ok=True)
            benchmark_path = benchmark_dir / f"{digest}.csv"
            if not benchmark_path.exists():
                benchmark_path.write_bytes(payload)
            benchmark_config["path"] = str(benchmark_path.resolve())
        launch({"start": str(start), "end": str(end), "max_stocks": int(max_stocks),
                "config": {"backtest": {"initial_cash": initial_cash, "max_positions": int(max_positions),
                                       "commission_rate": commission, "stamp_duty_rate": stamp,
                                       "slippage_bps": slippage, "rebalance_weekday": weekday},
                           "signals": {"version": signal_version, "enabled": enabled,
                                       "thresholds": ({"volume_ratio_20230111": ratio1,
                                                       "volume_ratio_20230112": ratio2,
                                                       "learning_ma_gap_min": learning_gap}
                                                      if signal_version == "legacy_v1" else {})},
                           "evaluation": {"risk_free_rate": risk_free, "sortino_target_return": sortino_target,
                                          "factors": eval_factors, "horizons": horizons,
                                          "benchmark": benchmark_config}}})

elif page == "结果与实验":
    st.subheader("结果与实验")
    if st.button("刷新实验列表"):
        st.rerun()
    directories = discover_runs(ROOT / "runs")
    if not directories:
        st.info("暂无成功回测；运行中的进度和失败原因在首页。")
    else:
        labels = {str(p): f"{run_id(p)} · {run_classification(p)}" for p in directories}
        selected = st.selectbox("选择实验", list(labels), format_func=labels.get)
        directory = Path(selected)
        if run_classification(directory) != "V1 修复版":
            st.warning(run_classification(directory) + "。原文件保留；请重新运行以获得修复版结果。")
        summary = read_json(directory / "summary.json")
        metadata = read_json(directory / "run_metadata.json")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("累计收益", f"{summary.get('cumulative_return', 0):.2%}")
        c2.metric("年化收益", f"{summary.get('annual_return', 0):.2%}")
        c3.metric("最大回撤", f"{summary.get('max_drawdown', 0):.2%}")
        c4.metric("交易笔数", summary.get("trade_count", summary.get("trades", 0)))
        st.caption(f"区间：{summary.get('start', '—')} 至 {summary.get('end', '—')} · 结果目录：{directory}")
        evaluation = read_json(directory / "evaluation" / "evaluation.json")
        if evaluation:
            account_tab, trading_tab, factor_tab, benchmark_tab = st.tabs(
                ["账户绩效", "交易分析", "因子诊断", "基准评价"])
            account_metrics = evaluation.get("account", {}).get("metrics", {})
            def render_eval_value(item):
                value = item.get("value")
                if value is None:
                    return "不可计算"
                if "%" in item.get("unit", ""):
                    return f"{value:.2%}"
                return f"{value:,.4f}" if isinstance(value, (float, int)) else str(value)
            with account_tab:
                columns = st.columns(5)
                for col, key, label in zip(columns, ["cagr", "max_drawdown", "calmar", "sortino", "worst_day"],
                                           ["复合年化收益", "最大回撤", "Calmar", "Sortino", "最差单日"]):
                    col.metric(label, render_eval_value(account_metrics.get(key, {})))
                goals = st.columns(2)
                cagr_goal = account_metrics.get("target_cagr_15pct_met", {}).get("value")
                drawdown_goal = account_metrics.get("target_drawdown_15pct_met", {}).get("value")
                goals[0].metric("历史年化收益 ≥15%", "达标" if cagr_goal is True else "未达标" if cagr_goal is False else "不可判断")
                goals[1].metric("历史最大回撤 <15%", "达标" if drawdown_goal is True else "未达标" if drawdown_goal is False else "不可判断")
                if evaluation.get("account", {}).get("annualization_warning"):
                    st.caption(evaluation["account"]["annualization_warning"])
                st.dataframe(pd.DataFrame([{"指标": key, "数值": render_eval_value(item), "口径": item.get("definition"),
                                            "样本数": item.get("n"), "说明": item.get("unavailable_reason") or ""}
                                           for key, item in account_metrics.items()]), hide_index=True, use_container_width=True)
                for filename, title in (("monthly_returns.csv", "月度收益"), ("annual_returns.csv", "年度收益"),
                                        ("weekly_returns.csv", "周度收益"), ("rolling_returns.csv", "滚动252日收益"),
                                        ("drawdowns.csv", "回撤过程"), ("daily_turnover.csv", "每日换手率")):
                    path = directory / "evaluation" / filename
                    if path.exists():
                        with st.expander(title):
                            st.dataframe(read_csv(path), hide_index=True, use_container_width=True)
            with trading_tab:
                trading_metrics = evaluation.get("trading", {}).get("metrics", {})
                columns = st.columns(4)
                for col, key, label in zip(columns, ["closed_cycles", "realized_win_rate", "profit_factor", "accounting_reconciliation_error"],
                                           ["完整持仓周期", "已实现胜率", "Profit Factor", "账本核对差额"]):
                    col.metric(label, render_eval_value(trading_metrics.get(key, {})))
                for filename, title in (("trade_pairs.csv", "FIFO 买卖配对"), ("trade_cycles.csv", "完整持仓周期"),
                                        ("open_positions.csv", "期末未平仓盈亏")):
                    path = directory / "evaluation" / filename
                    with st.expander(title):
                        if path.exists():
                            st.dataframe(read_csv(path), hide_index=True, use_container_width=True)
            with factor_tab:
                path = directory / "evaluation" / "factor_summary.csv"
                if path.exists():
                    st.dataframe(read_csv(path), hide_index=True, use_container_width=True)
                    st.caption("ICIR 未年化；分组远期收益不含费用，标签仅用于事后诊断。")
                    for filename, title in (("factor_groups.csv", "分组远期收益与信号命中"),
                                            ("factor_daily.csv", "每日 IC、Rank IC 与覆盖率")):
                        table_path = directory / "evaluation" / filename
                        if table_path.exists():
                            with st.expander(title):
                                st.dataframe(read_csv(table_path), hide_index=True, use_container_width=True)
                    panel = directory / "evaluation" / "factor_panel.csv.gz"
                    if panel.exists():
                        st.download_button("下载压缩因子面板", panel.read_bytes(),
                                           file_name=f"{run_id(directory)}_factor_panel.csv.gz")
                else:
                    st.info("未生成因子诊断文件。")
            with benchmark_tab:
                benchmark = evaluation.get("benchmark", {})
                st.write(benchmark.get("reason", f"基准状态：{benchmark.get('status', '未知')}"))
                if benchmark.get("metrics"):
                    st.dataframe(pd.DataFrame([{"指标": key, "数值": render_eval_value(item),
                                                "口径": item.get("definition"), "样本数": item.get("n"),
                                                "说明": item.get("unavailable_reason") or ""}
                                               for key, item in benchmark["metrics"].items()]), hide_index=True, use_container_width=True)
                path = directory / "evaluation" / "benchmark_daily.csv"
                if path.exists():
                    daily = read_csv(path)
                    if {"date", "strategy_nav", "benchmark_nav"}.issubset(daily.columns):
                        st.line_chart(daily.set_index("date")[["strategy_nav", "benchmark_nav"]])
                    with st.expander("逐日基准对齐"):
                        st.dataframe(daily, hide_index=True, use_container_width=True)
        else:
            st.warning("该历史实验没有新版评价文件；重新运行后可查看扩展指标。")
        guide_path = ROOT / "docs" / "08_回测评价指标说明.md"
        if guide_path.exists():
            with st.expander("查看评价指标与口径说明"):
                st.markdown(guide_path.read_text(encoding="utf-8"))
        curve_path = directory / "equity_curve.csv"
        if curve_path.exists():
            curve = read_csv(curve_path)
            if not curve.empty:
                st.line_chart(curve.set_index("date")["market_value"])
                with st.expander("每日权益与现金"):
                    st.dataframe(curve, hide_index=True)
        for filename, title in (("trades.csv", "交易记录"), ("positions.csv", "每日持仓"),
                                ("skipped_orders.csv", "跳过订单")):
            with st.expander(title):
                path = directory / filename
                if path.exists():
                    table = read_csv(path)
                    if table.empty:
                        st.info("暂无记录")
                    else:
                        st.dataframe(table, hide_index=True)
                    st.download_button(f"下载{title}", path.read_bytes(), file_name=f"{run_id(directory)}_{filename}")
                else:
                    st.info("该历史实验未保存此文件")
        with st.expander("生效参数与运行说明"):
            st.json(metadata.get("effective_config", {}))
            for warning in metadata.get("config_warnings", []):
                st.write(warning)
            st.json(summary)
        report = io.BytesIO()
        with zipfile.ZipFile(report, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.name not in {".factor_panel.sqlite", ".factor_panel.sqlite-journal"}:
                    archive.write(path, path.relative_to(directory))
        st.download_button("下载完整报告 ZIP", report.getvalue(), file_name=f"{run_id(directory)}.zip")
        st.subheader("比较两个实验")
        other = st.selectbox("对比实验", list(labels), format_func=labels.get, key="compare",
                             index=1 if len(labels) > 1 else 0)
        if other != selected:
            right = read_json(Path(other) / "summary.json")
            metrics = ["累计收益", "复合年化收益", "最大回撤", "交易笔数", "总费用"]
            left_values = [summary.get("cumulative_return"), summary.get("annual_return"), summary.get("max_drawdown"),
                           summary.get("trade_count"), summary.get("total_fees")]
            right_values = [right.get("cumulative_return"), right.get("annual_return"), right.get("max_drawdown"),
                            right.get("trade_count"), right.get("total_fees")]
            left_eval = read_json(directory / "evaluation" / "evaluation.json")
            right_eval = read_json(Path(other) / "evaluation" / "evaluation.json")
            for key, label in (("cagr", "新版复合年化收益"), ("max_drawdown", "新版最大回撤"),
                               ("calmar", "Calmar"), ("sortino", "Sortino"),
                               ("historical_es_95", "历史 ES 95%")):
                left_item = left_eval.get("account", {}).get("metrics", {}).get(key, {})
                right_item = right_eval.get("account", {}).get("metrics", {}).get(key, {})
                if left_item or right_item:
                    metrics.append(label)
                    left_values.append(left_item.get("value"))
                    right_values.append(right_item.get("value"))
            st.dataframe(pd.DataFrame({"指标": metrics, "实验 A": left_values,
                                       "实验 B": right_values}), hide_index=True)
            left_bt = metadata.get("effective_config", {}).get("backtest", {})
            right_meta = read_json(Path(other) / "run_metadata.json")
            right_bt = right_meta.get("effective_config", {}).get("backtest", {})
            left_config = metadata.get("effective_config", {})
            right_config = right_meta.get("effective_config", {})
            scope_differs = (left_config.get("data", {}).get("database") != right_config.get("data", {}).get("database")
                             or any(left_bt.get(k) != right_bt.get(k) for k in ("start", "end", "max_stocks", "max_rows")))
            risk_basis_differs = any(left_config.get("evaluation", {}).get(k) != right_config.get("evaluation", {}).get(k)
                                     for k in ("annualization_days", "risk_free_rate", "sortino_target_return"))
            left_benchmark = left_eval.get("benchmark", {})
            right_benchmark = right_eval.get("benchmark", {})
            benchmark_differs = (left_benchmark.get("file_sha256") != right_benchmark.get("file_sha256")
                                 or left_benchmark.get("return_type") != right_benchmark.get("return_type"))
            if scope_differs:
                st.warning("两次实验的数据库、日期区间或股票范围不同，结果不宜直接横向比较。")
            if risk_basis_differs:
                st.warning("两次实验的风险年化或无风险收益率参数不同，风险指标口径不一致。")
            if benchmark_differs and (left_benchmark.get("status") == "ok" or right_benchmark.get("status") == "ok"):
                st.warning("两次实验使用的基准数据或指数口径不同，基准相对指标不宜直接比较。")
            difference = config_diff(metadata.get("effective_config", {}),
                                     right_meta.get("effective_config", {}))
            st.write("参数差异")
            if difference.empty:
                st.info("保存的参数相同（历史实验可能未保存完整参数）")
            else:
                st.dataframe(difference, hide_index=True)

elif page == "学习路线":
    st.subheader("从零掌握策略设定、因子开发与回测")
    guide = ROOT / "docs" / "06_从零掌握策略与因子.md"
    if guide.exists():
        st.markdown(guide.read_text(encoding="utf-8"))
    else:
        st.error("学习手册未找到，请检查 docs 目录。")

else:
    st.subheader("编辑导航")
    st.write("先用网页调整参数；修改代码后保存并启动新实验。旧实验保持原结果。")
    st.dataframe(pd.DataFrame([
        ["信号开关和阈值", "configs/signals.yaml"], ["练习默认参数", "configs/learning/first_factor.yaml"],
        ["持仓与费用默认值", "configs/portfolio.yaml"],
        ["回测日期", "configs/backtest.yaml"], ["信号规则", "src/qlib_quant/signals/legacy_v1.py"],
        ["因子计算", "src/qlib_quant/factors/technical.py"], ["组合选择", "src/qlib_quant/portfolio/baseline.py"],
        ["回测账本", "src/qlib_quant/backtest/simple.py"], ["网页", "app/main.py"]],
        columns=["修改内容", "文件（相对项目目录）"]), hide_index=True)
    st.code(str(ROOT), language="text")
    st.write("同名信号的规则修改会在新进程中加载；新增策略文件需要在运行服务中显式接入。")
