$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $project 'src'
$python = 'D:\Anoconda3\envs\py311\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
& $python -m qlib_quant.cli validate
