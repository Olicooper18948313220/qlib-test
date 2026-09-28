from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..contracts import DataManifest


QUOTE_COLUMNS = (
    "stock_code", "stock_name", "trade_date", "open_price", "high_price",
    "low_price", "close_price", "volume", "turnover", "turnover_rate",
    "trade_status", "adjustment", "source"
)


@contextmanager
def _connect(path: str | Path, check_cancel=None):
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"数据库不存在: {p}")
    con = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=ON")
    if check_cancel:
        def interrupt():
            try:
                check_cancel()
                return 0
            except Exception:
                return 1
        con.set_progress_handler(interrupt, 10000)
    try:
        yield con
    except sqlite3.OperationalError:
        if check_cancel:
            check_cancel()
        raise
    finally:
        con.close()


def validate_database(path: str | Path, check_cancel=None) -> DataManifest:
    with _connect(path, check_cancel) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        required = {"daily_quote", "stock_info", "trade_calendar"}
        missing = required - tables
        if missing:
            raise ValueError(f"缺少必要表: {sorted(missing)}")
        fields = {r[1] for r in con.execute("pragma table_info(daily_quote)")}
        missing_fields = (set(QUOTE_COLUMNS) | {"instrument_type"}) - fields
        if missing_fields:
            raise ValueError(f"daily_quote 缺少必要字段: {sorted(missing_fields)}")
        row = con.execute("""
            select min(trade_date), max(trade_date), count(*), count(distinct stock_code),
                   sum(case when trade_status is null then 1 else 0 end)
            from daily_quote where instrument_type='equity'
        """).fetchone()
        source_values = tuple(r[0] for r in con.execute(
            "select distinct source from daily_quote where instrument_type='equity' order by source"))
        adjustment_row = con.execute(
            "select adjustment from daily_quote where instrument_type='equity' limit 1").fetchone()
        adjustment = adjustment_row[0] if adjustment_row else "unknown"
        duplicate_rows = con.execute("""
            select coalesce(sum(n - 1), 0) from (
              select stock_code, trade_date, count(*) n
              from daily_quote where instrument_type='equity'
              group by stock_code, trade_date having count(*) > 1
            )
        """).fetchone()[0] or 0
        invalid_price_rows = con.execute("""
            select count(*) from daily_quote
            where instrument_type='equity'
              and (open_price is null or high_price is null or low_price is null or close_price is null
                   or open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0
                   or open_price > 1e100 or high_price > 1e100 or low_price > 1e100 or close_price > 1e100)
        """).fetchone()[0] or 0
        invalid_date_rows = con.execute("""
            select count(*) from daily_quote
            where instrument_type='equity' and
              (trade_date is null or length(trade_date) != 10 or date(trade_date) is null
               or date(trade_date, '+0 days') != trade_date)
        """).fetchone()[0] or 0
        corp = (con.execute("select count(*) from corporate_action_factor").fetchone()[0]
                if "corporate_action_factor" in tables else 0)
        if not row or not row[2]:
            raise ValueError("daily_quote 中没有股票日线")
        return DataManifest(
            database=str(Path(path).resolve()), table="daily_quote", instrument_type="stock",
            adjustment=adjustment, first_date=row[0], last_date=row[1], rows=row[2],
            instruments=row[3], null_trade_status_rows=row[4] or 0,
            corporate_action_rows=corp, source_values=source_values,
            duplicate_rows=int(duplicate_rows), invalid_price_rows=int(invalid_price_rows),
            invalid_date_rows=int(invalid_date_rows))


def list_codes(path: str | Path, start: str, end: str, max_stocks: int = 0,
               max_rows: int = 0) -> list[str] | None:
    """Choose a deterministic stock subset before fetching quote rows."""
    if not max_stocks and not max_rows:
        return None
    with _connect(path) as con:
        rows = con.execute("""
            select stock_code, count(*) as n from daily_quote
            where instrument_type='equity' and trade_date between ? and ?
            group by stock_code order by stock_code
        """, (start, end)).fetchall()
    selected, total = [], 0
    for row in rows:
        if max_stocks and len(selected) >= max_stocks:
            break
        if max_rows and selected and total + int(row[1]) > max_rows:
            break
        selected.append(str(row[0]))
        total += int(row[1])
        if max_rows and total >= max_rows:
            break
    return selected


