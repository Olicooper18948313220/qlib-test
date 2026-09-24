$ErrorActionPreference = 'Stop'
Write-Host "Python:"
python --version
Write-Host "Project: $((Resolve-Path "$PSScriptRoot\..").Path)"
python -c "import sys; print(sys.executable); print(sys.version)"
try { python -c "import qlib; print('pyqlib: OK')" } catch { Write-Host 'pyqlib: 未安装，先运行 pip install -e .[qlib]' }

