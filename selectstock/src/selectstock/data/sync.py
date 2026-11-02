from __future__ import annotations

import random
import time

import pandas as pd
from filelock import FileLock

from ..contracts import SyncReport
from .calendar import NY, effective_asof, latest_closed, sessions, utc, utc_now
from .database import Database
from .download_guard import DownloadGuard, DownloadPaused
from .providers import NasdaqDirectory, YahooProvider
from .quality import metadata_fresh, validate_bars


def retry(function, settings):
    for attempt in range(settings["retries"]):
        try:
            return function()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if (
                isinstance(exc, DownloadPaused)
                or is_rate_limited(exc)
                or isinstance(exc, (ValueError, TypeError))
            ):
                raise
            if attempt + 1 == settings["retries"]:
                raise
            delay = min(settings["retry_backoff_max_seconds"], settings["retry_backoff_seconds"] * 2**attempt)
            time.sleep(delay + random.uniform(0, settings["request_jitter_seconds"]))


def is_rate_limited(exc):
    from yfinance.exceptions import YFRateLimitError

    return isinstance(exc, YFRateLimitError) or getattr(
        getattr(exc, "response", None), "status_code", None
    ) in (429, 403)


def fresh(observed_at, now, ttl):
    return observed_at is not None and utc(observed_at) <= now < utc(observed_at) + pd.Timedelta(days=ttl)


def groups(missing, all_days, max_days):
    """Merge adjacent missing sessions without requesting cached sessions between them."""
    result, group = [], []
    for day in all_days:
        if day not in missing:
            if group:
                result.append(group)
                group = []
            continue
        if group and (pd.Timestamp(day) - pd.Timestamp(group[0])).days >= max_days:
            result.append(group)
            group = []
        group.append(day)
    if group:
        result.append(group)
    return result


def sync(config, provider=None, directory=None, now=None, repair=False):
    report = SyncReport()
    if config["data"]["offline"]:
        return _sync(config, provider, directory, now, repair, report)
    guard = DownloadGuard(config["data"])
    owned_provider = provider is None
    try:
        guard.check()
        provider = provider or YahooProvider(config["data"]["include_extended_hours"], guard=guard)
        return _sync(config, provider, directory, now, repair, report)
    except Exception as exc:
        if is_rate_limited(exc):
            try:
                guard.pause("数据源返回限流，停止本轮下载")
            except DownloadPaused as paused:
                exc = paused
        if not isinstance(exc, DownloadPaused):
            raise
        report.paused = True
        report.pause_reason = exc.reason
        report.resume_after = exc.resume_after
        report.warnings.append(str(exc))
        return report
    finally:
        report.network_requests = guard.request_count
        if owned_provider and provider is not None:
            provider.close()


