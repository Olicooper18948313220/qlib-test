from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _database(args):
    return args.database or str(Path("D:/quanttrade20260207/data/stockdata/daily_hfq_repaired.db"))


def _resolve(p: str) -> Path:
    """Resolve a CLI path relative to the repo root when not absolute."""
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def main(argv=None):
    p = argparse.ArgumentParser(description="qlib_quant 本地研究入口")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate"); v.add_argument("--database")
    e = sub.add_parser("export"); e.add_argument("--database"); e.add_argument("--start", default="2018-07-02"); e.add_argument("--end", default="2024-12-31")
    e.add_argument("--output", default="data/curated")
    q = sub.add_parser("qlib-export"); q.add_argument("--database"); q.add_argument("--start", default="2018-07-02"); q.add_argument("--end", default="2024-12-31"); q.add_argument("--output", default="data/qlib"); q.add_argument("--max-rows", type=int, default=0)
    b = sub.add_parser("backtest"); b.add_argument("--database"); b.add_argument("--start", default="2020-01-01"); b.add_argument("--end", default="2024-12-31"); b.add_argument("--max-rows", type=int, default=0)
    b.add_argument("--output", default="runs/latest")
    b.add_argument("--config-dir", default="configs")
    b.add_argument("--benchmark-csv", default="data/benchmark/csi300.csv")
    b.add_argument("--no-benchmark", action="store_true")
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
        from .config import load_portfolio, load_signals, load_factors, portfolio_weekday
        from .benchmark import load_index_csv, align_benchmark, compute_metrics

        config_dir = _resolve(args.config_dir)
        pf = load_portfolio(config_dir)
        sig = load_signals(config_dir)
        fac = load_factors(config_dir)

        windows = tuple(fac.get("windows", [5, 10, 20, 30, 60, 120, 250]))
        thresholds = sig.get("thresholds", {}) or {}
        enabled = sig.get("enabled")

        # Load a warm-up window before ``--start`` so the 250-period rolling
        # features have history; trading still only starts at ``--start``.
        warmup_start = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        rows = list(iter_quotes(_database(args), warmup_start, args.end))
        df = pd.DataFrame(rows).rename(columns={"stock_name": "name"})
        if args.max_rows: df = df.head(args.max_rows)
        df = add_technical_features(df, windows=windows)
        df = score_signals(df, thresholds=thresholds, enabled=enabled)
        equity, trades = run_backtest(
            df, start=args.start,
            initial_cash=pf.get("initial_cash", 1_000_000),
            max_positions=pf.get("max_positions", 20),
            commission_rate=pf.get("commission_rate", 0.0003),
            stamp_duty_rate=pf.get("stamp_duty_rate", 0.001),
            slippage_bps=pf.get("slippage_bps", 5),
            rebalance_weekday=portfolio_weekday(pf),
        )

        benchmark_metrics = None
        benchmark_curve = None
        bench_source = None
        if not args.no_benchmark:
            bench_path = _resolve(args.benchmark_csv)
            if bench_path.exists():
                bench = load_index_csv(bench_path)
                aligned = align_benchmark(equity["date"], bench)
                if len(aligned) >= 2:
                    benchmark_metrics = compute_metrics(equity, aligned)
                    benchmark_curve = aligned.rename("close").reset_index().rename(columns={"index": "date"})
                    bench_source = str(bench_path)
                else:
                    print("warning: 基准与回测区间对齐后不足 2 个交易日，跳过基准指标")
            else:
                print(f"warning: 未找到基准文件 {bench_path}（可运行 python scripts/fetch_csi300.py 生成）")

        out = _resolve(args.output)
        write_report(equity, trades, out, benchmark_metrics=benchmark_metrics, benchmark_curve=benchmark_curve)

        try:
            qlib_version = __import__("qlib").__version__
        except Exception:
            qlib_version = "not-installed"
        metadata = {"database": _database(args), "start": args.start, "end": args.end,
                    "rows_loaded": int(len(df)), "signal_version": sig.get("version", "legacy_v1"),
                    "price_adjustment": "hfq", "research_approximation": True,
                    "config_dir": str(config_dir), "benchmark": bench_source,
                    "qlib_version": qlib_version}
        (out / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"rows": len(df), "output": str(out), "trades": len(trades),
                          "benchmark": benchmark_metrics}, ensure_ascii=False)); return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
