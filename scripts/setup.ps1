$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js 22.13+ 未安装或不在 PATH。"
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
  if (Get-Command corepack -ErrorAction SilentlyContinue) {
    corepack enable
    corepack prepare pnpm@11.9.0 --activate
  } else {
    throw "pnpm 未安装；请先安装 Node.js 并启用 corepack。"
  }
}

$PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($PythonLauncher) {
  & py -3.12 -m venv .venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  & python -m venv .venv
} else {
  throw "Python 3.11 或 3.12 未安装。"
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r backend\requirements.txt
& pnpm install

Write-Host "安装完成。运行 .\scripts\start.ps1 启动控制台。" -ForegroundColor Green
