from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _database(args):
    return args.database or str(Path("D:/quanttrade20260207/data/stockdata/daily_hfq_repaired.db"))


def main(argv=None):
    p = argparse.ArgumentParser(description="qlib_quant 本地研究入口")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate"); v.add_argument("--database")
    e = sub.add_parser("export"); e.add_argument("--database"); e.add_argument("--start", default="2018-07-02"); e.add_argument("--end", default="2024-12-31")
    e.add_argument("--output", default="data/curated")
    q = sub.add_parser("qlib-export"); q.add_argument("--database"); q.add_argument("--start", default="2018-07-02"); q.add_argument("--end", default="2024-12-31"); q.add_argument("--output", default="data/qlib"); q.add_argument("--max-rows", type=int, default=0)
    b = sub.add_parser("backtest"); b.add_argument("--database"); b.add_argument("--start", default="2020-01-01"); b.add_argument("--end", default="2024-12-31"); b.add_argument("--max-rows", type=int, default=0)
    b.add_argument("--output", default="runs/latest")
    args = p.parse_args(argv)
    if args.command == "validate":
        from .data.db import validate_database
        manifest = validate_database(_database(args)); manifest.write(ROOT / "data/curated/data_manifest.json")
        print(json.dumps(manifest.__dict__, ensure_ascii=False, indent=2)); return 0
    if args.command == "export":
        from .data.export import export_csv, write_manifest
        print(f"manifest: {write_manifest(_database(args), ROOT / 'data/curated')}")
        print(f"csv: {export_csv(_database(args), ROOT / args.output, args.start, args.end)}"); return 0
    if args.command == "qlib-export":
        from .data.qlib_adapter import export_qlib_csv
        print(export_qlib_csv(_database(args), ROOT / args.output, args.start, args.end, args.max_rows)); return 0
    if args.command == "backtest":
        import pandas as pd
        from .data.db import iter_quotes
        from .factors.technical import add_technical_features
        from .signals.legacy_v1 import score_signals
        from .backtest.simple import run_backtest
        from .reports import write_report
        rows = list(iter_quotes(_database(args), args.start, args.end))
        df = pd.DataFrame(rows).rename(columns={"stock_name": "name"})
        if args.max_rows: df = df.head(args.max_rows)
        df = add_technical_features(df); df = score_signals(df)
        equity, trades = run_backtest(df)
        out = ROOT / args.output; write_report(equity, trades, out)
        metadata = {"database": _database(args), "start": args.start, "end": args.end,
                    "rows_loaded": int(len(df)), "signal_version": "legacy_v1",
                    "price_adjustment": "hfq", "research_approximation": True,
                    "qlib_version": __import__("qlib").__version__}
        (out / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"rows": len(df), "output": str(out), "trades": len(trades)}, ensure_ascii=False)); return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

