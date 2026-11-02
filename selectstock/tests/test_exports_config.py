import json

import pytest
from conftest import NOW, seed

from selectstock.config import load_experiment, save_experiment, validate_config
from selectstock.data.providers.nasdaq import parse_directory
from selectstock.runner import export_watchlists, run_scans


def test_export_trace_and_dedup(config):
    seed(config)
    result = run_scans(config, now=NOW)
    path = export_watchlists(result)
    assert (path / "combined.txt").read_text() == "NASDAQ:AAA,NASDAQ:BBB"
    metadata = json.loads((path / "metadata.json").read_text())
    assert metadata["asof"] == "2026-10-30"
    assert metadata["rule_version"]
    assert metadata["coverage"]["potent"]["universe"] == 2


def test_configuration_roundtrip_and_unknown_rule(config, tmp_path):
    path = tmp_path / "experiment.yaml"
    save_experiment(config, path)
    assert load_experiment(path)["data"]["database"] == config["data"]["database"]
    config["scanners"]["leader"]["min_week_turnvoer"] = 1
    with pytest.raises(ValueError):
        validate_config(config)


def test_unimplemented_strategy_is_explicit(config):
    config["strategies"]["martin_luk"]["enabled"] = True
    with pytest.raises(NotImplementedError):
        validate_config(config)


def test_directory_security_types():
    text = "Symbol|Security Name|Test Issue|ETF|NextShares\nAAPL|Apple Common Stock|N|N|N\nADR|Example American Depositary Shares|N|N|N\nETF|Fund|N|Y|N\nWAR|Example Warrants|N|N|N\nPREF|Example Preferred|N|N|N\nTEST|Common Stock|Y|N|N\nFile Creation Time: 20261030||||\n"
    assert [r["symbol"] for r in parse_directory(text)] == ["AAPL", "ADR"]
