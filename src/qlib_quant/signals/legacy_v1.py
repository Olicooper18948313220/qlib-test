"""Readable migration of the active Caochen stock-selection rules.

Each rule uses only rows up to the current date. The implementation deliberately
returns one score per row so a later Qlib model or ranker can replace it.
"""
from __future__ import annotations

import pandas as pd


signal_names = [
    "caochen_volume_bloom_above_bottom_x_20230111",
    "caochen_volume_bloom_above_bottom_x_20230112",
    "caochen_price_rise_predict_rise_x_20220914",
    "caochen_price_down_predict_rise_1_x_20221129",
    "caochen_price_down_predict_rise_2_x_20221020",
    "caochen_price_down_predict_rise_3_x_20221020",
    "caochen_price_reach_year_rise_1_x_20230212",
    "caochen_price_reach_year_rise_2_x_20230210",
    "caochen_price_reach_year_rise_3_x_20230213",
    "caochen_price_low_above_previous_10_x_20230208",
    "caochen_volume_enlarge_price_rise_4_x_20230310",
]


def _safe_ratio(a, b):
    return a / b.where(b != 0) 


def score_signals(frame: pd.DataFrame, thresholds: dict | None = None, enabled: list[str] | None = None) -> pd.DataFrame:
    """Return ``signal_score`` and per-rule boolean columns.

    ``enabled`` is an optional list of signal names; when given, only those
    rules contribute to ``signal_score`` (the others are still computed as
    boolean columns but scored 0). The original ``amount_ratio=2`` predicate
    means today >= 3x yesterday because its helper checks
    ``today-yesterday >= 2*yesterday``; we preserve that exact semantics.
    """
    thresholds = thresholds or {}
    df = frame.sort_values(["code", "date"]).copy()
    g = df.groupby("code", sort=False)
    close, high, low, volume, amount = [df[x] for x in ("close", "high", "low", "volume", "amount")]
    prev_close = g["close"].shift(1)
    prev2_close = g["close"].shift(2)
    prev_low_10 = g["low"].transform(lambda s: s.shift(1).rolling(10, min_periods=10).min())
    prev_low_250 = g["low"].transform(lambda s: s.shift(1).rolling(250, min_periods=250).min())
    prev_high_250 = g["high"].transform(lambda s: s.shift(1).rolling(250, min_periods=250).max())
    vol_prev = g["volume"].shift(1)
    amount_prev = g["amount"].shift(1)
    rules = {}
    rules[signal_names[0]] = (volume >= vol_prev * (1 + thresholds.get("volume_ratio_20230111", 2.0))) & (close > prev_low_250)
    rules[signal_names[1]] = (volume >= vol_prev * (1 + thresholds.get("volume_ratio_20230112", 1.0))) & (close > prev_low_250)
    rules[signal_names[2]] = (close > g["close"].shift(5)) & (close > g["close"].shift(10))
    rules[signal_names[3]] = (prev2_close > prev_close) & (close > prev_close)
    rules[signal_names[4]] = (prev2_close > prev_close) & (close > prev_close) & (close > g["close"].shift(3))
    rules[signal_names[5]] = (prev2_close > prev_close) & (close > prev_close) & (close > g["close"].shift(5))
    rules[signal_names[6]] = close > prev_high_250 * 1.00
    rules[signal_names[7]] = close > prev_high_250 * 1.03
    rules[signal_names[8]] = close > prev_high_250 * 1.05
    rules[signal_names[9]] = close > prev_low_10
    rules[signal_names[10]] = (amount >= amount_prev * 2.0) & (close > prev_close)
    for name, values in rules.items():
        df[name] = values.fillna(False).astype(bool)
    enabled_set = set(enabled) if enabled is not None else set(signal_names)
    score_cols = [n for n in signal_names if n in enabled_set]
    df["signal_score"] = df[score_cols].sum(axis=1).astype(float)
    df["signal"] = df["signal_score"] > 0
    return df
