"""Benchmark (沪深300) loading and excess-return metrics.

The strategy equity curve is compared against an index close series aligned to
the same trading dates. Metrics are scale-invariant (both curves are normalized
to 1.0 at the start).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_index_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "close"]]


def align_benchmark(strategy_dates, bench: pd.DataFrame) -> pd.Series:
    """Return benchmark close aligned to strategy dates (forward-filled)."""
    s = bench.set_index("date")["close"].astype(float).sort_index()
    idx = pd.to_datetime(pd.Series(strategy_dates)).drop_duplicates().sort_values()
    filled = s.reindex(s.index.union(idx)).sort_index().ffill()
    return filled.reindex(idx)


def _years(nav: pd.Series) -> float:
    return max((nav.index[-1] - nav.index[0]).days / 365.25, 1 / 365.25)


def compute_metrics(equity: pd.DataFrame, bench_close: pd.Series) -> dict:
    """Return benchmark + excess metrics given the strategy equity curve."""
    strat = equity.set_index(pd.to_datetime(equity["date"]))["market_value"].astype(float)
    bench = bench_close.astype(float).reindex(strat.index).ffill().dropna()
    strat = strat.loc[bench.index]
    if len(strat) < 2:
        return {}

    strat_nav = strat / strat.iloc[0]
    bench_nav = bench / bench.iloc[0]

    def total(nav: pd.Series) -> float:
        return float(nav.iloc[-1] / nav.iloc[0] - 1)

    def annual(nav: pd.Series) -> float:
        return float((1 + total(nav)) ** (1 / _years(nav)) - 1)

    def max_dd(nav: pd.Series) -> float:
        return float((nav / nav.cummax() - 1).min())

    sr = strat.pct_change().fillna(0.0)
    br = bench.pct_change().fillna(0.0)
    excess = sr - br
    excess_std = float(excess.std(ddof=1))
    tracking_error = float(excess_std * (252 ** 0.5))
    information_ratio = float(excess.mean() / excess_std * (252 ** 0.5)) if excess_std > 0 else 0.0
    excess_cum = strat_nav - bench_nav
    excess_max_dd = float((excess_cum - excess_cum.cummax()).min())

    return {
        "benchmark_cumulative_return": total(bench_nav),
        "benchmark_annual_return": annual(bench_nav),
        "benchmark_max_drawdown": max_dd(bench_nav),
        "annual_excess_return": float(annual(strat_nav) - annual(bench_nav)),
        "information_ratio": information_ratio,
        "tracking_error_annualized": tracking_error,
        "excess_max_drawdown": excess_max_dd,
    }
