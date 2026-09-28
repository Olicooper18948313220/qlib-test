import sqlite3

import pandas as pd

from qlib_quant.runner import run_request


def test_runner_writes_reproducible_run_metadata(tmp_path):
    db = tmp_path / "quotes.db"
    con = sqlite3.connect(db)
    con.executescript("""
    create table daily_quote (
      stock_code text, stock_name text, trade_date text, open_price real,
      high_price real, low_price real, close_price real, volume real,
      turnover real, turnover_rate real, trade_status text, adjustment text,
      source text, instrument_type text
    );
    create table stock_info (stock_code text);
    create table trade_calendar (trade_date text);
    create table corporate_action_factor (stock_code text);
    """)
    rows = []
    for date in pd.date_range("2020-01-01", "2020-02-28", freq="B"):
        for code, price in (("A", 10), ("B", 20)):
            rows.append((code, code, date.strftime("%Y-%m-%d"), price, price, price, price,
                         1000, 10000, 0, "normal", "hfq", "test", "equity"))
    con.executemany("insert into daily_quote values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()
    out = run_request({"database": str(db), "start": "2020-01-01", "end": "2020-02-28",
                       "max_stocks": 1, "output": str(tmp_path / "run"),
                       "config": {"signals": {"enabled": []}}})
    assert (out / "summary.json").exists()
    metadata = (out / "run_metadata.json").read_text(encoding="utf-8")
    assert '"stocks_loaded": 1' in metadata
    assert '"state": "success"' in (out / "status.json").read_text(encoding="utf-8")
