import json

import numpy as np
import pandas as pd

from .calendar import utc


def metadata_fresh(snapshot, asof, now, ttl_days):
    if not snapshot or snapshot.get("market_cap") is None:
        return False
    observed = utc(snapshot["observed_at"])
    if observed > now:
        return False
    if now - observed < pd.Timedelta(days=ttl_days):
        return True
    return json.loads(snapshot.get("payload") or "{}").get("session_asof") == asof


def validate_bars(frame):
    if frame.empty:
        return frame
    required = ["open", "high", "low", "close", "volume"]
    values = frame[required].astype(float)
    valid = (
        np.isfinite(values).all(axis=1)
        & values[["open", "high", "low", "close"]].gt(0).all(axis=1)
        & values["volume"].ge(0)
        & values["high"].ge(values[["open", "close", "low"]].max(axis=1))
        & values["low"].le(values[["open", "close", "high"]].min(axis=1))
    )
    if not valid.all() or frame["ts"].duplicated().any():
        raise ValueError(f"无效 OHLCV 或重复时间戳: {int((~valid).sum())} 行")
    if frame["ts"].isna().any():
        raise ValueError("行情时间戳缺失")
    return frame.sort_values("ts").reset_index(drop=True)


def data_issues(frame, asof, required):
    issues = []
    if frame.empty:
        return ["missing_daily_bars"]
    if frame.iloc[-1]["session_date"] != asof:
        issues.append("stale_daily_bars")
    if len(frame) < required:
        issues.append(f"insufficient_warmup:{len(frame)}/{required}")
    if not frame["is_final"].astype(bool).all():
        issues.append("unfinished_bars")
    if frame["adjustment"].nunique() != 1:
        issues.append("mixed_price_basis")
    return issues


def partition_bars(frame):
    """Keep valid evidence; isolate bad/conflicting rows without fabricating prices."""
    if frame.empty:
        return frame, []
    frame = frame.drop_duplicates().copy()
    conflicts = frame["ts"].duplicated(keep=False)
    good, bad = [], []
    for i, row in frame.iterrows():
        reason = "conflicting_timestamp" if conflicts.loc[i] else None
        if reason is None:
            values = pd.to_numeric(row[["open", "high", "low", "close", "volume"]], errors="coerce")
            if not np.isfinite(values).all():
                reason = "non_finite_ohlcv"
            elif (values[["open", "high", "low", "close"]] <= 0).any():
                reason = "non_positive_price"
            elif values["volume"] < 0:
                reason = "negative_volume"
            elif values["high"] < max(values["open"], values["close"], values["low"]):
                reason = "high_below_ohlc"
            elif values["low"] > min(values["open"], values["close"], values["high"]):
                reason = "low_above_ohlc"
            elif pd.isna(row["ts"]):
                reason = "missing_timestamp"
        if reason:
            raw = {
                k: None if pd.isna(v) else str(v) if isinstance(v, pd.Timestamp) else v
                for k, v in row.to_dict().items()
            }
            bad.append({"session_date": row["session_date"], "reason": reason, "raw": raw})
        else:
            good.append(i)
    return frame.loc[good].sort_values("ts").reset_index(drop=True), bad
