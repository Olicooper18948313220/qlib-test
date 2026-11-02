from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from conftest import NOW, FakeProvider
from yfinance.exceptions import YFRateLimitError

from selectstock.data.download_guard import DownloadGuard, DownloadPaused
from selectstock.data.providers.yahoo import YahooProvider
from selectstock.data.sync import retry
from selectstock.runner import run_scans, update_data


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def make_guard(config, clock, **settings):
    d = dict(config["data"], request_pause_seconds=1.5, request_jitter_seconds=0.5, **settings)
    return DownloadGuard(d, clock=clock.time, sleep=clock.sleep, uniform=lambda a, b: b)


def response(status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {})


def test_spacing_is_shared_across_new_instances(config):
    clock = Clock()
    a, b = make_guard(config, clock), make_guard(config, clock)
    a.request(response)
    b.request(response)
    assert clock.waits == [2.0]
    assert a.request_count == b.request_count == 1


@pytest.mark.parametrize("status", [429, 403])
def test_rate_limit_stops_immediately_and_persists(config, status):
    clock = Clock()
    guard = make_guard(config, clock)
    calls = []

    def send():
        calls.append(1)
        return response(status)

    with pytest.raises(DownloadPaused):
        guard.request(send)
    with pytest.raises(DownloadPaused):
        make_guard(config, clock).request(send)
    assert len(calls) == 1
    assert clock.waits == []


@pytest.mark.parametrize(
    "header", ["1800", format_datetime(datetime.fromtimestamp(2800, timezone.utc), usegmt=True)]
)
def test_retry_after_may_extend_cooldown(config, header):
    guard = make_guard(config, Clock())
    with pytest.raises(DownloadPaused) as exc:
        guard.request(lambda: response(429, {"Retry-After": header}))
    assert exc.value.resume_at == 2800


def test_budget_pause_and_later_resume(config):
    clock = Clock()
    guard = make_guard(config, clock, max_requests_per_run=2)
    guard.request(response)
    guard.request(response)
    with pytest.raises(DownloadPaused):
        guard.request(response)
    clock.now += config["data"]["budget_cooldown_seconds"] + 1
    resumed = make_guard(config, clock)
    resumed.request(response)
    assert resumed.request_count == 1


def test_batch_rest(config):
    clock = Clock()
    guard = make_guard(config, clock, download_batch_requests=2, download_batch_pause_seconds=10)
    for _ in range(3):
        guard.request(response)
    assert clock.waits == [2.0, 12.0]


def test_consecutive_transport_errors_trip_breaker(config):
    guard = make_guard(config, Clock(), max_consecutive_failures=2)

    def fail():
        raise ConnectionError("offline")

    with pytest.raises(ConnectionError):
        guard.request(fail)
    with pytest.raises(DownloadPaused):
        guard.request(fail)
    with pytest.raises(DownloadPaused):
        guard.check()


def test_server_errors_trip_breaker(config):
    guard = make_guard(config, Clock(), max_consecutive_failures=2)
    guard.request(lambda: response(503))
    with pytest.raises(DownloadPaused):
        guard.request(lambda: response(503))


def test_no_retry_after_explicit_yfinance_limit(config):
    calls = []

    def fail():
        calls.append(1)
        raise YFRateLimitError()

    with pytest.raises(YFRateLimitError):
        retry(fail, config["data"])
    assert len(calls) == 1


def test_sync_does_not_visit_remaining_stocks_after_limit(config):
    class Limited(FakeProvider):
        def metadata(self, symbol):
            self.calls.append(symbol)
            raise YFRateLimitError()

    provider = Limited()
    result = update_data(config, provider=provider, now=NOW)
    assert provider.calls == ["AAA"]
    assert result.paused and result.resume_after
    assert not result.metadata_failures
    update_data(config, provider=provider, now=NOW)
    assert provider.calls == ["AAA"]


def test_cooldown_applies_to_another_database(config):
    guard = DownloadGuard(config["data"])
    with pytest.raises(DownloadPaused):
        guard.pause("limited")
    config["data"]["database"] += ".other"
    provider = FakeProvider()
    assert update_data(config, provider=provider, now=NOW).paused
    assert not provider.calls


def test_provider_does_not_suppress_guard_pause(config):
    guard = DownloadGuard(config["data"])
    provider = YahooProvider(guard=guard)

    def internally_suppressed():
        try:
            guard.pause("limited")
        except DownloadPaused:
            return None

    try:
        with pytest.raises(DownloadPaused):
            provider._call(internally_suppressed)
    finally:
        provider.close()


def test_weekend_does_not_refetch_same_session_metadata(config):
    class Counting(FakeProvider):
        def __init__(self):
            super().__init__()
            self.metadata_calls = []

        def metadata(self, symbol):
            self.metadata_calls.append(symbol)
            return super().metadata(symbol)

    provider = Counting()
    update_data(config, provider=provider, now=NOW)
    assert len(provider.metadata_calls) == 2
    result = update_data(config, provider=provider, now="2026-11-02T08:00:00Z")
    assert len(provider.metadata_calls) == 2 and result.cached == 2
    scan = run_scans(config, now="2026-11-02T08:00:00Z")
    assert not scan.frames["leader"].reasons.str.contains("stale_metadata").any()


def test_actual_session_intercepts_internal_http(config, monkeypatch):
    from curl_cffi import requests

    clock = Clock()
    guard = make_guard(config, clock)
    monkeypatch.setattr(requests.Session, "request", lambda *args, **kwargs: response())
    provider = YahooProvider(guard=guard)
    try:
        assert provider._ticker("AAPL") is provider._ticker("AAPL")
        provider.session.get("https://query1.finance.yahoo.com/example")
        provider.session.get("https://query1.finance.yahoo.com/example2")
        assert guard.request_count == 2
        assert clock.waits == [2.0]
    finally:
        provider.close()


def test_two_workers_share_one_network_lane(config):
    import time
    from concurrent.futures import ThreadPoolExecutor

    d = dict(config["data"], request_pause_seconds=0, request_jitter_seconds=0)
    guards = [DownloadGuard(d), DownloadGuard(d)]
    active = 0
    maximum = 0

    def send():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.02)
        active -= 1
        return response()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(f.result().status_code == 200 for f in [pool.submit(g.request, send) for g in guards])
    assert maximum == 1


def test_budget_keeps_committed_data_and_resumes(config, monkeypatch):
    import importlib

    module = importlib.import_module("selectstock.data.sync")
    clock = Clock()
    config["data"]["max_requests_per_run"] = 2
    original = DownloadGuard
    monkeypatch.setattr(
        module,
        "DownloadGuard",
        lambda d: original(d, clock=clock.time, sleep=clock.sleep, uniform=lambda a, b: 0),
    )

    class HTTPProvider(FakeProvider):
        def __init__(self, extended, guard):
            super().__init__()
            self.guard = guard

        def metadata(self, symbol):
            self.guard.request(response)
            return super().metadata(symbol)

        def history(self, *args):
            self.guard.request(response)
            return super().history(*args)

        def close(self):
            pass

    monkeypatch.setattr(module, "YahooProvider", HTTPProvider)
    first = update_data(config, now=NOW)
    assert first.paused and first.downloaded == 1 and first.network_requests == 2
    clock.now += config["data"]["budget_cooldown_seconds"] + 1
    second = update_data(config, now=NOW)
    assert not second.paused and second.cached == 1 and second.downloaded == 1
    assert second.network_requests == 2
