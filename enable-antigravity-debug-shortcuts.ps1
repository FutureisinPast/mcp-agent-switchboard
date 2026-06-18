param(
  [int]$Port = 9000
)

$ErrorActionPreference = "Stop"

function Resolve-AntigravityIcon {
  $local = [Environment]::GetFolderPath("LocalApplicationData")
  $candidates = @(
    (Join-Path $local "Programs\Antigravity IDE\Antigravity IDE.exe"),
    (Join-Path $local "Programs\Antigravity\Antigravity.exe")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
      return $candidate
    }
  }
  return $null
}

$wrapper = Join-Path $PSScriptRoot "start-antigravity-debug.ps1"
if (-not (Test-Path -LiteralPath $wrapper)) {
  throw "Missing debug launcher wrapper: $wrapper"
}

$powershell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
if (-not $powershell) {
  $powershell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
}
if (-not $powershell) {
  throw "Could not find powershell.exe or pwsh.exe"
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" -Port $Port"
$icon = Resolve-AntigravityIcon

$roots = @(
  (Join-Path $env:USERPROFILE "Desktop"),
  (Join-Path $env:PUBLIC "Desktop"),
  (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
  (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"),
  (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

$shell = New-Object -ComObject WScript.Shell
$backupDir = Join-Path $env:USERPROFILE (".agent-broker\shortcut-backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

$changed = @()
foreach ($root in $roots) {
  $links = Get-ChildItem -Path $root -Filter *.lnk -Recurse -ErrorAction SilentlyContinue
  foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link.FullName)
    $isAntigravity = $link.Name -match "Antigravity" -or $shortcut.TargetPath -like "*Antigravity*"
    if (-not $isAntigravity) {
      continue
    }

    $backupName = ($link.FullName -replace "[:\\\/]", "_")
    Copy-Item -LiteralPath $link.FullName -Destination (Join-Path $backupDir $backupName) -Force

    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $PSScriptRoot
    if ($icon) {
      $shortcut.IconLocation = "$icon,0"
    }
    $shortcut.Save()

    $changed += [pscustomobject]@{
      Shortcut = $link.FullName
      Target = $shortcut.TargetPath
      Arguments = $shortcut.Arguments
      Icon = $shortcut.IconLocation
      BackupDir = $backupDir
    }
  }
}

if (-not $changed) {
  Write-Warning "No Antigravity shortcuts found."
  exit 1
}

$changed | Format-List
