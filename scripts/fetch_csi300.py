"""Download CSI300 (沪深300) daily bars from Tencent and write a benchmark CSV.

Stdlib-only; run once to populate data/benchmark/csi300.csv, or re-run to refresh:
    python scripts/fetch_csi300.py [start] [end]
Defaults cover the source DB range (2018-07-02 .. 2024-12-31).

Tencent caps the per-request count, so the range is fetched in chunks and merged.
For an index the rows live under the ``day`` key (no adjustment applies).
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

INDEX_CODE = "sh000300"  # 沪深300
URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,{start},{end},1500,qfq"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
CHUNK_DAYS = 1000  # ~1000 calendar days per request, safely under the count cap


def fetch(code: str, start: str, end: str) -> list[list[str]]:
    start_d = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    all_rows: list[list[str]] = []
    seen: set[str] = set()
    chunk_start = start_d
    while chunk_start <= end_d:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end_d)
        req = urllib.request.Request(
            URL.format(code=code, start=chunk_start.strftime("%Y-%m-%d"),
                       end=chunk_end.strftime("%Y-%m-%d")),
            headers=HEADERS,
        )
        payload = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
        data = payload.get("data")
        node = data.get(code, {}) if isinstance(data, dict) else {}
        for r in node.get("qfqday") or node.get("day") or []:
            if r[0] not in seen:
                seen.add(r[0])
                all_rows.append(r)
        chunk_start = chunk_end + timedelta(days=1)
    all_rows.sort(key=lambda r: r[0])
    return all_rows


def main(argv: list[str]) -> int:
    start = argv[1] if len(argv) > 1 else "2018-07-02"
    end = argv[2] if len(argv) > 2 else "2024-12-31"
    rows = fetch(INDEX_CODE, start, end)
    out = Path(__file__).resolve().parents[1] / "data" / "benchmark" / "csi300.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "open", "close", "high", "low", "volume"])
        for r in rows:  # Tencent order: [date, open, close, high, low, volume]
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5]])
    print(f"wrote {len(rows)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
