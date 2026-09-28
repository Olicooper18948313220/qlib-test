"""Shared durable experiment files and discovery for CLI, workers and UI."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
from filelock import FileLock

ENGINE_VERSION = "v1.3-0708cao"
TERMINAL = {"success", "failed", "cancelled"}


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_status(run_dir, state, message, progress=0, **extra):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(run_dir / ".status.lock")):
        old = read_json(run_dir / "status.json")
        if old.get("state") in TERMINAL:
            return old
        if old.get("state") == "cancelling" and state == "running":
            return old
        if old.get("state") == "cancelling" and state == "success":
            state, message = "cancelled", "任务已取消"
        payload = {**old, "state": state, "message": message, "progress": progress,
                   "updated_at": datetime.now().isoformat(timespec="seconds"), **extra}
        atomic_json(run_dir / "status.json", payload)
        return payload


def discover_runs(base):
    base = Path(base)
    candidates = list(base.glob("*/summary.json")) + list(base.glob("jobs/*/result/summary.json"))
    result = []
    for summary in candidates:
        directory = summary.parent
        state = read_json(directory / "status.json").get("state")
        if state and state != "success":
            continue
        result.append(directory)
    return sorted(result, key=lambda p: (p / "summary.json").stat().st_mtime, reverse=True)


def run_id(directory):
    directory = Path(directory)
    return directory.parent.name if directory.name == "result" else directory.name


def run_classification(directory):
    metadata = read_json(Path(directory) / "run_metadata.json")
    if metadata.get("engine_version") == ENGINE_VERSION:
        return "V1 修复版"
    if Path(directory).name in {"full", "smoke", "v1_smoke", "v1_smoke2"}:
        return "旧版·含已知缺陷，请勿用于策略结论"
    return "历史版本·未通过本轮验收"


def read_csv(path):
    try:
        return pd.read_csv(path, dtype={"code": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def config_diff(left, right):
    def flatten(value, prefix=""):
        out = {}
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                out.update(flatten(item, name))
            else:
                out[name] = json.dumps(item, ensure_ascii=False, sort_keys=True)
        return out
    a, b = flatten(left), flatten(right)
    return pd.DataFrame([{"参数": key, "实验 A": a.get(key, "—"), "实验 B": b.get(key, "—")}
                         for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)],
                        columns=["参数", "实验 A", "实验 B"])
