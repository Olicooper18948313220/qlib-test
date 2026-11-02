import pandas as pd

from ..data.calendar import sessions, week_info


def anchor_index(dates, days):
    """TradingView daily-bar search: exact anchor, otherwise the nearest bar before it."""
    anchor = dates[-1] - pd.Timedelta(days=days)
    return int(dates.searchsorted(anchor, side="right") - 1)


def ema_series(values, window):
    series = values.astype(float).copy()
    if len(series) < window:
        return series * float("nan")
    series.iloc[: window - 1] = float("nan")
    series.iloc[window - 1] = values.iloc[:window].mean()
    return series.ewm(span=window, adjust=False, ignore_na=True).mean()


def compute_metrics(frame, leader=None, potent=None):
    if frame.empty:
        return {}
    close = float(frame.iloc[-1]["close"])
    last = frame.iloc[-1]
    dates = pd.DatetimeIndex(frame["session_date"])
    metrics = {
        "price": close,
        "day_turnover": close * float(last["volume"]),
        "change_pct": (close / float(frame.iloc[-2]["close"]) - 1) * 100 if len(frame) > 1 else float("nan"),
        "open_change_pct": (close / float(last["open"]) - 1) * 100,
    }
    if leader:
        window = leader["average_volume_days"]
        metrics["average_volume"] = (
            float(frame["volume"].tail(window).mean()) if len(frame) >= window else float("nan")
        )
        monday, full = week_info(last["session_date"])
        week = frame.loc[frame["session_date"].ge(monday)]
        expected = sessions(monday, last["session_date"])
        metrics.update(
            week_turnover=close * float(week["volume"].sum()),
            week_start=monday,
            week_complete=full,
            week_data_complete=set(expected) <= set(week["session_date"]),
        )
        index = anchor_index(dates, leader["performance_days"])
        past = float(frame.iloc[index]["open"]) if index >= 0 else None
        metrics["performance_1m"] = (close - past) * 100 / abs(past) if past else float("nan")
        metrics["performance_anchor"] = frame.iloc[index]["session_date"] if index >= 0 else None
    if potent:
        # Explicit SMA seed; extra observations reduce initialization sensitivity.
        for key, window in (("ema_fast", potent["ema_fast"]), ("ema_slow", potent["ema_slow"])):
            metrics[key] = float(ema_series(frame["close"], window).iloc[-1])
        index = anchor_index(dates, potent["volatility_days"])
        # Pine sum(..., bb) excludes the anchor bar itself, includes the current bar.
        sample = frame.iloc[index + 1 :] if index >= 0 else frame.iloc[0:0]
        metrics["volatility_1w"] = (
            float(((sample["high"] - sample["low"]) / sample["low"].abs() * 100).mean())
            if not sample.empty
            else float("nan")
        )
    return metrics
