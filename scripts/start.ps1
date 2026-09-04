$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  throw "未找到 .venv，请先运行 .\scripts\setup.ps1。"
}

& $Python scripts\deploy.py start @args
exit $LASTEXITCODE
