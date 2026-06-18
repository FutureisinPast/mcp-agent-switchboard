param(
  [string]$PythonPath = "",
  [switch]$DryRun,
  [switch]$NoDebugShortcuts
)

$ErrorActionPreference = "Stop"
$brokerDir = $PSScriptRoot
$setup = Join-Path $brokerDir "setup.py"

if (-not (Test-Path -LiteralPath $setup)) {
  throw "setup.py was not found next to this script: $setup"
}

function Resolve-Python {
  param([string]$ExplicitPath)
  if ($ExplicitPath) {
    if (-not (Test-Path -LiteralPath $ExplicitPath)) {
      throw "PythonPath was not found: $ExplicitPath"
    }
    return (Resolve-Path -LiteralPath $ExplicitPath).Path
  }

  foreach ($name in @("python", "py")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) {
      return $command.Source
    }
  }

  throw "Python was not found. Install Python 3.10+ or pass -PythonPath."
}

$python = Resolve-Python $PythonPath
$args = @($setup, "install")
if (-not $NoDebugShortcuts) {
  $args += "--debug-port"
}
if ($DryRun) {
  $args += "--dry-run"
}

& $python @args
exit $LASTEXITCODE