def _sync(config, provider, directory, now, repair, report):
    d = config["data"]
    now = utc(now) if now is not None else utc_now()
    db = Database(d["database"])
    db.path.parent.mkdir(parents=True, exist_ok=True)
    if d["offline"]:
        if not db.path.exists():
            raise FileNotFoundError("offline 模式需要已有本地数据库；可先运行离线示例")
        report.warnings.append("offline: 未请求网络")
        return report
    with FileLock(str(db.path) + ".lock", timeout=5):
        db.initialize()
        if not d["symbols"]:
            records, at = db.universe()
            if not fresh(at, now, d["universe_ttl_days"]):
                # A failed directory refresh aborts rather than silently reporting full-market coverage.
                records = retry((directory or NasdaqDirectory()).fetch, d)
                db.save_universe(records, now)
        else:
            records = db.instruments(sorted(set(d["symbols"])))
            # Explicit symbols need not download a full directory. Yahoo metadata supplies their exchange.
            with db.connect(True) as con:
                con.executemany(
                    "INSERT OR IGNORE INTO instruments VALUES (:symbol,:exchange,:name,:source_symbol,:instrument_type)",
                    records,
                )
        asof = effective_asof(config, now)
        current = asof == latest_closed(now, d["close_delay_minutes"])
        daily_symbols = [r["symbol"] for r in records]
        names = [n for n, s in config["scanners"].items() if s["enabled"]]
        minute_symbols = sorted(set(db.latest_selected(names)) | set(config["watchlist"]["symbols"]))
        for tf in d["timeframes"]:
            symbols = daily_symbols if tf == "1d" else minute_symbols
            if not symbols:
                report.warnings.append(f"{tf}: 无入选股或自选股，未下载分钟线")
            requested_start = pd.Timestamp(asof) - pd.Timedelta(days=d["history_days"][tf] - 1)
            earliest = now.tz_convert(NY).normalize().tz_localize(None) - pd.Timedelta(
                days=29 if tf == "1m" else 59
            )
            start = requested_start if tf == "1d" else max(requested_start, earliest)
            if start > requested_start:
                report.warnings.append(
                    f"{tf}: {requested_start.date()} 至 {start.date()} 超出远端窗口；已有本地历史保留，缺失部分无法补回"
                )
            all_days = sessions(start, asof) if start <= pd.Timestamp(asof) else []
            for offset in range(0, len(symbols), d["batch_size"]):
                for symbol in symbols[offset : offset + d["batch_size"]]:
                    report.requested += 1
                    if tf == "1d" and current:
                        previous = db.metadata(symbol)
                        if not metadata_fresh(previous, asof, now, d["metadata_ttl_days"]):
                            try:
                                metadata = retry(lambda: provider.metadata(symbol), d)
                                if metadata.get("market_cap") is None:
                                    raise ValueError("Yahoo 未返回市值")
                                metadata["session_asof"] = asof
                                db.save_metadata(symbol, metadata, now)
                            except Exception as exc:
                                if isinstance(exc, DownloadPaused) or is_rate_limited(exc):
                                    raise
                                report.metadata_failures.append({"symbol": symbol, "error": str(exc)})
                    missing = set(all_days) - (set() if repair else db.completed(symbol, tf))
                    if not missing:
                        report.cached += 1
                        continue
                    for days in groups(missing, all_days, 7 if tf == "1m" else 59 if tf != "1d" else 36500):
                        lo, hi = days[0], days[-1]
                        end = (pd.Timestamp(hi) + pd.Timedelta(days=1)).date().isoformat()
                        try:
                            frame = retry(lambda: provider.history(symbol, tf, lo, end), d)
                            actions = frame.attrs.get("actions", [])
                            frame = validate_bars(frame)
                            if not frame.empty:
                                frame = frame.loc[frame["session_date"].between(lo, hi)].copy()
                            known = db.known_actions(symbol)
                            new_splits = [
                                a
                                for a in actions
                                if a["split"] and known.get(a["day"], {}).get("split") != a["split"]
                            ]
                            invalidation = min((a["day"] for a in new_splits), default=None)
                            repair_record = None
                            first = db.first_day(symbol, tf)
                            if new_splits and first and first < lo:
                                repair_start = first if tf == "1d" else max(first, start.date().isoformat())
                                replacement = retry(
                                    lambda: provider.history(symbol, tf, repair_start, end), d
                                )
                                actions = replacement.attrs.get("actions", actions)
                                replacement = validate_bars(replacement)
                                if replacement.empty or (
                                    first < invalidation
                                    and not replacement["session_date"].lt(invalidation).any()
                                ):
                                    raise ValueError("拆股历史修复未返回足够数据；本次不提交")
                                frame = replacement.loc[
                                    replacement["session_date"].between(repair_start, hi)
                                ].copy()
                                repair_record = {
                                    "start": repair_start,
                                    "end": hi,
                                    "reason": "split: Yahoo historical price basis changed",
                                }
                            elif repair:
                                repair_record = {
                                    "start": lo,
                                    "end": hi,
                                    "reason": "explicit historical refresh",
                                }
                            present = set(frame["session_date"]) if not frame.empty else set()
                            states = []
                            for day in sessions(repair_record["start"], hi) if repair_record else days:
                                # Extended-hours data is provisional until 20:00 ET plus the close grace period.
                                complete = (
                                    tf == "1d"
                                    or not d["include_extended_hours"]
                                    or now
                                    >= pd.Timestamp(day + " 20:00", tz=NY).tz_convert("UTC")
                                    + pd.Timedelta(minutes=d["close_delay_minutes"])
                                )
                                status = (
                                    "complete"
                                    if day in present and complete
                                    else "provisional"
                                    if day in present
                                    else "no_data"
                                )
                                states.append(
                                    (
                                        day,
                                        status,
                                        "" if status == "complete" else "empty_or_unfinished_session",
                                    )
                                )
                            if not frame.empty:
                                final_days = {day for day, status, _ in states if status == "complete"}
                                frame["is_final"] = frame["session_date"].isin(final_days).astype(int)
                            db.commit_history(symbol, tf, frame, states, actions, repair_record, invalidation)
                            report.downloaded += 1
                            report.rows += len(frame)
                            for day, status, detail in states:
                                if status == "no_data":
                                    report.failures.append(
                                        {"symbol": symbol, "timeframe": tf, "day": day, "error": detail}
                                    )
                        except Exception as exc:
                            if isinstance(exc, DownloadPaused) or is_rate_limited(exc):
                                raise
                            db.commit_history(
                                symbol, tf, pd.DataFrame(), [(day, "failed", str(exc)) for day in days]
                            )
                            report.failures.append(
                                {"symbol": symbol, "timeframe": tf, "start": lo, "end": hi, "error": str(exc)}
                            )
    return report
