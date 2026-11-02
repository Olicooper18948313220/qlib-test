from unittest.mock import Mock

import pandas as pd
import pytest
from conftest import NOW, FakeProvider, seed

from selectstock.data.database import Database
from selectstock.data.download_guard import DownloadPaused
from selectstock.data.providers.yahoo import YahooProvider
from selectstock.runner import update_data


class BulkProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.wanted = []
        self.individual = []

    def metadata_pages(self, symbols):
        self.wanted.append(list(symbols))
        yield [
            (symbol, {"market_cap": 50_000_000 if symbol == "BBB" else 2e9, "exchange": "NASDAQ"})
            for symbol in symbols
        ]

    def metadata(self, symbol):
        self.individual.append(symbol)
        return super().metadata(symbol)


def test_bulk_saves_all_caps_and_keeps_all_stock_histories(config):
    config["data"]["metadata_batch_min_symbols"] = 2
    provider = BulkProvider()
    result = update_data(config, provider=provider, now=NOW)
    assert result.metadata_bulk_saved == 2
    assert result.downloaded == 2
    assert {call[0] for call in provider.calls} == {"AAA", "BBB"}
    assert not provider.individual
    db = Database(config["data"]["database"])
    assert db.metadata("BBB")["market_cap"] == 50_000_000
    assert not db.load_bars("BBB").empty


def test_cached_metadata_avoids_bulk_requery(config):
    config["data"]["metadata_batch_min_symbols"] = 2
    provider = BulkProvider()
    update_data(config, provider=provider, now=NOW)
    report = update_data(config, provider=provider, now=NOW)
    assert len(provider.wanted) == 1
    assert report.cached == 2 and report.metadata_bulk_saved == 0


def test_bulk_missing_cap_does_not_exclude_history(config):
    config["data"].update(metadata_batch_min_symbols=2, metadata_fallback_limit=0)

    class Partial(BulkProvider):
        def metadata_pages(self, symbols):
            yield [("AAA", {"market_cap": 2e9})]

    report = update_data(config, provider=Partial(), now=NOW)
    assert report.metadata_bulk_saved == 1 and report.metadata_deferred == 1
    assert report.metadata_failures[0]["symbol"] == "BBB"
    assert report.downloaded == 2


def test_missing_cap_uses_limited_fallback(config):
    config["data"].update(metadata_batch_min_symbols=2, metadata_fallback_limit=1)

    class Missing(BulkProvider):
        def metadata_pages(self, symbols):
            yield []

    p = Missing()
    report = update_data(config, provider=p, now=NOW)
    assert p.individual == ["AAA"]
    assert report.metadata_deferred == 1 and report.downloaded == 2


def test_failed_metadata_is_deferred_on_repeat(config):
    class Invalid(FakeProvider):
        count = 0

        def metadata(self, symbol):
            self.count += 1
            raise ValueError("missing cap")

    p = Invalid()
    update_data(config, provider=p, now=NOW)
    report = update_data(config, provider=p, now=NOW)
    assert p.count == 2
    assert report.metadata_deferred == 2 and report.cached == 2


def test_partial_pages_commit_before_pause(config):
    config["data"]["metadata_batch_min_symbols"] = 2

    class Paused(BulkProvider):
        def metadata_pages(self, symbols):
            yield [("AAA", {"market_cap": 2e9})]
            raise DownloadPaused("budget", NOW.timestamp() + 60)

    report = update_data(config, provider=Paused(), now=NOW)
    assert report.paused and report.metadata_bulk_saved == 1
    db = Database(config["data"]["database"])
    assert db.metadata("AAA")["market_cap"] == 2e9
    assert not db.metadata("BBB")


def test_bulk_keeps_known_industry(config):
    db = seed(config)
    db.save_metadata_many([("AAA", {"market_cap": 3e9, "industry": None})], NOW + pd.Timedelta(hours=1))
    assert db.metadata("AAA")["industry"] == "Technology"


def test_history_date_does_not_fetch_current_bulk_cap(config):
    config["data"].update(metadata_batch_min_symbols=2, end="2026-10-29")
    p = BulkProvider()
    update_data(config, provider=p, now=NOW)
    assert not p.wanted and not p.individual


def test_industry_filter_uses_full_info_when_needed(config):
    config["data"]["metadata_batch_min_symbols"] = 2
    config["scanners"]["potent"]["excluded_industries"] = ["Biotechnology"]
    p = BulkProvider()
    update_data(config, provider=p, now=NOW)
    assert not p.wanted and p.individual == ["AAA", "BBB"]


def test_bulk_pagination_and_symbol_normalization(monkeypatch):
    import yfinance as yf

    calls = []

    def screen(query, **kwargs):
        calls.append(kwargs["offset"])
        assert query.operator == "IS-IN"
        assert query.operands[0] == "exchange"
        if kwargs["offset"] == 0:
            return {"total": 2, "quotes": [{"symbol": "BRK-B", "marketCap": 50e6, "exchange": "NYQ"}]}
        return {"total": 2, "quotes": [{"symbol": "AAA", "marketCap": 1e9, "exchange": "NMS"}]}

    monkeypatch.setattr(yf, "screen", screen)
    monkeypatch.setattr(YahooProvider, "seed_timezone", staticmethod(lambda *args: None))
    p = YahooProvider()
    try:
        result = [row for page in p.metadata_pages(["BRK.B", "AAA"]) for row in page]
        assert calls == [0, 1]
        assert [row[0] for row in result] == ["BRK.B", "AAA"]
        assert result[0][1]["market_cap"] == 50e6
    finally:
        p.close()


def test_repeated_page_is_not_treated_as_complete(monkeypatch):
    import yfinance as yf

    monkeypatch.setattr(
        yf, "screen", lambda *a, **k: {"total": 3, "quotes": [{"symbol": "AAA", "marketCap": 1e9}]}
    )
    monkeypatch.setattr(YahooProvider, "seed_timezone", staticmethod(lambda *args: None))
    p = YahooProvider()
    try:
        with pytest.raises(ValueError, match="重复"):
            list(p.metadata_pages(["AAA", "BBB"]))
    finally:
        p.close()


def test_timezone_seed_avoids_an_extra_yahoo_lookup(monkeypatch):
    from yfinance import cache

    store = Mock()
    monkeypatch.setattr(cache, "get_tz_cache", lambda: Mock(store=store))
    YahooProvider.seed_timezone("BRK.B", "America/New_York")
    store.assert_called_once_with("BRK-B", "America/New_York")
    YahooProvider.seed_timezone("AAA", "invalid_timezone")
    assert store.call_count == 1


def test_recent_ipo_does_not_repeat_unavailable_prelisting_dates(config):
    config["data"]["metadata_batch_min_symbols"] = 2

    class IPO(BulkProvider):
        def metadata_pages(self, symbols):
            yield [(s, {"market_cap": 2e9, "first_trade_date": "2026-10-28"}) for s in symbols]

    p = IPO()
    report = update_data(config, provider=p, now=NOW)
    assert not report.failures
    assert all(call[2] == "2026-10-28" for call in p.calls)
    assert update_data(config, provider=p, now=NOW).cached == 2
    assert len(p.calls) == 2
