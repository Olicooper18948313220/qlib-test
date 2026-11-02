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
