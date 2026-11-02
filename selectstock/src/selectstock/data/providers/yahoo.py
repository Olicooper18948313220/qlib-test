import pandas as pd
import yfinance as yf

from ..calendar import NY, calendar, session_close, session_label

EXCHANGE_MAP = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYQ": "NYSE",
    "ASE": "AMEX",
    "PCX": "AMEX",
}


class YahooProvider:
    def __init__(self, include_extended_hours=True):
        self.include_extended_hours = include_extended_hours

    def history(self, symbol, timeframe, start, end):
        frame = yf.Ticker(symbol.replace(".", "-")).history(
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
        info = yf.Ticker(symbol.replace(".", "-")).get_info()
        return {
            "market_cap": info.get("marketCap"),
            "industry": info.get("industry"),
            "exchange": EXCHANGE_MAP.get(info.get("exchange"), ""),
            "quote_type": info.get("quoteType"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "source": "yahoo",
        }
