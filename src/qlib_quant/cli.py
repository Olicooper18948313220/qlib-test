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
    b = sub.add_parser("backtest")
    b.add_argument("--request", help="由网页任务生成的 JSON 请求")
    b.add_argument("--database"); b.add_argument("--start"); b.add_argument("--end")
    b.add_argument("--max-rows", type=int); b.add_argument("--max-stocks", type=int)
    b.add_argument("--config", help="本次参数覆盖 YAML，顶层为 data/backtest/signals/factors/evaluation")
    b.add_argument("--output")
    b.add_argument("--initial-cash", type=float); b.add_argument("--max-positions", type=int)
    b.add_argument("--rebalance-weekday", type=int); b.add_argument("--commission-rate", type=float)
    b.add_argument("--stamp-duty-rate", type=float); b.add_argument("--slippage-bps", type=float)
    b.add_argument("--benchmark-csv", help="本地基准 CSV（必需列 date,close）")
    b.add_argument("--benchmark-name"); b.add_argument("--benchmark-source")
    b.add_argument("--benchmark-return-type", choices=("total", "price"))
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
        from .runner import run_request
        if args.request:
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        else:
            request = {"database": args.database, "start": args.start, "end": args.end,
                       "max_rows": args.max_rows, "max_stocks": args.max_stocks, "output": args.output}
            bt = {key: value for key, value in {
                "initial_cash": args.initial_cash, "max_positions": args.max_positions,
                "rebalance_weekday": args.rebalance_weekday, "commission_rate": args.commission_rate,
                "stamp_duty_rate": args.stamp_duty_rate, "slippage_bps": args.slippage_bps,
            }.items() if value is not None}
            if args.config:
                import yaml
                request["config"] = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
            request.setdefault("config", {}).setdefault("backtest", {}).update(bt)
        if args.benchmark_csv:
            config = request.setdefault("config", {})
            benchmark = config.setdefault("evaluation", {}).setdefault("benchmark", {})
            benchmark.update({"path": str(Path(args.benchmark_csv).resolve()),
                              "name": args.benchmark_name or benchmark.get("name", ""),
                              "source": args.benchmark_source or benchmark.get("source", ""),
                              "return_type": args.benchmark_return_type or benchmark.get("return_type", "total")})
        if args.request and Path(args.request).parent.parent.name == "jobs":
            job = Path(args.request).resolve().parent
            out = run_request(request, run_dir=job / "result", job_dir=job)
        else:
            out = run_request(request)
        from .experiments import read_json
        result = {"output": str(out), "state": read_json(out / "status.json").get("state")}
        if (out / "summary.json").exists():
            result["summary"] = str(out / "summary.json")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
