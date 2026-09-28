from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from qlib_quant.evaluation import (
    _benchmark_evaluation,
    _factor_diagnostics,
    _trade_analysis,
    account_evaluation,
    create_factor_store,
    store_factor_frame,
)


def test_account_metrics_include_compounding_drawdown_and_recovery():
    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    equity = pd.DataFrame({"date": dates, "market_value": [100, 110, 99, 110],
                           "positions": [0, 1, 1, 0], "holdings_value": [0, 10, 9, 0]})
    summary, tables = account_evaluation(equity, pd.DataFrame(), pd.DataFrame(), 100)
    metrics = summary["metrics"]
    assert np.isclose(metrics["cumulative_return"]["value"], .1)
    assert np.isclose(metrics["max_drawdown"]["value"], -.1)
    assert metrics["sharpe"]["n"] == 3
    assert tables["drawdowns"].iloc[0]["peak_date"] == "2024-01-02"
    assert tables["drawdowns"].iloc[0]["recovery_date"] == "2024-01-04"
    assert np.isclose(tables["annual_returns"].iloc[0]["return"], .1)


def test_account_marks_unrecovered_drawdown_and_short_tail_sample():
    equity = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3, freq="D"),
                           "market_value": [100, 90, 95], "positions": [0, 0, 0],
                           "holdings_value": [0, 0, 0]})
    summary, tables = account_evaluation(equity, pd.DataFrame(), pd.DataFrame(), 100)
    assert not bool(tables["drawdowns"].iloc[0]["recovered"])
    assert summary["metrics"]["historical_var_95"]["value"] is None
    assert "少于100期" in summary["metrics"]["historical_es_95"]["unavailable_reason"]


def test_fifo_pairs_costs_and_open_positions_reconcile_account():
    trades = pd.DataFrame([
        {"date": "2024-01-01", "code": "AAA", "side": "buy", "shares": 10, "gross": 100, "fee": 1,
         "commission": 1, "tax": 0, "slippage_cost": .2},
        {"date": "2024-01-02", "code": "AAA", "side": "sell", "shares": 5, "gross": 60, "fee": 1,
         "commission": .5, "tax": .5, "slippage_cost": .2},
        {"date": "2024-01-03", "code": "AAA", "side": "sell", "shares": 5, "gross": 60, "fee": 1,
         "commission": .5, "tax": .5, "slippage_cost": .2},
        {"date": "2024-01-03", "code": "BBB", "side": "buy", "shares": 4, "gross": 80, "fee": 1,
         "commission": 1, "tax": 0, "slippage_cost": .1},
    ])
    equity = pd.DataFrame({"date": ["2024-01-01", "2024-01-02", "2024-01-03"],
                           "market_value": [1000, 1018, 1024]})
    positions = pd.DataFrame([{"date": "2024-01-03", "code": "BBB", "shares": 4,
                               "valuation_price": 22, "market_value": 88}])
    summary, tables = _trade_analysis(trades, positions, equity, 1000)
    metrics = summary["metrics"]
    assert len(tables["trade_pairs"]) == 2
    assert len(tables["trade_cycles"]) == 1
    assert np.isclose(metrics["realized_net_pnl"]["value"], 17)
    assert np.isclose(metrics["unrealized_net_pnl"]["value"], 7)
    assert abs(metrics["accounting_reconciliation_error"]["value"]) < 1e-9


