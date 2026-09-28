from __future__ import annotations

import json
import math
from typing import Callable

import pandas as pd

from ..portfolio.baseline import select_equal_weight

TRADE_COLUMNS = ["date", "code", "side", "shares", "raw_price", "price", "gross",
                 "commission", "tax", "fee", "slippage_cost", "reason"]
POSITION_COLUMNS = ["date", "code", "shares", "valuation_price", "quote_date", "market_value", "stale_valuation"]
ORDER_COLUMNS = ["date", "code", "side", "reason"]


def run_backtest(frame: pd.DataFrame, initial_cash=1_000_000, max_positions=20,
                 commission_rate=0.0003, stamp_duty_rate=0.001, slippage_bps=5,
                 lot_size=100, rebalance_weekday=4, start_date=None, end_date=None,
                 calendar=None, check_cancel: Callable | None = None,
                 progress: Callable | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """At each open execute the previous session's target, then mark at close.

    Missing/nonfinite/nonpositive opens and explicit suspensions cannot execute.
    Valuation alone may carry the last observed price forward. No warmup trades.
    The optional calendar prevents a stock sample from skipping market sessions.
    """
    required = {"date", "code", "open", "close", "signal", "signal_score"}
    if required - set(frame):
        raise ValueError(f"回测缺少字段: {sorted(required - set(frame))}")
    for name, value in (("initial_cash", initial_cash), ("commission_rate", commission_rate),
                        ("stamp_duty_rate", stamp_duty_rate), ("slippage_bps", slippage_bps)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须是有限的非负数")
    if initial_cash <= 0 or any(isinstance(v, bool) or int(v) != v or v <= 0 for v in (max_positions, lot_size)):
        raise ValueError("初始资金必须为正数；持仓数和每手股数必须为正整数")
    if not 0 <= commission_rate < 1 or not 0 <= stamp_duty_rate < 1 or not 0 <= slippage_bps < 10000:
        raise ValueError("费用率须小于 1，滑点须小于 10000 基点")
    if isinstance(rebalance_weekday, bool) or int(rebalance_weekday) != rebalance_weekday or not 0 <= rebalance_weekday <= 4:
        raise ValueError("调仓日须为周一至周五（0—4）")
    df = frame.copy()
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df["code"] = df["code"].astype(str)
    if df["date"].isna().any() or df.duplicated(["date", "code"]).any():
        raise ValueError("行情日期为空或股票/日期重复")
    if df.empty:
        raise ValueError("回测区间没有行情")
    start = pd.Timestamp(start_date) if start_date else df["date"].min()
    end = pd.Timestamp(end_date) if end_date else df["date"].max()
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    df = df[df["date"].between(start, end)].sort_values(["date", "code"])
    if df.empty:
        raise ValueError("回测区间没有行情")
    dates = sorted(set(pd.to_datetime(calendar))) if calendar is not None else sorted(df["date"].unique())
    dates = [pd.Timestamp(d) for d in dates if start <= pd.Timestamp(d) <= end]
    if not dates:
        raise ValueError("回测区间没有交易日")
    days = {d: g.set_index("code") for d, g in df.groupby("date", sort=False)}
    empty = df.iloc[:0].set_index("code")
    cash = float(initial_cash)
    holdings, last_prices, quote_dates = {}, {}, {}
    pending = None
    equities, trades, positions, skipped = [], [], [], []
    slip = slippage_bps / 10000

    def raw_price(day, code, field):
        if code not in day.index:
            return None
        try:
            value = float(day.at[code, field])
            return value if math.isfinite(value) and value > 0 else None
        except (ValueError, TypeError):
            return None

    def executable(day, code):
        price = raw_price(day, code, "open")
        if price is None:
            return None, "无有效开盘价"
        status = day.at[code, "trade_status"] if "trade_status" in day else None
        if str(status).lower() in {"0", "0.0", "suspended", "停牌", "halted"}:
            return None, "停牌"
        return price, ""

    for index, date in enumerate(dates):
        if check_cancel:
            check_cancel()
        day = days.get(date, empty)
        date_key = date.strftime("%Y-%m-%d")
        day_fees = 0.0

        def fill(code, side, shares):
            nonlocal cash, day_fees
            raw, why = executable(day, code)
            if raw is None:
                skipped.append(dict(date=date_key, code=code, side=side, reason=why))
                return
            price = raw * (1 + slip if side == "buy" else 1 - slip)
            if side == "buy":
                affordable = int(cash / (price * (1 + commission_rate)) / lot_size) * lot_size
                shares = min(shares, affordable)
            if shares <= 0:
                skipped.append(dict(date=date_key, code=code, side=side, reason="资金不足一手"))
                return
            gross = shares * price
            commission = gross * commission_rate
            tax = gross * stamp_duty_rate if side == "sell" else 0.0
            fee = commission + tax
            cash += gross - fee if side == "sell" else -gross - fee
            holdings[code] = holdings.get(code, 0) + (shares if side == "buy" else -shares)
            if not holdings[code]:
                del holdings[code]
            if code not in last_prices:
                last_prices[code], quote_dates[code] = raw, date_key
            day_fees += fee
            trades.append(dict(date=date_key, code=code, side=side, shares=shares, raw_price=raw,
                               price=price, gross=gross, commission=commission, tax=tax, fee=fee,
                               slippage_cost=abs(price - raw) * shares, reason="target_delta"))

        if pending is not None:
            # Freeze target budgets against pre-trade open equity; mark-only
            # fallback values are never passed to fill().
            open_equity = cash + sum(shares * (raw_price(day, code, "open") or last_prices[code])
                                     for code, shares in holdings.items())
            desired = {}
            for row in pending.itertuples():
                raw, why = executable(day, row.code)
                if raw is None:
                    skipped.append(dict(date=date_key, code=row.code, side="rebalance", reason=why))
                    continue
                desired[row.code] = int(open_equity * row.target_weight / raw / lot_size) * lot_size
            wanted = set(pending["code"])
            for code, shares in list(holdings.items()):
                target = 0 if code not in wanted else desired.get(code, shares)
                if shares > target:
                    fill(code, "sell", shares - target)
            for code, target in desired.items():
                delta = target - holdings.get(code, 0)
                if delta > 0:
                    fill(code, "buy", delta)

        held_value, any_stale = 0.0, False
        for code, shares in holdings.items():
            close = raw_price(day, code, "close")
            stale = close is None
            if close is not None:
                last_prices[code], quote_dates[code] = close, date_key
            value = shares * last_prices[code]
            held_value += value
            any_stale |= stale
            positions.append(dict(date=date_key, code=code, shares=shares,
                                  valuation_price=last_prices[code], quote_date=quote_dates[code],
                                  market_value=value, stale_valuation=stale))
        if cash < -1e-7:
            raise AssertionError("回测现金为负，账本核对失败")
        equities.append(dict(date=date_key, cash=cash, holdings_value=held_value, market_value=cash + held_value,
                             positions=len(holdings), stale_valuation=any_stale, total_fees=day_fees,
                             holdings=json.dumps(holdings, sort_keys=True, ensure_ascii=False)))
        pending = select_equal_weight(day.reset_index(), max_positions) if date.weekday() == rebalance_weekday else None
        if progress and (index % 20 == 0 or index == len(dates) - 1):
            progress(index + 1, len(dates))

    equity = pd.DataFrame(equities)
    equity["return"] = equity["market_value"].pct_change().fillna(0)
    equity["drawdown"] = equity["market_value"] / equity["market_value"].cummax() - 1
    equity.attrs["positions"] = pd.DataFrame(positions, columns=POSITION_COLUMNS)
    equity.attrs["skipped_orders"] = pd.DataFrame(skipped, columns=ORDER_COLUMNS)
    return equity, pd.DataFrame(trades, columns=TRADE_COLUMNS)
