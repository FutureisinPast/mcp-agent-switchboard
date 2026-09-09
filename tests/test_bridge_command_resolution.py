"""Focused tests for the Antigravity bridge launcher resolver."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "extensions" / "antigravity-agent-broker-bridge" / "broker_command.js"


def run_helper(options: dict, args: list[str] | None = None) -> dict:
    script = """
const helper = require(process.argv[1]);
const input = JSON.parse(process.argv[2]);
const existing = new Set(input.existing || []);
const resolution = helper.resolveBrokerCommand({
  brokerPath: input.brokerPath || '',
  pythonPath: input.pythonPath || 'python',
  homeDir: input.homeDir,
  existsSync: value => existing.has(value),
});
const invocation = resolution.ok
  ? helper.buildBrokerInvocation(resolution, input.args || [])
  : null;
process.stdout.write(JSON.stringify({ resolution, invocation }));
"""
    payload = dict(options)
    payload["args"] = args or []
    result = subprocess.run(
        ["node", "-e", script, str(HELPER), json.dumps(payload)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class BridgeCommandResolutionTests(unittest.TestCase):
    def test_packaged_executable_is_default_and_receives_bridge_argv(self):
        home = r"C:\Users\tester"
        executable = str(Path(home) / ".agent-broker" / "agent-switchboard.exe")
        result = run_helper(
            {"homeDir": home, "existing": [executable]},
            ["requests", "*", "50"],
        )
        self.assertEqual(result["resolution"]["kind"], "executable")
        self.assertEqual(result["invocation"]["command"], executable)
        self.assertEqual(result["invocation"]["args"], ["bridge", "requests", "*", "50"])

    def test_source_script_is_python_fallback(self):
        home = r"C:\Users\tester"
        script = str(Path(home) / ".agent-broker" / "agent_broker_mcp.py")
        result = run_helper(
            {"homeDir": home, "pythonPath": "py-custom", "existing": [script]},
            ["heartbeat"],
        )
        self.assertEqual(result["resolution"]["kind"], "python")
        self.assertEqual(result["invocation"]["command"], "py-custom")
        self.assertEqual(result["invocation"]["args"], [script, "bridge", "heartbeat"])

    def test_explicit_executable_override_wins(self):
        explicit = r"D:\tools\switchboard-custom.EXE"
        default = r"C:\Users\tester\.agent-broker\agent-switchboard.exe"
        result = run_helper(
            {"homeDir": r"C:\Users\tester", "brokerPath": explicit, "existing": [explicit, default]},
            ["claim"],
        )
        self.assertEqual(result["invocation"], {"command": explicit, "args": ["bridge", "claim"]})
        self.assertEqual(result["resolution"]["checkedPaths"], [explicit])

    def test_explicit_script_override_uses_configured_python(self):
        explicit = r"D:\source\agent_broker_mcp.py"
        result = run_helper(
            {
                "homeDir": r"C:\Users\tester",
                "brokerPath": explicit,
                "pythonPath": r"D:\Python\python.exe",
                "existing": [explicit],
            },
            ["resume-model"],
        )
        self.assertEqual(result["invocation"]["command"], r"D:\Python\python.exe")
        self.assertEqual(result["invocation"]["args"], [explicit, "bridge", "resume-model"])

    def test_missing_candidates_return_actionable_diagnostic(self):
        result = run_helper({"homeDir": r"C:\Users\tester", "existing": []})
        resolution = result["resolution"]
        self.assertFalse(resolution["ok"])
        self.assertIsNone(result["invocation"])
        self.assertIn("agent-switchboard.exe", resolution["error"])
        self.assertIn("agent_broker_mcp.py", resolution["error"])
        self.assertIn("agentBrokerBridge.brokerPath", resolution["error"])
        self.assertEqual(len(resolution["checkedPaths"]), 2)

    def test_missing_explicit_path_does_not_silently_fall_back(self):
        explicit = r"D:\missing\broker.exe"
        default = r"C:\Users\tester\.agent-broker\agent-switchboard.exe"
        result = run_helper(
            {"homeDir": r"C:\Users\tester", "brokerPath": explicit, "existing": [default]}
        )
        self.assertFalse(result["resolution"]["ok"])
        self.assertEqual(result["resolution"]["checkedPaths"], [explicit])
        self.assertIn("clear it to enable auto-detection", result["resolution"]["error"])


if __name__ == "__main__":
    unittest.main()
