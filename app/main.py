"""Optional local Streamlit UI. The research engine itself does not require Streamlit."""
from pathlib import Path
import json
import os

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
st.set_page_config(page_title="qlib_quant", layout="wide")
st.title("qlib_quant 本地量化研究")
st.caption("数据、因子、信号、组合、回测和报告分层；默认后复权成交是研究近似。")
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

