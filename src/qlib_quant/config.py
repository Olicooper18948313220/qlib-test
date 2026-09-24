"""Load the YAML configs and expose typed defaults for the CLI.

configs/*.yaml were written but not wired into the backtest until now. This
module reads them and merges with defaults so the CLI finally honours the files.
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PORTFOLIO: dict = {
    "initial_cash": 1_000_000,
    "max_positions": 20,
    "weighting": "equal",
    "rebalance": "weekly",
    "entry": "next_open",
    "lot_size": 100,
    "commission_rate": 0.0003,
    "stamp_duty_rate": 0.001,
    "slippage_bps": 5,
    "long_only": True,
}

# "rebalance: weekly" -> Friday. Daily rebalance is not wired into the simple
# backtest yet, so anything unknown falls back to Friday (weekday 4).
REBALANCE_WEEKDAY = {
    "weekly": 4,
    "friday": 4,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
}

DEFAULT_FACTOR_WINDOWS = [5, 10, 20, 30, 60, 120, 250]


def load_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_portfolio(config_dir: str | Path) -> dict:
    cfg = dict(DEFAULT_PORTFOLIO)
    cfg.update(load_yaml(Path(config_dir) / "portfolio.yaml"))
    return cfg


def load_signals(config_dir: str | Path) -> dict:
    return load_yaml(Path(config_dir) / "signals.yaml")


def load_factors(config_dir: str | Path) -> dict:
    return load_yaml(Path(config_dir) / "factors.yaml")


def portfolio_weekday(portfolio: dict) -> int:
    return REBALANCE_WEEKDAY.get(str(portfolio.get("rebalance", "weekly")).lower(), 4)
