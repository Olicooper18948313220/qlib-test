from dataclasses import dataclass, asdict
from pathlib import Path
import json


@dataclass(frozen=True)
class DataManifest:
    database: str
    table: str
    instrument_type: str
    adjustment: str
    first_date: str
    last_date: str
    rows: int
    instruments: int
    null_trade_status_rows: int
    corporate_action_rows: int
    source_values: tuple[str, ...]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

