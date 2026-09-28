param([int]$Port = 8501, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
$env:PYTHONPATH = Join-Path $project 'src'
$env:PYTHONIOENCODING = 'utf-8'
$candidates = @(
    (Join-Path $project '.venv\Scripts\python.exe'),
    'D:\Anoconda3\envs\py311\python.exe',
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
)
$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate)) {
        & $candidate -c "import sys,streamlit,psutil,filelock,yaml,pandas; from qlib_quant.signals.legacy_v1 import signal_names,signal_descriptions; assert (3,11) <= sys.version_info[:2] < (3,13); assert tuple(map(int,streamlit.__version__.split('.')[:2])) >= (1,37); assert signal_names and set(signal_names) <= set(signal_descriptions)" 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
}
if (-not $python) { throw 'Python 3.11/3.12 with UI dependencies was not found. In the project directory run: python -m pip install -e ".[ui]"' }
$selectedPort = $null
for ($candidatePort = $Port; $candidatePort -le ($Port + 20); $candidatePort++) {
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidatePort)
    try {
        $probe.Start()
        $selectedPort = $candidatePort
        break
    } catch {
        continue
    } finally {
        if ($probe) { $probe.Stop() }
    }
}
if ($null -eq $selectedPort) {
    throw "Ports $Port-$($Port + 20) are not available. Close another local Streamlit instance or pass -Port with a free port."
}
if ($selectedPort -ne $Port) {
    Write-Warning "Port $Port is occupied; using free port $selectedPort instead."
}
$headless = if ($NoBrowser) { 'true' } else { 'false' }
Write-Host "Project: $project"
Write-Host "Python: $python"
Write-Host "Open http://127.0.0.1:$selectedPort"
& $python -m streamlit run app/main.py --server.address 127.0.0.1 --server.port $selectedPort --server.headless $headless --browser.gatherUsageStats false
if ($LASTEXITCODE -ne 0) { throw "Streamlit exited with code $LASTEXITCODE. Check the error above." }
