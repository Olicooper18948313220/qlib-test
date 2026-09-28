import json
import math
import sqlite3
import sys
import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from qlib_quant.backtest.simple import run_backtest
from qlib_quant.data.db import _connect, iter_quotes, validate_database
from qlib_quant.experiments import read_csv, read_json, discover_runs, config_diff, run_classification
from qlib_quant.runner import run_request, resolve_request, CancelledError
from qlib_quant.signals.legacy_v1 import score_signals, signal_names
from qlib_quant.factors.technical import add_technical_features


def quotes():
    return pd.DataFrame([dict(date=str(d.date()), code=c, open=10., close=10., signal=True,
                              signal_score=2 if c == "A" else 1)
                         for d in pd.bdate_range("2020-01-03", "2020-01-24") for c in ("A", "B")])


def ledger(equity, trades, initial):
    cash, shares = initial, {}
    for row in equity.itertuples():
        for t in trades[trades.date == row.date].itertuples():
            sign = 1 if t.side == "buy" else -1
            cash -= sign * t.shares * t.price + t.fee
            shares[t.code] = shares.get(t.code, 0) + sign * t.shares
            if shares[t.code] == 0:
                shares.pop(t.code)
        assert row.cash == pytest.approx(cash)
        assert json.loads(row.holdings) == shares
        positions = equity.attrs["positions"]
        marked = positions[positions.date == row.date]
        assert row.holdings_value == pytest.approx((marked.shares * marked.valuation_price).sum())
        assert row.market_value == pytest.approx(cash + row.holdings_value)
        assert row.total_fees == pytest.approx(trades.loc[trades.date == row.date, "fee"].sum())
        assert row.cash >= -1e-7


def test_hand_calculated_buy_hold_sell_fees():
    frame = quotes()
    frame.loc[frame.date >= "2020-01-10", "signal"] = False
    frame.loc[frame.date >= "2020-01-13", ["open", "close"]] = 12.
    equity, trades = run_backtest(frame, initial_cash=10000, max_positions=1,
                                  commission_rate=.001, stamp_duty_rate=.002, slippage_bps=10)
    assert list(trades.side) == ["buy", "sell"]
    assert list(trades.shares) == [900, 900]
    assert list(trades.date) == ["2020-01-06", "2020-01-13"]
    assert trades.iloc[0].price == pytest.approx(10.01)
    assert trades.iloc[1].price == pytest.approx(11.988)
    assert trades.iloc[0].tax == 0
    assert equity.iloc[-1].cash == pytest.approx(11738.8234)
    ledger(equity, trades, 10000)


def test_reduce_increase_and_stable_holdings():
    frame = quotes()
    frame.loc[(frame.code == "B") & ((frame.date < "2020-01-10") | (frame.date >= "2020-01-17")), "signal"] = False
    equity, trades = run_backtest(frame, initial_cash=10000, max_positions=2,
                                  commission_rate=0, stamp_duty_rate=0, slippage_bps=0)
    assert list(trades.shares) == [1000, 500, 500, 500, 500]
    assert list(trades.side) == ["buy", "sell", "buy", "sell", "buy"]
    assert json.loads(equity.iloc[-1].holdings) == {"A": 1000}
    assert (equity.market_value == 10000).all()
    ledger(equity, trades, 10000)


@pytest.mark.parametrize("invalid", [0., -1., float("nan"), float("inf")])
def test_bad_open_never_executes_buy_or_sell(invalid):
    frame = quotes()
    frame.loc[(frame.date == "2020-01-06") & (frame.code == "A"), "open"] = invalid
    _, trades = run_backtest(frame, max_positions=1)
    assert not ((trades.date == "2020-01-06") & (trades.code == "A")).any()
    frame = quotes()
    frame.loc[frame.date >= "2020-01-10", "signal"] = False
    frame.loc[(frame.date == "2020-01-13") & (frame.code == "A"), "open"] = invalid
    equity, trades = run_backtest(frame, max_positions=1)
    assert not ((trades.date == "2020-01-13") & (trades.side == "sell")).any()
    assert equity.loc[equity.date == "2020-01-13", "positions"].iloc[0] == 1
    assert not equity.attrs["skipped_orders"].empty
    ledger(equity, trades, 1000000)


def test_missing_whole_day_and_invalid_close_are_marked():
    frame = quotes()
    calendar = sorted(frame.date.unique())
    frame = frame[frame.date != "2020-01-07"]
    frame.loc[(frame.date == "2020-01-08") & (frame.code == "A"), "close"] = float("nan")
    equity, trades = run_backtest(frame, max_positions=1, calendar=calendar)
    assert equity.loc[equity.date.isin(["2020-01-07", "2020-01-08"]), "stale_valuation"].all()
    ledger(equity, trades, 1000000)


