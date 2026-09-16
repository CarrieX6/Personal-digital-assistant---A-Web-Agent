$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Arguments = @($args)
if (-not ($Arguments -contains "--profile") -and -not ($Arguments -match "^--profile=")) {
  $Arguments += @("--profile", "core")
}
if (-not ($Arguments -contains "--photo-style") -and -not ($Arguments -match "^--photo-style=")) {
  $Arguments += @("--photo-style", "skip")
}

& $PSScriptRoot\bootstrap.ps1 @Arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Root\.venv\Scripts\python.exe scripts\deploy.py start
exit $LASTEXITCODE
