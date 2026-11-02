from __future__ import annotations

import copy
import math
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIMEFRAMES = {"1d", "15m", "5m", "1m"}
OPS = {">", ">=", "<", "<="}


def merge(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def load_config(root: str | Path | None = None, overrides: dict | None = None) -> dict:
    root = Path(root or ROOT).resolve()
    result = {
        name: yaml.safe_load((root / "configs" / f"{name}.yaml").read_text(encoding="utf-8"))
        for name in ("data", "scanners", "strategies", "watchlist")
    }
    result = merge(result, overrides or {})
    result["root"] = str(root)
    for key in ("database", "output_dir"):
        value = Path(result["data"][key]).expanduser()
        result["data"][key] = str(value if value.is_absolute() else root / value)
    validate_config(result)
    return result


def validate_config(config):
    d = config["data"]
    if d["provider"] != "yahoo":
        raise ValueError("当前 provider 仅支持 yahoo")
    if not d["timeframes"] or set(d["timeframes"]) - TIMEFRAMES:
        raise ValueError("timeframes 必须来自 1d/15m/5m/1m")
    for key in ("read_bars", "batch_size", "retries", "metadata_ttl_days", "universe_ttl_days"):
        if type(d[key]) is not int or d[key] < 1:
            raise ValueError(f"{key} 必须为正整数")
    for key in ("request_pause_seconds", "retry_backoff_seconds", "close_delay_minutes"):
        if not isinstance(d[key], (int, float)) or not math.isfinite(d[key]) or d[key] < 0:
            raise ValueError(f"{key} 必须为有限非负数")
    for tf in TIMEFRAMES:
        if type(d["history_days"][tf]) is not int or d["history_days"][tf] < 1:
            raise ValueError(f"history_days.{tf} 必须为正整数")
    import pandas as pd

    if d["end"] is not None and pd.isna(pd.Timestamp(d["end"])):
        raise ValueError("end 日期无效")
    for section in (d, config["watchlist"]):
        if not isinstance(section["symbols"], list) or any(
            not isinstance(x, str) or not x.strip() for x in section["symbols"]
        ):
            raise ValueError("symbols 必须为非空字符串列表")
    from .scanners import REGISTRY

    if set(config["scanners"]) != set(REGISTRY):
        raise ValueError("扫描器配置必须包含且仅包含 leader/potent")
    numeric = {
        "min_market_cap",
        "min_average_volume",
        "min_week_turnover",
        "min_volatility",
        "min_day_turnover",
        "open_change_lower",
        "open_change_upper",
    }
    for name, settings in config["scanners"].items():
        allowed = REGISTRY[name].keys
        if set(settings) != set(allowed):
            raise ValueError(f"{name} 配置字段不匹配: {set(settings) ^ set(allowed)}")
        for key, value in settings.items():
            if key.endswith("_op") and value not in OPS:
                raise ValueError(f"{name}.{key} 不支持的比较符")
            if key in numeric and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            ):
                raise ValueError(f"{name}.{key} 必须为有限数")
            if key.startswith("min_") and value < 0:
                raise ValueError(f"{name}.{key} 不得为负数")
            if key in {
                "average_volume_days",
                "performance_days",
                "ema_fast",
                "ema_slow",
                "volatility_days",
            } and (type(value) is not int or value < 1):
                raise ValueError(f"{name}.{key} 必须为正整数")
            if key in {"enabled", "ascending", "outside_inclusive"} and type(value) is not bool:
                raise ValueError(f"{name}.{key} 必须为布尔值")
        if settings["sort_by"] not in REGISTRY[name].sort_fields:
            raise ValueError(f"{name}.sort_by 不支持该指标")
    p = config["scanners"]["potent"]
    if p["open_change_lower"] >= p["open_change_upper"] or p["ema_fast"] > p["ema_slow"]:
        raise ValueError("Potent 上下界或 EMA 窗口顺序错误")
    if not isinstance(p["excluded_industries"], list):
        raise ValueError("excluded_industries 必须为列表")
    if config["scanners"]["leader"]["week_policy"] not in {"current", "completed"}:
        raise ValueError("week_policy 必须是 current/completed")
    from .strategies import validate_strategies

    validate_strategies(config["strategies"])


def save_experiment(config: dict, path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({k: v for k, v in config.items() if k != "root"}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_experiment(path: str | Path, root=None):
    return load_config(root, yaml.safe_load(Path(path).read_text(encoding="utf-8")))
