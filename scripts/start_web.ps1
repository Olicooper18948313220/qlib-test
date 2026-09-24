$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
& 'D:\Anoconda3\envs\py311\Scripts\streamlit.exe' run app/main.py --server.address 127.0.0.1

