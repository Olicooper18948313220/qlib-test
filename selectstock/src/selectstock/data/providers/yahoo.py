from functools import lru_cache

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from ..calendar import NY, calendar, session_close, session_label
from ..download_guard import DownloadPaused, guarded_session

EXCHANGE_MAP = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYQ": "NYSE",
    "ASE": "AMEX",
    "PCX": "AMEX",
}


class YahooProvider:
    def __init__(self, include_extended_hours=True, guard=None):
        self.include_extended_hours = include_extended_hours
        self.guard = guard
        self.session = guarded_session(guard) if guard is not None else None

    @lru_cache(maxsize=64)
    def _ticker(self, symbol):
        return yf.Ticker(symbol.replace(".", "-"), session=self.session)

    def _call(self, function):
        if self.guard:
            self.guard.check()
        try:
            result = function()
        except DownloadPaused:
            raise
        except YFRateLimitError:
            if self.guard:
                self.guard.pause("Yahoo 返回限流异常，下载已暂停")
            raise
        except Exception:
            if self.guard:
                self.guard.check()
            raise
        if self.guard:
            # yfinance may suppress a transport exception; preserve the guard's stop decision.
            self.guard.check()
        return result

    def close(self):
        self._ticker.cache_clear()
        if self.session is not None:
            self.session.close()

    def history(self, symbol, timeframe, start, end):
        frame = self._call(
            lambda: self._ticker(symbol).history(
                start=start,
                end=end,
                interval=timeframe,
                auto_adjust=False,
                back_adjust=False,
                actions=True,
                prepost=self.include_extended_hours if timeframe != "1d" else False,
                repair=False,
                timeout=30,
            )
        )
        rows, actions = [], []
        for ts, row in frame.iterrows():
            ts = pd.Timestamp(ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize(NY)
            day = ts.tz_convert(NY).date().isoformat()
            if not calendar().is_session(day):
                continue
            split, dividend = float(row.get("Stock Splits", 0)), float(row.get("Dividends", 0))
            if split or dividend:
                actions.append({"day": day, "split": split, "dividend": dividend})
            rows.append(
                {
                    "ts": session_close(day) if timeframe == "1d" else ts.tz_convert("UTC"),
                    "session_date": day,
                    "session": "regular" if timeframe == "1d" else session_label(ts, day),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                    "is_final": 1,
                    "adjustment": "yahoo_split_adjusted_no_dividend",
                    "source": "yahoo",
                }
            )
        result = pd.DataFrame(rows)
        result.attrs["actions"] = actions
        return result

    def metadata(self, symbol):
        info = self._call(lambda: self._ticker(symbol).get_info())
        return {
            "market_cap": info.get("marketCap"),
            "industry": info.get("industry"),
            "exchange": EXCHANGE_MAP.get(info.get("exchange"), ""),
            "quote_type": info.get("quoteType"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "source": "yahoo",
        }
