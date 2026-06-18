param(
  [switch]$DryRun,
  [switch]$KeepExtensions,
  [switch]$KeepMcpConfig,
  [switch]$KeepShortcuts,
  [switch]$RemoveData
)

$ErrorActionPreference = "Stop"

$brokerDir = Join-Path $env:USERPROFILE ".agent-broker"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $brokerDir "uninstall-backups\$timestamp"
$extensionId = "local.antigravity-agent-broker-bridge"
$actions = New-Object System.Collections.Generic.List[object]

function Add-Action {
  param(
    [string]$Area,
    [string]$Status,
    [string]$Detail
  )
  $script:actions.Add([pscustomobject]@{
    area = $Area
    status = $Status
    detail = $Detail
  }) | Out-Null
}

function Ensure-BackupDir {
  if (-not $DryRun -and -not (Test-Path -LiteralPath $backupDir)) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
  }
}

function Backup-File {
  param([string]$Path)
  Ensure-BackupDir
  $name = ($Path -replace "[:\\\/]", "_")
  $dest = Join-Path $backupDir $name
  if (-not $DryRun) {
    Copy-Item -LiteralPath $Path -Destination $dest -Force
  }
  return $dest
}

function Remove-DebugFlags {
  param([string]$Arguments)
  if (-not $Arguments) {
    return ""
  }
  $text = " $Arguments "
  $patterns = @(
    "\s+--remote-debugging-address(?:=|\s+)\S+",
    "\s+--remote-debugging-port(?:=|\s+)\S+",
    "\s+--remote-allow-origins(?:=|\s+)\S+"
  )
  foreach ($pattern in $patterns) {
    $text = [regex]::Replace($text, $pattern, " ")
  }
  return (($text -replace "\s+", " ").Trim())
}

function Uninstall-BridgeExtension {
  param(
    [string]$HostName,
    [string]$CommandName
  )
  $command = Get-Command $CommandName -ErrorAction SilentlyContinue
  if (-not $command) {
    Add-Action "extensions" "skipped" "$HostName CLI command '$CommandName' was not found."
    return
  }
  if ($DryRun) {
    Add-Action "extensions" "dry-run" "$HostName would run: $($command.Source) --uninstall-extension $extensionId"
    return
  }
  & $command.Source --uninstall-extension $extensionId
  if ($LASTEXITCODE -eq 0) {
    Add-Action "extensions" "removed" "$HostName extension $extensionId"
  } else {
    Add-Action "extensions" "error" "$HostName extension uninstall exited with code $LASTEXITCODE"
  }
}

function Resolve-AntigravityExe {
  # Prefer the real .exe (the best target for a Start-Menu .lnk), then the bin launcher
  # .cmd, then anything on PATH - so an Antigravity installed via .cmd / a PATH entry
  # (exactly what start-antigravity-debug.ps1 itself resolves) can still be restored to,
  # not only the two exe locations. Otherwise the fallback silently fails on such installs.
  $local = [Environment]::GetFolderPath("LocalApplicationData")
  $candidates = @(
    (Join-Path $local "Programs\Antigravity IDE\Antigravity IDE.exe"),
    (Join-Path $local "Programs\Antigravity\Antigravity.exe"),
    (Join-Path $local "Programs\Antigravity IDE\bin\antigravity-ide.cmd"),
    (Join-Path $local "Programs\Antigravity\bin\antigravity.cmd")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }
  foreach ($name in @("antigravity-ide", "antigravity-ide.cmd", "antigravity", "antigravity.cmd")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
  }
  return $null
}

function Get-OriginalShortcutFromBackup {
  param([string]$LinkPath, [object]$Shell)
  # The enable script copies each shortcut to ~/.agent-broker/shortcut-backups/<ts>/<mangled>
  # BEFORE replacing it. Return the EARLIEST backup whose target is a real exe (not the
  # powershell wrapper) = the true pre-patch original. (A later backup may itself be an
  # already-patched wrapper if the user enabled twice, so we skip powershell-target ones.)
  # Sort by the timestamped PARENT DIR NAME (yyyyMMdd-HHmmss), not LastWriteTime: Copy-Item
  # preserves the SOURCE .lnk's mtime on the backup, so file mtime is not the capture order.
  $backupRoot = Join-Path $env:USERPROFILE ".agent-broker\shortcut-backups"
  if (-not (Test-Path -LiteralPath $backupRoot)) { return $null }
  $name = ($LinkPath -replace "[:\\\/]", "_")
  $candidates = Get-ChildItem -Path $backupRoot -Recurse -ErrorAction SilentlyContinue |
    Where-Object { -not $_.PSIsContainer -and $_.Name -eq $name } |
    Sort-Object { $_.Directory.Name }
  foreach ($candidate in $candidates) {
    try { $sc = $Shell.CreateShortcut($candidate.FullName) } catch { continue }
    if ($sc.TargetPath -and $sc.TargetPath -notlike "*powershell*" -and $sc.TargetPath -notlike "*pwsh*") {
      return $sc
    }
  }
  return $null
}

