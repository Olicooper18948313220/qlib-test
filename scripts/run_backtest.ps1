$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
& 'D:\Anoconda3\envs\py311\python.exe' -m qlib_quant.cli backtest --start 2020-01-01 --end 2024-12-31 --output runs/full

