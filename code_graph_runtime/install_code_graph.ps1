<#
.SYNOPSIS
  Install/refresh the Switchboard code_graph runtime (graphify 0.9.77, code-only, no network at query time).
  Idempotent. Never touches agent-switchboard.exe, the Switchboard DB or any host config.
.PARAMETER CodeGraphHome  Install directory (default $HOME\.agent-broker\code-graph).
#>
param(
  [string]$CodeGraphHome = (Join-Path $HOME '.agent-broker\code-graph')
)
$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$repo = Split-Path -Parent $src
$utf8 = New-Object System.Text.UTF8Encoding($false)

New-Item -ItemType Directory -Force -Path $CodeGraphHome | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $CodeGraphHome 'guard') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $CodeGraphHome 'graphs') | Out-Null
$venvPy = Join-Path $CodeGraphHome 'venv\Scripts\python.exe'

if (-not (Test-Path $venvPy)) {
  Write-Host 'Creating venv (py -3.11)...'
  & py -3.11 -m venv (Join-Path $CodeGraphHome 'venv')
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
}
Write-Host 'Installing pinned requirements...'
& $venvPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $src 'requirements.lock.txt') pytest
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

foreach ($f in @('gfy_adapter.py','gfy_client.py','test_contract.py','ADAPTER_README.md','requirements.lock.txt','guard\sitecustomize.py')) {
  Copy-Item -Force (Join-Path $src $f) (Join-Path $CodeGraphHome $f)
}

# projects.json: create only if absent; otherwise add agent-broker only if missing.
$pj = Join-Path $CodeGraphHome 'projects.json'
$repoReal = (Resolve-Path -LiteralPath $repo).ProviderPath
$entry = @{ root = $repoReal; out_dir = (Join-Path $CodeGraphHome 'graphs\agent-broker') }
if (-not (Test-Path $pj)) {
  $obj = [ordered]@{ 'agent-broker' = $entry }
  [IO.File]::WriteAllText($pj, ($obj | ConvertTo-Json -Depth 5), $utf8)
  Write-Host 'projects.json created.'
} else {
  $raw = [IO.File]::ReadAllText($pj, $utf8)
  $cur = $raw | ConvertFrom-Json
  if ($cur.PSObject.Properties.Name -contains 'agent-broker') {
    Write-Host 'projects.json already has agent-broker; left unchanged.'
  } else {
    $cur | Add-Member -NotePropertyName 'agent-broker' -NotePropertyValue ([pscustomobject]$entry)
    [IO.File]::WriteAllText($pj, ($cur | ConvertTo-Json -Depth 5), $utf8)
    Write-Host 'agent-broker entry added to projects.json.'
  }
}

$driver = @'
import sys, json
sys.path.insert(0, sys.argv[1])
from gfy_client import Adapter
a = Adapter()
try:
    for op in ("refresh", "health"):
        r, line, dt = a.call(op=op, project="agent-broker")
        print(op, "->", line[:1500])
finally:
    a.close()
'@
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('cg_install_' + [guid]::NewGuid().ToString('N') + '.py')
[IO.File]::WriteAllText($tmp, $driver, $utf8)
try { & $venvPy $tmp $CodeGraphHome } finally { Remove-Item -Force $tmp -ErrorAction SilentlyContinue }
Write-Host "code_graph runtime installed at $CodeGraphHome"
