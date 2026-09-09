'use strict';

const fs = require('fs');
const path = require('path');

function commandForBroker(brokerPath, pythonPath) {
  if (/\.exe$/i.test(brokerPath)) {
    return {
      command: brokerPath,
      argsPrefix: [],
      kind: 'executable',
    };
  }
  return {
    command: pythonPath,
    argsPrefix: [brokerPath],
    kind: 'python',
  };
}

function resolveBrokerCommand(options = {}) {
  const existsSync = options.existsSync || fs.existsSync;
  const homeDir = options.homeDir;
  const pythonPath = String(options.pythonPath || 'python');
  const explicitPath = String(options.brokerPath || '').trim();
  const checkedPaths = [];

  if (explicitPath) {
    checkedPaths.push(explicitPath);
    if (existsSync(explicitPath)) {
      return {
        ok: true,
        brokerPath: explicitPath,
        checkedPaths,
        ...commandForBroker(explicitPath, pythonPath),
      };
    }
    return {
      ok: false,
      checkedPaths,
      error: `Configured Agent Switchboard path does not exist: ${explicitPath}. Update agentBrokerBridge.brokerPath or clear it to enable auto-detection.`,
    };
  }

  const installDir = path.join(homeDir, '.agent-broker');
  const candidates = [
    path.join(installDir, 'agent-switchboard.exe'),
    path.join(installDir, 'agent_broker_mcp.py'),
  ];
  for (const candidate of candidates) {
    checkedPaths.push(candidate);
    if (existsSync(candidate)) {
      return {
        ok: true,
        brokerPath: candidate,
        checkedPaths,
        ...commandForBroker(candidate, pythonPath),
      };
    }
  }
  return {
    ok: false,
    checkedPaths,
    error: `Agent Switchboard launcher was not found. Checked: ${checkedPaths.join(', ')}. Install Agent Switchboard or set agentBrokerBridge.brokerPath to an existing executable or Python script.`,
  };
}

function buildBrokerInvocation(resolution, args = []) {
  if (!resolution || !resolution.ok) {
    throw new Error((resolution && resolution.error) || 'Agent Switchboard launcher is unavailable.');
  }
  return {
    command: resolution.command,
    args: [...resolution.argsPrefix, 'bridge', ...args],
  };
}

module.exports = { buildBrokerInvocation, resolveBrokerCommand };
