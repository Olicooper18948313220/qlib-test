$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = 'D:\Anoconda3\envs\py311\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source) }
if (-not $python) { throw '找不到 Python。' }
Write-Host "Python:"
& $python --version
Write-Host "Project: $project"
& $python -c "import sys; print(sys.executable); print(sys.version)"
try { & $python -c "import streamlit; print('streamlit: OK')" } catch { Write-Host 'streamlit: 未安装，请运行 pip install -e .[ui]' }
try { & $python -c "import qlib; print('pyqlib: OK')" } catch { Write-Host 'pyqlib: 未安装，Qlib 适配功能不可用' }