def test_suspension_end_boundary_and_empty():
    frame = quotes()
    frame["trade_status"] = "normal"
    frame.loc[frame.date == "2020-01-06", "trade_status"] = 0
    equity, trades = run_backtest(frame, start_date="2020-01-03", end_date="2020-01-10")
    assert trades.empty
    assert equity.iloc[0].market_value == 1000000
    with pytest.raises(ValueError, match="没有行情"):
        run_backtest(frame, start_date="2021-01-01", end_date="2021-02-01")
    with pytest.raises(ValueError, match="重复"):
        run_backtest(pd.concat([frame, frame.iloc[:1]]))


def test_signals_stock_boundaries_index_alignment_and_no_future():
    rows = []
    for c, base in [("A", 1), ("B", 1000)]:
        for i, d in enumerate(pd.bdate_range("2019-01-01", periods=280)):
            p = base + i / 10
            rows.append(dict(code=c, date=str(d.date()), close=p, high=p, low=p, volume=100+i, amount=10000+i))
    frame = pd.DataFrame(rows).sample(frac=1, random_state=42)
    frame.index = range(1000, 1000 + len(frame))
    scored = score_signals(frame)
    for code in ("A", "B"):
        independently = score_signals(frame[frame.code == code])
        assert_frame_equal(scored[scored.code == code], independently)
    cutoff = "2019-12-31"
    changed = frame.copy()
    changed.loc[changed.date > cutoff, ["close", "high", "low", "volume", "amount"]] *= 5
    altered = score_signals(changed)
    assert_frame_equal(scored[scored.date <= cutoff], altered[altered.date <= cutoff])
    assert not score_signals(frame, enabled=[])["signal"].any()


