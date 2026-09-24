from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def summarize(equity: pd.DataFrame, trades: pd.DataFrame) -> dict:
    if equity.empty:
        return {"status": "empty", "trades": 0}
    curve = equity["market_value"]
    total = float(curve.iloc[-1] / curve.iloc[0] - 1)
    years = max((pd.Timestamp(equity["date"].iloc[-1]) - pd.Timestamp(equity["date"].iloc[0])).days / 365.25, 1 / 365.25)
    return {"status": "ok", "start": str(equity["date"].iloc[0]), "end": str(equity["date"].iloc[-1]),
            "initial_value": float(curve.iloc[0]), "ending_value": float(curve.iloc[-1]),
            "cumulative_return": total, "annual_return": float((1 + total) ** (1 / years) - 1),
            "max_drawdown": float(equity["drawdown"].min()), "trade_count": int(len(trades)),
            "ending_positions": int(equity["positions"].iloc[-1]),
            "price_adjustment": "hfq (research approximation)"}


def write_report(equity: pd.DataFrame, trades: pd.DataFrame, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    equity.to_csv(out / "equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    summary = summarize(equity, trades)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not equity.empty:
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(10, 4.5))
            ax.plot(pd.to_datetime(equity["date"]), equity["market_value"], color="#1f77b4", linewidth=1.2)
            ax.set_title("qlib_quant equity curve (HFQ research approximation)")
            ax.set_ylabel("portfolio value")
            ax.grid(alpha=0.25)
            fig.tight_layout()
            fig.savefig(out / "equity_curve.png", dpi=140)
            plt.close(fig)
        except Exception:
            pass
    return out / "summary.json"

