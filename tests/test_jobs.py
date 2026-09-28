import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from qlib_quant.experiments import atomic_json, read_json, discover_runs
from qlib_quant.jobs import start_job, cancel_job, job_status, _process
from test_acceptance import database, request


def wait_for_job(job, process, root):
    process.wait(timeout=45)
    return job_status(job, root)


def test_real_worker_success_refresh_discovery_and_terminal_cancel(database, tmp_path):
    job, process = start_job(request(database, tmp_path / "unused"), root=tmp_path)
    assert job_status(job, tmp_path)["state"] in {"queued", "running"}
    status = wait_for_job(job, process, tmp_path)
    assert status["state"] == "success", (job / "process.log").read_text(encoding="utf-8")
    assert job / "result" in discover_runs(tmp_path / "runs")
    assert job_status(job, tmp_path)["state"] == "success"
    assert cancel_job(job, tmp_path) is False


def test_cancel_waiting_or_running_worker_then_start_again(database, tmp_path):
    job, process = start_job(request(database, tmp_path / "unused"), root=tmp_path)
    assert cancel_job(job, tmp_path)
    assert wait_for_job(job, process, tmp_path)["state"] == "cancelled"
    assert job / "result" not in discover_runs(tmp_path / "runs")
    job2, process2 = start_job(request(database, tmp_path / "unused"), root=tmp_path)
    assert wait_for_job(job2, process2, tmp_path)["state"] == "success"


def test_crashed_worker_recovers_without_refresh_memory(database, tmp_path):
    job, process = start_job(request(database, tmp_path / "unused"), root=tmp_path)
    # This is the exact subprocess created by the test, never an arbitrary PID.
    process.terminate()
    process.wait(timeout=10)
    assert job_status(job, tmp_path)["state"] == "failed"
    assert "退出" in job_status(job, tmp_path)["message"]
    job2, process2 = start_job(request(database, tmp_path / "unused"), root=tmp_path)
    cancel_job(job2, tmp_path)
    assert wait_for_job(job2, process2, tmp_path)["state"] == "cancelled"


def test_worker_failure_persists_and_allows_retry(database, tmp_path):
    job, process = start_job(request(tmp_path / "missing.db", tmp_path / "unused"), root=tmp_path)
    assert wait_for_job(job, process, tmp_path)["state"] == "failed"
    assert (job / "result/error.log").exists()
    job2, process2 = start_job(request(database, tmp_path / "unused"), root=tmp_path)
    cancel_job(job2, tmp_path)
    assert wait_for_job(job2, process2, tmp_path)["state"] == "cancelled"


def test_simultaneous_starts_admit_only_one(database, tmp_path):
    def start():
        try:
            return start_job(request(database, tmp_path / "unused"), root=tmp_path)
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    admitted = [r for r in results if r]
    assert len(admitted) == 1
    job, process = admitted[0]
    cancel_job(job, tmp_path)
    assert wait_for_job(job, process, tmp_path)["state"] == "cancelled"


def test_pid_identity_mismatch_never_targets_another_process(tmp_path):
    job = tmp_path / "fake"
    job.mkdir()
    me = psutil.Process()
    atomic_json(job / "process.json", {"pid": me.pid, "create_time": me.create_time() - 10})
    assert _process(job) is None
