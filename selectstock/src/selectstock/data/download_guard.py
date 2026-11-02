"""Shared pacing and cooldown for actual Yahoo HTTP requests, including yfinance internals."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from filelock import FileLock


class DownloadPaused(Exception):
    def __init__(self, reason, resume_at):
        self.reason = reason
        self.resume_at = resume_at
        self.resume_after = datetime.fromtimestamp(resume_at, timezone.utc).isoformat()
        super().__init__(f"{reason}; resume_after={self.resume_after}")


class DownloadGuard:
    def __init__(self, settings, *, clock=time.time, sleep=time.sleep, uniform=random.uniform):
        self.settings = settings
        self.path = Path(settings["download_state"])
        self.clock, self.sleep, self.uniform = clock, sleep, uniform
        self.request_count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock", timeout=10)

    def _load(self):
        if not self.path.exists():
            return {}
        # Invalid state fails closed: do not erase an unknown cooldown and resume traffic.
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state):
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(self.path)

    def _check(self, state):
        until = state.get("cooldown_until", 0)
        if until > self.clock():
            raise DownloadPaused(state.get("pause_reason", "download cooldown"), until)

    def check(self):
        with self.lock:
            self._check(self._load())

    def _pause(self, state, reason, seconds):
        until = max(state.get("cooldown_until", 0), self.clock() + seconds)
        state.update(cooldown_until=until, pause_reason=reason)
        self._save(state)
        raise DownloadPaused(reason, until)

    def pause(self, reason, seconds=None):
        with self.lock:
            self._pause(self._load(), reason, seconds or self.settings["rate_limit_cooldown_seconds"])

    def _retry_after(self, value):
        if not value:
            return 0
        try:
            return max(0, float(value))
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0, date.timestamp() - self.clock())
            except (ValueError, TypeError, OverflowError):
                return 0

    def _failure(self, state):
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        if state["consecutive_failures"] >= self.settings["max_consecutive_failures"]:
            self._pause(state, "连续网络错误，下载已暂停", self.settings["failure_cooldown_seconds"])

    def request(self, send):
        # Hold the lock until response completion: separate Notebook kernels share one request lane.
        with self.lock:
            state = self._load()
            self._check(state)
            if self.request_count >= self.settings["max_requests_per_run"]:
                self._pause(
                    state, "本轮 HTTP 请求预算已用完，已保存进度", self.settings["budget_cooldown_seconds"]
                )
            wait = max(0, state.get("next_allowed_at", 0) - self.clock())
            if wait:
                self.sleep(wait)
            self.request_count += 1
            state["total_requests"] = state.get("total_requests", 0) + 1
            batch_count = state.get("batch_requests", 0) + 1
            extra = 0
            if batch_count >= self.settings["download_batch_requests"]:
                extra = self.settings["download_batch_pause_seconds"]
                batch_count = 0
            state["batch_requests"] = batch_count
            # Persist reservation before sending, including if a process is interrupted.
            state["next_allowed_at"] = (
                self.clock()
                + self.settings["request_pause_seconds"]
                + self.uniform(0, self.settings["request_jitter_seconds"])
                + extra
            )
            self._save(state)
            try:
                response = send()
            except Exception:
                self._failure(state)
                self._save(state)
                raise
            status = response.status_code
            if status in (429, 403):
                cooldown = max(
                    self.settings["rate_limit_cooldown_seconds"],
                    self._retry_after(response.headers.get("Retry-After")),
                )
                self._pause(state, f"Yahoo HTTP {status}，停止本轮下载并冷却", cooldown)
            if status >= 500:
                self._failure(state)
            else:
                state["consecutive_failures"] = 0
            self._save(state)
            return response


def guarded_session(guard):
    from curl_cffi import requests

    class GuardedSession(requests.Session):
        def request(self, *args, **kwargs):
            return guard.request(lambda: super(GuardedSession, self).request(*args, **kwargs))

    # Same transport as yfinance's default; no proxy rotation or limiter bypass.
    return GuardedSession(impersonate="chrome")
