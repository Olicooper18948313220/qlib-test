from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import fields

import pandas as pd
from filelock import FileLock

from ..contracts import SyncReport
from .calendar import NY, effective_asof, latest_closed, sessions, utc, utc_now
from .database import Database
from .download_guard import DownloadGuard, DownloadPaused
from .providers import NasdaqDirectory, YahooProvider
from .quality import metadata_fresh, partition_bars, validate_bars


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


def metadata_ready(snapshot, config, asof, now):
    if not metadata_fresh(snapshot, asof, now, config["data"]["metadata_ttl_days"]):
        return False
    needs_industry = (
        config["scanners"]["potent"]["enabled"] and config["scanners"]["potent"]["excluded_industries"]
    )
    return not needs_industry or bool(snapshot.get("industry"))


def prefetch_metadata(config, provider, db, records, report, asof, now):
    d = config["data"]
    if (
        d["metadata_mode"] != "auto"
        or len(records) < d["metadata_batch_min_symbols"]
        or not hasattr(provider, "metadata_pages")
        or (config["scanners"]["potent"]["enabled"] and config["scanners"]["potent"]["excluded_industries"])
    ):
        return False
    wanted = [
        r["symbol"]
        for r in records
        if not metadata_ready(db.metadata(r["symbol"]), config, asof, now)
        and db.metadata_retry_due(r["symbol"], now, d["metadata_failure_retry_minutes"])
    ]
    if not wanted:
        return True
    if len(wanted) < d["metadata_batch_min_symbols"]:
        return False
    try:
        for page in provider.metadata_pages(wanted):
            for _, metadata in page:
                metadata["session_asof"] = asof
            db.save_metadata_many(page, now)
            report.metadata_bulk_saved += len(page)
            guard = getattr(provider, "guard", None)
            if guard:
                guard.progress(stage="metadata", reason=f"已保存 {report.metadata_bulk_saved} 条批量资料")
                if hasattr(guard, "checkpoint"):
                    guard.checkpoint()
    except Exception as exc:
        if isinstance(exc, DownloadPaused) or is_rate_limited(exc):
            raise
        report.warnings.append(f"批量资料未完全获取，使用限量补查；所有股票仍保留历史下载任务: {exc}")
    if hasattr(provider, "metadata_offset"):
        provider.metadata_offset = 0
    return True


def sync(config, provider=None, directory=None, now=None, repair=False, progress=None):
    report = SyncReport()
    progress = progress or (lambda **event: None)
    if config["data"]["offline"]:
        return _sync(config, provider, directory, now, repair, report, progress=progress)
    guard = DownloadGuard(config["data"])
    guard.progress = lambda **event: progress(network_requests=guard.request_count, **event)
    guard.report = report
    owned_provider = provider is None
    try:
        guard.run(lambda: None)
        provider = provider or YahooProvider(config["data"]["include_extended_hours"], guard=guard)
        return _sync(config, provider, directory, now, repair, report, guard, progress)
    except KeyboardInterrupt:
        report.stopped = True
        report.status = "stopped"
        return report
    except Exception as exc:
        if is_rate_limited(exc):
            try:
                guard.pause("数据源返回限流，停止本轮下载")
            except DownloadPaused as paused:
                exc = paused
        if not isinstance(exc, DownloadPaused):
            raise
        report.paused = True
        report.status = "paused"
        report.pause_reason = exc.reason
        report.resume_after = exc.resume_after
        report.warnings.append(str(exc))
        return report
    finally:
        report.network_requests += guard.request_count
        if hasattr(guard, "checkpoint"):
            guard.checkpoint()
        progress(
            stage=report.status,
            processed=report.processed,
            total=report.total,
            network_requests=report.network_requests,
            cached=report.cached,
            downloaded=report.downloaded,
            skipped=report.skipped,
            failures=len(report.failures),
            reason=report.pause_reason or "",
        )
        if owned_provider and provider is not None:
            provider.close()


