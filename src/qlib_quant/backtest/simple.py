from __future__ import annotations

import pandas as pd

from ..portfolio.baseline import select_equal_weight


def run_backtest(frame: pd.DataFrame, initial_cash=1_000_000, max_positions=20,
                 commission_rate=0.0003, stamp_duty_rate=0.001, slippage_bps=5,
                 rebalance_weekday=4, start=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Next-open weekly baseline, delta-rebalanced so holdings are never overwritten.

    Signals are read at day t and orders are filled at day t+1 open, so the same
    day's close cannot leak in. ``start`` is an optional trading start date; rows
    before it are warm-up data used only to populate rolling features, and no
    positions are opened before that date.

    Accounting invariants (fixed vs. the first baseline):

    - A held name that stays in the target is adjusted by share *delta*, never
      re-bought over itself, so its existing value can no longer vanish.
    - A position can never be dropped without crediting cash back.
    - A name with no next-session quote is marked at its last known price
      instead of being silently excluded from equity.
    - Slippage is converted from basis points with /10000 (5 bp = 0.0005).
    """
    df = frame.sort_values(["date", "code"]).copy()
    dates = sorted(df["date"].unique())
    start_ts = pd.Timestamp(start) if start is not None else None
    cash = float(initial_cash)
    holdings: dict[str, float] = {}
    last_price: dict[str, float] = {}
    equity_rows, trade_rows = [], []

    def mv(code: str, shares: float, day: pd.DataFrame, column: str) -> float:
        """Value ``shares`` of ``code`` at ``day``'s ``column``, falling back to the last known price."""
        if code in day.index:
            return shares * float(day.loc[code, column])
        return shares * last_price.get(code, 0.0)

    for i, date in enumerate(dates[:-1]):
        today = df[df["date"] == date]        # keep ``code`` as a column for select_equal_weight
        today_day = today.set_index("code")   # indexed view for lookups
        next_date = dates[i + 1]
        next_day = df[df["date"] == next_date].set_index("code")
        ts, next_ts = pd.Timestamp(date), pd.Timestamp(next_date)

        # Refresh the last known price for every held code from today's close.
        for code in list(holdings):
            if code in today_day.index:
                last_price[code] = float(today_day.loc[code, "close"])

        trading = (start_ts is None or ts >= start_ts)
        if trading and ts.weekday() == rebalance_weekday:
            target = select_equal_weight(today, max_positions)
            wanted = set(target["code"])
            weights = {r.code: float(r.target_weight) for r in target.itertuples()}

            # 1) Sell positions that left the target set. Never drop a position
            #    without crediting cash; if it cannot be priced, keep it.
            for code, shares in list(holdings.items()):
                if code not in wanted:
                    if code in next_day.index:
                        price = float(next_day.loc[code, "open"]) * (1 - slippage_bps / 10000)
                    elif code in last_price:
                        price = last_price[code]
                    else:
                        continue
                    gross = shares * price
                    fee = gross * (commission_rate + stamp_duty_rate)
                    cash += gross - fee
                    trade_rows.append({"date": next_date, "code": code, "side": "sell",
                                       "shares": shares, "price": price, "fee": fee})
                    del holdings[code]
                    last_price.pop(code, None)

            # 2) Rebalance wanted names to their target weights via share delta.
            #    total_equity is recomputed after the sell step above.
            total_equity = cash + sum(mv(c, s, next_day, "open") for c, s in holdings.items())
            for code in wanted:
                if code not in next_day.index and code not in today_day.index:
                    continue
                if code in next_day.index:
                    buy_price = float(next_day.loc[code, "open"]) * (1 + slippage_bps / 10000)
                    sell_price = float(next_day.loc[code, "open"]) * (1 - slippage_bps / 10000)
                else:
                    buy_price = sell_price = last_price.get(code, float(today_day.loc[code, "close"]))
                cur = holdings.get(code, 0.0)
                target_shares = int((total_equity * weights[code]) // buy_price // 100) * 100
                delta = target_shares - cur
                if delta > 0:
                    gross = delta * buy_price
                    fee = gross * commission_rate
                    if gross + fee <= cash:
                        cash -= gross + fee
                        holdings[code] = cur + delta
                        last_price[code] = buy_price
                        trade_rows.append({"date": next_date, "code": code, "side": "buy",
                                           "shares": delta, "price": buy_price, "fee": fee})
                elif delta < 0:
                    sell_shares = -delta
                    gross = sell_shares * sell_price
                    fee = gross * (commission_rate + stamp_duty_rate)
                    cash += gross - fee
                    holdings[code] = cur - sell_shares
                    trade_rows.append({"date": next_date, "code": code, "side": "sell",
                                       "shares": sell_shares, "price": sell_price, "fee": fee})

        if start_ts is None or next_ts >= start_ts:
            market_value = cash + sum(mv(c, s, next_day, "close") for c, s in holdings.items())
            equity_rows.append({"date": next_date, "cash": cash, "market_value": market_value,
                                "positions": len(holdings)})

    equity = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trade_rows)
    if not equity.empty:
        equity["return"] = equity["market_value"].pct_change().fillna(0)
        equity["drawdown"] = equity["market_value"] / equity["market_value"].cummax() - 1
    return equity, trades
