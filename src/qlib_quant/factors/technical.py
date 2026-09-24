from __future__ import annotations

import pandas as pd


def add_technical_features(frame: pd.DataFrame, windows=(5, 10, 20, 30, 60, 120, 250)) -> pd.DataFrame:
    """Calculate causal features per instrument; shift(1) keeps today's signal from using tomorrow."""
    required = {"code", "date", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"缺少字段: {sorted(missing)}")
    df = frame.copy().sort_values(["code", "date"]).reset_index(drop=True)
    groups = df.groupby("code", sort=False)
    for n in windows:
        df[f"ma_{n}"] = groups["close"].transform(lambda s: s.rolling(n, min_periods=n).mean())
        df[f"high_max_{n}"] = groups["high"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).max())
        df[f"low_min_{n}"] = groups["low"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).min())
        df[f"return_{n}"] = groups["close"].transform(lambda s: s.pct_change(n))
    df["volume_ratio_1"] = groups["volume"].transform(lambda s: s / s.shift(1))
    df["amount_ratio_1"] = groups["amount"].transform(lambda s: s / s.shift(1))
    df["close_above_high_20"] = df["close"] > df["high_max_20"]
    df["close_above_low_10"] = df["close"] > df["low_min_10"]
    return df

