$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  throw "Missing .venv. Run .\scripts\setup.ps1 first."
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
  throw "Missing pnpm. Run .\scripts\setup.ps1 first."
}

$RunDirectory = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $RunDirectory | Out-Null
$BackendOutput = Join-Path $RunDirectory "backend.out.log"
$BackendError = Join-Path $RunDirectory "backend.error.log"
$Backend = Start-Process -FilePath $Python -ArgumentList @(
  "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $BackendOutput `
  -RedirectStandardError $BackendError -PassThru

try {
  $BackendReady = $false
  $Deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $Deadline) {
    $Backend.Refresh()
    if ($Backend.HasExited) {
      $Detail = if (Test-Path $BackendError) {
        (Get-Content $BackendError -Raw).Trim()
      } else {
        "The backend process exited unexpectedly."
      }
      throw "Local API startup failed: $Detail"
    }
    try {
      $Health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 1
      if ($Health.StatusCode -eq 200) {
        $BackendReady = $true
        break
      }
    } catch {
      Start-Sleep -Milliseconds 250
    }
  }
  if (-not $BackendReady) {
    throw "The local API was not ready within 20 seconds. Log: $BackendError"
  }
  Write-Host "Console: http://localhost:3000" -ForegroundColor Cyan
  Write-Host "Local API ready: http://127.0.0.1:8000" -ForegroundColor Green
  Write-Host "The spatial-photo LAN viewer uses port 8766 on demand." -ForegroundColor Cyan
  & pnpm run dev
} finally {
  if (-not $Backend.HasExited) { Stop-Process -Id $Backend.Id }
}
