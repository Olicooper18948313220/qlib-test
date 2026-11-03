from __future__ import annotations

import copy
from uuid import uuid4

import pandas as pd

from .config import load_config, validate_config
from .contracts import ScanResult
from .data.calendar import effective_asof, latest_closed, session_close, sessions, utc, utc_now, week_info
from .data.database import Database
from .data.quality import data_issues, metadata_fresh
from .data.sync import sync
from .exports import export_watchlists
from .indicators import compute_metrics
from .scanners import REGISTRY

__all__ = ["update_data", "load_bars", "run_scans", "export_watchlists", "check_data"]


def update_data(config=None, *, provider=None, directory=None, now=None, repair=False, progress=None):
    config = config or load_config()
    validate_config(config)
    return sync(config, provider, directory, now, repair, progress)


def load_bars(symbol, timeframe="1d", start=None, end=None, limit=None, *, config=None, database=None):
    config = config or load_config()
    return Database(database or config["data"]["database"]).load_bars(symbol, timeframe, start, end, limit)


def check_data(config=None):
    return Database((config or load_config())["data"]["database"]).health()


def run_scans(config=None, enabled=None, *, persist=True, now=None):
    config = copy.deepcopy(config or load_config())
    validate_config(config)
    enabled = (
        list(enabled)
        if enabled is not None
        else [name for name, s in config["scanners"].items() if s["enabled"]]
    )
    if set(enabled) - set(REGISTRY):
        raise ValueError("未知扫描器")
    enabled = [name for name in dict.fromkeys(enabled) if config["scanners"][name]["enabled"]]
    now = utc(now) if now is not None else utc_now()
    asof = effective_asof(config, now)
    db = Database(config["data"]["database"])
    records = db.instruments(sorted(set(config["data"]["symbols"])) or None)
    universe, _ = db.universe()
    active_symbols = {r["symbol"] for r in universe} if universe else None
    current = asof == latest_closed(now, config["data"]["close_delay_minutes"])
    frames = {name: [] for name in enabled}
    for offset in range(0, len(records), config["data"]["batch_size"]):
        for instrument in records[offset : offset + config["data"]["batch_size"]]:
            symbol = instrument["symbol"]
            metadata = db.metadata(symbol, None if current else session_close(asof))
            for name in enabled:
                settings = config["scanners"][name]
                target = asof
                if name == "leader" and settings["week_policy"] == "completed" and not week_info(asof)[1]:
                    monday = pd.Timestamp(week_info(asof)[0])
                    target = sessions(monday - pd.Timedelta(days=10), monday - pd.Timedelta(days=1))[-1]
                warmup = (
                    max(2, settings["average_volume_days"])
                    if name == "leader"
                    else max(2, settings["ema_slow"] * 5)
                )
                # Include enough calendar history for configurable anchor windows as well as EMA warmup.
                read_count = max(
                    config["data"]["read_bars"],
                    warmup,
                    settings.get("performance_days", 0) + 5,
                    settings.get("volatility_days", 0) + 5,
                )
                bars = db.load_bars(symbol, "1d", end=target, limit=read_count)
                # Validate only the actual indicator window, not unrelated older rows requested for display.
                if name == "leader" and not bars.empty:
                    anchor = pd.Timestamp(target) - pd.Timedelta(days=settings["performance_days"])
                    dates = pd.to_datetime(bars["session_date"])
                    position = max(0, int(dates.searchsorted(anchor, side="right")) - 1)
                    position = min(position, max(0, len(bars) - warmup))
                    bars = bars.iloc[position:].copy()
                issues = data_issues(bars, target, warmup)
                if not bars.empty and set(sessions(bars.iloc[0]["session_date"], target)) - set(
                    bars["session_date"]
                ):
                    issues.append("missing_trading_sessions")
                if not metadata:
                    issues.append("missing_point_in_time_metadata" if not current else "missing_metadata")
                elif current and not metadata_fresh(metadata, asof, now, config["data"]["metadata_ttl_days"]):
                    issues.append("stale_metadata")
                if current and active_symbols is not None and symbol not in active_symbols:
                    issues.append("inactive_directory")
                metrics = (
                    {}
                    if any(
                        x.startswith(
                            (
                                "missing_daily",
                                "insufficient_warmup",
                                "missing_trading",
                                "stale_daily",
                                "inactive",
                            )
                        )
                        for x in issues
                    )
                    else compute_metrics(
                        bars, settings if name == "leader" else None, settings if name == "potent" else None
                    )
                )
                metrics.update(
                    market_cap=metadata.get("market_cap"),
                    industry=metadata.get("industry"),
                    metadata_observed_at=metadata.get("observed_at"),
                )
                if not bars.empty and bars["adjustment"].eq("stale_before_split").any():
                    issues.append("split_repair_required")
                evaluated = REGISTRY[name].evaluate(metrics, settings, issues)
                frames[name].append(
                    {
                        "symbol": symbol,
                        "exchange": instrument["exchange"],
                        "asof": target,
                        **metrics,
                        **evaluated,
                    }
                )
    for name in enabled:
        frame = pd.DataFrame(frames[name])
        if frame.empty:
            frame = pd.DataFrame(
                columns=[
                    "symbol",
                    "exchange",
                    "asof",
                    "status",
                    "reasons",
                    config["scanners"][name]["sort_by"],
                ]
            )
        if config["scanners"][name]["sort_by"] not in frame:
            frame[config["scanners"][name]["sort_by"]] = float("nan")
        frame = frame.sort_values(
            [config["scanners"][name]["sort_by"], "symbol"],
            ascending=[config["scanners"][name]["ascending"], True],
            na_position="last",
        ).reset_index(drop=True)
        frames[name] = frame
    config["executed_scanners"] = enabled
    coverage = {
        name: {
            "universe": len(frame),
            "passed": int(frame["status"].eq("passed").sum()),
            "failed": int(frame["status"].eq("failed").sum()),
            "insufficient": int(frame["status"].eq("insufficient").sum()),
            "skipped": int(frame["status"].eq("skipped").sum()),
        }
        for name, frame in frames.items()
    }
    result = ScanResult(
        utc_now().strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8], asof, frames, config, coverage
    )
    if persist:
        db.save_scan(result)
    return result
