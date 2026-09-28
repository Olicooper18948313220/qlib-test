"""Independent translation of the 0708cao rules in C模型策略-1016.xlsx.

The signal day is T. All factors are causal and the existing runner trades no
earlier than the next session. Conditions intentionally follow the workbook's
F-column definitions, including its documented unusual formulas.
"""
from __future__ import annotations

import pandas as pd

from ..factors.technical import ZFF0708_MACD_WARMUP, zff0708_factor_names


signal_names = [f"0708cao_sig{i}" for i in (1, 11, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
signal_lookbacks = dict(zip(signal_names, [76, 76, 76, 30, 76, 30, 253, 252, 253, 19, 50]))
signal_descriptions = {
    "0708cao_sig1": "底部上破1：放量、BBI、MACD、5期窄幅区间突破",
    "0708cao_sig11": "底部上破2：放量、BBI、MACD、11期窄幅区间突破",
    "0708cao_sig2": "上升趋势：上穿MA5、MACD、首次站上BBI",
    "0708cao_sig3": "上涨中继1：回调缩量、靠近MA10、多头均线、涨幅区间",
    "0708cao_sig4": "上涨中继2：回调缩量、靠近MA20、MACD、多头均线",
    "0708cao_sig5": "上涨中继3：缩量、窄幅横盘、上穿MA5、多头均线",
    "0708cao_sig6": "年线企稳上涨1：跌破后站回MA250并确认",
    "0708cao_sig7": "年线企稳上涨2：靠近MA250并连续站上",
    "0708cao_sig8": "年线企稳上涨3：向上穿越MA250并确认",
    "0708cao_sig9": "低点中线上移：短期横盘低点高于前期低点",
    "0708cao_sig10": "价增量涨：成交量、换手率放大，均线高于20期前",
}
signal_trigger_descriptions = {
    "0708cao_sig1": "成交额≥昨日2倍；close>BBI；MACD DIF/DEA绝对值≤0.25且BAR为0–99；close突破T−8至T−4最高价，区间HLC振幅≤20%。",
    "0708cao_sig11": "与Sig1相同，突破区间改为T−19至T−9，区间HLC振幅≤20%。",
    "0708cao_sig2": "low<MA5且close>MA5；MACD条件；close>BBI且前4期各自close低于对应BBI。",
    "0708cao_sig3": "HLC3连续两期回落、VMA3低于昨日；MA10与close均处于较大值±10%；四均线多头；T至T−8低点对应涨幅满足两项比例条件。",
    "0708cao_sig4": "HLC3连续两期回落、VMA3低于昨日；low与MA20满足表中±5%上下界；MACD条件；四均线多头。",
    "0708cao_sig5": "昨日成交量≥今日1.1倍；T−4至T−1的HLC振幅≤20%、VMA3波动≤30%；close>MA5、昨日low<昨日MA5；四均线多头。",
    "0708cao_sig6": "T和T−1收盘均高于对应MA250；T−2低点跌破且收盘站回MA250；T−3收盘高于MA250；T收盘高于20期前收盘。",
    "0708cao_sig7": "T和T−1收盘均高于对应MA250；T−2低点处于MA250至其103%；T收盘高于20期前收盘。",
    "0708cao_sig8": "T和T−1收盘均高于对应MA250；T−3低点低于MA250；T−2收盘高于MA250。",
    "0708cao_sig9": "T至T−3的HLC振幅≤5%；该区间最低点相对T−18至T−5最低点的表定比率在10%至99%之间。",
    "0708cao_sig10": "当日成交量与换手率分别≥昨日4倍；MA5、MA10、MA20、MA30分别高于自身20期前的值。",
}
_COMPONENTS = (
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
component_factor_names = [f"signal_condition_{member[3:]}_0708cao" for member in _COMPONENTS]


def _valid(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    return frame[columns].notna().all(axis=1)


def _macd_ok(frame: pd.DataFrame) -> pd.Series:
    count = frame.groupby("code", sort=False).cumcount() + 1
    return (count >= ZFF0708_MACD_WARMUP) & _valid(
        frame, ["macd_dif_0708cao", "macd_dea_0708cao", "macd_bar_0708cao"])


def score_0708cao(frame: pd.DataFrame, enabled: list[str] | None = None) -> pd.DataFrame:
    """Add 0708cao component booleans, named signals, score and union signal."""
    unknown = set(enabled or ()) - set(signal_names)
    if unknown:
        raise ValueError(f"0708cao 未知信号: {sorted(unknown)}")
    df = frame.sort_values(["code", "date"]).copy().reset_index(drop=True)
    g = df.groupby("code", sort=False)
    close, low, volume, amount = df["close"], df["low"], df["volume"], df["amount"]
    bbi, dif = df["bbi_0708cao"], df["macd_dif_0708cao"]
    dea, bar = df["macd_dea_0708cao"], df["macd_bar_0708cao"]
    ma5, ma10, ma20, ma30, ma250 = (df[f"ma_{n}"] for n in (5, 10, 20, 30, 250))
    macd_ok = _macd_ok(df)
    macd_near_zero = (dif.abs() <= 0.25) & (dea.abs() <= 0.25) & bar.between(0, 99)
    previous_amount = g["amount"].shift(1)
    previous_volume = g["volume"].shift(1)
    previous_close = g["close"].shift(1)
    previous_low = g["low"].shift(1)
    components = {
        "sig1_amount": (amount >= previous_amount * 2),
        "sig1_bbi": close > bbi,
        "sig1_macd": macd_ok & macd_near_zero,
        "sig1_breakout": close > df["high_max_5_0708cao"],
        "sig1_range": df["hlc_amplitude_5_0708cao"] <= 0.20,
        "sig11_amount": (amount >= previous_amount * 2),
        "sig11_bbi": close > bbi,
        "sig11_macd": macd_ok & macd_near_zero,
        "sig11_breakout": close > df["high_max_11_0708cao"],
        "sig11_range": df["hlc_amplitude_11_0708cao"] <= 0.20,
        "sig2_cross_ma5": (low < ma5) & (close > ma5),
        "sig2_macd": macd_ok & macd_near_zero,
        "sig2_first_bbi": (close > bbi) & pd.concat(
            [g["close"].shift(lag) < g["bbi_0708cao"].shift(lag) for lag in range(1, 5)], axis=1).all(axis=1),
        "sig3_hlc_pullback": (df["hlc3_0708cao"] < g["hlc3_0708cao"].shift(1)) &
                             (df["hlc3_0708cao"] < g["hlc3_0708cao"].shift(2)),
        "sig3_volume_pullback": df["vma3_0708cao"] < g["vma3_0708cao"].shift(1),
        "sig3_near_ma10": (ma10 >= pd.concat([ma10, close], axis=1).max(axis=1) * 0.90) &
                          (ma10 <= pd.concat([ma10, close], axis=1).max(axis=1) * 1.10) &
                          (close >= pd.concat([ma10, close], axis=1).max(axis=1) * 0.90) &
                          (close <= pd.concat([ma10, close], axis=1).max(axis=1) * 1.10),
        "sig3_ma_order": (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma30),
        "sig3_rise": ((close - df["low_min_9_0708cao"]) / close >= 0.15) &
                     ((close - df["low_min_9_0708cao"]) / df["low_min_9_0708cao"] <= 0.40),
        "sig4_hlc_pullback": (df["hlc3_0708cao"] < g["hlc3_0708cao"].shift(1)) &
                             (df["hlc3_0708cao"] < g["hlc3_0708cao"].shift(2)),
        "sig4_volume_pullback": df["vma3_0708cao"] < g["vma3_0708cao"].shift(1),
        "sig4_near_ma20": (low >= pd.concat([low, ma20], axis=1).min(axis=1) * 0.95) &
                          (low <= pd.concat([low, ma20], axis=1).max(axis=1) * 1.05) &
                          (ma20 >= pd.concat([low, ma20], axis=1).min(axis=1) * 0.95) &
                          (ma20 <= pd.concat([low, ma20], axis=1).max(axis=1) * 1.05),
        "sig4_macd": macd_ok & macd_near_zero,
        "sig4_ma_order": (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma30),
        "sig5_volume": previous_volume >= volume * 1.1,
        "sig5_price_range": df["hlc_amplitude_4_prior_0708cao"] <= 0.20,
        "sig5_volume_range": df["volume_vma3_range_4_0708cao"] <= 0.30,
        "sig5_ma_cross": (close > ma5) & (previous_low < g["ma_5"].shift(1)),
        "sig5_ma_order": (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma30),
        "sig6_ma250_pair": (close > ma250) & (previous_close > g["ma_250"].shift(1)),
        "sig6_breakdown_recovery": df["prior_breakdown_0708cao"],
        "sig6_rise": df["yearline_slope_0708cao"],
        "sig6_prior_close": df["prior_close_above_ma250_0708cao"],
        "sig7_ma250_pair": (close > ma250) & (previous_close > g["ma_250"].shift(1)),
        "sig7_near_ma250": df["near_ma250_0708cao"],
        "sig7_rise": df["yearline_slope_0708cao"],
        "sig8_ma250_pair": (close > ma250) & (previous_close > g["ma_250"].shift(1)),
        "sig8_cross_ma250": df["crossed_ma250_0708cao"],
        "sig9_price_range": df["hlc_amplitude_4_0708cao"] <= 0.05,
        "sig9_higher_low": ((df["low_min_4_0708cao"] - df["low_min_14_0708cao"])
                            / df["low_min_4_0708cao"]).between(0.10, 99.0),
        "sig10_volume": volume >= previous_volume * 4,
        "sig10_turnover": df["turnover_rate"] >= g["turnover_rate"].shift(1) * 4,
        "sig10_ma_rise": (df["ma_5_above_lag20_0708cao"] & df["ma_10_above_lag20_0708cao"] &
                          df["ma_20_above_lag20_0708cao"] & df["ma_30_above_lag20_0708cao"]),
    }
    component_for_signal = {
        "0708cao_sig1": ["sig1_amount", "sig1_bbi", "sig1_macd", "sig1_breakout", "sig1_range"],
        "0708cao_sig11": ["sig11_amount", "sig11_bbi", "sig11_macd", "sig11_breakout", "sig11_range"],
        "0708cao_sig2": ["sig2_cross_ma5", "sig2_macd", "sig2_first_bbi"],
        "0708cao_sig3": ["sig3_hlc_pullback", "sig3_volume_pullback", "sig3_near_ma10", "sig3_ma_order", "sig3_rise"],
        "0708cao_sig4": ["sig4_hlc_pullback", "sig4_volume_pullback", "sig4_near_ma20", "sig4_macd", "sig4_ma_order"],
        "0708cao_sig5": ["sig5_volume", "sig5_price_range", "sig5_volume_range", "sig5_ma_cross", "sig5_ma_order"],
        "0708cao_sig6": ["sig6_ma250_pair", "sig6_breakdown_recovery", "sig6_rise", "sig6_prior_close"],
        "0708cao_sig7": ["sig7_ma250_pair", "sig7_near_ma250", "sig7_rise"],
        "0708cao_sig8": ["sig8_ma250_pair", "sig8_cross_ma250"],
        "0708cao_sig9": ["sig9_price_range", "sig9_higher_low"],
        "0708cao_sig10": ["sig10_volume", "sig10_turnover", "sig10_ma_rise"],
    }
    active = set(signal_names if enabled is None else enabled)
    for name, members in component_for_signal.items():
        component_columns = []
        for member in members:
            column = f"signal_condition_{member[3:]}_0708cao"
            values = components[member].fillna(False).astype(bool)
            df[column] = values
            component_columns.append(column)
        values = df[component_columns].all(axis=1) if name in active else pd.Series(False, index=df.index)
        df[name] = values
    df["signal_score"] = df[signal_names].sum(axis=1).astype(float)
    df["signal"] = df["signal_score"] > 0
    return df


diagnostic_factor_names = zff0708_factor_names
