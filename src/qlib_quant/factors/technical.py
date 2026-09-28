from __future__ import annotations

import pandas as pd


ZFF0708_WINDOWS = (3, 6, 12, 24, 250)
ZFF0708_MACD_WARMUP = 76


def zff0708_factor_names() -> list[str]:
    """Numeric and boolean columns required to explain the 0708cao rules."""
    names = {"bbi_0708cao", "hlc3_0708cao", "vma3_0708cao", "macd_dif_0708cao",
             "macd_dea_0708cao", "macd_bar_0708cao", "turnover_rate_ratio_0708cao",
             "hlc_amplitude_5_0708cao", "hlc_amplitude_11_0708cao",
             "hlc_amplitude_4_0708cao", "hlc_amplitude_4_prior_0708cao", "volume_vma3_range_4_0708cao",
             "close_above_ma250_0708cao", "prior_breakdown_0708cao",
             "prior_close_above_ma250_0708cao", "yearline_slope_0708cao",
             "near_ma250_0708cao", "crossed_ma250_0708cao"}
    names.update(f"ma_{window}" for window in ZFF0708_WINDOWS)
    names.update({f"close_above_bbi_lag_{lag}_0708cao" for lag in range(1, 5)})
    names.update({f"high_max_{window}_0708cao" for window in (5, 11)})
    names.update({f"low_min_{window}_0708cao" for window in (4, 9, 14)})
    names.update({f"ma_{window}_above_lag20_0708cao" for window in (5, 10, 20, 30)})
    components = (
        "sig1_amount", "sig1_bbi", "sig1_macd", "sig1_breakout", "sig1_range",
        "sig11_amount", "sig11_bbi", "sig11_macd", "sig11_breakout", "sig11_range",
        "sig2_cross_ma5", "sig2_macd", "sig2_first_bbi",
        "sig3_hlc_pullback", "sig3_volume_pullback", "sig3_near_ma10", "sig3_ma_order", "sig3_rise",
        "sig4_hlc_pullback", "sig4_volume_pullback", "sig4_near_ma20", "sig4_macd", "sig4_ma_order",
        "sig5_volume", "sig5_price_range", "sig5_volume_range", "sig5_ma_cross", "sig5_ma_order",
        "sig6_ma250_pair", "sig6_breakdown_recovery", "sig6_rise", "sig6_prior_close",
        "sig7_ma250_pair", "sig7_near_ma250", "sig7_rise",
        "sig8_ma250_pair", "sig8_cross_ma250", "sig9_price_range", "sig9_higher_low",
        "sig10_volume", "sig10_turnover", "sig10_ma_rise",
    )
    names.update(f"signal_condition_{member[3:]}_0708cao" for member in components)
    return sorted(names)


