<#
.SYNOPSIS
  Build the Agent Switchboard release artifacts for GitHub:
    1) the bridge VSIX (vsce)            -> extensions/antigravity-agent-broker-bridge/*.vsix
    2) the self-contained agent-switchboard.exe (PyInstaller, embeds the VSIX + scripts) -> dist/

  Both the .py installer and the .exe register `<broker> serve` and install the bundled
  VSIX, and both expose a built-in uninstall. Upload dist/agent-switchboard.exe (+ optionally
  the .vsix) to the GitHub Release page.

.PARAMETER SkipVsix   Skip the VSIX build (reuse the newest existing one).
.PARAMETER SkipExe    Skip the exe build.
#>
param(
  [switch]$SkipVsix,
  [switch]$SkipExe
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$bridge = Join-Path $root 'extensions\antigravity-agent-broker-bridge'

function Resolve-Python {
  foreach ($c in @('python','py')) {
    $p = (Get-Command $c -ErrorAction SilentlyContinue)
    if ($p) { return $p.Source }
  }
  throw 'Python 3.10+ not found on PATH (needed to build the exe).'
}

if (-not $SkipVsix) {
  Write-Host '=== Building bridge VSIX (vsce) ===' -ForegroundColor Cyan
  Push-Location $bridge
  try {
    # --allow-missing-repository keeps a local/unpublished extension packageable.
    & npm.cmd exec --yes --package @vscode/vsce -- vsce package --allow-missing-repository
    if ($LASTEXITCODE -ne 0) { throw "vsce package failed (exit $LASTEXITCODE)" }
  } finally { Pop-Location }
  $newest = Get-ChildItem (Join-Path $bridge '*.vsix') | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  Write-Host "VSIX: $($newest.FullName)" -ForegroundColor Green
}

if (-not $SkipExe) {
  Write-Host '=== Building agent-switchboard.exe (PyInstaller) ===' -ForegroundColor Cyan
  $py = Resolve-Python
  & $py -c "import PyInstaller" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing pyinstaller...' -ForegroundColor Yellow
    & $py -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'pip install pyinstaller failed' }
  }
  Push-Location $root
  try {
    & $py -m PyInstaller --noconfirm 'agent-broker.spec'
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed (exit $LASTEXITCODE)" }
  } finally { Pop-Location }
  $exe = Join-Path $root 'dist\agent-switchboard.exe'
  if (Test-Path $exe) {
    Write-Host "EXE: $exe ($([math]::Round((Get-Item $exe).Length/1MB,1)) MB)" -ForegroundColor Green
  } else {
    throw 'Build reported success but dist\agent-switchboard.exe is missing.'
  }
}

Write-Host 'Done.' -ForegroundColor Green
