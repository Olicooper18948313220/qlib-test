"""Optional local Streamlit UI. The research engine itself does not require Streamlit."""
import base64
from pathlib import Path
import json
import os

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
st.set_page_config(page_title="qlib_quant", layout="wide")
st.title("qlib_quant 本地量化研究")
st.caption("数据、因子、信号、组合、回测和报告分层；默认后复权成交是研究近似。")


def read_doc(name: str) -> str:
    path = ROOT / "docs" / name
    return path.read_text(encoding="utf-8") if path.exists() else f"文档不存在：`{path}`"


def render_svg(path: Path) -> None:
    if not path.exists():
        st.warning(f"结构图不存在：{path}")
        return
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    st.markdown(
        f'<img src="data:image/svg+xml;base64,{encoded}" alt="qlib_quant框架结构图" style="width:100%;">',
        unsafe_allow_html=True,
    )


architecture_tab, strategy_tab, report_tab = st.tabs(["框架结构", "现有策略", "回测结果"])

with architecture_tab:
    st.subheader("当前真实运行链路")
    render_svg(ROOT / "docs" / "architecture.svg")
    st.markdown(read_doc("00_框架结构图.md"))

with strategy_tab:
    st.markdown(read_doc("03_现有策略说明.md"))

with report_tab:
    run_dir = Path(os.environ.get("QLIB_QUANT_RUN_DIR", str(ROOT / "runs/full")))
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        st.json(data)
        curve = run_dir / "equity_curve.csv"
        if curve.exists():
            import pandas as pd
            frame = pd.read_csv(curve)
            st.line_chart(frame.set_index("date")["market_value"])
    else:
        st.info("先运行 python -m src.qlib_quant.cli validate 或 backtest。")

