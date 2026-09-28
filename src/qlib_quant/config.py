from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]


DEFAULTS: dict[str, Any] = {
    "data": {
        "database": "D:/quanttrade20260207/data/stockdata/daily_hfq_repaired.db",
        "start": "2020-01-01",
        "end": "2024-12-31",
    },
    "backtest": {
        "initial_cash": 1_000_000.0,
        "max_positions": 20,
        "rebalance_weekday": 4,
        "commission_rate": 0.0003,
        "stamp_duty_rate": 0.001,
        "slippage_bps": 5.0,
        "lot_size": 100,
        "max_stocks": 0,
    },
    "signals": {"version": "legacy_v1", "enabled": [], "thresholds": {}},
    "factors": {"windows": [5, 10, 20, 30, 60, 120, 250], "fields": [], "features": []},
    "evaluation": {
        "enabled": True, "annualization_days": 252, "risk_free_rate": 0.0,
        "sortino_target_return": 0.0,
        "factors": ["ma_gap_20", "return_20", "volume_ratio_1", "signal_score"],
        "horizons": [1, 5, 20], "min_cross_section": 20, "min_per_quantile": 3,
        "benchmark": {"path": "", "name": "", "source": "", "return_type": "total"},
    },
}


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(root: Path | None = None) -> dict[str, Any]:
    """Load the checked-in YAML defaults without mutating them."""
    root = root or ROOT
    result = copy.deepcopy(DEFAULTS)
    for name, section in (("data.yaml", "data"), ("backtest.yaml", "backtest"),
                          ("portfolio.yaml", "backtest"),
                          ("signals.yaml", "signals"), ("factors.yaml", "factors"),
                          ("evaluation.yaml", "evaluation")):
        path = root / "configs" / name
        if path.exists():
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            result = _merge(result, {section: value})
    return result


def effective_config(overrides: dict[str, Any] | None = None, root: Path | None = None) -> dict[str, Any]:
    """Return defaults followed by per-run overrides."""
    return _merge(load_config(root), overrides or {})


def supported_signal_thresholds() -> set[str]:
    return {"volume_ratio_20230111", "volume_ratio_20230112", "learning_ma_gap_min"}


def config_warnings(config: dict[str, Any]) -> list[str]:
    thresholds = config.get("signals", {}).get("thresholds", {}) or {}
    unknown = sorted(set(thresholds) - supported_signal_thresholds())
    warnings = [f"signals.thresholds.{key} 当前尚未接入，已保留但不会影响回测" for key in unknown]
    if config.get("signals", {}).get("version", "legacy_v1") not in {"legacy_v1", "0708cao"}:
        warnings.append(f"不支持的信号版本: {config.get('signals', {}).get('version')}")
    if config.get("signals", {}).get("version") == "0708cao":
        warnings.extend([
            "0708cao 的 MACD ±0.25 使用后复权价格，绝对阈值会受价格尺度影响",
            "0708cao_sig5 按F列保留昨日成交量至少为今日1.1倍的条件",
        ])
    factors = config.get("factors", {})
    if factors.get("fields"):
        warnings.append("factors.fields 仅用于文档记录，当前因子函数使用固定行情字段")
    if factors.get("features"):
        warnings.append("factors.features 表达式尚未接入，当前使用 technical.py 的内置因子")
    return warnings


