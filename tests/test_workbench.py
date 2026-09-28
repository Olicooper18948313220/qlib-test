import pandas as pd

from qlib_quant.backtest.simple import run_backtest


def _quotes(codes=("A", "B")):
    rows = []
    for date in pd.date_range("2020-01-03", "2020-01-17", freq="B"):
        for code in codes:
            price = 10.0 if code == "A" else 20.0
            rows.append({"date": date, "code": code, "open": price, "close": price,
                         "signal": True, "signal_score": 2 if code == "A" else 1})
    return pd.DataFrame(rows)


def test_warmup_does_not_spend_initial_cash_and_repeat_target_is_delta_only():
    equity, trades = run_backtest(_quotes(), initial_cash=100_000, max_positions=1,
                                  start_date="2020-01-06", end_date="2020-01-17")
    assert not trades.empty
    assert all(pd.to_datetime(trades["date"]) >= pd.Timestamp("2020-01-06"))
    # The stable target is held across weekly rebalances; no duplicate buys
    # should overwrite the original share count.
    buys = trades[trades["side"] == "buy"]
    assert len(buys) == 1
    assert equity.iloc[0]["market_value"] == 100_000
    assert equity["cash"].min() >= 0


def test_missing_next_quote_keeps_position_and_marks_stale_valuation():
    frame = _quotes()
    # Remove A after it has been acquired, making the next valuation stale.
    frame = frame[~((frame["date"] == pd.Timestamp("2020-01-17")) & (frame["code"] == "A"))]
    equity, trades = run_backtest(frame, initial_cash=100_000, max_positions=1,
                                  start_date="2020-01-03", end_date="2020-01-17")
    assert not trades.empty
    assert "stale_valuation" in equity
    assert equity["stale_valuation"].any()
