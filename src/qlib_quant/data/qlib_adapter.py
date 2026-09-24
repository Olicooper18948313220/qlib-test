"""Bridge normalized CSV rows to Qlib's ordinary OHLCV convention.

The adapter writes a portable CSV first. Installing a Qlib version can then
convert it with the matching Qlib data tool without changing the source DB.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .db import iter_quotes


QLIB_COLUMNS = ["date", "instrument", "$open", "$high", "$low", "$close", "$volume", "$amount"]


def export_qlib_csv(database: str, output_dir: str | Path, start: str, end: str, max_rows: int = 0) -> Path:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    path = out / f"qlib_features_{start.replace('-', '')}_{end.replace('-', '')}.csv"
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=QLIB_COLUMNS)
        writer.writeheader()
        for item in iter_quotes(database, start, end):
            writer.writerow({"date": item["date"], "instrument": item["code"], "$open": item["open"],
                             "$high": item["high"], "$low": item["low"], "$close": item["close"],
                             "$volume": item["volume"], "$amount": item["amount"]})
            count += 1
            if max_rows and count >= max_rows:
                break
    (out / "README.txt").write_text(
        "This is the normalized Qlib input CSV. Prices are HFQ; volume and amount are unadjusted.\n"
        "Use the data conversion command that ships with your installed Qlib version to create .bin files.\n",
        encoding="utf-8")
    return path


def handler_from_csv(path: str | Path):
    """Create a Qlib ``DataHandlerLP`` from a normalized CSV sample.

    This keeps the first migration easy to inspect. For the full universe,
    convert the CSV to Qlib's binary provider after the data audit is signed
    off; the signal and factor code does not change.
    """
    import pandas as pd
    from qlib.data.dataset import DataHandlerLP
    df = pd.read_csv(path, parse_dates=["date"])
    df["instrument"] = df["instrument"].astype(str).str.zfill(6)
    df = df.set_index(["instrument", "date"]).sort_index()
    return DataHandlerLP.from_df(df)

