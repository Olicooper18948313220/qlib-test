$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $project 'src'
& 'D:\Anoconda3\envs\py311\python.exe' -m qlib_quant.cli validate

