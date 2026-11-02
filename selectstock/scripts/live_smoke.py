"""Small opt-in network check, independent of deterministic unit tests."""

import json
from pathlib import Path

from selectstock.config import load_config
from selectstock.runner import export_watchlists, load_bars, run_scans, update_data

ROOT = Path(__file__).resolve().parents[1]
config = load_config(
    ROOT,
    {
        "data": {
            "database": str(ROOT / "data" / "live_smoke.sqlite3"),
            "symbols": ["AAPL"],
            "timeframes": ["1d"],
            "retries": 2,
        },
        "watchlist": {"symbols": ["AAPL"]},
    },
)
daily = update_data(config)
scans = run_scans(config)
config["data"]["timeframes"] = ["15m", "5m", "1m"]
minute = update_data(config)
counts = {tf: len(load_bars("AAPL", tf, limit=100000, config=config)) for tf in ("1d", "15m", "5m", "1m")}
summary = {
    "daily": daily.to_dict(),
    "minute": minute.to_dict(),
    "rows": counts,
    "coverage": scans.coverage,
    "output": str(export_watchlists(scans)),
}
path = ROOT / "runs" / "live_smoke_summary.json"
path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
if daily.failures or minute.failures or daily.metadata_failures or not all(counts.values()):
    raise SystemExit(1)
