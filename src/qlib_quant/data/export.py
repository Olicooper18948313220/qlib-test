from __future__ import annotations

import csv
import json
from pathlib import Path

from .db import iter_quotes, validate_database


def export_csv(database: str, output_dir: str | Path, start: str, end: str) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"daily_quote_{start.replace('-', '')}_{end.replace('-', '')}.csv"
    fields = ["date", "code", "name", "open", "high", "low", "close", "volume", "amount", "turnover_rate", "trade_status"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in iter_quotes(database, start, end):
            item["name"] = item.pop("stock_name", "")
            writer.writerow(item)
    return path


def write_manifest(database: str, output_dir: str | Path) -> Path:
    manifest = validate_database(database)
    path = Path(output_dir) / "data_manifest.json"
    manifest.write(path)
    return path