function Restore-DebugShortcuts {
  param(
    [string]$HostName,
    [string[]]$NamePatterns,
    [string[]]$TargetPatterns
  )
  $roots = @(
    (Join-Path $env:USERPROFILE "Desktop"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
    (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"),
    (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar")
  ) | Where-Object { Test-Path -LiteralPath $_ }

  if (-not $roots) {
    Add-Action "shortcuts" "skipped" "No shortcut roots were found."
    return
  }

  $shell = New-Object -ComObject WScript.Shell
  $changed = 0
  foreach ($root in $roots) {
    $links = Get-ChildItem -Path $root -Filter *.lnk -Recurse -ErrorAction SilentlyContinue
    foreach ($link in $links) {
      $shortcut = $shell.CreateShortcut($link.FullName)
      $target = [string]$shortcut.TargetPath
      $nameMatch = $false
      foreach ($pattern in $NamePatterns) {
        if ($link.Name -match $pattern) {
          $nameMatch = $true
          break
        }
      }
      $targetMatch = $false
      foreach ($pattern in $TargetPatterns) {
        if ($target -like $pattern) {
          $targetMatch = $true
          break
        }
      }
      if (-not ($nameMatch -or $targetMatch)) {
        continue
      }

      $oldArgs = [string]$shortcut.Arguments
      # Only Antigravity's enable patch REPLACES the shortcut with a powershell wrapper
      # (VS Code's points straight at Code.exe), and that wrapper always invokes
      # start-antigravity-debug.ps1. Gate strictly to the Antigravity host AND that script
      # name, so we can never clobber an unrelated user shortcut that merely launches
      # powershell with a -Port argument and happens to match the VS Code name patterns.
      $isWrapper = ($HostName -eq 'Antigravity') -and `
                   (($target -like "*powershell*") -or ($target -like "*pwsh*")) -and `
                   ($oldArgs -like "*start-antigravity-debug*")
      if ($isWrapper) {
        # There are NO inline --remote-debugging flags to strip here (the old code did
        # nothing, leaving Antigravity launching through the debug wrapper after uninstall).
        # Restore the pre-patch original from backup; else repoint straight at the installed
        # Antigravity launcher so it opens normally with no debug port.
        $backup = Backup-File $link.FullName
        $orig = Get-OriginalShortcutFromBackup -LinkPath $link.FullName -Shell $shell
        # Trust the backup only if its target still exists (the user may have migrated from
        # "Antigravity" to "Antigravity IDE"); otherwise fall back to the installed launcher.
        $useBackup = $orig -and $orig.TargetPath -and (Test-Path -LiteralPath $orig.TargetPath)
        $exe = if ($useBackup) { $null } else { Resolve-AntigravityExe }
        if ($useBackup) {
          if (-not $DryRun) {
            $shortcut.TargetPath = $orig.TargetPath
            $shortcut.Arguments = $orig.Arguments
            $shortcut.WorkingDirectory = $orig.WorkingDirectory
            if ($orig.IconLocation) { $shortcut.IconLocation = $orig.IconLocation }
            $shortcut.Save()
          }
          $changed += 1
          Add-Action "shortcuts" ($(if ($DryRun) { "dry-run" } else { "restored" })) "$HostName shortcut $($link.FullName) (restored from backup); backup $backup"
        } elseif ($exe) {
          if (-not $DryRun) {
            $shortcut.TargetPath = $exe
            $shortcut.Arguments = ""
            $shortcut.WorkingDirectory = (Split-Path $exe)
            $shortcut.IconLocation = "$exe,0"
            $shortcut.Save()
          }
          $changed += 1
          Add-Action "shortcuts" ($(if ($DryRun) { "dry-run" } else { "restored" })) "$HostName shortcut $($link.FullName) (repointed to Antigravity launcher); backup $backup"
        } else {
          # Neither a usable backup nor an installed Antigravity launcher: do NOT claim
          # success (the old code falsely reported "restored" while changing nothing). Leave
          # the now-defunct wrapper and tell the user to delete it manually.
          Add-Action "shortcuts" "warning" "$HostName shortcut $($link.FullName) still points at the debug wrapper; no backup and no installed Antigravity found to restore to - delete this shortcut manually. backup $backup"
        }
        continue
      }
      $newArgs = Remove-DebugFlags $oldArgs
      if ($newArgs -eq $oldArgs) {
        continue
      }

      $backup = Backup-File $link.FullName
      if (-not $DryRun) {
        $shortcut.Arguments = $newArgs
        $shortcut.Save()
      }
      $changed += 1
      Add-Action "shortcuts" ($(if ($DryRun) { "dry-run" } else { "restored" })) "$HostName shortcut $($link.FullName); backup $backup"
    }
  }

  if ($changed -eq 0) {
    Add-Action "shortcuts" "unchanged" "$HostName shortcuts had no Agent Switchboard debug flags."
  }
}

function Remove-CodexMcpConfig {
  $path = Join-Path $env:USERPROFILE ".codex\config.toml"
  if (-not (Test-Path -LiteralPath $path)) {
    Add-Action "mcp" "skipped" "Codex config was not found."
    return
  }
  $text = Get-Content -LiteralPath $path -Raw
  $updated = $text
  $patterns = @(
    '(?ms)^\[mcp_servers\.agent_broker\.env\]\r?\n.*?(?=^\[|\z)',
    '(?ms)^\[mcp_servers\.agent_broker\]\r?\n.*?(?=^\[|\z)',
    '(?ms)^\[mcp_servers\."agent-broker"\.env\]\r?\n.*?(?=^\[|\z)',
    '(?ms)^\[mcp_servers\."agent-broker"\]\r?\n.*?(?=^\[|\z)'
  )
  foreach ($pattern in $patterns) {
    $updated = [regex]::Replace($updated, $pattern, "")
  }
  if ($updated -eq $text) {
    Add-Action "mcp" "unchanged" "Codex config had no agent-broker MCP block."
    return
  }
  $backup = Backup-File $path
  if (-not $DryRun) {
    Set-Content -LiteralPath $path -Value $updated.TrimStart() -Encoding UTF8
  }
  Add-Action "mcp" ($(if ($DryRun) { "dry-run" } else { "removed" })) "Codex MCP config; backup $backup"
}

function Remove-McpServersRecursive {
  param([object]$Node)
  $removed = 0
  if ($null -eq $Node) {
    return 0
  }
  if ($Node -is [System.Array]) {
    foreach ($item in $Node) {
      $removed += Remove-McpServersRecursive $item
    }
    return $removed
  }
  if ($Node -isnot [pscustomobject]) {
    return 0
  }

  $props = @($Node.PSObject.Properties.Name)
  if ($props -contains "mcpServers") {
    $servers = $Node.mcpServers
    if ($servers -is [pscustomobject]) {
      foreach ($key in @("agent-broker", "agent_broker")) {
        if (@($servers.PSObject.Properties.Name) -contains $key) {
          $servers.PSObject.Properties.Remove($key)
          $removed += 1
        }
      }
    }
  }

  foreach ($prop in @($Node.PSObject.Properties)) {
    $removed += Remove-McpServersRecursive $prop.Value
  }
  return $removed
}

function Remove-JsonMcpConfig {
  param(
    [string]$Label,
    [string]$Path
  )
  if (-not (Test-Path -LiteralPath $Path)) {
    Add-Action "mcp" "skipped" "$Label config was not found at $Path"
    return
  }
  try {
    $raw = Get-Content -LiteralPath $Path -Raw
    $json = $raw | ConvertFrom-Json
    $removed = Remove-McpServersRecursive $json
  } catch {
    Add-Action "mcp" "error" "$Label config could not be parsed: $($_.Exception.Message)"
    return
  }
  if ($removed -eq 0) {
    Add-Action "mcp" "unchanged" "$Label config had no agent-broker MCP server."
    return
  }
  $backup = Backup-File $Path
  if (-not $DryRun) {
    $json | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $Path -Encoding UTF8
  }
  Add-Action "mcp" ($(if ($DryRun) { "dry-run" } else { "removed" })) "$Label MCP config entries: $removed; backup $backup"
}

if (-not $KeepExtensions) {
  Uninstall-BridgeExtension "Antigravity" "antigravity"
  Uninstall-BridgeExtension "VS Code" "code"
} else {
  Add-Action "extensions" "skipped" "KeepExtensions was set."
}

if (-not $KeepShortcuts) {
  Restore-DebugShortcuts "Antigravity" @("Antigravity") @("*Antigravity*")
  Restore-DebugShortcuts "VS Code" @("Visual Studio Code", "VS Code", "^Code") @("*Microsoft VS Code*", "*\Code.exe")
} else {
  Add-Action "shortcuts" "skipped" "KeepShortcuts was set."
}

if (-not $KeepMcpConfig) {
  Remove-CodexMcpConfig
  Remove-JsonMcpConfig "Antigravity" (Join-Path $env:APPDATA "Antigravity\User\mcp_config.json")
  Remove-JsonMcpConfig "VS Code" (Join-Path $env:APPDATA "Code\User\mcp.json")
  Remove-JsonMcpConfig "Claude" (Join-Path $env:USERPROFILE ".claude.json")
} else {
  Add-Action "mcp" "skipped" "KeepMcpConfig was set."
}

if ($RemoveData) {
  $archive = Join-Path $env:USERPROFILE (".agent-broker.uninstalled.$timestamp")
  if ($DryRun) {
    Add-Action "data" "dry-run" "Would move $brokerDir to $archive"
  } else {
    Move-Item -LiteralPath $brokerDir -Destination $archive
    Add-Action "data" "moved" "$brokerDir moved to $archive"
  }
} else {
  Add-Action "data" "kept" "$brokerDir was kept. Pass -RemoveData to move it out of service."
}

$actions | ConvertTo-Json -Depth 5