def test_learning_factor_signal_math_causality_and_default_off():
    rows = []
    dates = pd.bdate_range("2020-01-01", periods=35)
    for code, offset in (("A", 10.0), ("B", 100.0)):
        for i, date in enumerate(dates):
            close = offset + i * .1
            rows.append({"code": code, "date": str(date.date()), "open": close,
                         "high": close, "low": close, "close": close,
                         "volume": 1000 + i, "amount": 10000 + i})
    frame = pd.DataFrame(rows)
    featured = add_technical_features(frame)
    row = featured[(featured.code == "A") & (featured.date == str(dates[24].date()))].iloc[0]
    expected = row.close / frame.loc[frame.code.eq("A"), "close"].iloc[5:25].mean() - 1
    assert row.ma_gap_20 == pytest.approx(expected)
    default = score_signals(featured)
    assert not default.learning_ma_gap.any()
    enabled = score_signals(featured, {"learning_ma_gap_min": .02}, ["learning_ma_gap"])
    observation = enabled[(enabled.code == "A") & (enabled.date == str(dates[24].date()))]
    assert observation["learning_ma_gap"].iloc[0]
    stricter = score_signals(featured, {"learning_ma_gap_min": .2}, ["learning_ma_gap"])
    assert not stricter.learning_ma_gap.any()
    changed = frame.copy()
    cutoff = str(dates[24].date())
    changed.loc[changed.date > cutoff, ["close", "high", "low"]] *= 4
    altered = score_signals(add_technical_features(changed), {"learning_ma_gap_min": .02},
                            ["learning_ma_gap"])
    left = enabled[enabled.date <= cutoff].sort_values(["code", "date"]).reset_index(drop=True)
    right = altered[altered.date <= cutoff].sort_values(["code", "date"]).reset_index(drop=True)
    assert_frame_equal(left, right)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "quotes.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
        create table daily_quote (stock_code text, stock_name text, trade_date text, open_price real,
        high_price real, low_price real, close_price real, volume real, turnover real, turnover_rate real,
        trade_status text, adjustment text, source text, instrument_type text);
        create table stock_info(stock_code text);
        create table trade_calendar(trade_date text, is_open integer);
        """)
        dates = pd.bdate_range("2018-01-01", "2020-02-28")
        rows = []
        for i, d in enumerate(dates):
            for code, base in (("A", 10), ("B", 20), ("C", 30)):
                p = base + i / 100
                rows.append((code, code, str(d.date()), p, p, p, p, 1000, 10000, 0, "normal", "hfq", "test", "equity"))
        con.executemany("insert into daily_quote values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("insert into trade_calendar values (?,1)", [(str(d.date()),) for d in dates])
    return path


def request(database, out, **kwargs):
    return dict(database=str(database), start="2020-01-01", end="2020-02-28", max_stocks=2,
                output=str(out), **kwargs)


def test_runner_snapshot_warmup_repeat_and_empty_csv(database, tmp_path):
    req = request(database, tmp_path / "runs/a", config={"signals": {"enabled": []}})
    out = run_request(req)
    metadata = read_json(out / "run_metadata.json")
    assert metadata["effective_config"]["data"]["database"] == str(database.resolve())
    assert metadata["effective_config"]["backtest"]["start"] == req["start"]
    assert metadata["effective_config"]["backtest"]["max_stocks"] == 2
    assert all(s["warmup_rows"] == 250 for s in metadata["stock_sample"])
    assert read_csv(out / "trades.csv").empty
    out2 = run_request(req)
    assert out2 != out
    assert (out / "equity_curve.csv").read_bytes() == (out2 / "equity_curve.csv").read_bytes()
    assert run_classification(out) == "V1 修复版"
    assert len(discover_runs(tmp_path / "runs")) == 2
    assert config_diff(metadata["effective_config"], metadata["effective_config"]).empty


def test_runner_executes_0708cao_and_saves_component_diagnostics(database, tmp_path):
    req = request(database, tmp_path / "runs/0708cao", config={
        "signals": {"version": "0708cao", "enabled": ["0708cao_sig9"], "thresholds": {}}
    })
    out = run_request(req)
    metadata = read_json(out / "run_metadata.json")
    assert metadata["effective_config"]["signals"]["version"] == "0708cao"
    assert metadata["warmup_rows_required"] == 250
    panel = pd.read_csv(out / "evaluation" / "factor_panel.csv.gz")
    assert "0708cao_sig9" in panel
    assert "signal_condition_9_price_range_0708cao" in panel
    assert "hlc_amplitude_4_0708cao" in panel


def test_database_readonly_and_missing_fields(database, tmp_path):
    with _connect(database) as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("delete from daily_quote")
    assert list(iter_quotes(database, "2020-01-01", "2020-02-28", codes=[])) == []
    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as con:
        con.executescript("create table daily_quote(x text); create table stock_info(x); create table trade_calendar(x);")
    with pytest.raises(ValueError, match="字段"):
        validate_database(empty)


def test_invalid_parameters_and_persisted_failure(database, tmp_path):
    for config in ({"backtest": {"commission_rate": -1}}, {"signals": {"version": "madeup"}},
                   {"backtest": {"max_positions": 0}}, {"backtest": {"rebalance": "daily"}}):
        with pytest.raises(ValueError):
            resolve_request(request(database, tmp_path / "invalid", config=config))
    learning = resolve_request(request(database, tmp_path / "learning", config={
        "signals": {"enabled": ["learning_ma_gap"], "thresholds": {"learning_ma_gap_min": .05}}
    }))
    assert learning["signals"]["thresholds"]["learning_ma_gap_min"] == .05
    with pytest.raises(ValueError):
        run_request(dict(request(database, tmp_path / "failed"), start="2030-01-01"))
    assert read_json(tmp_path / "failed/status.json")["state"] == "failed"
    assert (tmp_path / "failed/error.log").exists()


def test_config_snapshot_does_not_reread_defaults(database, tmp_path, monkeypatch):
    import qlib_quant.runner as runner
    config = resolve_request(request(database, tmp_path / "unused"))
    monkeypatch.setattr(runner, "effective_config", lambda *_: (_ for _ in ()).throw(AssertionError("reloaded defaults")))
    out = run_request({"resolved": True, "config": config, "output": str(tmp_path / "snapshot")})
    assert read_json(out / "status.json")["state"] == "success"


def test_cli_and_shared_service_produce_identical_results(database, tmp_path):
    direct = run_request(request(database, tmp_path / "direct",
                                 config={"backtest": {"max_positions": 1}}))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-m", "qlib_quant.cli", "backtest",
                             "--database", str(database), "--start", "2020-01-01", "--end", "2020-02-28",
                             "--max-stocks", "2", "--max-positions", "1", "--output", str(tmp_path / "cli")],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for name in ("trades.csv", "equity_curve.csv", "positions.csv", "summary.json",
                 "evaluation/evaluation.json", "evaluation/factor_panel.csv.gz",
                 "evaluation/factor_summary.csv", "evaluation/trade_pairs.csv"):
        assert (direct / name).read_bytes() == (tmp_path / "cli" / name).read_bytes()
