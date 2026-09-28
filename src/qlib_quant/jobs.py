"""Disk-backed worker lifecycle with Windows-safe process identity checks."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import psutil
from filelock import FileLock

from .config import ROOT
from .experiments import TERMINAL, atomic_json, read_json, set_status


def _process(job):
    identity = read_json(job / "process.json")
    try:
        process = psutil.Process(identity["pid"])
        if abs(process.create_time() - identity["create_time"]) > 0.01:
            return None
        command = process.cmdline()
        request = str((job / "request.json").resolve())
        if request not in command or "qlib_quant.cli" not in command:
            return None
        return process if process.is_running() and process.status() != psutil.STATUS_ZOMBIE else None
    except (psutil.Error, KeyError, ValueError):
        return None


def _refresh(job):
    status = read_json(job / "result/status.json")
    if status.get("state") not in TERMINAL and status and _process(job) is None:
        cancelled = (job / "cancel.request").exists()
        status = set_status(job / "result", "cancelled" if cancelled else "failed",
                            "任务已取消" if cancelled else "工作进程已退出，未产生完整结果；请查看日志", 100)
    return status


def job_status(job, root=ROOT):
    with FileLock(str(Path(root) / "runs/.dispatch.lock")):
        return _refresh(Path(job))


def start_job(request, root=ROOT):
    from .runner import resolve_request
    root = Path(root)
    snapshot = resolve_request(request)
    jobs = root / "runs/jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "runs/.dispatch.lock")):
        for existing in jobs.iterdir():
            if existing.is_dir() and _refresh(existing).get("state") in {"queued", "running", "cancelling"}:
                raise RuntimeError(f"已有任务运行中：{existing.name}")
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        job = jobs / identifier
        job.mkdir()
        atomic_json(job / "request.json", {"config": snapshot, "resolved": True,
                                         "action": request.get("action", "backtest")})
        set_status(job / "result", "queued", "等待工作进程启动", 0)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        command = [sys.executable, "-m", "qlib_quant.cli", "backtest", "--request", str((job / "request.json").resolve())]
        try:
            with (job / "process.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            identity = {"pid": process.pid, "create_time": psutil.Process(process.pid).create_time()}
            atomic_json(job / "process.json", identity)
            return job, process
        except Exception as exc:
            set_status(job / "result", "failed", f"工作进程启动失败: {exc}", 100)
            raise


def cancel_job(job, root=ROOT):
    # Cooperative cancellation checks SQLite queries, stock batches and trading
    # days. Never kill an arbitrary PID (which can have been reused).
    job = Path(job)
    with FileLock(str(Path(root) / "runs/.dispatch.lock")):
        state = _refresh(job)
        if state.get("state") in TERMINAL:
            return False
        atomic_json(job / "cancel.request", {"requested": datetime.now().isoformat()})
        set_status(job / "result", "cancelling", "正在取消，等待当前计算安全退出", state.get("progress", 0))
        return True
