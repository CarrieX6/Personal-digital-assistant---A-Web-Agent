$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  throw "未找到 .venv，请先运行 .\scripts\setup.ps1。"
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
  throw "未找到 pnpm，请先运行 .\scripts\setup.ps1。"
}

$Backend = Start-Process -FilePath $Python -ArgumentList @(
  "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $Root -NoNewWindow -PassThru

try {
  Write-Host "控制台: http://localhost:3000" -ForegroundColor Cyan
  Write-Host "空间照片局域网 Viewer 会按需监听 8766 端口。" -ForegroundColor Cyan
  & pnpm run dev
} finally {
  if (-not $Backend.HasExited) { Stop-Process -Id $Backend.Id }
}