def _sync(config, provider, directory, now, repair, report, guard=None, progress=None):
    d = config["data"]
    progress = progress or (lambda **event: None)

    def call(function):
        def operation():
            return retry(function, d)

        # per-stock operations may include several HTTP requests.
        try:
            return guard.run(operation) if guard else operation()
        finally:
            if guard:
                progress(network_requests=guard.request_count)

    now = utc(now) if now is not None else utc_now()
    observed_now = now
    db = Database(d["database"])
    db.path.parent.mkdir(parents=True, exist_ok=True)
    if d["offline"]:
        if not db.path.exists():
            raise FileNotFoundError("offline 模式需要已有本地数据库；可先运行离线示例")
        report.warnings.append("offline: 未请求网络")
        report.status = "offline"
        return report
    with FileLock(str(db.path) + ".lock", timeout=5):
        db.initialize()
        job_key = {k: d[k] for k in ("symbols", "timeframes", "history_days", "end")}
        job_key.update(
            watchlist=config["watchlist"],
            repair=repair,
            scanners={k: v["enabled"] for k, v in config["scanners"].items()},
        )
        job_id = hashlib.sha256(json.dumps(job_key, sort_keys=True).encode()).hexdigest()[:20]
        saved_job = db.job(job_id)
        progress(stage="preparing", reason="读取股票池与断点")
        if saved_job:
            records = saved_job["records"]
            now = utc(saved_job["now"])
            for f in fields(report):
                if f.name in saved_job["report"]:
                    setattr(report, f.name, saved_job["report"][f.name])
            report.paused = report.stopped = False
            report.pause_reason = report.resume_after = None
            report.status = "running"
            if hasattr(provider, "metadata_offset"):
                provider.metadata_offset = saved_job.get("metadata_offset", 0)
        elif not d["symbols"]:
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
        universe, _ = db.universe()
        active_symbols = {r["symbol"] for r in universe} if universe else None
        asof = effective_asof(config, now)
        current = asof == latest_closed(observed_now, d["close_delay_minutes"])
        report.asof = asof
        report.job_id = job_id
        tasks = saved_job.get("tasks") if saved_job else None
        cursor = saved_job.get("cursor", 0) if saved_job else 0

        base_requests = report.network_requests

        def checkpoint(status="running"):
            snapshot = report.to_dict()
            snapshot["network_requests"] = base_requests + (guard.request_count if guard else 0)
            db.save_job(
                job_id,
                status,
                {
                    "records": records,
                    "now": now.isoformat(),
                    "tasks": tasks,
                    "cursor": cursor,
                    "report": snapshot,
                    "metadata_offset": getattr(provider, "metadata_offset", 0),
                },
            )

        checkpoint()
        if guard:
            guard.checkpoint = lambda: checkpoint(report.status)
        progress(stage="metadata", reason="批量检查证券资料")
        bulk_mode = (
            prefetch_metadata(config, provider, db, records, report, asof, observed_now)
            if current and "1d" in d["timeframes"]
            else False
        )
        metadata_fallbacks = 0
        daily_symbols = [r["symbol"] for r in records]
        names = [n for n, s in config["scanners"].items() if s["enabled"]]
        minute_symbols = sorted(set(db.latest_selected(names)) | set(config["watchlist"]["symbols"]))
        if tasks is None:
            tasks = [
                (tf, s) for tf in d["timeframes"] for s in (daily_symbols if tf == "1d" else minute_symbols)
            ]
        report.total = len(tasks)
        if any(tf != "1d" for tf in d["timeframes"]) and not minute_symbols:
            report.warnings.append("分钟线：无入选股或自选股，未下载")
        for task_index, (tf, symbol) in enumerate(tasks):
            if task_index < cursor:
                continue
            symbols = daily_symbols if tf == "1d" else minute_symbols
            if not symbols:
                report.warnings.append(f"{tf}: 无入选股或自选股，未下载分钟线")
            requested_start = pd.Timestamp(asof) - pd.Timedelta(days=d["history_days"][tf] - 1)
            earliest = observed_now.tz_convert(NY).normalize().tz_localize(None) - pd.Timedelta(
                days=29 if tf == "1m" else 59
            )
            start = requested_start if tf == "1d" else max(requested_start, earliest)
            if start > requested_start:
                report.warnings.append(
                    f"{tf}: {requested_start.date()} 至 {start.date()} 超出远端窗口；已有本地历史保留，缺失部分无法补回"
                )
            all_days = sessions(start, asof) if start <= pd.Timestamp(asof) else []
            progress(
                stage="history",
                symbol=symbol,
                timeframe=tf,
                processed=report.processed,
                total=report.total,
                cached=report.cached,
                downloaded=report.downloaded,
                skipped=report.skipped,
                failures=len(report.failures),
            )
            if active_symbols is not None and symbol not in active_symbols:
                report.skipped += 1
                report.warnings.append(f"{symbol}: inactive_directory，保留历史并跳过")
                cursor = task_index + 1
                report.processed = cursor
                checkpoint()
                continue
            report.requested += 1
            if tf == "1d" and current:
                previous = db.metadata(symbol)
                if not metadata_ready(previous, config, asof, observed_now):
                    due = db.metadata_retry_due(symbol, observed_now, d["metadata_failure_retry_minutes"])
                    if not due or (bulk_mode and metadata_fallbacks >= d["metadata_fallback_limit"]):
                        report.metadata_deferred += 1
                        report.metadata_failures.append(
                            {
                                "symbol": symbol,
                                "error": "资料待核查：已暂缓重复请求或达到逐股补查预算",
                            }
                        )
                        due = False
                    if due:
                        metadata_fallbacks += 1
                        try:
                            metadata = call(lambda: provider.metadata(symbol))
                            if not metadata.get("market_cap") or metadata["market_cap"] <= 0:
                                raise ValueError("Yahoo 未返回有效市值")
                            metadata["session_asof"] = asof
                            db.save_metadata(symbol, metadata, observed_now)
                        except Exception as exc:
                            if isinstance(exc, DownloadPaused) or is_rate_limited(exc):
                                raise
                            db.record_metadata_failure(symbol, str(exc), observed_now)
                            report.metadata_failures.append({"symbol": symbol, "error": str(exc)})
            completed = set() if repair else db.completed(symbol, tf)
            listing = json.loads(db.metadata(symbol).get("payload") or "{}").get("first_trade_date")
            if listing:
                first_existing = db.first_day(symbol, tf)
                # Preserve existing evidence when a ticker was reused or source dates disagree.
                if first_existing is None or first_existing >= listing:
                    before_listing = [day for day in all_days if day < listing and day not in completed]
                    if before_listing:
                        db.commit_history(
                            symbol,
                            tf,
                            pd.DataFrame(),
                            [(day, "not_listed", "before_first_trade") for day in before_listing],
                        )
                        completed.update(before_listing)
            deferred = set() if repair else db.deferred_days(symbol, tf, observed_now)
            missing = set(all_days) - completed - deferred
            if not missing:
                if not all_days or set(all_days) - completed:
                    report.skipped += 1
                else:
                    report.cached += 1
                cursor = task_index + 1
                report.processed = cursor
                checkpoint()
                continue
            for days in groups(missing, all_days, 7 if tf == "1m" else 59 if tf != "1d" else 36500):
                lo, hi = days[0], days[-1]
                end = (pd.Timestamp(hi) + pd.Timedelta(days=1)).date().isoformat()
                try:
                    frame = call(lambda: provider.history(symbol, tf, lo, end))
                    actions = frame.attrs.get("actions", [])
                    frame, quarantine = partition_bars(frame)
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
                        replacement = call(lambda: provider.history(symbol, tf, repair_start, end))
                        actions = replacement.attrs.get("actions", actions)
                        replacement = validate_bars(replacement)
                        if replacement.empty or (
                            first < invalidation and not replacement["session_date"].lt(invalidation).any()
                        ):
                            raise ValueError("拆股历史修复未返回足够数据；本次不提交")
                        frame = replacement.loc[replacement["session_date"].between(repair_start, hi)].copy()
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
                    bad_days = {r["session_date"] for r in quarantine}
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
                            "invalid"
                            if day in bad_days
                            else "complete"
                            if day in present and complete
                            else "provisional"
                            if day in present
                            else "no_data"
                        )
                        states.append(
                            (
                                day,
                                status,
                                ";".join(r["reason"] for r in quarantine if r["session_date"] == day)
                                if status == "invalid"
                                else ""
                                if status == "complete"
                                else "empty_or_unfinished_session",
                            )
                        )
                    if not frame.empty:
                        final_days = {day for day, status, _ in states if status == "complete"}
                        frame["is_final"] = frame["session_date"].isin(final_days).astype(int)
                    db.commit_history(
                        symbol,
                        tf,
                        frame,
                        states,
                        actions,
                        repair_record,
                        invalidation,
                        quarantine=quarantine,
                        observed_at=observed_now,
                    )
                    report.quarantined_rows += len(quarantine)
                    report.downloaded += 1
                    report.rows += len(frame)
                    for day, status, detail in states:
                        if status in {"no_data", "invalid"}:
                            report.failures.append(
                                {"symbol": symbol, "timeframe": tf, "day": day, "error": detail}
                            )
                except Exception as exc:
                    if isinstance(exc, DownloadPaused) or is_rate_limited(exc):
                        raise
                    db.commit_history(
                        symbol,
                        tf,
                        pd.DataFrame(),
                        [(day, "failed", str(exc)) for day in days],
                        observed_at=observed_now,
                    )
                    report.failures.append(
                        {"symbol": symbol, "timeframe": tf, "start": lo, "end": hi, "error": str(exc)}
                    )
            cursor = task_index + 1
            report.processed = cursor
            checkpoint()
        report.status = (
            "partial" if report.failures or report.metadata_failures or report.skipped else "completed"
        )
        checkpoint(report.status)
    return report