def test_factor_ic_and_quantile_spread_use_aligned_future_opens(tmp_path):
    codes = [f"{i:06d}" for i in range(30)]
    dates = pd.date_range("2024-01-01", periods=3, freq="B")
    rows = []
    for day_index, day in enumerate(dates):
        for rank, code in enumerate(codes):
            if day_index == 0:
                open_price = 100.0
            elif day_index == 1:
                open_price = 100.0
            else:
                open_price = 100.0 + rank
            rows.append({"date": day, "code": code, "open": open_price,
                         "ma_gap_20": float(rank), "signal_score": float(rank)})
    frame = pd.DataFrame(rows)
    db_path = tmp_path / "panel.sqlite"
    con = create_factor_store(db_path, ["ma_gap_20", "signal_score"], [])
    index = {day.strftime("%Y-%m-%d"): i for i, day in enumerate(dates)}
    store_factor_frame(con, frame, index, ["ma_gap_20", "signal_score"], [])
    con.commit()
    con.close()
    result = _factor_diagnostics(db_path, tmp_path / "evaluation", ["ma_gap_20", "signal_score"], [], [1], 20, 3)
    row = result["metrics"][0]
    assert np.isclose(row["metrics"]["mean_ic"]["value"], 1.0)
    assert np.isclose(row["metrics"]["rank_autocorrelation"]["value"], 1.0)
    groups = pd.read_csv(tmp_path / "evaluation" / "factor_groups.csv")
    q1 = groups.loc[groups.group == "Q1", "mean_forward_return"].iloc[0]
    q5 = groups.loc[groups.group == "Q5", "mean_forward_return"].iloc[0]
    assert q5 > q1


def test_factor_ties_are_not_split_and_missing_market_open_is_not_filled(tmp_path):
    codes = [f"{i:06d}" for i in range(30)]
    dates = pd.date_range("2024-01-01", periods=3, freq="B")
    factor_values = [0.0] * 26 + [1.0, 2.0, 3.0, 4.0]
    rows = []
    for day_index, day in enumerate(dates):
        for rank, code in enumerate(codes):
            opening = 100.0 if day_index == 1 else (100.0 + rank if day_index == 2 else 100.0)
            if day_index == 1 and code == "000000":
                opening = np.nan
            rows.append({"date": day, "code": code, "open": opening,
                         "ma_gap_20": factor_values[rank]})
    db_path = tmp_path / "tied.sqlite"
    con = create_factor_store(db_path, ["ma_gap_20"], [])
    store_factor_frame(con, pd.DataFrame(rows), {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)},
                       ["ma_gap_20"], [])
    con.commit()
    con.close()
    _factor_diagnostics(db_path, tmp_path / "evaluation", ["ma_gap_20"], [], [1], 20, 3)
    panel = pd.read_csv(tmp_path / "evaluation" / "factor_panel.csv.gz", dtype={"code": str})
    first = panel[(panel.date == "2024-01-01") & (panel.code == "000000")].iloc[0]
    assert pd.isna(first.forward_1)
    groups = pd.read_csv(tmp_path / "evaluation" / "factor_groups.csv")
    skipped = groups[(groups.date == "2024-01-01") & (groups.group == "跳过")]
    assert not skipped.empty


def test_benchmark_same_returns_have_unit_beta_and_zero_relative_wealth(tmp_path):
    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    curve = pd.DataFrame({"date": dates, "market_value": [100, 101, 99, 102],
                          "return": [0, .01, 99 / 101 - 1, 102 / 99 - 1]})
    csv_path = tmp_path / "benchmark.csv"
    pd.DataFrame({"date": dates, "close": [10, 10.1, 9.9, 10.2]}).to_csv(csv_path, index=False)
    result, aligned = _benchmark_evaluation(curve, {"path": str(csv_path), "name": "test",
                                                    "source": "fixture", "return_type": "total"},
                                            tmp_path / "evaluation", 252, 0.0)
    assert result["status"] == "ok"
    assert np.isclose(result["metrics"]["beta"]["value"], 1.0, atol=1e-10)
    assert np.isclose(result["metrics"]["relative_wealth_return"]["value"], 0.0, atol=1e-10)
    assert (tmp_path / "evaluation" / "benchmark_source.csv").exists()


def test_benchmark_missing_strategy_day_is_not_forward_filled(tmp_path):
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    curve = pd.DataFrame({"date": dates, "market_value": [100, 101, 102], "return": [0, .01, .0099]})
    path = tmp_path / "short_benchmark.csv"
    pd.DataFrame({"date": [dates[0], dates[2]], "close": [10, 10.2]}).to_csv(path, index=False)
    result, _ = _benchmark_evaluation(curve, {"path": str(path), "name": "test", "source": "fixture",
                                              "return_type": "price"}, tmp_path / "evaluation", 252, 0.0)
    assert result["status"] == "unavailable"
    assert "未覆盖策略交易日" in result["reason"]
