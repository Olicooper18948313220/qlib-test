from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .calendar import utc, utc_now

BAR_COLUMNS = [
    "symbol",
    "timeframe",
    "ts",
    "session_date",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_final",
    "adjustment",
    "source",
]
SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments(symbol TEXT PRIMARY KEY, exchange TEXT NOT NULL, name TEXT, source_symbol TEXT, instrument_type TEXT);
CREATE TABLE IF NOT EXISTS universe_snapshots(observed_at TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS security_snapshots(symbol TEXT, observed_at TEXT, market_cap REAL, industry TEXT, payload TEXT, PRIMARY KEY(symbol,observed_at));
CREATE TABLE IF NOT EXISTS bars(symbol TEXT, timeframe TEXT, ts TEXT, session_date TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, is_final INTEGER, adjustment TEXT, source TEXT, PRIMARY KEY(symbol,timeframe,ts));
CREATE INDEX IF NOT EXISTS bars_window ON bars(symbol,timeframe,session_date,ts);
CREATE TABLE IF NOT EXISTS corporate_actions(symbol TEXT, day TEXT, split REAL, dividend REAL, observed_at TEXT, PRIMARY KEY(symbol,day));
CREATE TABLE IF NOT EXISTS sync_state(symbol TEXT, timeframe TEXT, day TEXT, status TEXT, observed_at TEXT, detail TEXT, PRIMARY KEY(symbol,timeframe,day));
CREATE TABLE IF NOT EXISTS scan_runs(run_id TEXT PRIMARY KEY, asof TEXT, created_at TEXT, config TEXT, coverage TEXT, rule_version TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS scan_results(run_id TEXT, scanner TEXT, symbol TEXT, status TEXT, payload TEXT, PRIMARY KEY(run_id,scanner,symbol));
CREATE TABLE IF NOT EXISTS repairs(id INTEGER PRIMARY KEY, symbol TEXT, created_at TEXT, reason TEXT, start TEXT, end TEXT);
"""


def json_text(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def connect(self, write=False):
        if not write and not self.path.exists():
            raise FileNotFoundError(f"数据库尚未初始化: {self.path}")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(
            str(self.path) if write else self.path.as_uri() + "?mode=ro", uri=not write, timeout=30
        )
        con.row_factory = sqlite3.Row
        if write:
            con.execute("PRAGMA journal_mode=WAL")
        else:
            con.execute("PRAGMA query_only=ON")
        try:
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def initialize(self):
        with self.connect(True) as con:
            con.executescript(SCHEMA)
            con.execute("PRAGMA user_version=1")

    def save_universe(self, records, observed_at=None):
        at = utc(observed_at).isoformat() if observed_at is not None else utc_now().isoformat()
        with self.connect(True) as con:
            con.executemany(
                "INSERT OR REPLACE INTO instruments VALUES (:symbol,:exchange,:name,:source_symbol,:instrument_type)",
                records,
            )
            con.execute("INSERT OR REPLACE INTO universe_snapshots VALUES (?,?)", (at, json_text(records)))

    def universe(self):
        with self.connect() as con:
            row = con.execute("SELECT * FROM universe_snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
        return (json.loads(row["payload"]), row["observed_at"]) if row else ([], None)

    def instruments(self, symbols=None):
        with self.connect() as con:
            if symbols is None:
                records, _ = self.universe()
                return records
            result = []
            for symbol in symbols:
                row = con.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()
                result.append(
                    dict(row)
                    if row
                    else {
                        "symbol": symbol,
                        "exchange": "",
                        "name": symbol,
                        "source_symbol": symbol.replace(".", "-"),
                        "instrument_type": "unknown",
                    }
                )
            return result

    def save_metadata(self, symbol, metadata, observed_at=None):
        at = utc(observed_at).isoformat() if observed_at is not None else utc_now().isoformat()
        cap = metadata.get("market_cap")
        if cap is not None and (not isinstance(cap, (float, int)) or not pd.notna(cap) or cap <= 0):
            cap = None
        with self.connect(True) as con:
            con.execute(
                "INSERT OR REPLACE INTO security_snapshots VALUES (?,?,?,?,?)",
                (symbol, at, cap, metadata.get("industry"), json_text(metadata)),
            )
            if metadata.get("exchange"):
                con.execute(
                    "UPDATE instruments SET exchange=? WHERE symbol=?", (metadata["exchange"], symbol)
                )

    def metadata(self, symbol, before=None):
        with self.connect() as con:
            query = "SELECT * FROM security_snapshots WHERE symbol=?"
            args = [symbol]
            if before is not None:
                query += " AND observed_at<=?"
                args.append(utc(before).isoformat())
            row = con.execute(query + " ORDER BY observed_at DESC LIMIT 1", args).fetchone()
        return dict(row) if row else {}

    def load_bars(self, symbol, timeframe="1d", start=None, end=None, limit=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit 必须为正整数")
        query, args = "SELECT * FROM bars WHERE symbol=? AND timeframe=?", [symbol, timeframe]
        for op, value in ((">=", start), ("<=", end)):
            if value is not None:
                query += f" AND session_date {op} ?"
                args.append(str(value)[:10])
        query += " ORDER BY ts DESC"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        with self.connect() as con:
            rows = con.execute(query, args).fetchall()
        frame = pd.DataFrame([dict(r) for r in reversed(rows)], columns=BAR_COLUMNS)
        if not frame.empty:
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        return frame

    def completed(self, symbol, tf):
        with self.connect() as con:
            return {
                r[0]
                for r in con.execute(
                    "SELECT day FROM sync_state WHERE symbol=? AND timeframe=? AND status='complete'",
                    (symbol, tf),
                )
            }

    def commit_history(self, symbol, tf, frame, states, actions=(), repair=None, invalidate_before=None):
        """Bars and progress share one transaction; a failed commit never advances the cursor."""
        at = utc_now().isoformat()
        with self.connect(True) as con:
            if invalidate_before:
                con.execute(
                    "UPDATE bars SET adjustment='stale_before_split' WHERE symbol=? AND session_date<?",
                    (symbol, invalidate_before),
                )
                con.execute(
                    "UPDATE sync_state SET status='needs_repair' WHERE symbol=? AND day<?",
                    (symbol, invalidate_before),
                )
            if repair:
                con.execute(
                    "DELETE FROM bars WHERE symbol=? AND timeframe=? AND session_date BETWEEN ? AND ?",
                    (symbol, tf, repair["start"], repair["end"]),
                )
                con.execute(
                    "INSERT INTO repairs(symbol,created_at,reason,start,end) VALUES (?,?,?,?,?)",
                    (symbol, at, repair["reason"], repair["start"], repair["end"]),
                )
            values = []
            for row in frame.to_dict("records"):
                row.update(symbol=symbol, timeframe=tf)
                row["ts"] = utc(row["ts"]).isoformat()
                values.append(tuple(row[k] for k in BAR_COLUMNS))
            con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            con.executemany(
                "INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?,?,?)",
                [(symbol, tf, day, status, at, detail) for day, status, detail in states],
            )
            for action in actions:
                con.execute(
                    "INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?,?)",
                    (symbol, action["day"], action["split"], action["dividend"], at),
                )

    def known_actions(self, symbol):
        with self.connect() as con:
            return {
                r["day"]: dict(r)
                for r in con.execute("SELECT * FROM corporate_actions WHERE symbol=?", (symbol,))
            }

    def first_day(self, symbol, tf):
        with self.connect() as con:
            return con.execute(
                "SELECT min(session_date) FROM bars WHERE symbol=? AND timeframe=?", (symbol, tf)
            ).fetchone()[0]

    def save_scan(self, result):
        from ..contracts import RULE_VERSION

        with self.connect(True) as con:
            con.execute(
                "INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?)",
                (
                    result.run_id,
                    result.asof,
                    utc_now().isoformat(),
                    json_text(result.config),
                    json_text(result.coverage),
                    RULE_VERSION,
                    "success",
                ),
            )
            for name, frame in result.frames.items():
                for row in frame.to_dict("records"):
                    clean = {
                        k: (None if isinstance(v, float) and not pd.notna(v) else v) for k, v in row.items()
                    }
                    con.execute(
                        "INSERT INTO scan_results VALUES (?,?,?,?,?)",
                        (result.run_id, name, row["symbol"], row["status"], json_text(clean)),
                    )

    def latest_selected(self, scanners):
        """Latest successful run per enabled scanner, including a valid empty result."""
        result = set()
        with self.connect() as con:
            for name in scanners:
                rows = con.execute(
                    "SELECT run_id,config FROM scan_runs WHERE status='success' ORDER BY created_at DESC"
                ).fetchall()
                for row in rows:
                    if name in json.loads(row["config"]).get("executed_scanners", []):
                        result.update(
                            r[0]
                            for r in con.execute(
                                "SELECT symbol FROM scan_results WHERE run_id=? AND scanner=? AND status='passed'",
                                (row["run_id"], name),
                            )
                        )
                        break
        return sorted(result)

    def health(self):
        with self.connect() as con:
            groups = [
                dict(r)
                for r in con.execute(
                    "SELECT timeframe,count(*) rows,count(DISTINCT symbol) symbols,min(session_date) first_day,max(session_date) last_day FROM bars GROUP BY timeframe"
                )
            ]
            pending = [
                dict(r) for r in con.execute("SELECT status,count(*) count FROM sync_state GROUP BY status")
            ]
            integrity = con.execute("PRAGMA quick_check").fetchone()[0]
        return {
            "database": str(self.path),
            "integrity": integrity,
            "timeframes": groups,
            "sync_status": pending,
        }
