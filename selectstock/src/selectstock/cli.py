import argparse
import json
from pathlib import Path

from .config import load_config, load_experiment
from .data.database import Database
from .runner import check_data, export_watchlists, run_scans, update_data


def main():
    parser = argparse.ArgumentParser(description="美股本地数据与 Martin Luk 扫描器")
    parser.add_argument("command", choices=["init-db", "update", "scan", "export", "check", "demo"])
    parser.add_argument("--config", help="Notebook 保存的完整实验 YAML")
    parser.add_argument("--database")
    parser.add_argument("--symbols", help="日线股票子集，逗号分隔")
    parser.add_argument("--watchlist", help="分钟线自选股，逗号分隔")
    parser.add_argument("--timeframes")
    parser.add_argument("--end")
    parser.add_argument("--enabled", help="本次执行的扫描器，逗号分隔")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--repair", action="store_true", help="显式重取所选区间，修复历史修订")
    args = parser.parse_args()
    config = load_experiment(args.config) if args.config else load_config()
    if args.database:
        config["data"]["database"] = str(Path(args.database).resolve())
    for key in ("symbols", "timeframes"):
        if getattr(args, key) is not None:
            config["data"][key] = [x.strip() for x in getattr(args, key).split(",") if x.strip()]
    if args.watchlist:
        config["watchlist"]["symbols"] = args.watchlist.split(",")
    if args.end:
        config["data"]["end"] = args.end
    if args.offline:
        config["data"]["offline"] = True
    if args.command == "init-db":
        Database(config["data"]["database"]).initialize()
        result = {"database": config["data"]["database"]}
    elif args.command == "update":
        result = update_data(config, repair=args.repair).to_dict()
    elif args.command == "check":
        result = check_data(config)
    else:
        if args.command == "demo":
            from .demo import create_demo_config

            config = create_demo_config(database=args.database)
        scan = run_scans(config, args.enabled.split(",") if args.enabled else None)
        result = {"asof": scan.asof, "coverage": scan.coverage, "output": str(export_watchlists(scan))}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "update" and (result["failures"] or result["metadata_failures"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
