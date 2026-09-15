$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  throw "Missing .venv. Run .\scripts\setup.ps1 first."
}
& $Python scripts\deploy.py start @args
exit $LASTEXITCODE
