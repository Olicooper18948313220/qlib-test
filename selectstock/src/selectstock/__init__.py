"""Local-first scanner workbench. Public functions are imported lazily."""

import hashlib
from pathlib import Path

__version__ = "0.2.0"
_source_hash = hashlib.sha256()
for _path in sorted(Path(__file__).parent.rglob("*.py")):
    _source_hash.update(_path.read_bytes())
__source_digest__ = _source_hash.hexdigest()


def __getattr__(name):
    if name in {"update_data", "load_bars", "run_scans", "export_watchlists", "check_data"}:
        from . import runner

        return getattr(runner, name)
    raise AttributeError(name)
