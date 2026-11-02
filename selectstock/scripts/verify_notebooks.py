"""Execute both delivered notebooks in separate clean kernels; keep originals output-free."""

import json
import os
import re
import sys
from pathlib import Path

import nbformat
from jupyter_client import AsyncKernelManager
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "runs" / "notebook-validation"
ARTIFACTS.mkdir(parents=True, exist_ok=True)
for variable, dirname in (
    ("IPYTHONDIR", "ipython"),
    ("MPLCONFIGDIR", "matplotlib"),
    ("JUPYTER_CONFIG_DIR", "jupyter"),
    ("JUPYTER_RUNTIME_DIR", "runtime"),
):
    os.environ[variable] = str(ARTIFACTS / dirname)
kernel_root = ARTIFACTS / "kernels"
(kernel_root / "python3").mkdir(parents=True, exist_ok=True)
(kernel_root / "python3" / "kernel.json").write_text(
    json.dumps(
        {
            "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
            "display_name": "Selectstock validation",
            "language": "python",
        }
    )
)
for path in sorted((ROOT / "notebooks").glob("*.ipynb")):
    nb = nbformat.read(path, as_version=4)
    nbformat.validate(nb)
    # Validate an in-memory copy, preserving the user's saved outputs and parameters.
    demo_assignments = 0
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        cell.outputs = []
        cell.execution_count = None
        cell.source, count = re.subn(r"^DEMO\s*=.*$", "DEMO = True", cell.source, flags=re.MULTILINE)
        demo_assignments += count
        cell.source = re.sub(
            r"^RUN_(DAILY|INTRADAY)_UPDATE\s*=.*$", r"RUN_\1_UPDATE = False", cell.source, flags=re.MULTILINE
        )
    assert demo_assignments == 1, "Notebook 演示开关缺失或重复，拒绝执行验证"
    nb.cells.insert(
        0,
        nbformat.v4.new_code_cell("""from selectstock.data.providers import YahooProvider, NasdaqDirectory
def _validation_no_network(*args, **kwargs):
    raise AssertionError("Offline validation must not request market data")
YahooProvider.history = _validation_no_network
YahooProvider.metadata = _validation_no_network
YahooProvider.metadata_pages = _validation_no_network
NasdaqDirectory.fetch = _validation_no_network
"""),
    )
    km = AsyncKernelManager(
        kernel_name="python3", kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernel_root)])
    )
    client = NotebookClient(nb, km=km, timeout=180, resources={"metadata": {"path": str(ROOT / "notebooks")}})
    with client.setup_kernel(cleanup_kc=True):
        client.execute()
    nbformat.write(nb, ARTIFACTS / path.name)
    print(f"PASS {path.name} (all cells, offline)", flush=True)
