import csv
import io
import re

import requests

EXCHANGES = {"Q": "NASDAQ", "N": "NYSE", "A": "AMEX"}
EXCLUDED = re.compile(r"\b(warrants?|rights?|units?|preferred|preference|debentures?|notes? due)\b", re.I)


def parse_directory(text, nasdaq=True):
    result = []
    for row in csv.DictReader(io.StringIO(text), delimiter="|"):
        symbol = row.get("Symbol") if nasdaq else row.get("ACT Symbol")
        if not symbol or symbol.startswith("File Creation"):
            continue
        name = row.get("Security Name", "")
        exchange = "NASDAQ" if nasdaq else EXCHANGES.get(row.get("Exchange"))
        if (
            not exchange
            or row.get("Test Issue") != "N"
            or row.get("ETF") == "Y"
            or row.get("NextShares") == "Y"
            or EXCLUDED.search(name)
        ):
            continue
        if any(x in symbol for x in ("$", "^", "+", "=", "/")):
            continue
        result.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "name": name,
                "source_symbol": symbol.replace(".", "-"),
                "instrument_type": "adr"
                if re.search(r"\b(ADR|ADS|American Depositary)\b", name, re.I)
                else "common",
            }
        )
    return result


class NasdaqDirectory:
    def fetch(self):
        records = {}
        for filename, nasdaq in (("nasdaqlisted.txt", True), ("otherlisted.txt", False)):
            response = requests.get(f"https://www.nasdaqtrader.com/dynamic/SymDir/{filename}", timeout=30)
            response.raise_for_status()
            if "Security Name|" not in response.text:
                raise ValueError("Nasdaq 股票目录格式错误，拒绝覆盖已有股票池")
            rows = parse_directory(response.text, nasdaq)
            if not rows:
                raise ValueError("Nasdaq 返回空股票目录")
            records.update({r["symbol"]: r for r in rows})
        return sorted(records.values(), key=lambda r: r["symbol"])
