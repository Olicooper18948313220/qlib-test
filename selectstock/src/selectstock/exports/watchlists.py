from pathlib import Path

import pandas as pd

from ..contracts import RULE_VERSION
from ..data.database import json_text


def export_watchlists(results, output_dir=None):
    root = Path(output_dir or results.config["data"]["output_dir"]) / results.run_id
    root.mkdir(parents=True, exist_ok=False)
    selected = []
    for name, frame in results.frames.items():
        frame.to_csv(root / f"{name}_diagnostics.csv", index=False)
        picks = results.selected(name)
        picks.to_csv(root / f"{name}.csv", index=False)
        selected.append(picks)
        write_txt(picks, root / f"{name}.txt")
    combined = (
        pd.concat(selected, ignore_index=True).drop_duplicates("symbol")
        if selected
        else pd.DataFrame(columns=["symbol", "exchange"])
    )
    combined.to_csv(root / "combined.csv", index=False)
    unresolved = write_txt(combined, root / "combined.txt")
    metadata = {
        "run_id": results.run_id,
        "asof": results.asof,
        "rule_version": RULE_VERSION,
        "coverage": results.coverage,
        "config": results.config,
        "unresolved_exchange_symbols": unresolved,
        "sources": [
            "https://martinluk.com/screeners/",
            "https://www.youtube.com/watch?v=iMBI3ZcQ6sU&t=4651s",
        ],
    }
    (root / "metadata.json").write_text(json_text(metadata), encoding="utf-8")
    return root


def write_txt(frame, path):
    resolved, missing = [], []
    for row in frame.to_dict("records"):
        if row.get("exchange") in {"NASDAQ", "NYSE", "AMEX"}:
            resolved.append(f"{row['exchange']}:{row['symbol']}")
        else:
            missing.append(row["symbol"])
    path.write_text(",".join(resolved), encoding="utf-8")
    return missing
