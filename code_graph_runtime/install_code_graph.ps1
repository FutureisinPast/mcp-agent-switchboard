<#
.SYNOPSIS
  Install/refresh the Switchboard code_graph runtime (graphify 0.9.77, code-only, no network at query time).
  Idempotent. Never touches agent-switchboard.exe, the Switchboard DB or any host config.
  The runtime is copied into an immutable release directory <home>eleases\<12-char sha256>, validated
  (py_compile + a stdio `health` request, no refresh), and only then published by atomically writing the
  marker <home>untime.json (always written last). Bridges hot-reload from the marker. Legacy top-level
  copies are kept for already-installed bridges.
.PARAMETER CodeGraphHome  Install directory (default $HOME\.agent-broker\code-graph).
.PARAMETER SkipPip        Do not (re)install the pinned requirements (fast re-publish of runtime files).
#>
param(
  [string]$CodeGraphHome = (Join-Path $HOME '.agent-broker\code-graph'),
  [switch]$SkipPip
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
if (-not $SkipPip) {
  Write-Host 'Installing pinned requirements...'
  & $venvPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $src 'requirements.lock.txt') pytest
  if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
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

# ---- immutable release: copy -> validate -> legacy copies -> marker LAST --------------------------------
$runtimeFiles = @('gfy_adapter.py','gfy_client.py','guard\sitecustomize.py')
$lines = foreach ($f in ($runtimeFiles | Sort-Object)) {
  $h = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $src $f)).Hash.ToLower()
  ($f.Replace('\','/') + ':' + $h)
}
$sha = [Security.Cryptography.SHA256]::Create()
$fullHash = ([BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes(($lines -join "`n")))) -replace '-','').ToLower()
$releaseId = $fullHash.Substring(0,12)
$releasesDir = Join-Path $CodeGraphHome 'releases'
$releaseDir = Join-Path $releasesDir $releaseId
New-Item -ItemType Directory -Force -Path $releasesDir | Out-Null
if (Test-Path -LiteralPath $releaseDir) {
  Write-Host "release $releaseId already exists; reusing."
} else {
  $tmpDir = Join-Path $releasesDir ('.tmp-' + [guid]::NewGuid().ToString('N'))
  try {
    New-Item -ItemType Directory -Force -Path (Join-Path $tmpDir 'guard') | Out-Null
    foreach ($f in $runtimeFiles) { Copy-Item -Force (Join-Path $src $f) (Join-Path $tmpDir $f) }
    Move-Item -LiteralPath $tmpDir -Destination $releaseDir
  } finally {
    if (Test-Path -LiteralPath $tmpDir) { Remove-Item -Recurse -Force -LiteralPath $tmpDir -ErrorAction SilentlyContinue }
  }
  Write-Host "release $releaseId created."
}

# validate BEFORE publishing: syntax, then a side-effect-free health request over stdio
foreach ($f in ($runtimeFiles | Where-Object { $_ -like '*.py' })) {
  & $venvPy -m py_compile (Join-Path $releaseDir $f)
  if ($LASTEXITCODE -ne 0) { throw "release $releaseId failed py_compile ($f); marker NOT updated" }
}
$health = @'
import sys, json, subprocess, threading, os
py, adapter, projects = sys.argv[1:4]
env = dict(os.environ, PYTHONHASHSEED="0", PYTHONUTF8="1")
p = subprocess.Popen([py, adapter, "--projects", projects], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
res = []
def rd():
    try:
        res.append(p.stdout.readline())
    except Exception:
        pass
t = threading.Thread(target=rd, daemon=True); t.start()
try:
    p.stdin.write(json.dumps({"id": 1, "op": "health", "max_chars": 500}) + "\n"); p.stdin.flush()
    t.join(60)
    ok = bool(res) and json.loads(res[0]).get("ok") is True
except Exception:
    ok = False
finally:
    try: p.stdin.close()
    except Exception: pass
    try: p.kill()
    except Exception: pass
print("health", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
'@
$hp = Join-Path ([IO.Path]::GetTempPath()) ('cg_health_' + [guid]::NewGuid().ToString('N') + '.py')
[IO.File]::WriteAllText($hp, $health, $utf8)
try {
  & $venvPy $hp $venvPy (Join-Path $releaseDir 'gfy_adapter.py') $pj
  if ($LASTEXITCODE -ne 0) { throw "release $releaseId failed the stdio health check; marker NOT updated" }
} finally { Remove-Item -Force $hp -ErrorAction SilentlyContinue }

# legacy top-level copies (bridges installed before the marker existed read these)
foreach ($f in @('gfy_adapter.py','gfy_client.py','test_contract.py','ADAPTER_README.md','requirements.lock.txt','guard\sitecustomize.py')) {
  Copy-Item -Force (Join-Path $src $f) (Join-Path $CodeGraphHome $f)
}

# marker: temp file then atomic replace; ALWAYS the last write
$markerPath = Join-Path $CodeGraphHome 'runtime.json'
$prevRelease = $null
if (Test-Path -LiteralPath $markerPath) {
  try { $prevRelease = ([IO.File]::ReadAllText($markerPath, $utf8) | ConvertFrom-Json).release } catch { $prevRelease = $null }
}
$markerObj = [ordered]@{ release = $releaseId; hash = $fullHash; installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
$markerTmp = $markerPath + '.tmp'
[IO.File]::WriteAllText($markerTmp, ($markerObj | ConvertTo-Json), $utf8)
if (Test-Path -LiteralPath $markerPath) { [IO.File]::Replace($markerTmp, $markerPath, $null) } else { [IO.File]::Move($markerTmp, $markerPath) }
Write-Host "runtime.json -> release $releaseId"

# prune: keep current + previous; remove older only when no process is using them (unsure -> keep)
try {
  $procs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction Stop)
  foreach ($d in (Get-ChildItem -LiteralPath $releasesDir -Directory)) {
    if ($d.Name -eq $releaseId -or $d.Name -eq $prevRelease) { continue }
    if ($d.Name -like '.tmp-*') {
      if ($d.LastWriteTime -lt (Get-Date).AddDays(-1)) { Remove-Item -Recurse -Force -LiteralPath $d.FullName -ErrorAction SilentlyContinue }
      continue
    }
    $needle = [IO.Path]::Combine('releases', $d.Name)
    $inUse = $procs | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
    if (-not $inUse) { Remove-Item -Recurse -Force -LiteralPath $d.FullName -ErrorAction SilentlyContinue; Write-Host "pruned release $($d.Name)" }
  }
} catch { Write-Host "release pruning skipped: $($_.Exception.Message)" }

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
