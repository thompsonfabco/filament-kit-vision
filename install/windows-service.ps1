# Windows setup and background daemon launcher for filament-kit-vision
param(
    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Tier = "auto",
    [int]$Port = 7915
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -e . --no-build-isolation
.\.venv\Scripts\filament-kit-vision.exe config --tier $Tier --host 127.0.0.1 --port $Port

Write-Host "[filament-kit-vision] Starting local daemon on http://127.0.0.1:$Port (tier=$Tier)..."
Start-Process -FilePath "$RepoRoot\.venv\Scripts\filament-kit-vision.exe" -ArgumentList "serve --host 127.0.0.1 --port $Port --tier $Tier" -WindowStyle Hidden
