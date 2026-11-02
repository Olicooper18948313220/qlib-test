import pandas as pd
import pytest

from selectstock.config import load_config
from selectstock.data.calendar import session_close, sessions
from selectstock.data.database import Database

NOW = pd.Timestamp("2026-10-31T02:00:00Z")


@pytest.fixture
def config(tmp_path):
    return load_config(
        overrides={
            "data": {
                "database": str(tmp_path / "market.db"),
                "output_dir": str(tmp_path / "runs"),
                "symbols": ["AAA", "BBB"],
                "end": "2026-10-30",
                "history_days": {"1d": 400},
                "request_pause_seconds": 0,
                "retry_backoff_seconds": 0,
                "retries": 1,
            }
        }
    )


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.empty = False
        self.split = None
        self.scale = 1

    def metadata(self, symbol):
        return {"market_cap": 2_000_000_000, "industry": "Technology", "exchange": "NASDAQ"}

    def history(self, symbol, timeframe, start, end):
        self.calls.append((symbol, timeframe, start, end))
        if self.fail:
            raise ConnectionError("network unavailable")
        days = sessions(start, (pd.Timestamp(end) - pd.Timedelta(days=1)).date())
        if self.empty:
            return pd.DataFrame()
        frame = bars(days, timeframe, self.scale)
        if self.split and start <= self.split < end:
            frame.attrs["actions"] = [{"day": self.split, "split": 2.0, "dividend": 0.0}]
        return frame


def bars(days, timeframe="1d", scale=1):
    rows = []
    for day in days:
        price = (50 + (pd.Timestamp(day) - pd.Timestamp("2025-01-01")).days / 30) * scale
        rows.append(
            {
                "ts": session_close(day),
                "session_date": day,
                "session": "regular",
                "open": price * 0.99,
                "high": price * 1.03,
                "low": price * 0.97,
                "close": price,
                "volume": 2_000_000.0,
                "is_final": 1,
                "adjustment": "yahoo_split_adjusted_no_dividend",
                "source": "test",
            }
        )
    return pd.DataFrame(rows)


def seed(config, end="2026-10-30", count=300):
    db = Database(config["data"]["database"])
    db.initialize()
    records = [
        {
            "symbol": name,
            "exchange": "NASDAQ",
            "name": name,
            "source_symbol": name,
            "instrument_type": "common",
        }
        for name in config["data"]["symbols"]
    ]
    db.save_universe(records, NOW)
    days = sessions("2025-01-01", end)[-count:]
    for symbol in config["data"]["symbols"]:
        db.commit_history(symbol, "1d", bars(days), [(day, "complete", "") for day in days])
        db.save_metadata(symbol, {"market_cap": 2_000_000_000, "industry": "Technology"}, NOW)
    return db
