"""Tests for setup.py's registration-visibility helpers added alongside WP1:
`host_is_installed`, `missing_registrations`, and `antigravity_profile_report`.

Everything here operates on temp paths patched onto the `setup` module's globals --
never on the real ~/.codex, ~/.claude.json, %APPDATA%, or ~/.agent-broker. Fixture
style copied from tests/test_registration_repair.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import setup  # noqa: E402
import agent_broker_entry  # noqa: E402
import agent_broker_mcp as broker  # noqa: E402


class RegistrationHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # Same isolation contract as test_registration_repair.py: every host config +
        # BROKER_HOME points at nonexistent temp locations, and every CLI-detection
        # helper is neutered, so nothing on the real machine (~/.codex, ~/.claude.json,
        # real %APPDATA%/%LOCALAPPDATA%, ~/.agent-broker) is ever read or touched.
        self._patch("CODEX_TOML", self.root / "codex" / "config.toml")
        self._patch("CLAUDE_JSON", self.root / "claude" / ".claude.json")
        self._patch("CLAUDE_DESKTOP_CONFIG", self.root / "claude_desktop" / "claude_desktop_config.json")
        legacy_dir = self.root / "appdata_missing" / "Antigravity IDE" / "User"
        authoritative = self.root / "home" / ".gemini" / "config" / "mcp_config.json"
        self._patch("ANTIGRAVITY_USER_DIRS", [legacy_dir])
        self._patch("ANTIGRAVITY_MCP", authoritative)
        self._patch("ANTIGRAVITY_LEGACY_MCP_CANDIDATES", [legacy_dir / "mcp_config.json"])
        self._patch("ANTIGRAVITY_MCP_CANDIDATES", [authoritative, legacy_dir / "mcp_config.json"])
        self._patch("VSCODE_MCP", self.root / "vscode_missing" / "mcp.json")
        self._patch("BROKER_HOME", self.root / "broker")
        self._patch("_backup_root", None)
        self._patch("FROZEN", False)
        # host_is_installed("vscode") checks (APPDATA / "Code" / "User").exists()
        # directly, independent of VSCODE_MCP -- patch APPDATA too so a real VS Code
        # install on this box can't make the "nothing installed" case flaky.
        self._patch("APPDATA", self.root / "appdata_missing")

        # host_is_installed() falls back to CLI probes (which()/antigravity_cli()/
        # vscode_cli()/claude_desktop_installed()) when a config file is absent. Those
        # helpers read shutil.which(), LOCALAPPDATA, and (for Claude Desktop) shell out
        # to powershell's AppX registry -- all real-machine state. Patch them to no-op
        # module globals so "not installed" is actually determined by our temp fixture,
        # not by whatever happens to be on this box.
        self._patch("which", lambda name: None)
        self._patch("antigravity_cli", lambda: None)
        self._patch("vscode_cli", lambda: None)
        self._patch("claude_desktop_installed", lambda: False)

    def _patch(self, name, value):
        original = getattr(setup, name)
        setattr(setup, name, value)
        self.addCleanup(setattr, setup, name, original)

    def _write_json(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _registration(self, env=None):
        config = setup.antigravity_mcp_path()
        self._write_json(config, {
            "mcpServers": {
                "agent-switchboard": {
                    "command": sys.executable,
                    "args": [str(REPO_ROOT / "agent_broker_mcp.py")],
                    "env": env or {"PYTHONUTF8": "1"},
                }
            }
        })
        return config

    @staticmethod
    def _stdio(*, names=None, initialize=True, call=True):
        rows = []
        if initialize:
            rows.append({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}})
        else:
            rows.append({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}})
        rows.append({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": name} for name in (names or [])]}})
        rows.append(
            {"jsonrpc": "2.0", "id": 3, "result": {"content": []}}
            if call else
            {"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "call failed"}}
        )
        return "\n".join(json.dumps(row) for row in rows) + "\n"

    def _fake_popen(self, stdout="", timeout=False, capture=None):
        should_timeout = timeout
        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                self.kwargs = kwargs
                self.killed = False
                self.calls = 0
                if capture is not None:
                    capture.append(self)

            def communicate(self, payload=None, timeout=None):
                self.calls += 1
                if should_timeout and not self.killed:
                    raise subprocess.TimeoutExpired(self.command, timeout)
                return stdout, ""

            def kill(self):
                self.killed = True

            def poll(self):
                return 0 if self.calls and not should_timeout else (0 if self.killed else None)

        return FakeProcess

    # -- missing_registrations -------------------------------------------------

    def test_missing_registrations_flags_installed_host_with_empty_mcp_servers(self):
        # Regression: registration_report() used to skip a host with no registered
        # command entirely, so an installed app with `{"mcpServers": {}}` (the entry
        # was removed/never written) read as silently healthy.
        self._write_json(setup.CLAUDE_JSON, {"mcpServers": {}})

        rows = setup.missing_registrations()

        hosts = {r["host"] for r in rows}
        self.assertIn("claude", hosts)

    def test_missing_registrations_flags_installed_host_with_no_config_file(self):
        # Regression: a config file that was never created at all (not even an empty
        # shell) must be just as loud as an empty mcpServers block. Installation is
        # signaled here via the CLI probe, not the config file.
        setup.which = lambda name: (r"C:\fake\claude.exe" if name == "claude" else None)

        self.assertFalse(setup.CLAUDE_JSON.exists())
        rows = setup.missing_registrations()

        hosts = {r["host"] for r in rows}
        self.assertIn("claude", hosts)

    def test_missing_registrations_silent_for_properly_registered_host(self):
        # A host that IS registered must produce no row -- missing_registrations()
        # only reports total silence, not staleness (that's registration_report()'s job).
        self._write_json(
            setup.CLAUDE_JSON,
            {"mcpServers": {"agent-switchboard": {"command": r"C:\path\agent-switchboard.exe"}}},
        )

        rows = setup.missing_registrations()

        hosts = {r["host"] for r in rows}
        self.assertNotIn("claude", hosts)

    def test_missing_registrations_silent_for_uninstalled_host(self):
        # Guards against nagging about an app the user simply does not have: no config
        # file and no CLI means "not installed", not "missing registration".
        self.assertFalse(setup.CLAUDE_JSON.exists())
        rows = setup.missing_registrations()

        hosts = {r["host"] for r in rows}
        self.assertNotIn("claude", hosts)

    # -- antigravity_profile_report ---------------------------------------------

    def test_antigravity_profile_report_selects_authoritative_home_path(self):
        legacy = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        legacy.parent.mkdir(parents=True)

        rows = setup.antigravity_profile_report()

        self.assertEqual(len(rows), 2)
        selected_rows = [r for r in rows if r["selected"]]
        self.assertEqual(len(selected_rows), 1)
        self.assertEqual(selected_rows[0]["config_path"], str(setup.ANTIGRAVITY_MCP))
        self.assertTrue(selected_rows[0]["authoritative"])
        legacy_row = [r for r in rows if r["legacy"]][0]
        self.assertEqual(legacy_row["config_path"], str(legacy))
        self.assertFalse(legacy_row["selected"])

    def test_legacy_only_registration_is_reported_but_not_authoritative_or_healthy(self):
        legacy = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        self._write_json(
            legacy,
            {"mcpServers": {"agent-switchboard": {"command": r"C:\path\agent-switchboard.exe"}}},
        )

        profiles = setup.antigravity_profile_report()
        authoritative = [row for row in profiles if row["authoritative"]][0]
        legacy_row = [row for row in profiles if row["legacy"]][0]
        self.assertIsNone(authoritative["registered"])
        self.assertEqual(legacy_row["registered"], r"C:\path\agent-switchboard.exe")
        self.assertIsNone(setup.registered_command("antigravity"))
        self.assertFalse(any(row["host"] == "antigravity" for row in setup.registration_report()))
        self.assertIn("antigravity", {row["host"] for row in setup.missing_registrations()})

    def test_antigravity_profile_report_always_includes_authoritative_row(self):
        rows = setup.antigravity_profile_report()

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["selected"])
        self.assertTrue(rows[0]["authoritative"])
        self.assertEqual(rows[0]["config_path"], str(setup.ANTIGRAVITY_MCP))

    def test_effective_registration_prefers_authoritative_over_legacy(self):
        self._registration()
        legacy = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        self._write_json(legacy, {
            "mcpServers": {"agent-switchboard": {"command": "legacy.exe", "args": [], "env": {}}}
        })

        result = setup.effective_antigravity_mcp_registration()

        self.assertEqual(result["config_path"], str(setup.ANTIGRAVITY_MCP))
        self.assertEqual(result["command"], sys.executable)

    # -- Antigravity MCP stdio diagnostic ---------------------------------

    def test_mcp_diagnostic_reports_missing_selected_registration(self):
        result = setup.antigravity_mcp_stdio_diagnostic()
        self.assertEqual(result["status"], "mcp_unavailable")
        self.assertFalse(result["registered"])
        self.assertIn("no agent-switchboard", result["reason"])

    def test_mcp_diagnostic_reports_initialize_failure_and_timeout(self):
        self._registration()
        required = sorted(setup.ANTIGRAVITY_REQUIRED_MCP_TOOLS)
        with mock.patch.object(setup.subprocess, "Popen", self._fake_popen(self._stdio(names=required, initialize=False))):
            failed = setup.antigravity_mcp_stdio_diagnostic()
        self.assertEqual(failed["status"], "mcp_unavailable")
        self.assertFalse(failed["server_reachable"])
        with mock.patch.object(setup.subprocess, "Popen", self._fake_popen(timeout=True)):
            timed_out = setup.antigravity_mcp_stdio_diagnostic(timeout=0.1)
        self.assertEqual(timed_out["status"], "mcp_unavailable")
        self.assertIn("timed out", timed_out["reason"])

    def test_mcp_diagnostic_reports_missing_required_tool(self):
        self._registration()
        names = sorted(setup.ANTIGRAVITY_REQUIRED_MCP_TOOLS - {"consult_decision"})
        with mock.patch.object(setup.subprocess, "Popen", self._fake_popen(self._stdio(names=names))):
            result = setup.antigravity_mcp_stdio_diagnostic()
        self.assertTrue(result["server_reachable"])
        self.assertFalse(result["tools_declared"]["ok"])
        self.assertEqual(result["tools_declared"]["missing"], ["consult_decision"])
        self.assertEqual(result["status"], "mcp_unavailable")

    def test_mcp_diagnostic_reports_declared_tools_but_failed_call(self):
        self._registration()
        names = sorted(setup.ANTIGRAVITY_REQUIRED_MCP_TOOLS)
        with mock.patch.object(setup.subprocess, "Popen", self._fake_popen(self._stdio(names=names, call=False))):
            result = setup.antigravity_mcp_stdio_diagnostic()
        self.assertTrue(result["tools_declared"]["ok"])
        self.assertFalse(result["tool_callable"])
        self.assertEqual(result["status"], "mcp_unavailable")

    def test_mcp_diagnostic_local_success_is_host_unverified_and_redacts_env_values(self):
        secret = "do-not-return-this-value"
        self._registration({"PYTHONUTF8": "1", "PRIVATE_TEST_VALUE": secret})
        names = sorted(setup.ANTIGRAVITY_REQUIRED_MCP_TOOLS)
        capture = []
        with mock.patch.object(setup.subprocess, "Popen", self._fake_popen(self._stdio(names=names), capture=capture)):
            result = setup.antigravity_mcp_stdio_diagnostic()
        self.assertEqual(result["status"], "local_callable_host_unverified")
        self.assertEqual(result["host_exposure"], "unverified")
        self.assertTrue(result["tool_callable"])
        self.assertIn("fresh chat", result["handoff"])
        self.assertEqual(result["registration"]["env_keys"], ["PRIVATE_TEST_VALUE", "PYTHONUTF8"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(capture[0].kwargs["env"]["PRIVATE_TEST_VALUE"], secret)
        self.assertEqual(capture[0].kwargs["env"]["AGENT_BROKER_DIAGNOSTIC_PROBE"], "1")
        self.assertFalse(capture[0].kwargs["shell"])

    def test_diagnostic_serve_flag_skips_only_hierarchy_refresh(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_DIAGNOSTIC_PROBE": "1"}), \
             mock.patch.object(sys, "argv", ["agent-switchboard.exe", "serve"]), \
             mock.patch.object(setup, "refresh_hierarchy") as refresh, \
             mock.patch.object(broker, "main", return_value=0) as serve:
            self.assertEqual(agent_broker_entry.run(), 0)
        refresh.assert_not_called()
        serve.assert_called_once()

    def test_doctor_integrates_unverified_mcp_handoff_without_healthy_claim(self):
        mcp = {
            "registered": True, "server_reachable": True,
            "tools_declared": {"required": [], "present": [], "missing": [], "ok": True},
            "tool_callable": True, "host_exposure": "unverified",
            "status": "local_callable_host_unverified", "reason": "local only",
            "handoff": "Gracefully reload Antigravity and verify tools in a fresh chat.",
        }
        cli = {"found": True, "smoke_ok": True, "version": "test", "source": "test", "path": "test"}
        nerve = {"claude_desktop": {"installed": False, "registered": False}}
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "detect_agent_surfaces", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value=None), \
             mock.patch.object(broker, "_cli_probe", return_value=cli), \
             mock.patch.object(broker, "_bridge_package_version", return_value=None), \
             mock.patch.object(broker, "_nerve_system_report", return_value=nerve), \
             mock.patch.object(setup, "antigravity_mcp_stdio_diagnostic", return_value=mcp):
            report = broker.broker_doctor()
        self.assertEqual(report["surfaces"]["antigravity"]["mcp"], mcp)
        self.assertNotIn("All core surfaces look healthy.", report["recommendations"])
        self.assertTrue(any("fresh chat" in item for item in report["recommendations"]))

    # -- host_is_installed --------------------------------------------------

    def test_host_is_installed_false_for_all_hosts_when_nothing_present(self):
        # With every config path pointed at an empty temp dir and every CLI probe
        # neutered, no host should ever read as "installed" -- otherwise
        # missing_registrations() would nag about apps the user does not have.
        for host, _ in setup.REGISTRATION_HOSTS:
            self.assertFalse(setup.host_is_installed(host), host)

    def test_bare_gemini_config_directory_does_not_mean_antigravity_is_installed(self):
        setup.ANTIGRAVITY_MCP.parent.mkdir(parents=True)

        self.assertFalse(setup.ANTIGRAVITY_MCP.exists())
        self.assertFalse(setup.host_is_installed("antigravity"))

    def test_existing_authoritative_config_or_legacy_profile_is_antigravity_evidence(self):
        self._write_json(setup.ANTIGRAVITY_MCP, {"mcpServers": {}})
        self.assertTrue(setup.host_is_installed("antigravity"))

        setup.ANTIGRAVITY_MCP.unlink()
        setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0].parent.mkdir(parents=True)
        self.assertTrue(setup.host_is_installed("antigravity"))


if __name__ == "__main__":
    unittest.main()
