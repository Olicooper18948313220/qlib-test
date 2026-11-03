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
CREATE TABLE IF NOT EXISTS metadata_failures(symbol TEXT PRIMARY KEY, attempted_at TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS bars(symbol TEXT, timeframe TEXT, ts TEXT, session_date TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, is_final INTEGER, adjustment TEXT, source TEXT, PRIMARY KEY(symbol,timeframe,ts));
CREATE INDEX IF NOT EXISTS bars_window ON bars(symbol,timeframe,session_date,ts);
CREATE TABLE IF NOT EXISTS corporate_actions(symbol TEXT, day TEXT, split REAL, dividend REAL, observed_at TEXT, PRIMARY KEY(symbol,day));
CREATE TABLE IF NOT EXISTS sync_state(symbol TEXT, timeframe TEXT, day TEXT, status TEXT, observed_at TEXT, detail TEXT, PRIMARY KEY(symbol,timeframe,day));
CREATE TABLE IF NOT EXISTS scan_runs(run_id TEXT PRIMARY KEY, asof TEXT, created_at TEXT, config TEXT, coverage TEXT, rule_version TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS scan_results(run_id TEXT, scanner TEXT, symbol TEXT, status TEXT, payload TEXT, PRIMARY KEY(run_id,scanner,symbol));
CREATE TABLE IF NOT EXISTS bar_quarantine(symbol TEXT, timeframe TEXT, observed_at TEXT, day TEXT, reason TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS retry_days(symbol TEXT, timeframe TEXT, day TEXT, attempts INTEGER, last_session TEXT, next_retry TEXT, PRIMARY KEY(symbol,timeframe,day));
CREATE TABLE IF NOT EXISTS job_context(job_id TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS sync_jobs(job_id TEXT PRIMARY KEY, status TEXT, payload TEXT, updated_at TEXT);
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
        if self.path.exists():
            with self.connect() as source:
                version = source.execute("PRAGMA user_version").fetchone()[0]
                if version < 2:
                    backup = self.path.with_name(self.path.name + ".before_v2.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as target:
                            source.backup(target)
        with self.connect(True) as con:
            con.executescript(SCHEMA)
            con.execute("PRAGMA user_version=2")

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
        self.save_metadata_many([(symbol, metadata)], observed_at)

    def save_metadata_many(self, records, observed_at=None):
        at = utc(observed_at).isoformat() if observed_at is not None else utc_now().isoformat()
        with self.connect(True) as con:
            for symbol, metadata in records:
                cap = metadata.get("market_cap")
                if cap is not None and (not isinstance(cap, (float, int)) or not pd.notna(cap) or cap <= 0):
                    cap = None
                # A bulk quote may not include industry; retain an existing known industry.
                industry = metadata.get("industry")
                if not industry:
                    old = con.execute(
                        "SELECT industry FROM security_snapshots WHERE symbol=? ORDER BY observed_at DESC LIMIT 1",
                        (symbol,),
                    ).fetchone()
                    industry = old[0] if old else None
                con.execute(
                    "INSERT OR REPLACE INTO security_snapshots VALUES (?,?,?,?,?)",
                    (symbol, at, cap, industry, json_text(metadata)),
                )
                con.execute("DELETE FROM metadata_failures WHERE symbol=?", (symbol,))
                if metadata.get("exchange"):
                    con.execute(
                        "UPDATE instruments SET exchange=? WHERE symbol=?", (metadata["exchange"], symbol)
                    )

    def metadata_retry_due(self, symbol, now, minutes):
        with self.connect() as con:
            row = con.execute(
                "SELECT attempted_at FROM metadata_failures WHERE symbol=?", (symbol,)
            ).fetchone()
        return not row or utc(now) >= utc(row[0]) + pd.Timedelta(minutes=minutes)

    def record_metadata_failure(self, symbol, detail, observed_at):
        with self.connect(True) as con:
            con.execute(
                "INSERT OR REPLACE INTO metadata_failures VALUES (?,?,?)",
                (symbol, utc(observed_at).isoformat(), detail),
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
                    "SELECT day FROM sync_state WHERE symbol=? AND timeframe=? AND status IN ('complete','not_listed')",
                    (symbol, tf),
                )
            }

    def commit_history(
        self,
        symbol,
        tf,
        frame,
        states,
        actions=(),
        repair=None,
        invalidate_before=None,
        quarantine=(),
        observed_at=None,
    ):
        """Bars and progress share one transaction; a failed commit never advances the cursor."""
        at = utc(observed_at).isoformat() if observed_at is not None else utc_now().isoformat()
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
                bad_days = {r["session_date"] for r in quarantine}
                valid_days = set(frame["session_date"]) - bad_days if not frame.empty else set()
                con.executemany(
                    "DELETE FROM bars WHERE symbol=? AND timeframe=? AND session_date=?",
                    [(symbol, tf, day) for day in valid_days],
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
            for row in quarantine:
                con.execute(
                    "INSERT INTO bar_quarantine VALUES (?,?,?,?,?,?)",
                    (symbol, tf, at, row["session_date"], row["reason"], json_text(row["raw"])),
                )
            for day, status, detail in states:
                existing = con.execute(
                    "SELECT status FROM sync_state WHERE symbol=? AND timeframe=? AND day=?",
                    (symbol, tf, day),
                ).fetchone()
                if status in {"no_data", "invalid"} and existing and existing[0] == "complete":
                    continue  # A bad remote response cannot invalidate valid cached data.
                con.execute(
                    "INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?,?,?)",
                    (symbol, tf, day, status, at, detail),
                )
                if status in {"complete", "not_listed"}:
                    con.execute(
                        "DELETE FROM retry_days WHERE symbol=? AND timeframe=? AND day=?", (symbol, tf, day)
                    )
                elif status in {"no_data", "invalid"}:
                    from .calendar import NY, latest_closed, sessions

                    local_day = latest_closed(at, 0)
                    previous = con.execute(
                        "SELECT attempts,last_session FROM retry_days WHERE symbol=? AND timeframe=? AND day=?",
                        (symbol, tf, day),
                    ).fetchone()
                    attempts = (previous[0] + (previous[1] != local_day)) if previous else 1
                    if attempts >= 3:
                        next_at = utc(at) + pd.Timedelta(days=7)
                    elif status == "invalid":
                        next_at = utc(at) + pd.Timedelta(hours=24)
                    else:
                        following = sessions(
                            pd.Timestamp(utc(at).tz_convert(NY).date()) + pd.Timedelta(days=1),
                            pd.Timestamp(utc(at).tz_convert(NY).date()) + pd.Timedelta(days=10),
                        )[0]
                        next_at = pd.Timestamp(following + " 00:00", tz=NY).tz_convert("UTC")
                    con.execute(
                        "INSERT OR REPLACE INTO retry_days VALUES (?,?,?,?,?,?)",
                        (symbol, tf, day, attempts, local_day, next_at.isoformat()),
                    )
            for action in actions:
                con.execute(
                    "INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?,?)",
                    (symbol, action["day"], action["split"], action["dividend"], at),
                )

    def deferred_days(self, symbol, tf, now):
        with self.connect() as con:
            return {
                r[0]
                for r in con.execute(
                    "SELECT day FROM retry_days WHERE symbol=? AND timeframe=? AND next_retry>?",
                    (symbol, tf, utc(now).isoformat()),
                )
            }

    def inactive(self, symbol):
        records, _ = self.universe()
        # No directory snapshot means explicit-symbol research, not evidence of inactivity.
        return bool(records) and symbol not in {r["symbol"] for r in records}

    def job(self, job_id):
        with self.connect() as con:
            row = con.execute("SELECT status,payload FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row[0] not in {"running", "paused", "stopped"}:
            return None
        with self.connect() as con:
            context = con.execute("SELECT payload FROM job_context WHERE job_id=?", (job_id,)).fetchone()
        return {**(json.loads(context[0]) if context else {}), **json.loads(row[1])}

    def save_job(self, job_id, status, payload):
        context = {k: payload[k] for k in ("records", "now", "tasks")}
        state = {k: v for k, v in payload.items() if k not in context}
        with self.connect(True) as con:
            previous = con.execute("SELECT status FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
            old_context = con.execute(
                "SELECT json_type(payload,'$.tasks') FROM job_context WHERE job_id=?", (job_id,)
            ).fetchone()
            if (
                not previous
                or previous[0] not in {"running", "paused", "stopped"}
                or not old_context
                or (old_context[0] == "null" and context["tasks"] is not None)
            ):
                con.execute("INSERT OR REPLACE INTO job_context VALUES (?,?)", (job_id, json_text(context)))
            con.execute(
                "INSERT OR REPLACE INTO sync_jobs VALUES (?,?,?,?)",
                (job_id, status, json_text(state), utc_now().isoformat()),
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
