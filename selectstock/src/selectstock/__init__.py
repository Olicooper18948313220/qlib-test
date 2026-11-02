"""Local-first scanner workbench. Public functions are imported lazily."""

__version__ = "0.1.0"


def __getattr__(name):
    if name in {"update_data", "load_bars", "run_scans", "export_watchlists", "check_data"}:
        from . import runner

        return getattr(runner, name)
    raise AttributeError(name)