def iter_quotes(path: str | Path, start: str, end: str, batch_size: int = 10000,
                codes: list[str] | None = None) -> Iterator[dict]:
    """Yield normalized rows without loading the 6.9m-row table into memory."""
    with _connect(path) as con:
        query = """
          select stock_code, stock_name, trade_date, open_price, high_price,
                 low_price, close_price, volume, turnover, turnover_rate,
                 trade_status, adjustment, source
          from daily_quote
          where instrument_type='equity' and trade_date between ? and ?
        """
        params: list[str] = [start, end]
        if codes is not None:
            if not codes:
                return
            query += " and stock_code in (" + ",".join("?" for _ in codes) + ")"
            params.extend(codes)
        query += " order by trade_date, stock_code"
        cur = con.execute(query, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                item = dict(row)
                item.update({"code": item.pop("stock_code"), "date": item.pop("trade_date"),
                             "open": item.pop("open_price"), "high": item.pop("high_price"),
                             "low": item.pop("low_price"), "close": item.pop("close_price"),
                             "amount": item.pop("turnover")})
                yield item


def iter_stock_frames(path, start, end, warmup_rows, max_stocks=0, max_rows=0, check_cancel=None):
    """Load one stock at a time, including the exact prior-observation window."""
    import pandas as pd
    with _connect(path, check_cancel) as con:
        code_sql = """select stock_code, count(*) n from daily_quote
                      where instrument_type='equity' and trade_date between ? and ?
                      group by stock_code order by stock_code"""
        if max_stocks:
            code_sql += " limit " + str(int(max_stocks))
        codes = con.execute(code_sql, (start, end)).fetchall()
        used = 0
        columns = ", ".join(QUOTE_COLUMNS)
        for index, item in enumerate(codes):
            if check_cancel:
                check_cancel()
            if max_rows and index and used + item["n"] > max_rows:
                break
            code = item["stock_code"]
            # Each stock has its own start boundary: sparse stocks need more
            # calendar history to obtain the same number of observations.
            prior = con.execute(f"""select {columns} from daily_quote
                where stock_code=? and instrument_type='equity' and trade_date < ?
                order by trade_date desc limit ?""", (code, start, warmup_rows)).fetchall()
            current = con.execute(f"""select {columns} from daily_quote
                where stock_code=? and instrument_type='equity' and trade_date between ? and ?
                order by trade_date""", (code, start, end)).fetchall()
            used += len(current)
            frame = pd.DataFrame([dict(r) for r in reversed(prior)] + [dict(r) for r in current])
            frame = frame.rename(columns={"stock_code": "code", "stock_name": "name", "trade_date": "date",
                                          "open_price": "open", "high_price": "high", "low_price": "low",
                                          "close_price": "close", "turnover": "amount"})
            yield frame, len(prior), index + 1, len(codes)


def trading_dates(path, start, end, check_cancel=None):
    with _connect(path, check_cancel) as con:
        fields = {r[1] for r in con.execute("pragma table_info(trade_calendar)")}
        if {"trade_date", "is_open"} <= fields:
            rows = con.execute("""select distinct trade_date from trade_calendar
                where is_open=1 and trade_date between ? and ? order by trade_date""", (start, end)).fetchall()
            if rows:
                return [r[0] for r in rows], "trade_calendar"
        rows = con.execute("""select distinct trade_date from daily_quote
            where instrument_type='equity' and trade_date between ? and ? order by trade_date""",
                           (start, end)).fetchall()
        return [r[0] for r in rows], "daily_quote 日期并集（交易日历缺失时回退）"
