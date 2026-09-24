from __future__ import annotations

import pandas as pd


def select_equal_weight(day: pd.DataFrame, max_positions: int = 20) -> pd.DataFrame:
    """Select highest scoring stocks, with deterministic code tie-break."""
    selected = day[day["signal"]].sort_values(["signal_score", "code"], ascending=[False, True]).head(max_positions).copy()
    if selected.empty:
        return selected.assign(target_weight=pd.Series(dtype=float))
    selected["target_weight"] = 1.0 / len(selected)
    return selected

