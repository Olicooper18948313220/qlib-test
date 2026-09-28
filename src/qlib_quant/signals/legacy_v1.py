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
    "learning_ma_gap",
]
legacy_signal_names = signal_names[:11]

signal_descriptions = {
    signal_names[0]: "放量至少 3 倍且收盘高于前 250 期低点",
    signal_names[1]: "放量至少 2 倍且收盘高于前 250 期低点",
    signal_names[2]: "收盘高于 5 期及 10 期前",
    signal_names[3]: "前两日走弱后当日反弹",
    signal_names[4]: "反弹且收盘高于 3 期前",
    signal_names[5]: "反弹且收盘高于 5 期前",
    signal_names[6]: "突破前 250 期最高价",
    signal_names[7]: "突破前 250 期最高价 3%",
    signal_names[8]: "突破前 250 期最高价 5%",
    signal_names[9]: "收盘高于前 10 期最低价",
    signal_names[10]: "成交额至少翻倍且收盘上涨",
    signal_names[11]: "教学：收盘高于 20 期均价指定比例",
}

# Number of prior observations needed for a rule. The runner uses this
# registry to load per-stock warmup history before calculating any signal.
signal_lookbacks = dict(zip(signal_names, [250, 250, 10, 2, 3, 5, 250, 250, 250, 10, 1, 20]))


def _safe_ratio(a, b):
    return a / b.where(b != 0) 


def score_signals(frame: pd.DataFrame, thresholds: dict | None = None,
                  enabled: list[str] | None = None) -> pd.DataFrame:
    """Return ``signal_score`` and per-rule boolean columns.

    The original ``amount_ratio=2`` predicate means today >= 3x yesterday
    because its helper checks ``today-yesterday >= 2*yesterday``. We preserve
    that exact semantics in the 20230111 rule.
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
    ma_gap_20 = df["ma_gap_20"] if "ma_gap_20" in df else g["close"].transform(
        lambda s: s / s.rolling(20, min_periods=20).mean() - 1
    )
    rules[signal_names[11]] = ma_gap_20 > thresholds.get("learning_ma_gap_min", 0.02)
    # Keep direct callers on the original strategy unless they explicitly opt
    # into the teaching signal, just like the checked-in default YAML.
    enabled_set = set(legacy_signal_names) if enabled is None else set(enabled)
    for name, values in rules.items():
        if name not in enabled_set:
            values = pd.Series(False, index=df.index)
        df[name] = values.fillna(False).astype(bool)
    df["signal_score"] = df[signal_names].sum(axis=1).astype(float)
    df["signal"] = df["signal_score"] > 0
    return df
