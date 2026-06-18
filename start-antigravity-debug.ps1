param(
  [int]$Port = 9000,
  [switch]$NoReuseWindow,
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"

function Test-DebugPort {
  param([int]$Port)
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Resolve-AntigravityCommand {
  $local = [Environment]::GetFolderPath("LocalApplicationData")
  $candidates = @(
    (Join-Path $local "Programs\Antigravity IDE\bin\antigravity-ide.cmd"),
    (Join-Path $local "Programs\Antigravity IDE\Antigravity IDE.exe"),
    (Join-Path $local "Programs\Antigravity\bin\antigravity.cmd"),
    (Join-Path $local "Programs\Antigravity\Antigravity.exe")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
      return $candidate
    }
  }

  foreach ($name in @("antigravity-ide", "antigravity-ide.cmd", "antigravity", "antigravity.cmd")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) {
      return $command.Source
    }
  }

  throw "Could not find Antigravity IDE. Install Antigravity or pass a valid path in Agent Broker config."
}

function Focus-AntigravityWindow {
  param($Process)
  Add-Type @'
using System;
using System.Runtime.InteropServices;
public class AgentBrokerAgFocus {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
}
'@
  [AgentBrokerAgFocus]::ShowWindow($Process.MainWindowHandle, 9) | Out-Null
  [AgentBrokerAgFocus]::SetForegroundWindow($Process.MainWindowHandle) | Out-Null
}

$running = Get-Process -Name "Antigravity IDE", "Antigravity" -ErrorAction SilentlyContinue |
  Where-Object { $_.MainWindowHandle -ne 0 } |
  Select-Object -First 1
if ($running -and -not $NoReuseWindow) {
  Focus-AntigravityWindow -Process $running
  if (-not (Test-DebugPort -Port $Port)) {
    Write-Warning "Antigravity is already running without debug port $Port. Focused the existing window and did not open another. Close all Antigravity windows once and open it from this shortcut so the debug flags can apply."
  }
  exit 0
}

$command = Resolve-AntigravityCommand
$args = @("--remote-debugging-address=127.0.0.1", "--remote-debugging-port=$Port")
if (-not $NoReuseWindow) {
  $args += "--reuse-window"
}
$args += $RemainingArgs

$workDir = Split-Path -Parent $command
if ($command -like "*.cmd") {
  $workDir = Split-Path -Parent (Split-Path -Parent $command)
}

Push-Location $workDir
try {
  & $command @args
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
