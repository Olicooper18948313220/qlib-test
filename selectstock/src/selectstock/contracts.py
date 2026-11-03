from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

import pandas as pd

RULE_VERSION = "martin-capture-2026-10-28-v1"


class MarketProvider(Protocol):
    def history(self, symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame: ...
    def metadata(self, symbol: str) -> dict: ...


@dataclass
class SyncReport:
    requested: int = 0
    downloaded: int = 0
    cached: int = 0
    rows: int = 0
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    metadata_failures: list = field(default_factory=list)
    paused: bool = False
    pause_reason: str | None = None
    resume_after: str | None = None
    network_requests: int = 0
    metadata_bulk_saved: int = 0
    metadata_deferred: int = 0
    segments: int = 1
    recoveries: int = 0
    skipped: int = 0
    quarantined_rows: int = 0
    stopped: bool = False
    status: str = "running"
    processed: int = 0
    total: int = 0
    job_id: str | None = None
    asof: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class ScanResult:
    run_id: str
    asof: str
    frames: dict[str, pd.DataFrame]
    config: dict
    coverage: dict

    def selected(self, name: str) -> pd.DataFrame:
        frame = self.frames[name]
        return frame.loc[frame["status"].eq("passed")].copy()
