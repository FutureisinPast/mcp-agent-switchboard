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
    Add-Action "shortcuts" "unchanged" "$HostName shortcuts had no Agent Broker debug flags."
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