def add_technical_features(frame: pd.DataFrame, windows=(5, 10, 20, 30, 60, 120, 250)) -> pd.DataFrame:
    """Calculate causal features per instrument; shift(1) keeps today's signal from using tomorrow."""
    required = {"code", "date", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"缺少字段: {sorted(missing)}")
    df = frame.copy().sort_values(["code", "date"]).reset_index(drop=True)
    if "turnover_rate" not in df:
        df["turnover_rate"] = float("nan")
    groups = df.groupby("code", sort=False)
    for n in sorted(set(windows) | set(ZFF0708_WINDOWS) | {10, 20}):
        df[f"ma_{n}"] = groups["close"].transform(lambda s: s.rolling(n, min_periods=n).mean())
        df[f"high_max_{n}"] = groups["high"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).max())
        df[f"low_min_{n}"] = groups["low"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).min())
        df[f"return_{n}"] = groups["close"].transform(lambda s: s.pct_change(n, fill_method=None))
    df["volume_ratio_1"] = groups["volume"].transform(lambda s: s / s.shift(1))
    df["amount_ratio_1"] = groups["amount"].transform(lambda s: s / s.shift(1))
    # Teaching feature: current close relative to the 20-observation mean.
    # It is causal at the close; the backtest acts no earlier than next open.
    df["ma_gap_20"] = df["close"] / df["ma_20"] - 1
    df["close_above_high_20"] = df["close"] > df["high_max_20"]
    df["close_above_low_10"] = df["close"] > df["low_min_10"]

    # Factors required by the independent 0708cao signal family.
    df["bbi_0708cao"] = df[["ma_3", "ma_6", "ma_12", "ma_24"]].mean(axis=1, skipna=False)
    df["hlc3_0708cao"] = (df["high"] + df["low"] + df["close"]) / 3
    df["vma3_0708cao"] = groups["volume"].transform(lambda s: s.rolling(3, min_periods=3).mean())
    df["macd_dif_0708cao"] = groups["close"].transform(
        lambda s: s.ewm(span=12, adjust=False, min_periods=12).mean()
        - s.ewm(span=26, adjust=False, min_periods=26).mean())
    df["macd_dea_0708cao"] = df.groupby("code", sort=False)["macd_dif_0708cao"].transform(
        lambda s: s.ewm(span=9, adjust=False, min_periods=9).mean())
    df["macd_bar_0708cao"] = 2 * (df["macd_dif_0708cao"] - df["macd_dea_0708cao"])
    df["turnover_rate_ratio_0708cao"] = groups["turnover_rate"].transform(
        lambda s: s / s.shift(1).where(s.shift(1) != 0))

    for window, lag, name in ((5, 4, "hlc_amplitude_5_0708cao"),
                              (11, 9, "hlc_amplitude_11_0708cao"),
                              (4, 0, "hlc_amplitude_4_0708cao"),
                              (4, 1, "hlc_amplitude_4_prior_0708cao")):
        highs = groups["high"].transform(lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).max())
        high_lows = groups["low"].transform(lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).max())
        high_closes = groups["close"].transform(lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).max())
        low_highs = groups["high"].transform(lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).min())
        lows = groups["low"].transform(lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).min())
        low_closes = groups["close"].transform(lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).min())
        max_hlc3, min_hlc3 = (highs + high_lows + high_closes) / 3, (low_highs + lows + low_closes) / 3
        df[name] = ((max_hlc3 - min_hlc3) / min_hlc3.where(min_hlc3 != 0)).abs()
    df["high_max_5_0708cao"] = groups["high"].transform(lambda s: s.shift(4).rolling(5, min_periods=5).max())
    df["high_max_11_0708cao"] = groups["high"].transform(lambda s: s.shift(9).rolling(11, min_periods=11).max())
    # VMA3 range for Sig5 uses T-1 through T-4, inclusive.
    vma3_history = groups["volume"].transform(lambda s: s.rolling(3, min_periods=3).mean())
    df["volume_vma3_range_4_0708cao"] = (
        vma3_history.groupby(df["code"], sort=False).transform(lambda s: s.shift(1).rolling(4, min_periods=4).max())
        - vma3_history.groupby(df["code"], sort=False).transform(lambda s: s.shift(1).rolling(4, min_periods=4).min())
    ) / vma3_history.groupby(df["code"], sort=False).transform(
        lambda s: s.shift(1).rolling(4, min_periods=4).min().where(
            s.shift(1).rolling(4, min_periods=4).min() != 0))
    # The longer Sig9 reference is T-18..T-5: retain the exact trough windows.
    for window, lag, name in ((4, 0, "low_min_4_0708cao"), (9, 0, "low_min_9_0708cao"),
                              (14, 5, "low_min_14_0708cao")):
        df[name] = groups["low"].transform(
            lambda s, n=window, shift=lag: s.shift(shift).rolling(n, min_periods=n).min())
    for window in (5, 10, 20, 30):
        df[f"ma_{window}_above_lag20_0708cao"] = df[f"ma_{window}"] > groups[f"ma_{window}"].shift(20)
    df["close_above_ma250_0708cao"] = df["close"] > df["ma_250"]
    df["prior_breakdown_0708cao"] = (groups["low"].shift(2) < groups["ma_250"].shift(2)) & (
        groups["close"].shift(2) > groups["ma_250"].shift(2))
    df["prior_close_above_ma250_0708cao"] = groups["close"].shift(3) > groups["ma_250"].shift(3)
    df["yearline_slope_0708cao"] = df["close"] > groups["close"].shift(20)
    df["near_ma250_0708cao"] = (groups["low"].shift(2) >= groups["ma_250"].shift(2)) & (
        groups["low"].shift(2) <= groups["ma_250"].shift(2) * 1.03)
    df["crossed_ma250_0708cao"] = (groups["low"].shift(3) < groups["ma_250"].shift(3)) & (
        groups["close"].shift(2) > groups["ma_250"].shift(2))
    for lag in range(1, 5):
        df[f"close_above_bbi_lag_{lag}_0708cao"] = groups["close"].shift(lag) > groups["bbi_0708cao"].shift(lag)
    return df
