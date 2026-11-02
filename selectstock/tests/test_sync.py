import pandas as pd
import pytest
from conftest import NOW, FakeProvider, seed

from selectstock.data.database import Database
from selectstock.runner import run_scans, update_data


def test_incremental_idempotence_and_gap_catchup(config):
    provider = FakeProvider()
    config["data"]["end"] = "2026-10-27"
    update_data(config, provider=provider, now=NOW)
    initial = len(provider.calls)
    update_data(config, provider=provider, now=NOW)
    assert len(provider.calls) == initial
    config["data"]["end"] = "2026-10-30"
    report = update_data(config, provider=provider, now=NOW)
    assert not report.failures
    assert all(call[2] == "2026-10-28" for call in provider.calls[initial:])
    db = Database(config["data"]["database"])
    rows = db.load_bars("AAA")
    assert not rows.ts.duplicated().any()
    provider.calls.clear()
    report = update_data(config, provider=provider, now=NOW)
    assert not provider.calls and report.cached == 2


def test_failure_then_resume(config):
    provider = FakeProvider()
    provider.fail = True
    report = update_data(config, provider=provider, now=NOW)
    assert len(report.failures) == 2
    assert not Database(config["data"]["database"]).completed("AAA", "1d")
    provider.fail = False
    assert not update_data(config, provider=provider, now=NOW).failures


def test_empty_response_is_not_complete(config):
    provider = FakeProvider()
    provider.empty = True
    report = update_data(config, provider=provider, now=NOW)
    assert report.failures
    assert not Database(config["data"]["database"]).completed("AAA", "1d")


def test_atomic_write_does_not_advance_state(config):
    db = seed(config)
    from conftest import bars

    broken = bars(["2026-11-02"]).drop(columns=["volume"])
    with pytest.raises(KeyError):
        db.commit_history("AAA", "1d", broken, [("2026-11-02", "complete", "")])
    assert "2026-11-02" not in db.completed("AAA", "1d")


def test_minutes_only_picks_and_watchlist_and_expiry(config):
    seed(config)
    provider = FakeProvider()
    config["data"].update(timeframes=["1m"], history_days={"1d": 400, "1m": 60, "5m": 30, "15m": 30})
    config["watchlist"]["symbols"] = ["BBB"]
    report = update_data(config, provider=provider, now=NOW)
    assert {c[0] for c in provider.calls} == {"BBB"}
    assert report.warnings
    assert all((pd.Timestamp(c[3]) - pd.Timestamp(c[2])).days <= 7 for c in provider.calls)


def test_split_repairs_daily_and_marks_old_minutes(config):
    db = seed(config, end="2026-10-27")
    old = db.load_bars("AAA").iloc[0].close
    provider = FakeProvider()
    provider.split = "2026-10-28"
    provider.scale = 0.5
    report = update_data(config, provider=provider, now=NOW)
    assert not report.failures
    assert db.load_bars("AAA").iloc[0].close == pytest.approx(old * 0.5)
    assert db.load_bars("AAA").adjustment.nunique() == 1
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM repairs").fetchone()[0] == 2


def test_latest_empty_scan_replaces_old_minute_candidates(config):
    db = seed(config)
    run_scans(config, enabled=["leader"], now=NOW)
    assert db.latest_selected(["leader"]) == ["AAA", "BBB"]
    config["scanners"]["leader"]["min_week_turnover"] = 1e20
    run_scans(config, enabled=["leader"], now=NOW)
    assert db.latest_selected(["leader"]) == []


def test_missing_internal_session_is_retried_without_cached_ranges(config):
    db = seed(config)
    with db.connect(True) as con:
        con.execute("DELETE FROM bars WHERE symbol='AAA' AND session_date='2026-10-28'")
        con.execute("DELETE FROM sync_state WHERE symbol='AAA' AND day='2026-10-28'")
    config["data"]["history_days"]["1d"] = 10
    provider = FakeProvider()
    update_data(config, provider=provider, now=NOW)
    assert provider.calls == [("AAA", "1d", "2026-10-28", "2026-10-29")]
