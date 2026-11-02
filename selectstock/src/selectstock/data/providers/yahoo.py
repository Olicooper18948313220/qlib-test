from functools import lru_cache
from zoneinfo import ZoneInfo

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

    def metadata_pages(self, symbols):
        """Page US listing exchanges without price, market-cap or volume exclusions."""
        wanted = {symbol.replace(".", "-").upper(): symbol for symbol in symbols}
        query = yf.EquityQuery("is-in", ["exchange", "NMS", "NGM", "NCM", "NYQ", "ASE"])
        offset = 0
        seen = set()
        while wanted:
            result = self._call(
                lambda: yf.screen(
                    query, size=250, offset=offset, sortField="ticker", sortAsc=True, session=self.session
                )
            )
            quotes = result.get("quotes", [])
            total = result.get("total")
            if not isinstance(total, int) or total < 0:
                raise ValueError("批量报价响应缺少 total")
            if not quotes:
                if offset < total:
                    raise ValueError("批量报价在分页完成前返回空页")
                break
            page = []
            new_symbols = {q.get("symbol") for q in quotes} - seen
            if not new_symbols:
                raise ValueError("批量报价重复返回同一页")
            seen.update(new_symbols)
            for quote in quotes:
                key = str(quote.get("symbol", "")).replace(".", "-").upper()
                symbol = wanted.pop(key, None)
                if symbol is None:
                    continue
                cap = quote.get("marketCap")
                if not isinstance(cap, (int, float)) or not pd.notna(cap) or cap <= 0:
                    continue
                timezone = quote.get("exchangeTimezoneName")
                self.seed_timezone(symbol, timezone)
                first_trade_date = None
                first = quote.get("firstTradeDateMilliseconds")
                if isinstance(first, (int, float)) and pd.notna(first) and first > 0:
                    try:
                        first_trade_date = (
                            pd.Timestamp(first, unit="ms", tz="UTC")
                            .tz_convert(timezone or NY)
                            .date()
                            .isoformat()
                        )
                    except (ValueError, OverflowError, KeyError):
                        pass
                page.append(
                    (
                        symbol,
                        {
                            "market_cap": cap,
                            "exchange": EXCHANGE_MAP.get(quote.get("exchange"), ""),
                            "industry": quote.get("industry"),
                            "quote_type": quote.get("quoteType"),
                            "shares_outstanding": quote.get("sharesOutstanding"),
                            "exchange_timezone": timezone,
                            "first_trade_date": first_trade_date,
                            "source": "yahoo_bulk_quote",
                        },
                    )
                )
            if page:
                yield page
            offset += len(quotes)
            if offset >= total:
                break

    @staticmethod
    def seed_timezone(symbol, timezone):
        if not timezone:
            return
        try:
            ZoneInfo(timezone)
        except (ValueError, KeyError, TypeError):
            return
        from yfinance import cache

        cache.get_tz_cache().store(symbol.replace(".", "-").upper(), timezone)

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
