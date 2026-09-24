import pandas as pd

from qlib_quant.factors.technical import add_technical_features
from qlib_quant.signals.legacy_v1 import score_signals, signal_names
from qlib_quant.portfolio.baseline import select_equal_weight


def sample():
    rows = []
    for code in ["000001", "600000"]:
        for i in range(300):
            close = 10 + i * 0.01
            rows.append({"code": code, "date": f"2020-01-{(i % 28) + 1:02d}", "open": close,
                         "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1000 + i, "amount": 10000 + i})
    return pd.DataFrame(rows)


def test_features_are_causal():
    out = add_technical_features(sample())
    assert out.loc[out.index[0], "return_5"] != out.loc[out.index[0], "return_5"]
    assert "close_above_high_20" in out


def test_signal_registry_and_portfolio():
    out = score_signals(add_technical_features(sample()))
    assert len(signal_names) == 11
    assert out["signal_score"].ge(0).all()
    selected = select_equal_weight(out[out["date"] == out["date"].max()], 1)
    assert len(selected) <= 1

