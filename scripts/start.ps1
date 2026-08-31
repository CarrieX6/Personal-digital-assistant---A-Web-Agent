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

$PhotoStyleAutoStart = & $Python -c @'
import os
from dotenv import load_dotenv
load_dotenv()
print("true" if os.getenv("PHOTO_STYLE_AUTO_START", "").strip().lower() in {"1", "true", "yes", "on"} else "false")
'@
if ($PhotoStyleAutoStart -eq "true") {
  & $Python scripts\manage_photo_style.py start
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "图片风格化独立服务未能自动启动；控制台会继续启动并显示真实不可用状态。"
  }
}

$Backend = Start-Process -FilePath $Python -ArgumentList @(
  "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $Root -NoNewWindow -PassThru
$PublicViewer = Start-Process -FilePath $Python -ArgumentList @(
  "scripts/start_public_viewer.py", "--from-env", "--reconnect"
) -WorkingDirectory $Root -NoNewWindow -PassThru

try {
  Write-Host "控制台: http://localhost:3000" -ForegroundColor Cyan
  Write-Host "空间照片局域网 Viewer 会按需监听 8766 端口。" -ForegroundColor Cyan
  & pnpm run dev
} finally {
  if (-not $Backend.HasExited) { Stop-Process -Id $Backend.Id }
  if (-not $PublicViewer.HasExited) { Stop-Process -Id $PublicViewer.Id }
}
