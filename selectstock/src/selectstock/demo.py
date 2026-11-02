"""Deterministic synthetic data for learning and offline notebook acceptance."""

from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from .config import load_config
from .data.calendar import calendar, latest_closed, session_close, sessions
from .data.database import Database


def create_demo_config(root=None, database=None):
    config = load_config(root)
    end = latest_closed()
    path = Path(database) if database else Path(config["root"]) / "data" / f"demo_{uuid4().hex[:8]}.sqlite3"
    config["data"].update(
        database=str(path), offline=True, end=end, symbols=["DEMOA", "DEMOB", "DEMOC"], demo=True
    )
    db = Database(path)
    db.initialize()
    days = sessions(pd.Timestamp(end) - pd.Timedelta(days=600), end)[-300:]
    records = [
        {
            "symbol": name,
            "exchange": "NASDAQ",
            "name": "SYNTHETIC DEMO " + name,
            "source_symbol": name,
            "instrument_type": "synthetic",
        }
        for name in config["data"]["symbols"]
    ]
    db.save_universe(records)
    for n, symbol in enumerate(config["data"]["symbols"]):
        close = np.linspace(20, 60 if n < 2 else 15, len(days))
        rows = [
            {
                "ts": session_close(day),
                "session_date": day,
                "session": "regular",
                "open": float(c * 0.99),
                "high": float(c * 1.03),
                "low": float(c * 0.97),
                "close": float(c),
                "volume": 2_000_000.0 if n < 2 else 100_000.0,
                "is_final": 1,
                "adjustment": "synthetic",
                "source": "synthetic",
            }
            for day, c in zip(days, close)
        ]
        db.commit_history(symbol, "1d", pd.DataFrame(rows), [(day, "complete", "demo") for day in days])
        db.save_metadata(
            symbol,
            {
                "market_cap": 2_000_000_000 if n < 2 else 20_000_000,
                "industry": "Synthetic",
                "source": "synthetic",
            },
        )
        for tf, minutes in (("1m", 1), ("5m", 5), ("15m", 15)):
            rows = []
            for ts in pd.date_range(
                calendar().session_open(end), session_close(end), freq=f"{minutes}min", inclusive="left"
            ):
                rows.append(
                    {
                        "ts": ts,
                        "session_date": end,
                        "session": "regular",
                        "open": 60.0,
                        "high": 60.5,
                        "low": 59.5,
                        "close": 60.2,
                        "volume": 10000.0 * minutes,
                        "is_final": 1,
                        "adjustment": "synthetic",
                        "source": "synthetic",
                    }
                )
            db.commit_history(symbol, tf, pd.DataFrame(rows), [(end, "complete", "demo")])
    return config
