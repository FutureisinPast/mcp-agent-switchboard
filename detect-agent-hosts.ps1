$candidates = @(
  @{
    Name = "antigravity"
    Commands = @("antigravity")
    Exes = @((Join-Path $env:LOCALAPPDATA "Programs\Antigravity\Antigravity.exe"))
    ExtensionCommand = "antigravity"
    ExtensionArgs = @("--list-extensions", "--show-versions")
  },
  @{
    Name = "vscode"
    Commands = @("code")
    Exes = @((Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code\Code.exe"))
    ExtensionCommand = "code"
    ExtensionArgs = @("--list-extensions", "--show-versions")
  },
  @{
    Name = "codex"
    Commands = @("codex")
    Exes = @()
  },
  @{
    Name = "claude"
    Commands = @("claude")
    Exes = @()
  }
)

$debugPorts = @{
  antigravity = 9000
  vscode = 9010
}

$appPatterns = @{
  antigravity = @("Antigravity")
  vscode = @("Visual Studio Code", "VS Code")
  codex = @("Codex", "OpenAI", "ChatGPT")
  claude = @("Claude")
}

$startApps = @()
try {
  $startApps = Get-StartApps
} catch {
  $startApps = @()
}

$results = @()
foreach ($candidate in $candidates) {
  $command = $null
  foreach ($name in $candidate.Commands) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) {
      $command = $found.Source
      break
    }
  }
  $exe = $null
  foreach ($path in $candidate.Exes) {
    if ($path -and (Test-Path -LiteralPath $path)) {
      $exe = $path
      break
    }
  }
  $installed = [bool]($command -or $exe)
  $extensions = @()
  if ($installed -and $candidate.ExtensionCommand) {
    $extCommand = Get-Command $candidate.ExtensionCommand -ErrorAction SilentlyContinue
    if ($extCommand) {
      try {
        $extensions = & $extCommand.Source @($candidate.ExtensionArgs) 2>$null
      } catch {
        $extensions = @()
      }
    }
  }
  $appMatch = $null
  if ($appPatterns.ContainsKey($candidate.Name)) {
    foreach ($pattern in $appPatterns[$candidate.Name]) {
      $appMatch = $startApps | Where-Object {
        $_.Name -like "*$pattern*" -or $_.AppID -like "*$pattern*"
      } | Select-Object -First 1
      if ($appMatch) { break }
    }
  }
  $debugLive = $false
  if ($debugPorts.ContainsKey($candidate.Name)) {
    try {
      $null = Invoke-RestMethod "http://127.0.0.1:$($debugPorts[$candidate.Name])/json/version" -TimeoutSec 1
      $debugLive = $true
    } catch {
      $debugLive = $false
    }
  }
  $results += [pscustomobject]@{
    name = $candidate.Name
    installed = $installed
    command = $command
    exe = $exe
    appInstalled = [bool]$appMatch
    appId = if ($appMatch) { $appMatch.AppID } else { $null }
    hasAgentBrokerBridge = [bool]($extensions | Where-Object { $_ -match "agent-broker" })
    debugPort = if ($debugPorts.ContainsKey($candidate.Name)) { $debugPorts[$candidate.Name] } else { $null }
    debugLive = $debugLive
  }
}

$results | ConvertTo-Json -Depth 4
