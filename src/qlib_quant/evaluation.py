from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd

from .experiments import atomic_json


EVALUATION_VERSION = "1.0"
BASE_FACTORS = ("ma_gap_20", "return_20", "volume_ratio_1", "signal_score")
BINARY_FACTORS = {"close_above_high_20", "close_above_low_10"}


def supported_factors(windows=(5, 10, 20, 30, 60, 120, 250)) -> list[str]:
    names = set(BASE_FACTORS) | {"amount_ratio_1", *BINARY_FACTORS}
    for window in set(windows) | {10, 20}:
        names.update({f"ma_{window}", f"high_max_{window}", f"low_min_{window}", f"return_{window}"})
    from .signals.zff0708 import diagnostic_factor_names
    names.update(name for name in diagnostic_factor_names() if not name.startswith("signal_condition_"))
    return sorted(names)


def factor_names(config: dict, enabled_signals: list[str]) -> list[str]:
    return list(dict.fromkeys(config.get("factors", BASE_FACTORS)))


def panel_factor_names(config: dict, enabled_signals: list[str]) -> list[str]:
    names = factor_names(config, enabled_signals)
    if any(name.startswith("0708cao_") for name in enabled_signals):
        from .signals.zff0708 import component_factor_names, diagnostic_factor_names
        names = list(dict.fromkeys([*names, *diagnostic_factor_names(), *component_factor_names]))
    return names


def create_factor_store(path: str | Path, factors: list[str], signals: list[str]) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    cols = [f'"f_{name}" REAL' for name in factors]
    cols += [f'"s_{name}" INTEGER' for name in signals]
    con.execute("DROP TABLE IF EXISTS panel")
    con.execute("CREATE TABLE panel (day_index INTEGER NOT NULL, date TEXT NOT NULL, code TEXT NOT NULL, open REAL, "
                + ", ".join(cols) + ", PRIMARY KEY(day_index, code)) WITHOUT ROWID")
    con.execute("CREATE INDEX panel_code_day ON panel(code, day_index)")
    con.execute("CREATE INDEX panel_date_code ON panel(date, code)")
    con.commit()
    return con


def store_factor_frame(con: sqlite3.Connection, frame: pd.DataFrame, calendar_index: dict[str, int],
                       factors: list[str], signals: list[str]) -> None:
    if frame.empty:
        return
    selected = frame.loc[:, ["date", "code", "open", *factors, *signals]].copy()
    selected["date"] = pd.to_datetime(selected["date"]).dt.strftime("%Y-%m-%d")
    selected["day_index"] = selected["date"].map(calendar_index)
    selected = selected.dropna(subset=["day_index"])
    selected["day_index"] = selected["day_index"].astype(int)
    names = ["day_index", "date", "code", "open", *[f"f_{n}" for n in factors], *[f"s_{n}" for n in signals]]
    values = selected[["day_index", "date", "code", "open", *factors, *signals]].itertuples(index=False, name=None)
    placeholders = ",".join("?" for _ in names)
    con.executemany(f"INSERT INTO panel ({','.join(names)}) VALUES ({placeholders})", values)


def _metric(value, unit="", definition="", n=None, reason=None):
    if value is None or not np.isfinite(value):
        return {"value": None, "unit": unit, "definition": definition, "n": n,
                "unavailable_reason": reason or "有效样本不足或分母为零"}
    return {"value": value if isinstance(value, (bool, np.bool_)) else float(value),
            "unit": unit, "definition": definition, "n": n,
            "unavailable_reason": None}


def _returns_by_period(equity: pd.DataFrame, initial_value: float) -> pd.DataFrame:
    curve = equity[["date", "market_value"]].copy()
    # The backtest attaches positions/skipped-orders DataFrames to equity.attrs.
    # pandas groupby compares attrs on its grouper and frame; DataFrame-valued
    # attrs make that comparison ambiguous. Evaluation operates on plain columns.
    curve.attrs = {}
    curve["date"] = pd.to_datetime(curve["date"])
    curve = curve.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    prior = curve["market_value"].shift(1)
    prior.iloc[0] = initial_value
    curve["return"] = curve["market_value"] / prior - 1
    return curve


def _drawdowns(curve: pd.DataFrame) -> pd.DataFrame:
    nav = curve["market_value"].astype(float).to_numpy()
    dates = pd.to_datetime(curve["date"]).to_numpy()
    peak = -np.inf
    peak_i = 0
    in_dd = False
    trough_i = 0
    trough = 0.0
    events = []
    for i, value in enumerate(nav):
        if value >= peak:
            if in_dd:
                events.append((peak_i, trough_i, i, trough))
                in_dd = False
            peak, peak_i = value, i
        dd = value / peak - 1 if peak else 0.0
        if dd < 0:
            if not in_dd:
                in_dd, trough_i, trough = True, i, dd
            elif dd < trough:
                trough_i, trough = i, dd
    if in_dd:
        events.append((peak_i, trough_i, None, trough))
    return pd.DataFrame([
        {"peak_date": pd.Timestamp(dates[p]).strftime("%Y-%m-%d"),
         "trough_date": pd.Timestamp(dates[t]).strftime("%Y-%m-%d"),
         "recovery_date": pd.Timestamp(dates[r]).strftime("%Y-%m-%d") if r is not None else None,
         "drawdown": float(d), "duration_trading_days": int((r if r is not None else len(dates) - 1) - p),
         "recovered": r is not None}
        for p, t, r, d in events
    ], columns=["peak_date", "trough_date", "recovery_date", "drawdown", "duration_trading_days", "recovered"])


