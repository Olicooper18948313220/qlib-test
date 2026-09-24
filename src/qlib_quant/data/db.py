from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from ..contracts import DataManifest


QUOTE_COLUMNS = (
    "stock_code", "stock_name", "trade_date", "open_price", "high_price",
    "low_price", "close_price", "volume", "turnover", "turnover_rate",
    "trade_status", "adjustment", "source"
)


def _connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"数据库不存在: {p}")
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    return con


def validate_database(path: str | Path) -> DataManifest:
    with _connect(path) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        required = {"daily_quote", "stock_info", "trade_calendar"}
        missing = required - tables
        if missing:
            raise ValueError(f"缺少必要表: {sorted(missing)}")
        row = con.execute("""
            select min(trade_date), max(trade_date), count(*), count(distinct stock_code),
                   sum(case when trade_status is null then 1 else 0 end)
            from daily_quote where instrument_type='equity'
        """).fetchone()
        source_values = tuple(r[0] for r in con.execute(
            "select distinct source from daily_quote where instrument_type='equity' order by source"))
        adjustment = con.execute(
            "select adjustment from daily_quote where instrument_type='equity' limit 1").fetchone()[0]
        corp = con.execute("select count(*) from corporate_action_factor").fetchone()[0]
        if not row or not row[2]:
            raise ValueError("daily_quote 中没有股票日线")
        return DataManifest(
            database=str(Path(path).resolve()), table="daily_quote", instrument_type="stock",
            adjustment=adjustment, first_date=row[0], last_date=row[1], rows=row[2],
            instruments=row[3], null_trade_status_rows=row[4] or 0,
            corporate_action_rows=corp, source_values=source_values)


def iter_quotes(path: str | Path, start: str, end: str, batch_size: int = 10000) -> Iterator[dict]:
    """Yield normalized rows without loading the 6.9m-row table into memory."""
    with _connect(path) as con:
        cur = con.execute("""
          select stock_code, stock_name, trade_date, open_price, high_price,
                 low_price, close_price, volume, turnover, turnover_rate,
                 trade_status, adjustment, source
          from daily_quote
          where instrument_type='equity' and trade_date between ? and ?
          order by trade_date, stock_code
        """, (start, end))
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

