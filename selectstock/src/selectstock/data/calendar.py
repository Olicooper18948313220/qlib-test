from __future__ import annotations

from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

NY = "America/New_York"


def utc_now():
    return pd.Timestamp.now(tz="UTC")


def utc(value):
    value = pd.Timestamp(value)
    return value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")


@lru_cache(maxsize=1)
def calendar():
    return xcals.get_calendar("XNYS", start="1990-01-01", end=f"{utc_now().year + 3}-12-31")


def sessions(start, end):
    return [x.date().isoformat() for x in calendar().sessions_in_range(str(start)[:10], str(end)[:10])]


def session_close(day):
    return calendar().session_close(pd.Timestamp(day))


def latest_closed(now=None, delay_minutes=30):
    now = utc(now) if now is not None else utc_now()
    today = now.tz_convert(NY).date()
    for day in reversed(sessions(today - pd.Timedelta(days=14), today)):
        if session_close(day) + pd.Timedelta(minutes=delay_minutes) <= now:
            return day
    raise ValueError("找不到最近已收盘交易日")


def effective_asof(config, now=None):
    last = latest_closed(now, config["data"]["close_delay_minutes"])
    wanted = str(config["data"].get("end") or last)[:10]
    days = sessions(pd.Timestamp(wanted) - pd.Timedelta(days=14), min(wanted, last))
    if not days:
        raise ValueError("截止日期不在可用交易日范围")
    return days[-1]


def week_info(day):
    day = pd.Timestamp(day)
    monday = day - pd.Timedelta(days=day.weekday())
    days = sessions(monday, monday + pd.Timedelta(days=4))
    return monday.date().isoformat(), bool(days and str(day.date()) == days[-1])


def session_label(ts, day):
    ts = utc(ts)
    if ts < calendar().session_open(pd.Timestamp(day)):
        return "pre"
    return "regular" if ts < session_close(day) else "post"