def validate_config(config: dict[str, Any]) -> None:
    """Reject unsupported settings rather than silently running another strategy."""
    import pandas as pd
    from .signals.legacy_v1 import signal_names as legacy_names
    from .signals.zff0708 import signal_names as cao_names
    bt = config["backtest"]
    for key in ("start", "end"):
        if pd.isna(pd.Timestamp(bt[key])):
            raise ValueError(f"{key} 日期无效")
    if pd.Timestamp(bt["start"]) > pd.Timestamp(bt["end"]):
        raise ValueError("开始日期不能晚于结束日期")
    for key in ("initial_cash", "commission_rate", "stamp_duty_rate", "slippage_bps"):
        value = bt[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} 必须是有限的非负数")
    if bt["initial_cash"] <= 0 or bt["commission_rate"] >= 1 or bt["stamp_duty_rate"] >= 1 or bt["slippage_bps"] >= 10000:
        raise ValueError("初始资金须为正，费用率须小于 1，滑点须小于 10000")
    for key, minimum in (("max_positions", 1), ("lot_size", 1), ("max_stocks", 0), ("max_rows", 0)):
        if type(bt.get(key, 0)) is not int or bt.get(key, 0) < minimum:
            raise ValueError(f"{key} 必须为不小于 {minimum} 的整数")
    if type(bt["rebalance_weekday"]) is not int or not 0 <= bt["rebalance_weekday"] <= 4:
        raise ValueError("调仓日必须是 0—4 的整数")
    for key, supported in {"weighting": "equal", "rebalance": "weekly", "entry": "next_open",
                           "long_only": True,
                           "price_adjustment": "hfq", "research_approximation": True}.items():
        if key in bt and bt[key] != supported:
            raise ValueError(f"当前只支持 {key}={supported}")
    signals = config["signals"]
    version = signals.get("version")
    registries = {"legacy_v1": set(legacy_names), "0708cao": set(cao_names)}
    if version not in registries:
        raise ValueError(f"signals.version 必须是以下之一: {sorted(registries)}")
    if not isinstance(signals.get("enabled"), list) or set(signals["enabled"]) - registries[version]:
        raise ValueError("启用信号列表包含未知信号")
    for key, value in signals.get("thresholds", {}).items():
        if key in supported_signal_thresholds() and (not isinstance(value, (int, float)) or
                                                    not math.isfinite(value) or value < 0):
            raise ValueError(f"信号阈值 {key} 须为有限非负数")
    if signals.get("thresholds", {}).get("learning_ma_gap_min", 0.02) >= 5:
        raise ValueError("教学均线偏离率阈值须小于 5")
    windows = config["factors"].get("windows", [])
    if not windows or any(type(v) is not int or v < 1 for v in windows):
        raise ValueError("因子窗口必须为正整数列表")
    if config["data"].get("adjustment", "hfq") != "hfq":
        raise ValueError("当前仅验收了后复权 hfq 数据口径")
    evaluation = config.get("evaluation", {})
    allowed_evaluation = set(DEFAULTS["evaluation"])
    if set(evaluation) - allowed_evaluation:
        raise ValueError(f"evaluation 包含未知配置: {sorted(set(evaluation) - allowed_evaluation)}")
    if type(evaluation.get("enabled")) is not bool:
        raise ValueError("evaluation.enabled 必须为布尔值")
    for key in ("risk_free_rate", "sortino_target_return"):
        value = evaluation.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= -1:
            raise ValueError(f"evaluation.{key} 必须为大于 -1 的有限年化收益率")
    for key, minimum in (("annualization_days", 1), ("min_cross_section", 2), ("min_per_quantile", 1)):
        value = evaluation.get(key)
        if type(value) is not int or value < minimum:
            raise ValueError(f"evaluation.{key} 必须为不小于 {minimum} 的整数")
    horizons = evaluation.get("horizons")
    if not isinstance(horizons, list) or not horizons or any(type(x) is not int or x < 1 for x in horizons) or len(set(horizons)) != len(horizons):
        raise ValueError("evaluation.horizons 必须是非空且不重复的正整数列表")
    from .evaluation import supported_factors
    factor_selection = evaluation.get("factors", [])
    if not isinstance(factor_selection, list) or any(not isinstance(x, str) for x in factor_selection):
        raise ValueError("evaluation.factors 必须为字符串列表")
    if len(set(factor_selection)) != len(factor_selection):
        raise ValueError("evaluation.factors 不得重复")
    unknown_factors = set(factor_selection) - set(supported_factors(windows))
    if unknown_factors:
        raise ValueError(f"evaluation.factors 包含未实现因子: {sorted(unknown_factors)}")
    if evaluation["enabled"] and not factor_selection and not signals["enabled"]:
        raise ValueError("评价已启用时，至少选择一个因子或信号")
    benchmark = evaluation.get("benchmark", {})
    if not isinstance(benchmark, dict):
        raise ValueError("evaluation.benchmark 必须为对象")
    if set(benchmark) - {"path", "name", "source", "return_type"}:
        raise ValueError("evaluation.benchmark 包含未知配置")
    if benchmark.get("return_type", "total") not in {"total", "price"}:
        raise ValueError("evaluation.benchmark.return_type 只能为 total 或 price")
    if benchmark.get("path") and not all(str(benchmark.get(key, "")).strip() for key in ("name", "source")):
        raise ValueError("配置基准 CSV 时必须填写基准名称与来源")
