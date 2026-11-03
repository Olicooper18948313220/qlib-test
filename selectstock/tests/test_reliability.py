import importlib
from io import StringIO
from unittest.mock import Mock

import pandas as pd
import pytest
from conftest import NOW, FakeProvider, bars, seed
from test_download_guard import Clock, response

from selectstock.data.database import Database
from selectstock.data.download_guard import DownloadGuard, DownloadPaused
from selectstock.data.quality import partition_bars
from selectstock.progress import DownloadProgress
from selectstock.runner import run_scans, update_data


def test_auto_budget_continues_at_same_stock(config, monkeypatch):
    module = importlib.import_module("selectstock.data.sync")
    clock = Clock()
    config["data"].update(auto_resume=True, max_requests_per_run=2)
    monkeypatch.setattr(
        module, "DownloadGuard", lambda d: DownloadGuard(d, clock=clock.time, sleep=clock.sleep)
    )
    instances = []

    class HTTP(FakeProvider):
        def __init__(self, extended, guard):
            super().__init__()
            self.guard = guard
            instances.append(self)

        def metadata(self, symbol):
            self.guard.request(response)
            return super().metadata(symbol)

        def history(self, *args):
            self.guard.request(response)
            return super().history(*args)

        def close(self):
            pass

    monkeypatch.setattr(module, "YahooProvider", HTTP)
    events = []
    result = update_data(config, now=NOW, progress=lambda **e: events.append(e))
    assert result.status == "completed" and not result.paused
    assert result.network_requests == 4 and result.segments == 2
    assert [r[0] for r in instances[0].calls] == ["AAA", "BBB"]
    assert any(e.get("stage") == "waiting" for e in events)
    assert clock.now >= 1060


@pytest.mark.parametrize("status", [429, 403, 503])
def test_three_failed_recoveries_then_stop(config, status):
    clock = Clock()
    config["data"].update(auto_resume=True, max_consecutive_failures=1)
    guard = DownloadGuard(config["data"], clock=clock.time, sleep=clock.sleep)
    send = Mock(side_effect=lambda: response(status))
    with pytest.raises(DownloadPaused):
        guard.run(lambda: guard.request(send))
    assert send.call_count == 4
    assert clock.now >= 1000 + 3 * (300 if status == 503 else 900)


def test_wait_is_interruptible(config):
    config["data"]["auto_resume"] = True
    clock = Clock()

    def stop(seconds):
        raise KeyboardInterrupt()

    guard = DownloadGuard(config["data"], clock=clock.time, sleep=stop)
    with pytest.raises(KeyboardInterrupt):
        guard.run(lambda: guard.request(lambda: response(429)))


def test_one_bad_row_preserves_rest_and_does_not_repeat(config):
    class Bad(FakeProvider):
        def history(self, *args):
            frame = super().history(*args)
            if args[0] == "AAA":
                frame.loc[len(frame) - 80, "low"] = 0
            return frame

    provider = Bad()
    first = update_data(config, provider=provider, now=NOW)
    assert first.quarantined_rows == 1
    db = Database(config["data"]["database"])
    assert len(db.load_bars("BBB")) == len(db.load_bars("AAA")) + 1
    count = len(provider.calls)
    second = update_data(config, provider=provider, now=NOW)
    assert len(provider.calls) == count and second.skipped == 1
    result = run_scans(config, now=NOW)
    assert result.frames["leader"].set_index("symbol").loc["AAA", "status"] == "passed"
    assert result.frames["potent"].set_index("symbol").loc["AAA", "status"] == "skipped"
    with db.connect() as c:
        assert c.execute("SELECT reason FROM bar_quarantine").fetchone()[0] == "non_positive_price"


def test_identical_duplicates_and_conflicts():
    frame = bars(["2026-10-29", "2026-10-30"])
    good, bad = partition_bars(pd.concat([frame, frame.iloc[:1]], ignore_index=True))
    assert len(good) == 2 and not bad
    altered = frame.iloc[:1].copy()
    altered["volume"] += 1
    good, bad = partition_bars(pd.concat([frame, altered], ignore_index=True))
    assert len(good) == 1 and len(bad) == 2
    assert all(r["reason"] == "conflicting_timestamp" for r in bad)


def test_bad_repair_never_overwrites_good_cache(config):
    db = seed(config)
    previous = db.load_bars("AAA").set_index("session_date").loc["2026-10-30", "close"]

    class Bad(FakeProvider):
        def history(self, *args):
            frame = super().history(*args)
            frame.loc[frame["session_date"] == "2026-10-30", "low"] = 0
            return frame

    update_data(config, provider=Bad(), now=NOW, repair=True)
    assert db.load_bars("AAA").set_index("session_date").loc["2026-10-30", "close"] == previous
    assert "2026-10-30" in db.completed("AAA", "1d")


