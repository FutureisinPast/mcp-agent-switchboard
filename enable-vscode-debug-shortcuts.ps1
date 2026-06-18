param(
  [int]$Port = 9010
)

$flags = "--remote-debugging-address=127.0.0.1 --remote-debugging-port=$Port"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code"
$exe = Join-Path $installDir "Code.exe"
if (-not (Test-Path -LiteralPath $exe)) {
  throw "Could not find Code.exe at $exe"
}

$roots = @(
  (Join-Path $env:USERPROFILE "Desktop"),
  (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
  (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"),
  (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar")
) | Where-Object { Test-Path -LiteralPath $_ }

$shell = New-Object -ComObject WScript.Shell
$backupDir = Join-Path $env:USERPROFILE (".agent-broker\shortcut-backups\vscode-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

$changed = @()
foreach ($root in $roots) {
  $links = Get-ChildItem -Path $root -Filter *.lnk -Recurse -ErrorAction SilentlyContinue
  foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link.FullName)
    $target = [string]$shortcut.TargetPath
    $isCode = $link.Name -match "Visual Studio Code|VS Code|Code" -or $target -like "*Microsoft VS Code*"
    if (-not $isCode -or $target -notmatch "Code\.exe$") {
      continue
    }

    $backupName = ($link.FullName -replace "[:\\\/]", "_")
    Copy-Item -LiteralPath $link.FullName -Destination (Join-Path $backupDir $backupName) -Force

    $shortcut.TargetPath = $exe
    $shortcut.Arguments = $flags
    $shortcut.WorkingDirectory = $installDir
    $shortcut.Save()

    $changed += [pscustomobject]@{
      Shortcut = $link.FullName
      Target = $shortcut.TargetPath
      Arguments = $shortcut.Arguments
      BackupDir = $backupDir
    }
  }
}

if (-not $changed) {
  Write-Warning "No VS Code shortcuts found."
  exit 1
}

$changed | Format-List
