from __future__ import annotations

import pandas as pd

from ..portfolio.baseline import select_equal_weight


def run_backtest(frame: pd.DataFrame, initial_cash=1_000_000, max_positions=20,
                 commission_rate=0.0003, stamp_duty_rate=0.001, slippage_bps=5,
                 rebalance_weekday=4) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simple next-open weekly baseline for research and teaching.

    It is intentionally small and auditable. Signals are read at day t and
    orders are filled at day t+1 open, so the same day's close cannot leak in.
    """
    df = frame.sort_values(["date", "code"]).copy()
    dates = sorted(df["date"].unique())
    cash = float(initial_cash)
    holdings: dict[str, float] = {}
    equity_rows, trade_rows = [], []
    for i, date in enumerate(dates[:-1]):
        today = df[df["date"] == date]
        today_day = today.set_index("code")
        next_date = dates[i + 1]
        next_day = df[df["date"] == next_date].set_index("code")
        if pd.Timestamp(date).weekday() == rebalance_weekday:
            target = select_equal_weight(today, max_positions)
            wanted = set(target["code"])
            for code, shares in list(holdings.items()):
                if code not in wanted:
                    # If a security has no next-session quote, exit at the
                    # last known close rather than crashing or using future data.
                    if code in next_day.index:
                        price = float(next_day.loc[code, "open"])
                    elif code in today_day.index:
                        price = float(today_day.loc[code, "close"])
                    else:
                        del holdings[code]
                        continue
                    gross = shares * price
                    fee = gross * (commission_rate + stamp_duty_rate) + gross * slippage_bps / 100000
                    cash += gross - fee
                    trade_rows.append({"date": next_date, "code": code, "side": "sell", "shares": shares, "price": price, "fee": fee})
                    del holdings[code]
            if not target.empty:
                available = cash + sum(shares * float(next_day.loc[c, "open"]) for c, shares in holdings.items() if c in next_day.index)
                for row in target.itertuples():
                    if row.code not in next_day.index:
                        continue
                    price = float(next_day.loc[row.code, "open"]) * (1 + slippage_bps / 100000)
                    budget = available * float(row.target_weight)
                    shares = int(budget // price // 100) * 100
                    if shares <= 0:
                        continue
                    gross = shares * price
                    fee = gross * commission_rate
                    if gross + fee <= cash:
                        cash -= gross + fee
                        holdings[row.code] = float(shares)
                        trade_rows.append({"date": next_date, "code": row.code, "side": "buy", "shares": shares, "price": price, "fee": fee})
        market_value = cash + sum(shares * float(next_day.loc[c, "close"]) for c, shares in holdings.items() if c in next_day.index)
        equity_rows.append({"date": next_date, "cash": cash, "market_value": market_value, "positions": len(holdings)})
    equity = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trade_rows)
    if not equity.empty:
        equity["return"] = equity["market_value"].pct_change().fillna(0)
        equity["drawdown"] = equity["market_value"] / equity["market_value"].cummax() - 1
    return equity, trades

