from pathlib import Path

from streamlit.testing.v1 import AppTest

from qlib_quant.config import ROOT
from qlib_quant.experiments import read_json
from qlib_quant.runner import run_request
from test_acceptance import database, request


def test_ui_empty_trade_history_and_parameter_comparison(database, tmp_path, monkeypatch):
    import qlib_quant.experiments as experiments
    first = run_request(request(database, tmp_path / "a", config={"signals": {"enabled": []},
                                                               "backtest": {"max_positions": 1}}))
    second = run_request(request(database, tmp_path / "b", config={"signals": {"enabled": []},
                                                                "backtest": {"max_positions": 2}}))
    monkeypatch.setattr(experiments, "discover_runs", lambda _: [first, second])
    app = AppTest.from_file(str(ROOT / "app/main.py"), default_timeout=20).run()
    assert not app.exception
    app.sidebar.radio[0].set_value("结果与实验").run()
    assert not app.exception
    assert app.metric[3].value == "0"
    assert any("暂无记录" in message.value for message in app.info)
    assert any("backtest.max_positions" in str(table.value) for table in app.dataframe)
    # Legacy empty (headerless) CSV must render without EmptyDataError.
    (first / "trades.csv").write_text("", encoding="utf-8")
    app.run()
    assert not app.exception


def test_ui_form_dispatches_shared_request_and_all_signals_can_be_disabled(monkeypatch):
    import qlib_quant.jobs as jobs
    received = []
    def start(req):
        received.append(req)
        return Path("test-job"), None
    monkeypatch.setattr(jobs, "start_job", start)
    app = AppTest.from_file(str(ROOT / "app/main.py"), default_timeout=20).run()
    app.sidebar.radio[0].set_value("新建回测").run()
    app.number_input[1].set_value(3)
    app.multiselect[0].set_value([])
    app.button[0].click().run()
    assert not app.exception
    assert received[0]["config"]["backtest"]["max_positions"] == 3
    assert received[0]["config"]["signals"]["enabled"] == []
    assert received[0]["config"]["signals"]["thresholds"]["learning_ma_gap_min"] == .02
    assert app.success


def test_ui_exposes_learning_route():
    app = AppTest.from_file(str(ROOT / "app/main.py"), default_timeout=20).run()
    assert not app.exception
    app.sidebar.radio[0].set_value("学习路线").run()
    assert not app.exception
    assert any("因子开发" in markdown.value for markdown in app.markdown)


def test_signal_series_switch_refreshes_options_before_submission(monkeypatch):
    import qlib_quant.jobs as jobs
    from qlib_quant.signals.zff0708 import signal_names

    received = []

    def start(req):
        received.append(req)
        return Path("test-job"), None

    monkeypatch.setattr(jobs, "start_job", start)
    app = AppTest.from_file(str(ROOT / "app/main.py"), default_timeout=20).run()
    app.sidebar.radio[0].set_value("新建回测").run()
    series = app.selectbox(key="signal_version")
    # Form widgets do not send changes until submit in the real browser.
    assert not series.proto.form_id
    series.set_value("0708cao").run()
    assert not app.exception
    assert not received
    selector = app.multiselect(key="signals_0708cao")
    assert len(selector.options) == len(signal_names) == 11
    assert all(name in label for name, label in zip(signal_names, selector.options))
    assert selector.value == []
    selector.set_value(["0708cao_sig1", "0708cao_sig6"])
    app.button[0].click().run()
    assert not app.exception
    assert received[0]["config"]["signals"] == {
        "version": "0708cao", "enabled": ["0708cao_sig1", "0708cao_sig6"], "thresholds": {}}
    app.selectbox(key="signal_version").set_value("legacy_v1").run()
    assert not app.exception
    assert app.multiselect(key="signals_legacy_v1").value
    assert len(received) == 1