def test_empty_days_backoff_but_new_days_download(config):
    p = FakeProvider()
    p.empty = True
    update_data(config, provider=p, now=NOW)
    count = len(p.calls)
    assert update_data(config, provider=p, now=NOW).skipped == 2
    assert len(p.calls) == count
    config["data"]["end"] = "2026-11-02"
    p.empty = False
    update_data(config, provider=p, now=NOW)
    assert all(x[2] == "2026-11-02" for x in p.calls[count:])


def test_directory_exit_and_reentry_preserves_history(config):
    db = seed(config)
    records, _ = db.universe()
    db.save_universe(records[:1], NOW)
    result = run_scans(config, now=NOW)
    assert result.frames["leader"].set_index("symbol").loc["BBB", "status"] == "skipped"
    report = update_data(config, provider=FakeProvider(), now=NOW)
    assert report.skipped == 1 and not db.load_bars("BBB").empty
    db.save_universe(records, NOW)
    assert run_scans(config, now=NOW).frames["leader"].status.eq("passed").all()


def test_new_stock_can_run_leader_but_not_potent(config):
    seed(config, count=60)
    result = run_scans(config, now=NOW)
    assert result.frames["leader"].status.eq("passed").all()
    assert result.frames["potent"].status.eq("skipped").all()
    assert result.frames["potent"].reasons.str.contains("insufficient_warmup").all()


def test_stop_and_resume_uses_cursor_and_frozen_cutoff(config):
    class Stop(FakeProvider):
        def history(self, *args):
            if args[0] == "BBB":
                raise KeyboardInterrupt()
            return super().history(*args)

    first = update_data(config, provider=Stop(), now=NOW)
    assert first.stopped and first.processed == 1
    p = FakeProvider()
    second = update_data(config, provider=p, now=NOW + pd.Timedelta(days=3))
    assert second.processed == 2 and second.downloaded == 2
    assert [x[0] for x in p.calls] == ["BBB"]
    assert p.calls[0][3] == "2026-10-31"


def test_progress_text_contains_wait_and_finish():
    stream = StringIO()
    p = DownloadProgress(stream=stream)
    p(stage="waiting", remaining_seconds=60, reason="budget", resume_after="later")
    p(stage="completed", processed=3, total=3)
    assert "budget" in stream.getvalue() and "3/3" in stream.getvalue()


def test_retry_after_midnight_not_already_expired(config):
    p = FakeProvider()
    p.empty = True
    now = pd.Timestamp("2026-11-03T06:00:00Z")
    update_data(config, provider=p, now=now)
    before = len(p.calls)
    result = update_data(config, provider=p, now=now + pd.Timedelta(seconds=2))
    assert len(p.calls) == before and result.skipped == 2


def test_three_distinct_sessions_defer_seven_days(config):
    p = FakeProvider()
    p.empty = True
    for now in ["2026-11-03T22:00:00Z", "2026-11-04T22:00:00Z", "2026-11-05T22:00:00Z"]:
        update_data(config, provider=p, now=now)
    db = Database(config["data"]["database"])
    with db.connect() as con:
        row = con.execute("SELECT attempts,next_retry FROM retry_days LIMIT 1").fetchone()
    assert row[0] == 3 and pd.Timestamp(row[1]) == pd.Timestamp("2026-11-12T22:00:00Z")


def test_runtime_detects_changed_imported_function(tmp_path, monkeypatch):
    import sys
    import types

    from selectstock.runtime import check_runtime

    path = tmp_path / "module.py"
    source = "def example():\n    return 1\n"
    path.write_text(source)
    module = types.ModuleType("selectstock.runtime_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    check_runtime()
    path.write_text(source.replace("return 1", "return 2"))
    with pytest.raises(RuntimeError, match="Restart"):
        check_runtime()


def test_resumed_old_cutoff_does_not_fetch_future_metadata(config):
    class Stop(FakeProvider):
        def history(self, *args):
            if args[0] == "BBB":
                raise KeyboardInterrupt()
            return super().history(*args)

    update_data(config, provider=Stop(), now=NOW)
    db = Database(config["data"]["database"])
    with db.connect(True) as con:
        con.execute("DELETE FROM security_snapshots WHERE symbol='BBB'")

    class Resume(FakeProvider):
        def metadata(self, symbol):
            raise AssertionError("Do not fetch current metadata for frozen historical cutoff")

    result = update_data(config, provider=Resume(), now=NOW + pd.Timedelta(days=4))
    assert not result.metadata_failures and result.asof == "2026-10-30"


def test_metadata_pagination_can_start_at_saved_offset(monkeypatch):
    import yfinance as yf

    from selectstock.data.providers.yahoo import YahooProvider

    screen = Mock(return_value={"total": 501, "quotes": [{"symbol": "AAA", "marketCap": 2e9}]})
    monkeypatch.setattr(yf, "screen", screen)
    provider = YahooProvider()
    provider.metadata_offset = 500
    rows = list(provider.metadata_pages(["AAA"]))
    assert rows[0][0][0] == "AAA"
    assert screen.call_args.kwargs["offset"] == 500 and provider.metadata_offset == 501
    provider.close()
