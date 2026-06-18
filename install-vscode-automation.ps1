param(
  [switch]$EnableDebugShortcut,
  [int]$DebugPort = 9010,
  [string]$VsixPath = ""
)

$code = Get-Command code -ErrorAction SilentlyContinue
if (-not $code) {
  throw "VS Code command 'code' was not found."
}

if (-not $VsixPath) {
  $latestVsix = Get-ChildItem -Path (Join-Path $env:USERPROFILE ".agent-broker\extensions") -Filter "antigravity-agent-broker-bridge-*.vsix" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if (-not $latestVsix) {
    throw "No bridge VSIX was found under $env:USERPROFILE\.agent-broker\extensions"
  }
  $VsixPath = $latestVsix.FullName
}
if (-not (Test-Path -LiteralPath $VsixPath)) {
  throw "Bridge VSIX was not found at $VsixPath"
}

& $code.Source --install-extension $VsixPath --force
if ($LASTEXITCODE -ne 0) {
  throw "VS Code extension install failed with exit code $LASTEXITCODE"
}

if ($EnableDebugShortcut) {
  & powershell -ExecutionPolicy Bypass -File (Join-Path $env:USERPROFILE ".agent-broker\enable-vscode-debug-shortcuts.ps1") -Port $DebugPort
}

$installed = & $code.Source --list-extensions --show-versions | Where-Object { $_ -match "agent-broker|antigravity-agent-broker" }
[pscustomobject]@{
  vscode = $code.Source
  installedBridge = $installed
  debugShortcutEnabled = [bool]$EnableDebugShortcut
  debugPort = if ($EnableDebugShortcut) { $DebugPort } else { $null }
} | ConvertTo-Json -Depth 4
