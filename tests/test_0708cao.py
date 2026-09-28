import pandas as pd

from qlib_quant.config import effective_config, validate_config
from qlib_quant.factors.technical import add_technical_features
from qlib_quant.runner import resolve_request, warmup_window
from qlib_quant.signals.zff0708 import score_0708cao, signal_lookbacks, signal_names


def quotes(periods=300):
    rows = []
    dates = pd.bdate_range("2020-01-01", periods=periods)
    for code, shift in (("A", 0.0), ("B", 100.0)):
        for index, date in enumerate(dates):
            close = 10 + shift + index * 0.01
            rows.append({"code": code, "date": date, "open": close, "high": close + 0.5,
                         "low": close - 0.5, "close": close, "volume": 1000.0,
                         "amount": 10000.0, "turnover_rate": 1.0})
    return pd.DataFrame(rows)


def test_0708cao_registry_and_exact_warmup():
    assert len(signal_names) == 11
    assert signal_lookbacks["0708cao_sig6"] == 253
    config = effective_config({"signals": {"version": "0708cao", "enabled": ["0708cao_sig6"]}})
    validate_config(config)
    assert warmup_window(config) == 253
    resolved = resolve_request({"config": {"signals": {"version": "0708cao", "enabled": ["0708cao_sig6"]}}})
    assert resolved["signals"]["thresholds"] == {}


def test_new_factors_are_stock_isolated_and_causal():
    frame = quotes()
    all_features = add_technical_features(frame)
    isolated = add_technical_features(frame[frame.code == "B"].drop(columns="code").assign(code="B"))
    actual = all_features[all_features.code == "B"].reset_index(drop=True)
    pd.testing.assert_series_equal(actual["bbi_0708cao"], isolated["bbi_0708cao"], check_names=False)
    pd.testing.assert_series_equal(actual["macd_bar_0708cao"], isolated["macd_bar_0708cao"], check_names=False)

    cutoff = pd.Timestamp("2020-10-01")
    changed = frame.copy()
    future = changed.date > cutoff
    changed.loc[future, ["open", "high", "low", "close", "volume", "amount"]] *= 3
    changed_features = add_technical_features(changed)
    columns = ["bbi_0708cao", "macd_dif_0708cao", "macd_dea_0708cao", "macd_bar_0708cao",
               "hlc_amplitude_5_0708cao", "ma_250"]
    before = all_features[(all_features.code == "A") & (all_features.date <= cutoff)].reset_index(drop=True)
    after = changed_features[(changed_features.code == "A") & (changed_features.date <= cutoff)].reset_index(drop=True)
    pd.testing.assert_frame_equal(before[columns], after[columns])


def test_sig1_uses_macd_and_workbook_breakout_conditions():
    frame = quotes(76)
    frame.loc[frame.index[-1], "amount"] = 20000.0
    features = add_technical_features(frame)
    final = features.index[-1]
    features.loc[final, ["bbi_0708cao", "macd_dif_0708cao", "macd_dea_0708cao",
                         "macd_bar_0708cao", "high_max_5_0708cao", "hlc_amplitude_5_0708cao"]] = [
                             10.0, 0.1, 0.05, 0.1, 10.5, 0.1]
    # Lift the previous close enough for the amount ratio to reach 2x.
    features.loc[final - 1, "amount"] = 10000.0
    result = score_0708cao(features, ["0708cao_sig1"])
    assert result.loc[final, "0708cao_sig1"]
    assert result.loc[final, "signal_score"] == 1
    disabled = score_0708cao(features, [])
    assert not disabled["signal"].any()


def test_sig9_formula_and_sig10_turnover_field():
    frame = quotes(80)
    frame.loc[frame.index[-1], "volume"] = 4000.0
    frame.loc[frame.index[-1], "turnover_rate"] = 4.0
    features = add_technical_features(frame)
    final = features.index[-1]
    features.loc[final, ["hlc_amplitude_4_0708cao", "low_min_4_0708cao", "low_min_14_0708cao"]] = [
        0.01, 10.0, 8.0]
    both = score_0708cao(features, ["0708cao_sig9", "0708cao_sig10"])
    assert both.loc[final, "0708cao_sig9"]
    assert both.loc[final, "0708cao_sig10"]
    assert both.loc[final, "signal_score"] == 2

    # Missing turnover rate prevents Sig10 without preventing other signals.
    features.loc[final, "turnover_rate"] = float("nan")
    missing_turnover = score_0708cao(features, ["0708cao_sig10"])
    assert not missing_turnover.loc[final, "0708cao_sig10"]
