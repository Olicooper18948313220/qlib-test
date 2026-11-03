import pytest
from conftest import NOW, bars, seed

from selectstock.data.calendar import latest_closed, sessions, week_info
from selectstock.indicators import compute_metrics
from selectstock.runner import run_scans
from selectstock.scanners import REGISTRY
from selectstock.scanners.base import compare


@pytest.mark.parametrize(
    "value,op,expected",
    [
        (100, ">", False),
        (100, ">=", True),
        (99, "<", True),
        (100, "<=", True),
        (None, ">", None),
        (float("nan"), ">", None),
    ],
)
def test_comparison_boundary(value, op, expected):
    assert compare(value, 100, op) is expected


def metrics():
    return {
        "price": 100.0,
        "market_cap": 2e9,
        "ema_fast": 100.0,
        "ema_slow": 90.0,
        "volatility_1w": 5.0,
        "day_turnover": 1e8,
        "open_change_pct": 1.0,
        "industry": "Technology",
    }


@pytest.mark.parametrize(
    "change,expected", [(-1, "passed"), (1, "passed"), (-0.5, "failed"), (0.5, "failed"), (0, "failed")]
)
def test_potent_both_sides(config, change, expected):
    values = metrics()
    values["open_change_pct"] = change
    assert REGISTRY["potent"].evaluate(values, config["scanners"]["potent"], [])["status"] == expected


def test_ema_equality_and_cap(config):
    values = metrics()
    settings = config["scanners"]["potent"]
    assert REGISTRY["potent"].evaluate(values, settings, [])["status"] == "passed"
    values["ema_slow"] = 100
    assert REGISTRY["potent"].evaluate(values, settings, [])["status"] == "failed"
    values["ema_slow"] = 90
    values["market_cap"] = 1e8
    assert REGISTRY["potent"].evaluate(values, settings, [])["status"] == "failed"


def test_tradingview_anchor_and_week_turnover(config):
    frame = bars(sessions("2026-09-01", "2026-10-30"))
    m = compute_metrics(frame, config["scanners"]["leader"], config["scanners"]["potent"])
    past = frame.loc[frame.session_date.eq("2026-09-30")].iloc[0]
    assert m["performance_anchor"] == "2026-09-30"
    assert m["performance_1m"] == pytest.approx((frame.iloc[-1]["close"] / past["open"] - 1) * 100)
    assert m["week_turnover"] == frame.iloc[-1]["close"] * 10_000_000
    sample = frame.loc[frame.session_date.gt("2026-10-23")]
    assert m["volatility_1w"] == pytest.approx(((sample.high - sample.low) / sample.low * 100).mean())
    assert m["week_complete"] is True


def test_calendar_holiday_dst_and_early_close():
    assert "2026-11-26" not in sessions("2026-11-25", "2026-11-27")
    assert week_info("2026-11-27")[1] is True
    assert latest_closed("2026-11-27T18:31:00Z") == "2026-11-27"
    assert latest_closed("2026-11-27T18:10:00Z") == "2026-11-25"
    assert latest_closed("2026-10-30T20:31:00Z") == "2026-10-30"
    assert latest_closed("2026-11-02T21:31:00Z") == "2026-11-02"


def test_month_anchor_on_weekend(config):
    frame = bars(sessions("2026-08-01", "2026-10-05"))
    m = compute_metrics(frame, config["scanners"]["leader"])
    assert m["performance_anchor"] == "2026-09-04"  # Sep 5 was Saturday


def test_warmup_stale_and_historical_metadata(config):
    seed(config, count=20)
    result = run_scans(config, now=NOW)
    assert result.frames["potent"].status.eq("skipped").all()
    assert result.frames["potent"].reasons.str.contains("warmup").all()
    config["data"]["end"] = "2026-10-29"
    result = run_scans(config, now=NOW)
    assert result.frames["leader"].reasons.str.contains("point_in_time").all()


def test_disabled_scanner_does_not_compute(config, monkeypatch):
    seed(config)
    config["scanners"]["potent"]["enabled"] = False

    def fail(*args):
        raise AssertionError("disabled scanner called")

    monkeypatch.setattr(REGISTRY["potent"], "evaluate", fail)
    result = run_scans(config, now=NOW)
    assert list(result.frames) == ["leader"]
    assert result.frames["leader"].status.eq("passed").all()


def test_completed_week_and_bounded_query(config, monkeypatch):
    from selectstock.data.database import Database

    seed(config)
    config["data"]["end"] = "2026-10-28"
    config["scanners"]["leader"]["week_policy"] = "completed"
    original = Database.load_bars

    def bounded(self, *args, **kwargs):
        assert kwargs["limit"] <= 300
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Database, "load_bars", bounded)
    result = run_scans(config, enabled=["leader"], now=NOW)
    assert result.frames["leader"]["asof"].eq("2026-10-23").all()
    assert result.frames["leader"].week_complete.all()
