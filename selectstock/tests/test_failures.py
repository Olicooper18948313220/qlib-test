import pandas as pd
from conftest import NOW, FakeProvider, bars, seed

from selectstock.data.calendar import sessions
from selectstock.data.database import Database
from selectstock.indicators.technical import ema_series
from selectstock.runner import run_scans, update_data


def test_empty_bars_report_insufficient_instead_of_crashing(config):
    provider = FakeProvider()
    provider.empty = True
    update_data(config, provider=provider, now=NOW)
    result = run_scans(config, now=NOW)
    for frame in result.frames.values():
        assert frame.status.eq("skipped").all()
        assert frame.reasons.str.contains("missing_daily_bars").all()


def test_stale_metadata_excludes_candidate(config):
    seed(config)
    with Database(config["data"]["database"]).connect(True) as con:
        con.execute("UPDATE security_snapshots SET observed_at='2026-10-20T02:00:00+00:00'")
    result = run_scans(config, now=NOW)
    assert result.frames["leader"].reasons.str.contains("stale_metadata").all()


def test_provisional_minutes_refreshed_after_extended_close(config):
    seed(config)
    config["data"]["timeframes"] = ["5m"]
    config["data"]["history_days"]["5m"] = 1
    config["watchlist"]["symbols"] = ["AAA"]
    provider = FakeProvider()
    update_data(config, provider=provider, now="2026-10-30T21:00:00Z")
    db = Database(config["data"]["database"])
    assert not db.completed("AAA", "5m")
    assert db.load_bars("AAA", "5m").is_final.eq(0).all()
    update_data(config, provider=provider, now=NOW)
    assert db.completed("AAA", "5m") == {"2026-10-30"}
    assert len(provider.calls) == 2


def test_split_marks_unrecoverable_old_minutes(config):
    db = seed(config, end="2026-10-27")
    db.commit_history("AAA", "1m", bars(["2026-07-01"]), [("2026-07-01", "complete", "")])
    provider = FakeProvider()
    provider.split = "2026-10-28"
    provider.scale = 0.5
    update_data(config, provider=provider, now=NOW)
    assert db.load_bars("AAA", "1m").adjustment.eq("stale_before_split").all()
    assert not db.completed("AAA", "1m")


def test_ema_seed_and_recursive_value():
    result = ema_series(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == 2
    assert result.iloc[3] == 3
    assert result.iloc[4] == 4


def test_read_limit_does_not_load_full_minutes(config):
    db = seed(config)
    frame = bars(sessions("2025-01-01", "2026-10-30"))
    db.commit_history("AAA", "1m", frame, [])
    assert len(db.load_bars("AAA", "1m", limit=10)) == 10
    with db.connect() as con:
        plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM bars WHERE symbol='AAA' AND timeframe='1m' AND session_date<= '2026-10-30' ORDER BY ts DESC LIMIT 10"
        ).fetchall()
        assert any("INDEX" in str(tuple(r)) for r in plan)
