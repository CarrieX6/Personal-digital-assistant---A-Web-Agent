$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$InstallSystemDeps = $args -contains "--install-system-deps"
$DeployArgs = @($args | Where-Object { $_ -ne "--install-system-deps" })

function Find-Python {
  $ProjectPython = Join-Path $Root ".venv\Scripts\python.exe"
  if (Test-Path $ProjectPython) { return @($ProjectPython) }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return @("py", "-3.12") }
  }
  if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return @("python") }
  }
  return @()
}

function Test-Node {
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) { return $false }
  & node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>22 || (a===22 && b>=13) ? 0 : 1)" 2>$null
  return $LASTEXITCODE -eq 0
}

$PythonCommand = @(Find-Python)
$NodeReady = Test-Node
$GitReady = [bool](Get-Command git -ErrorAction SilentlyContinue)
$Missing = @()
if ($PythonCommand.Count -eq 0) { $Missing += "Python 3.11-3.13 (recommended 3.12)" }
if (-not $NodeReady) { $Missing += "Node.js 22.13+ with Corepack" }
if (-not $GitReady) { $Missing += "Git" }

if ($Missing.Count -gt 0) {
  Write-Host "Missing or incompatible runtime dependencies:"
  $Missing | ForEach-Object { Write-Host "- $_" }
  Write-Host "Recommended commands:"
  Write-Host "  winget install --id Python.Python.3.12 -e"
  Write-Host "  winget install --id OpenJS.NodeJS.LTS -e"
  Write-Host "  winget install --id Git.Git -e"
  if (-not $InstallSystemDeps) {
    Write-Host "Run .\scripts\bootstrap.ps1 --install-system-deps to install them, then retry."
    exit 2
  }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is unavailable. Follow docs\guides\installation.md and install the prerequisites manually."
  }
  & winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
  & winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements
  & winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  $PythonCommand = @(Find-Python)
  if ($PythonCommand.Count -eq 0 -or -not (Test-Node)) {
    throw "The runtime was installed, but this PowerShell session cannot see it yet. Reopen PowerShell and run .\scripts\quickstart.ps1."
  }
}

$PythonExecutable = $PythonCommand[0]
$PythonPrefix = @()
if ($PythonCommand.Count -gt 1) { $PythonPrefix = $PythonCommand[1..($PythonCommand.Count - 1)] }
& $PythonExecutable @PythonPrefix scripts\deploy.py install @DeployArgs
exit $LASTEXITCODE