def account_evaluation(equity: pd.DataFrame, trades: pd.DataFrame, positions: pd.DataFrame,
                       initial_cash: float, annualization_days=252, risk_free_rate=0.0,
                       sortino_target_return=0.0) -> tuple[dict, dict[str, pd.DataFrame]]:
    if equity.empty:
        return {"status": "unavailable", "reason": "回测没有净值记录", "metrics": {}}, {}
    curve = _returns_by_period(equity, initial_cash)
    returns = curve["return"].iloc[1:].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    nav = curve["market_value"].astype(float)
    total = float(nav.iloc[-1] / initial_cash - 1)
    elapsed_days = max((curve["date"].iloc[-1] - curve["date"].iloc[0]).days, 0)
    cagr = (float(nav.iloc[-1] / initial_cash) ** (365.25 / elapsed_days) - 1
            if elapsed_days > 0 and nav.iloc[-1] > 0 else None)
    peak = nav.cummax()
    dd = nav / peak - 1
    max_dd = float(dd.min())
    vol = float(returns.std(ddof=1) * math.sqrt(annualization_days)) if len(returns) >= 2 else None
    daily_rf = (1 + risk_free_rate) ** (1 / annualization_days) - 1
    excess = returns - daily_rf
    sharpe = (float(excess.mean() / excess.std(ddof=1) * math.sqrt(annualization_days))
              if len(excess) >= 2 and excess.std(ddof=1) > 0 else None)
    daily_target = (1 + sortino_target_return) ** (1 / annualization_days) - 1
    downside = np.minimum(returns.to_numpy() - daily_target, 0.0)
    downside_dev = float(np.sqrt(np.mean(downside ** 2))) if len(downside) else 0.0
    sortino = (float((returns.mean() - daily_target) / downside_dev * math.sqrt(annualization_days))
               if len(returns) and downside_dev > 0 else None)
    calmar = cagr / abs(max_dd) if cagr is not None and max_dd < 0 else None
    tail = returns.to_numpy()
    tail_var = tail_es = None
    if len(tail) >= 100:
        q05 = float(np.quantile(tail, 0.05))
        tail_var = max(0.0, -q05)
        tail_es = max(0.0, -float(tail[tail <= q05].mean())) if np.any(tail <= q05) else 0.0

    def grouped_returns(freq: str, display: str) -> pd.DataFrame:
        with_period = curve.assign(_period=curve["date"].dt.to_period(freq))
        grouped = with_period.groupby("_period", sort=True)["return"].apply(
            lambda values: float(np.prod(1 + values) - 1))
        return pd.DataFrame({"period": [p.strftime(display) for p in grouped.index], "return": grouped.to_numpy()})

    monthly_table = grouped_returns("M", "%Y-%m")
    yearly_table = grouped_returns("Y", "%Y")
    weekly_table = grouped_returns("W-FRI", "%Y-%m-%d")
    rolling = (nav / nav.shift(annualization_days) - 1).rename("rolling_return").reset_index(drop=True)
    rolling_table = pd.DataFrame({"date": curve["date"], "rolling_return_252": rolling})
    underwater = dd < 0
    longest_water = 0
    current_water = 0
    for value in underwater:
        current_water = current_water + 1 if value else 0
        longest_water = max(longest_water, current_water)
    drawdowns = _drawdowns(curve)
    underwater_rmse = float(np.sqrt(np.mean(dd.to_numpy() ** 2)))
    trades = trades.copy()
    if not trades.empty:
        trades["date"] = pd.to_datetime(trades["date"])
    gross = (trades.assign(notional=trades["gross"].astype(float)).groupby("date")["notional"].sum()
             if not trades.empty else pd.Series(dtype=float))
    nav_by_date = curve.set_index("date")["market_value"]
    denom = nav_by_date.shift(1)
    if len(denom):
        denom.iloc[0] = initial_cash
    turnover = pd.DataFrame({"date": nav_by_date.index,
                             "turnover": gross.reindex(nav_by_date.index, fill_value=0.0) / denom})
    position_count = equity["positions"].astype(float)
    total_exposure = (equity["holdings_value"].astype(float) / equity["market_value"].astype(float)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    max_weight = 0.0
    if not positions.empty:
        pos = positions.copy()
        pos["date"] = pd.to_datetime(pos["date"])
        nav_map = curve.set_index("date")["market_value"]
        weights = pos["market_value"].astype(float) / pos["date"].map(nav_map)
        max_weight = float(weights.max()) if len(weights) else 0.0
    active_signal = (equity["positions"].astype(float) > 0).mean()
    fees = float(trades["fee"].sum()) if "fee" in trades else 0.0
    commissions = float(trades["commission"].sum()) if "commission" in trades else 0.0
    taxes = float(trades["tax"].sum()) if "tax" in trades else 0.0
    slippage = float(trades["slippage_cost"].sum()) if "slippage_cost" in trades else 0.0
    metrics = {
        "cumulative_return": _metric(total, "%", "期末净值 / 初始资金 - 1", len(curve)),
        "cagr": _metric(cagr, "%/年", "按实际日历跨度复合年化；区间不足一年时属于年化外推", len(curve)),
        "annualized_volatility": _metric(vol, "%/年", "日收益样本标准差 × sqrt(年化交易日数)", len(returns)),
        "sharpe": _metric(sharpe, "比率", "日收益减无风险日收益后的均值 / 样本标准差 × sqrt(年化交易日数)", len(returns)),
        "sortino": _metric(sortino, "比率", "超额目标收益均值 / 全样本下行偏差 × sqrt(年化交易日数)", len(returns)),
        "calmar": _metric(calmar, "比率", "复合年化收益 / 最大回撤幅度", len(curve)),
        "max_drawdown": _metric(max_dd, "%", "净值 / 历史峰值 - 1，负值表示回撤", len(curve)),
        "longest_underwater_days": _metric(longest_water, "交易日", "连续低于历史净值高点的最长交易日数", len(curve)),
        "ulcer_index": _metric(underwater_rmse, "%", "每日回撤幅度的均方根", len(curve)),
        "historical_var_95": _metric(tail_var, "%/日", "历史日收益最差5%分位对应的损失幅度；少于100期不计算", len(returns), "日收益样本少于100期" if len(returns) < 100 else None),
        "historical_es_95": _metric(tail_es, "%/日", "历史日收益最差5%尾部的平均损失幅度；少于100期不计算", len(returns), "日收益样本少于100期" if len(returns) < 100 else None),
        "worst_day": _metric(float(returns.min()) if len(returns) else None, "%", "单日最差收益", len(returns)),
        "worst_week": _metric(float(weekly_table["return"].min()) if len(weekly_table) else None, "%", "按周复利汇总的最差自然周收益", len(weekly_table)),
        "worst_month": _metric(float(monthly_table["return"].min()) if len(monthly_table) else None, "%", "按月复利汇总的最差自然月收益", len(monthly_table)),
        "positive_month_ratio": _metric(float((monthly_table["return"] > 0).mean()) if len(monthly_table) else None, "%", "正收益月数 / 有效自然月数", len(monthly_table)),
        "average_positions": _metric(float(position_count.mean()), "只", "每日持仓股票数平均值", len(position_count)),
        "maximum_positions": _metric(float(position_count.max()), "只", "每日持仓股票数最大值", len(position_count)),
        "average_gross_exposure": _metric(float(total_exposure.mean()), "%", "持仓市值 / 总资产的日均值", len(total_exposure)),
        "maximum_gross_exposure": _metric(float(total_exposure.max()), "%", "持仓市值 / 总资产的最大值", len(total_exposure)),
        "cash_days_ratio": _metric(float((position_count == 0).mean()), "%", "完全空仓交易日占比", len(position_count)),
        "max_single_position_weight": _metric(max_weight, "%", "单只股票市值 / 当日总资产的最大值", len(positions)),
        "average_daily_turnover": _metric(float(turnover["turnover"].mean()) if not turnover.empty else 0.0, "%/日", "双边买卖成交额 / 前一交易日总资产", len(turnover)),
        "total_fees": _metric(fees, "货币", "已扣佣金与印花税合计", len(trades)),
        "total_commission": _metric(commissions, "货币", "已扣佣金合计", len(trades)),
        "total_stamp_duty": _metric(taxes, "货币", "已扣印花税合计", len(trades)),
        "estimated_slippage_cost": _metric(slippage, "货币", "成交价相对原始开盘价产生的估计摩擦；已反映在成交价格中，不重复扣款", len(trades)),
    }
    enough_target_history = len(curve) >= 2
    metrics["target_cagr_15pct_met"] = _metric(
        bool(cagr >= .15) if enough_target_history and cagr is not None else None,
        "布尔", "历史样本复合年化收益是否达到15%", len(curve),
        "至少需要两个净值日期" if not enough_target_history else None)
    metrics["target_drawdown_15pct_met"] = _metric(
        bool(max_dd > -.15) if enough_target_history else None,
        "布尔", "历史样本最大回撤幅度是否小于15%", len(curve),
        "至少需要两个净值日期" if not enough_target_history else None)
    summary = {"status": "ok", "annualization_days": annualization_days, "risk_free_rate": risk_free_rate,
               "sortino_target_return": sortino_target_return, "metrics": metrics,
               "annualization_warning": "样本不足一年时年化指标为外推值" if elapsed_days < 365 else None,
               "tail_sample_warning": "日收益样本少于100期，VaR/ES 不计算" if len(returns) < 100 else None}
    return summary, {"monthly_returns": monthly_table, "annual_returns": yearly_table,
                     "weekly_returns": weekly_table,
                     "rolling_returns": rolling_table, "drawdowns": drawdowns,
                     "daily_turnover": turnover}


def _trade_analysis(trades: pd.DataFrame, positions: pd.DataFrame, equity: pd.DataFrame,
                    initial_cash: float) -> tuple[dict, dict[str, pd.DataFrame]]:
    columns = ["code", "buy_date", "sell_date", "shares", "buy_price", "sell_price",
               "buy_cost", "sell_proceeds_net", "realized_net_pnl", "holding_days"]
    if trades.empty:
        pairs = pd.DataFrame(columns=columns)
        cycles = pd.DataFrame(columns=["code", "start_date", "end_date", "net_pnl", "cost_basis", "net_return", "holding_days"])
        open_rows = pd.DataFrame(columns=["code", "shares", "remaining_cost", "market_value", "unrealized_pnl", "valuation_date"])
    else:
        lots: dict[str, deque] = defaultdict(deque)
        active: dict[str, dict] = {}
        pair_rows, cycle_rows = [], []
        ordered = trades.copy()
        ordered["date"] = pd.to_datetime(ordered["date"])
        ordered = ordered.sort_values(["date"], kind="stable")
        for trade in ordered.to_dict("records"):
            code, shares = str(trade["code"]), int(trade["shares"])
            date = pd.Timestamp(trade["date"])
            gross = float(trade["gross"])
            fee = float(trade.get("fee", 0.0) or 0.0)
            if trade["side"] == "buy":
                if not lots[code]:
                    active[code] = {"code": code, "start_date": date, "end_date": None, "net_pnl": 0.0,
                                    "cost_basis": 0.0, "last_date": date}
                lot = {"date": date, "shares": shares, "unit_cost": (gross + fee) / shares,
                       "remaining_fee": fee, "unit_price": gross / shares}
                lots[code].append(lot)
                active[code]["cost_basis"] += gross + fee
                active[code]["last_date"] = date
            elif trade["side"] == "sell":
                qty_left = shares
                if sum(item["shares"] for item in lots[code]) < qty_left:
                    raise ValueError(f"FIFO 配对发现卖出超过可用持仓: {code} {date.date()}")
                net_proceeds = gross - fee
                while qty_left:
                    lot = lots[code][0]
                    qty = min(qty_left, lot["shares"])
                    buy_cost = lot["unit_cost"] * qty
                    sell_net = net_proceeds * qty / shares
                    pnl = sell_net - buy_cost
                    pair_rows.append({"code": code, "buy_date": lot["date"].strftime("%Y-%m-%d"),
                                      "sell_date": date.strftime("%Y-%m-%d"), "shares": qty,
                                      "buy_price": lot["unit_price"], "sell_price": gross / shares,
                                      "buy_cost": buy_cost, "sell_proceeds_net": sell_net,
                                      "realized_net_pnl": pnl,
                                      "holding_days": (date - lot["date"]).days})
                    active[code]["net_pnl"] += pnl
                    active[code]["last_date"] = date
                    lot["shares"] -= qty
                    qty_left -= qty
                    if lot["shares"] == 0:
                        lots[code].popleft()
                if not lots[code]:
                    episode = active.pop(code)
                    episode["end_date"] = date
                    cycle_rows.append({"code": code, "start_date": episode["start_date"].strftime("%Y-%m-%d"),
                                       "end_date": date.strftime("%Y-%m-%d"), "net_pnl": episode["net_pnl"],
                                       "cost_basis": episode["cost_basis"],
                                       "net_return": episode["net_pnl"] / episode["cost_basis"] if episode["cost_basis"] else None,
                                       "holding_days": (date - episode["start_date"]).days})
        pairs = pd.DataFrame(pair_rows, columns=columns)
        cycles = pd.DataFrame(cycle_rows, columns=["code", "start_date", "end_date", "net_pnl", "cost_basis", "net_return", "holding_days"])
        last_positions = positions.sort_values("date").groupby("code", sort=False).tail(1) if not positions.empty else positions
        position_map = {str(r.code): r for r in last_positions.itertuples()} if not last_positions.empty else {}
        open_data = []
        for code, queue in lots.items():
            remaining = sum(lot["shares"] for lot in queue)
            cost = sum(lot["unit_cost"] * lot["shares"] for lot in queue)
            row = position_map.get(code)
            mark = float(row.valuation_price) * remaining if row is not None else 0.0
            open_data.append({"code": code, "shares": remaining, "remaining_cost": cost,
                              "market_value": mark, "unrealized_pnl": mark - cost,
                              "valuation_date": str(row.date)[:10] if row is not None else None})
        open_rows = pd.DataFrame(open_data, columns=["code", "shares", "remaining_cost", "market_value", "unrealized_pnl", "valuation_date"])
    initial = float(initial_cash)
    ending = float(equity["market_value"].iloc[-1]) if not equity.empty else initial
    realized = float(pairs["realized_net_pnl"].sum()) if not pairs.empty else 0.0
    unrealized = float(open_rows["unrealized_pnl"].sum()) if not open_rows.empty else 0.0
    wins = cycles[cycles["net_pnl"] > 0] if not cycles.empty else cycles
    losses = cycles[cycles["net_pnl"] < 0] if not cycles.empty else cycles
    gross_win = float(wins["net_pnl"].sum()) if not wins.empty else 0.0
    gross_loss = abs(float(losses["net_pnl"].sum())) if not losses.empty else 0.0
    pf = gross_win / gross_loss if gross_loss else (None if gross_win == 0 else None)
    avg_win = float(wins["net_pnl"].mean()) if not wins.empty else None
    avg_loss = float(losses["net_pnl"].mean()) if not losses.empty else None
    metrics = {
        "fill_count": _metric(len(trades), "笔", "差额成交行数，不等于完整交易周期", len(trades)),
        "closed_cycles": _metric(len(cycles), "笔", "股票持仓从零到重新归零的完整周期数", len(cycles)),
        "realized_win_rate": _metric(float((cycles["net_pnl"] > 0).mean()) if len(cycles) else None, "%", "完整持仓周期净盈利比例；未平仓周期排除", len(cycles)),
        "profit_factor": _metric(pf, "比率", "完整周期净盈利总额 / 完整周期净亏损总额绝对值", len(cycles), "没有亏损周期或没有完整周期" if gross_loss == 0 else None),
        "payoff_ratio": _metric(abs(avg_win / avg_loss) if avg_win is not None and avg_loss is not None and avg_loss else None, "比率", "平均盈利周期净收益 / 平均亏损周期净收益绝对值", len(cycles)),
        "expectancy_per_cycle": _metric(float(cycles["net_pnl"].mean()) if len(cycles) else None, "货币", "完整持仓周期平均净盈亏", len(cycles)),
        "average_holding_days": _metric(float(cycles["holding_days"].mean()) if len(cycles) else None, "日", "完整周期起止日期的自然日间隔", len(cycles)),
        "realized_net_pnl": _metric(realized, "货币", "FIFO 已卖出数量的净盈亏，含分摊佣金税费", len(pairs)),
        "unrealized_net_pnl": _metric(unrealized, "货币", "期末未平仓数量按最后估值价减剩余成本", len(open_rows)),
        "accounting_reconciliation_error": _metric(realized + unrealized - (ending - initial), "货币", "已实现净盈亏 + 未实现净盈亏 - (期末资产 - 初始资金)，应接近0", len(trades)),
        "max_consecutive_losing_cycles": _metric(_max_losing_streak(cycles), "笔", "完整周期净亏损的最大连续次数", len(cycles)),
    }
    return {"status": "ok", "metrics": metrics}, {"trade_pairs": pairs, "trade_cycles": cycles, "open_positions": open_rows}


def _max_losing_streak(cycles: pd.DataFrame) -> int:
    if cycles.empty:
        return 0
    best = current = 0
    for pnl in cycles["net_pnl"]:
        current = current + 1 if pnl < 0 else 0
        best = max(best, current)
    return best


def _factor_query(con: sqlite3.Connection, factors: list[str], signals: list[str], horizons: list[int]):
    select = ["p.date", "p.code", "p.open"]
    select += [f'p."f_{f}" AS "{f}"' for f in factors]
    select += [f'p."s_{s}" AS "{s}"' for s in signals]
    joins = []
    for h in horizons:
        joins += [f'LEFT JOIN panel s{h} ON s{h}.code=p.code AND s{h}.day_index=p.day_index+1',
                  f'LEFT JOIN panel e{h} ON e{h}.code=p.code AND e{h}.day_index=p.day_index+{h + 1}']
        select.append(f'CASE WHEN s{h}.open>0 AND e{h}.open>0 THEN e{h}.open/s{h}.open-1 END AS "forward_{h}"')
    return "SELECT " + ",".join(select) + " FROM panel p " + " ".join(joins)


def _factor_diagnostics(db_path: str | Path, out: Path, factors: list[str], signals: list[str],
                        horizons: list[int], min_cross_section: int, min_per_quantile: int,
                        check_cancel=None, progress=None, panel_factors: list[str] | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM panel ORDER BY date")]
        query = _factor_query(con, factors, signals, horizons)
        panel_query = _factor_query(con, panel_factors or factors, signals, horizons)
        panel_path = out / "factor_panel.csv.gz"
        tmp_panel = panel_path.with_suffix(panel_path.suffix + ".tmp")
        header_written = False
        with tmp_panel.open("wb") as raw, __import__("gzip").GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            for day_index, date in enumerate(dates):
                if check_cancel:
                    check_cancel()
                day = pd.read_sql_query(panel_query + " WHERE p.date=? ORDER BY p.code", con, params=(date,))
                if day.empty:
                    continue
                day.to_csv(gz, index=False, header=not header_written)
                header_written = True
        tmp_panel.replace(panel_path)

        daily_rows, group_rows, summaries, structured_summaries = [], [], [], []
        factor_metrics = list(dict.fromkeys([*factors, *signals]))
        for horizon_index, h in enumerate(horizons):
            factor_ic: dict[str, list[float]] = defaultdict(list)
            factor_rank: dict[str, list[float]] = defaultdict(list)
            previous_ranks: dict[str, pd.Series] = {}
            previous_top: dict[str, set[str]] = {}
            for index, date in enumerate(dates):
                if check_cancel:
                    check_cancel()
                day = pd.read_sql_query(query + " WHERE p.date=? ORDER BY p.code", con, params=(date,))
                label = f"forward_{h}"
                returns = pd.to_numeric(day[label], errors="coerce")
                total_n = int(len(day))
                for factor in factor_metrics:
                    values = pd.to_numeric(day[factor], errors="coerce")
                    factor_available = values.notna()
                    valid = values.notna() & returns.notna()
                    x, y = values[valid], returns[valid]
                    pearson = float(x.corr(y, method="pearson")) if len(x) >= min_cross_section and x.nunique() > 1 else None
                    rank_ic = float(x.corr(y, method="spearman")) if len(x) >= min_cross_section and x.nunique() > 1 and y.nunique() > 1 else None
                    if pearson is not None and np.isfinite(pearson):
                        factor_ic[factor].append(pearson)
                    else:
                        pearson = None
                    if rank_ic is not None and np.isfinite(rank_ic):
                        factor_rank[factor].append(rank_ic)
                    else:
                        rank_ic = None
                    available_codes = day.loc[factor_available, "code"].astype(str)
                    available_values = values[factor_available]
                    ranks = pd.Series(available_values.to_numpy(), index=available_codes.to_numpy()).rank(method="average")
                    shared = ranks.index.intersection(previous_ranks.get(factor, pd.Series(dtype=float)).index)
                    autocorr = None
                    if len(shared) >= min_cross_section:
                        autocorr = float(ranks.loc[shared].corr(previous_ranks[factor].loc[shared], method="spearman"))
                        if not np.isfinite(autocorr):
                            autocorr = None
                    top_turnover = None
                    top_set: set[str] = set()
                    if len(available_values) and available_values.nunique() > 2:
                        n_top = max(1, int(math.ceil(len(available_values) * .2)))
                        top_set = set(available_codes.loc[available_values.nlargest(n_top).index].tolist())
                    if factor in previous_top and previous_top[factor] and top_set:
                        top_turnover = 1 - len(top_set & previous_top[factor]) / max(len(top_set), 1)
                    previous_top[factor] = top_set
                    previous_ranks[factor] = ranks
                    eligible = int(valid.sum()) >= min_cross_section
                    skip_reason = None if eligible else f"有效横截面不足{min_cross_section}只"
                    if not eligible:
                        pearson = rank_ic = None
                    daily_record = {"date": date, "horizon": h, "factor": factor, "n": int(valid.sum()),
                                    "coverage": float(factor_available.sum() / total_n) if total_n else 0.0,
                                    "label_coverage": float(valid.sum() / factor_available.sum()) if factor_available.any() else 0.0,
                                    "ic": pearson, "rank_ic": rank_ic,
                                    "rank_autocorrelation": autocorr,
                                    "top_quintile_turnover": top_turnover,
                                    "skip_reason": skip_reason}
                    daily_rows.append(daily_record)
                    if not eligible:
                        continue
                    if factor == "signal_score":
                        for score, members in day.loc[valid].groupby(factor, sort=True):
                            if len(members) < min_per_quantile:
                                continue
                            group_y = returns.loc[members.index]
                            group_rows.append({"date": date, "horizon": h, "factor": factor,
                                               "group": f"score={score:g}", "n": int(len(members)),
                                               "mean_forward_return": float(group_y.mean()),
                                               "positive_forward_ratio": float((group_y > 0).mean())})
                    elif factor in signals or factor in BINARY_FACTORS:
                        yes = (values > 0) & returns.notna()
                        no = (values <= 0) & returns.notna()
                        hit_rate = float((returns[yes] > 0).mean()) if yes.any() else None
                        group_rows.append({"date": date, "horizon": h, "factor": factor, "group": "命中/正分",
                                           "n": int(yes.sum()), "mean_forward_return": float(returns[yes].mean()) if yes.any() else None,
                                           "positive_forward_ratio": hit_rate})
                        group_rows.append({"date": date, "horizon": h, "factor": factor, "group": "未命中/零分",
                                           "n": int(no.sum()), "mean_forward_return": float(returns[no].mean()) if no.any() else None,
                                           "positive_forward_ratio": float((returns[no] > 0).mean()) if no.any() else None})
                    else:
                        if x.nunique() < 5:
                            daily_record["skip_reason"] = "连续因子有效值少于5种，不能形成五组"
                            group_rows.append({"date": date, "horizon": h, "factor": factor, "group": "跳过",
                                               "n": int(len(x)), "mean_forward_return": None,
                                               "positive_forward_ratio": None,
                                               "skip_reason": daily_record["skip_reason"]})
                        else:
                            bins = pd.qcut(x.rank(method="average"), q=5, labels=False, duplicates="drop")
                            members_by_bin = [(bin_id, bins.index[bins == bin_id]) for bin_id in sorted(bins.dropna().unique())]
                            if len(members_by_bin) != 5 or any(len(members) < min_per_quantile for _, members in members_by_bin):
                                daily_record["skip_reason"] = "并列值导致分组数不足5组或组内样本少于要求"
                                group_rows.append({"date": date, "horizon": h, "factor": factor, "group": "跳过",
                                                   "n": int(len(x)), "mean_forward_return": None,
                                                   "positive_forward_ratio": None,
                                                   "skip_reason": daily_record["skip_reason"]})
                            else:
                                for bin_id, members in members_by_bin:
                                    group_rows.append({"date": date, "horizon": h, "factor": factor,
                                                       "group": f"Q{int(bin_id) + 1}", "n": len(members),
                                                       "mean_forward_return": float(y.loc[members].mean()),
                                                       "positive_forward_ratio": float((y.loc[members] > 0).mean()),
                                                       "skip_reason": None})
                if index == 0 or index == len(dates) - 1 or index % 100 == 0:
                    if progress:
                        done = horizon_index * max(len(dates), 1) + index + 1
                        total = max(len(horizons) * len(dates), 1)
                        progress(96 + int(2 * done / total), f"因子诊断 {h} 日期限：{index + 1}/{len(dates)} 日")
            for factor in factor_metrics:
                vals = np.asarray(factor_ic[factor], dtype=float)
                ranks = np.asarray(factor_rank[factor], dtype=float)
                valid_rows = [r for r in daily_rows if r["factor"] == factor and r["horizon"] == h]
                autocorr_values = [r["rank_autocorrelation"] for r in valid_rows if r["rank_autocorrelation"] is not None]
                turnover_values = [r["top_quintile_turnover"] for r in valid_rows if r["top_quintile_turnover"] is not None]
                mean_ic = float(vals.mean()) if len(vals) else None
                ic_std = float(vals.std(ddof=1)) if len(vals) >= 2 else None
                icir = float(vals.mean() / vals.std(ddof=1)) if len(vals) >= 2 and vals.std(ddof=1) > 0 else None
                mean_rank = float(ranks.mean()) if len(ranks) else None
                mean_coverage = float(np.mean([r["coverage"] for r in valid_rows])) if valid_rows else None
                mean_label_coverage = float(np.mean([r["label_coverage"] for r in valid_rows])) if valid_rows else None
                mean_autocorr = float(np.mean(autocorr_values)) if autocorr_values else None
                mean_turnover = float(np.mean(turnover_values)) if turnover_values else None
                no_ic_reason = "没有达到最小横截面样本数的交易日" if not len(vals) else None
                no_icir_reason = "IC有效日少于2天或IC无波动" if icir is None else None
                no_autocorr_reason = "没有足够的相邻交易日截面" if mean_autocorr is None else None
                no_turnover_reason = "因子为二元/常量或没有相邻有效日期" if mean_turnover is None else None
                summaries.append({"horizon": h, "factor": factor, "valid_days": int(len(vals)),
                                  "mean_ic": mean_ic, "ic_std": ic_std, "icir": icir,
                                  "mean_rank_ic": mean_rank, "mean_coverage": mean_coverage,
                                  "mean_label_coverage": mean_label_coverage,
                                  "mean_rank_autocorrelation": mean_autocorr,
                                  "mean_top_quintile_turnover": mean_turnover,
                                  "ic_unavailable_reason": no_ic_reason,
                                  "icir_unavailable_reason": no_icir_reason,
                                  "rank_autocorrelation_unavailable_reason": no_autocorr_reason,
                                  "top_quintile_turnover_unavailable_reason": no_turnover_reason})
                structured_summaries.append({"horizon": h, "factor": factor, "metrics": {
                    "mean_ic": _metric(mean_ic, "相关系数", "每日横截面 Pearson IC 的时间均值", len(vals), no_ic_reason),
                    "ic_std": _metric(ic_std, "相关系数", "每日横截面 IC 的样本标准差", len(vals), "IC有效日少于2天" if ic_std is None else None),
                    "icir": _metric(icir, "比率", "每日 IC 均值 / IC 样本标准差；不年化", len(vals), no_icir_reason),
                    "mean_rank_ic": _metric(mean_rank, "相关系数", "每日横截面 Spearman Rank IC 的时间均值", len(ranks), "没有有效 Rank IC 日" if mean_rank is None else None),
                    "factor_coverage": _metric(mean_coverage, "%", "有效因子值 / 当日样本股票数的日均值", len(valid_rows), "没有因子观测日" if mean_coverage is None else None),
                    "label_coverage": _metric(mean_label_coverage, "%", "可计算远期标签数 / 有效因子值数的日均值", len(valid_rows), "没有可用标签" if mean_label_coverage is None else None),
                    "rank_autocorrelation": _metric(mean_autocorr, "相关系数", "相邻交易日共同股票的因子排名 Spearman 相关", len(autocorr_values), no_autocorr_reason),
                    "top_quintile_turnover": _metric(mean_turnover, "%/日", "相邻交易日因子最高五分位成员变动比例", len(turnover_values), no_turnover_reason),
                }})
        pd.DataFrame(daily_rows).to_csv(out / "factor_daily.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(group_rows).to_csv(out / "factor_groups.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(summaries).to_csv(out / "factor_summary.csv", index=False, encoding="utf-8-sig")
        group_summary = []
        group_frame = pd.DataFrame(group_rows)
        if not group_frame.empty:
            valid_groups = group_frame[group_frame["mean_forward_return"].notna()]
            for (horizon, factor, group), subset in valid_groups.groupby(["horizon", "factor", "group"], sort=True):
                sample_count = int(subset["n"].sum())
                weighted_return = float((subset["mean_forward_return"] * subset["n"]).sum() / sample_count) if sample_count else None
                ratios = subset["positive_forward_ratio"].dropna()
                weighted_positive = float((subset.loc[ratios.index, "positive_forward_ratio"] * subset.loc[ratios.index, "n"]).sum()
                                          / subset.loc[ratios.index, "n"].sum()) if len(ratios) else None
                group_summary.append({"horizon": int(horizon), "factor": factor, "group": group,
                                      "metrics": {
                                          "mean_forward_return": _metric(weighted_return, "%", "组内股票-日期远期收益按样本数加权平均，不含费用", sample_count),
                                          "positive_forward_ratio": _metric(weighted_positive, "%", "组内远期收益为正的股票-日期样本比例", sample_count),
                                          "sample_days": _metric(int(subset["date"].nunique()), "日", "包含有效样本的交易日数", int(subset["date"].nunique())),
                                      }})
        return {"status": "ok", "factors": factors, "signals": signals, "horizons": horizons,
                "minimum_cross_section": min_cross_section, "icir_is_annualized": False,
                "quantile_returns_exclude_costs": True, "metrics": structured_summaries,
                "group_metrics": group_summary}
    finally:
        con.close()


def _benchmark_evaluation(curve: pd.DataFrame, config: dict, out: Path,
                          annualization_days: int, risk_free_rate: float) -> tuple[dict, pd.DataFrame]:
    path = str(config.get("path", "") or "").strip()
    if not path:
        return {"status": "not_configured", "reason": "未配置本地基准 CSV"}, pd.DataFrame()
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return {"status": "unavailable", "reason": f"基准文件不存在: {source}"}, pd.DataFrame()
    digest = None
    try:
        source_bytes = source.read_bytes()
        digest = hashlib.sha256(source_bytes).hexdigest()
        out.mkdir(parents=True, exist_ok=True)
        (out / "benchmark_source.csv").write_bytes(source_bytes)
        raw = pd.read_csv(source)
        if not {"date", "close"}.issubset(raw.columns):
            raise ValueError("CSV 必须含 date、close 两列")
        raw = raw[["date", "close"]].copy()
        raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
        raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
        if raw["date"].isna().any() or raw["close"].isna().any() or (raw["close"] <= 0).any():
            raise ValueError("基准日期或收盘价存在缺失、无法解析或非正值")
        if raw["date"].duplicated().any():
            raise ValueError("基准 CSV 存在重复日期")
        raw = raw.sort_values("date")
        portfolio_end = pd.to_datetime(curve["date"]).max()
        raw = raw[raw["date"] <= portfolio_end]
        if raw.empty:
            raise ValueError("基准行情全部晚于回测结束日期")
        raw["benchmark_return"] = raw["close"].pct_change()
        portfolio = curve[["date", "market_value", "return"]].copy()
        portfolio["date"] = pd.to_datetime(portfolio["date"])
        aligned = portfolio.merge(raw[["date", "close", "benchmark_return"]], on="date", how="left", validate="one_to_one")
        missing = aligned["close"].isna()
        if missing.any():
            miss_dates = aligned.loc[missing, "date"].dt.strftime("%Y-%m-%d").tolist()
            return {"status": "unavailable", "reason": f"基准未覆盖策略交易日: {', '.join(miss_dates[:5])}",
                    "missing_dates": len(miss_dates), "file_sha256": digest,
                    "name": config.get("name"), "source": config.get("source"),
                    "return_type": config.get("return_type")}, aligned
        aligned["strategy_nav"] = aligned["market_value"] / float(portfolio["market_value"].iloc[0])
        aligned["benchmark_nav"] = aligned["close"] / float(aligned["close"].iloc[0])
        aligned["relative_wealth_return"] = aligned["strategy_nav"] / aligned["benchmark_nav"] - 1
        strat_ret = aligned["return"].iloc[1:].astype(float)
        bench_ret = aligned["benchmark_return"].iloc[1:].astype(float)
        diff = strat_ret - bench_ret
        var_b = float(bench_ret.var(ddof=1)) if len(bench_ret) >= 2 else 0.0
        beta = float(strat_ret.cov(bench_ret) / var_b) if var_b > 0 else None
        daily_rf = (1 + risk_free_rate) ** (1 / annualization_days) - 1
        alpha_daily = float((strat_ret - daily_rf).mean() - beta * (bench_ret - daily_rf).mean()) if beta is not None else None
        alpha = (1 + alpha_daily) ** annualization_days - 1 if alpha_daily is not None else None
        tracking = float(diff.std(ddof=1) * math.sqrt(annualization_days)) if len(diff) >= 2 else None
        ir = float(diff.mean() / diff.std(ddof=1) * math.sqrt(annualization_days)) if len(diff) >= 2 and diff.std(ddof=1) > 0 else None
        up = bench_ret > 0
        down = bench_ret < 0
        up_capture = float(strat_ret[up].mean() / bench_ret[up].mean()) if up.any() and bench_ret[up].mean() != 0 else None
        down_capture = float(strat_ret[down].mean() / bench_ret[down].mean()) if down.any() and bench_ret[down].mean() != 0 else None
        metrics = {"relative_wealth_return": _metric(float(aligned["relative_wealth_return"].iloc[-1]), "%", "(策略期末净值倍数 / 基准期末净值倍数) - 1", len(aligned)),
                   "alpha": _metric(alpha, "%/年", "日收益回归截距复合年化", len(diff)),
                   "beta": _metric(beta, "比率", "策略日收益对基准日收益的协方差 / 基准方差", len(diff)),
                   "tracking_error": _metric(tracking, "%/年", "日主动收益标准差 × sqrt(年化交易日数)", len(diff)),
                   "information_ratio": _metric(ir, "比率", "日主动收益均值 / 日主动收益标准差 × sqrt(年化交易日数)", len(diff)),
                   "up_capture": _metric(up_capture, "比率", "基准上涨日策略平均收益 / 基准平均收益", int(up.sum())),
                   "down_capture": _metric(down_capture, "比率", "基准下跌日策略平均收益 / 基准平均收益", int(down.sum()))}
        aligned.to_csv(out / "benchmark_daily.csv", index=False, encoding="utf-8-sig")
        return {"status": "ok", "name": config.get("name"), "source": config.get("source"),
                "return_type": config.get("return_type"), "file_sha256": digest,
                "date_range": [raw["date"].min().strftime("%Y-%m-%d"), raw["date"].max().strftime("%Y-%m-%d")],
                "metrics": metrics}, aligned
    except (ValueError, OSError, UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        return {"status": "unavailable", "reason": str(exc), "file_sha256": digest,
                "name": config.get("name"), "source": config.get("source"),
                "return_type": config.get("return_type")}, pd.DataFrame()


def write_evaluation(equity: pd.DataFrame, trades: pd.DataFrame, positions: pd.DataFrame,
                     initial_cash: float, config: dict, factor_db: str | Path,
                     signals: list[str], check_cancel=None, progress=None) -> dict:
    out = Path(factor_db).parent
    out.mkdir(parents=True, exist_ok=True)
    if check_cancel:
        check_cancel()
    account, tables = account_evaluation(equity, trades, positions, initial_cash,
                                         config["annualization_days"], config["risk_free_rate"],
                                         config["sortino_target_return"])
    for name, table in tables.items():
        table.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
    if check_cancel:
        check_cancel()
    trading, trade_tables = _trade_analysis(trades, positions, equity, initial_cash)
    for name, table in trade_tables.items():
        table.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
    factor_settings = config
    factors = factor_names(factor_settings, signals)
    all_panel_factors = panel_factor_names(factor_settings, signals)
    factor_result = _factor_diagnostics(factor_db, out, factors, signals, config["horizons"],
                                        config["min_cross_section"], config["min_per_quantile"], check_cancel,
                                        progress, panel_factors=all_panel_factors)
    curve = _returns_by_period(equity, initial_cash)
    benchmark, benchmark_daily = _benchmark_evaluation(curve, config["benchmark"], out,
                                                        config["annualization_days"], config["risk_free_rate"])
    if benchmark.get("status") != "ok":
        try:
            (out / "benchmark_daily.csv").unlink()
        except FileNotFoundError:
            pass
    result = {"version": EVALUATION_VERSION, "status": "ok", "account": account,
              "trading": trading, "factors": factor_result, "benchmark": benchmark}
    atomic_json(out / "evaluation.json", result)
    return result
