"""Execute both delivered notebooks in separate clean kernels; keep originals output-free."""

import json
import os
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
    assert all(
        not cell.get("outputs") and cell.get("execution_count") is None
        for cell in nb.cells
        if cell.cell_type == "code"
    )
    km = AsyncKernelManager(
        kernel_name="python3", kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernel_root)])
    )
    client = NotebookClient(nb, km=km, timeout=180, resources={"metadata": {"path": str(ROOT / "notebooks")}})
    with client.setup_kernel(cleanup_kc=True):
        client.execute()
    nbformat.write(nb, ARTIFACTS / path.name)
    print(f"PASS {path.name}", flush=True)
