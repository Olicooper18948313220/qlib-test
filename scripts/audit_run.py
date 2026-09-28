"""Independently reconcile the saved cash, shares, fees and daily valuations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qlib_quant.experiments import atomic_json, read_json


def audit(directory):
    directory = Path(directory)
    metadata = read_json(directory / "run_metadata.json")
    params = metadata["effective_config"]["backtest"]
    equity = pd.read_csv(directory / "equity_curve.csv")
    trades = pd.read_csv(directory / "trades.csv", dtype={"code": str})
    positions = pd.read_csv(directory / "positions.csv", dtype={"code": str})
    cash, holdings, largest = float(params["initial_cash"]), {}, 0.
    dates = set(equity.date)
    assert set(trades.date) <= dates, "交易日期在权益区间外"
    assert not positions.duplicated(["date", "code"]).any(), "重复持仓"
    assert equity.iloc[0].market_value == cash, "初始资金不一致"
    for day in equity.itertuples():
        fee_sum = 0.
        for trade in trades[trades.date == day.date].itertuples():
            sign = 1 if trade.side == "buy" else -1
            expected_price = trade.raw_price * (1 + sign * params["slippage_bps"] / 10000)
            expected_fee = trade.shares * expected_price * (
                params["commission_rate"] + (params["stamp_duty_rate"] if trade.side == "sell" else 0))
            assert abs(expected_price - trade.price) < 1e-7
            assert abs(expected_fee - trade.fee) < 1e-7
            cash -= sign * trade.shares * expected_price + expected_fee
            fee_sum += expected_fee
            holdings[trade.code] = holdings.get(trade.code, 0) + sign * trade.shares
            if holdings[trade.code] == 0:
                del holdings[trade.code]
            assert all(shares >= 0 for shares in holdings.values())
        day_positions = positions[positions.date == day.date]
        assert dict(zip(day_positions.code, day_positions.shares)) == holdings
        assert json.loads(day.holdings) == holdings
        marked = sum(row.shares * row.valuation_price for row in day_positions.itertuples())
        errors = [abs(day.cash - cash), abs(day.holdings_value - marked),
                  abs(day.market_value - cash - marked), abs(day.total_fees - fee_sum)]
        largest = max(largest, *errors)
        assert max(errors) < 1e-6, f"{day.date} 账本不平: {errors}"
        assert cash >= -1e-6
    summary = read_json(directory / "summary.json")
    assert abs(summary["cumulative_return"] - (equity.iloc[-1].market_value / params["initial_cash"] - 1)) < 1e-10
    return {"run": str(directory), "passed": True, "days": len(equity), "trades": len(trades),
            "initial_cash": params["initial_cash"], "max_positions": params["max_positions"],
            "largest_accounting_error": largest, "code_digest": metadata["code_digest"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = {"audits": [audit(p) for p in args.directories]}
    if args.output:
        atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
