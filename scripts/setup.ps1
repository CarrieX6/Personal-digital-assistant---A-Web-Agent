$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3.12 scripts\deploy.py install @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  & python scripts\deploy.py install @args
} else {
  throw "Python 3.11 或更高版本未安装。"
}
exit $LASTEXITCODE
