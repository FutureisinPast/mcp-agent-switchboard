"""Tests for setup.py's MCP registration health/repair logic (WP1).

Everything here operates on temp paths patched onto the `setup` module's globals —
never on the real ~/.codex, ~/.claude.json, or ~/.agent-broker.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import setup  # noqa: E402


class RegistrationRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # Point every host config + BROKER_HOME at nonexistent temp locations so no
        # host other than the one under test is ever "present", and so nothing on
        # the real machine (~/.codex, ~/.claude.json, ~/.agent-broker, real APPDATA
        # dirs) is ever touched.
        self._patch("CODEX_TOML", self.root / "codex" / "config.toml")
        self._patch("CLAUDE_JSON", self.root / "claude" / ".claude.json")
        self._patch("CLAUDE_DESKTOP_CONFIG", self.root / "claude_desktop" / "claude_desktop_config.json")
        self._patch("ANTIGRAVITY_USER_DIRS", [self.root / "antigravity_missing" / "User"])
        self._patch("VSCODE_MCP", self.root / "vscode_missing" / "mcp.json")
        self._patch("BROKER_HOME", self.root / "broker")
        self._patch("FROZEN", False)

    def _patch(self, name, value):
        original = getattr(setup, name)
        setattr(setup, name, value)
        self.addCleanup(setattr, setup, name, original)

    # -- registered_command: codex TOML -------------------------------------
    def test_registered_command_reads_codex_toml(self):
        command_value = r"C:\path\agent-switchboard-1.0.25.exe"
        setup.CODEX_TOML.parent.mkdir(parents=True, exist_ok=True)
        text = (
            f"[mcp_servers.{setup.CODEX_KEY}]\n"
            f"command = {json.dumps(command_value)}\n"
            f'args = ["serve"]\n\n'
            f"[other]\n"
            f'command = "should-not-be-read.exe"\n'
        )
        setup.CODEX_TOML.write_text(text, encoding="utf-8")

        self.assertEqual(setup.registered_command("codex"), command_value)

    # -- registered_command: claude.json -------------------------------------
    def test_registered_command_reads_claude_json(self):
        command_value = r"C:\path\agent-switchboard.exe"
        setup.CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
        setup.CLAUDE_JSON.write_text(
            json.dumps({"mcpServers": {"agent-switchboard": {"command": command_value}}}),
            encoding="utf-8",
        )
        self.assertEqual(setup.registered_command("claude"), command_value)

        # No agent-switchboard entry -> None.
        setup.CLAUDE_JSON.write_text(
            json.dumps({"mcpServers": {"some-other-server": {"command": "x"}}}),
            encoding="utf-8",
        )
        self.assertIsNone(setup.registered_command("claude"))

    # -- registered_command: malformed files -------------------------------
    def test_registered_command_tolerates_malformed_files(self):
        setup.CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
        setup.CLAUDE_JSON.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(setup.registered_command("claude"))

        setup.CODEX_TOML.parent.mkdir(parents=True, exist_ok=True)
        setup.CODEX_TOML.write_text("[other]\ncommand = \"x\"\n", encoding="utf-8")
        self.assertIsNone(setup.registered_command("codex"))

    # -- probe_exe_version ---------------------------------------------------
    def test_probe_exe_version_parses_banner(self):
        fake_exe = self.root / "fake.exe"
        fake_exe.write_bytes(b"MZ")

        class FakeProc:
            def __init__(self, stdout, stderr=""):
                self.stdout = stdout
                self.stderr = stderr

        original_run = setup.subprocess.run

        def banner_run(*args, **kwargs):
            return FakeProc("Agent Switchboard 1.0.34\n")

        def usage_run(*args, **kwargs):
            return FakeProc("usage: agent-switchboard.exe [install|uninstall|status]\n")

        try:
            setup.subprocess.run = banner_run
            self.assertEqual(setup.probe_exe_version(str(fake_exe)), "1.0.34")

            setup.subprocess.run = usage_run
            self.assertEqual(setup.probe_exe_version(str(fake_exe)), setup.LEGACY_VERSION)
        finally:
            setup.subprocess.run = original_run

        # Non-.exe path -> None (never even reaches subprocess).
        non_exe = self.root / "fake.bin"
        non_exe.write_bytes(b"data")
        self.assertIsNone(setup.probe_exe_version(str(non_exe)))

        # Nonexistent path -> None.
        missing = self.root / "missing.exe"
        self.assertIsNone(setup.probe_exe_version(str(missing)))

    # -- registration_report -------------------------------------------------
    def test_registration_report_flags_stale_pin(self):
        setup.BROKER_HOME.mkdir(parents=True, exist_ok=True)
        canonical_exe = setup.frozen_broker_exe()
        canonical_exe.write_bytes(b"MZ")

        stale_command = str(self.root / "old" / "agent-switchboard-1.0.25.exe")
        setup.CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
        setup.CLAUDE_JSON.write_text(
            json.dumps({"mcpServers": {"agent-switchboard": {"command": stale_command}}}),
            encoding="utf-8",
        )

        rows = setup.registration_report()
        claude_rows = [r for r in rows if r["host"] == "claude"]
        self.assertEqual(len(claude_rows), 1)
        row = claude_rows[0]
        self.assertIs(row["matches_canonical"], False)
        self.assertIs(row["healthy"], False)

    def test_registration_report_healthy_when_canonical(self):
        setup.BROKER_HOME.mkdir(parents=True, exist_ok=True)
        canonical_exe = setup.frozen_broker_exe()
        canonical_exe.write_bytes(b"MZ")

        setup.CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
        setup.CLAUDE_JSON.write_text(
            json.dumps({"mcpServers": {"agent-switchboard": {"command": str(canonical_exe)}}}),
            encoding="utf-8",
        )

        original_run = setup.subprocess.run

        class FakeProc:
            stdout = f"Agent Switchboard {setup.BROKER_VERSION}\n"
            stderr = ""

        def fake_run(*args, **kwargs):
            return FakeProc()

        try:
            setup.subprocess.run = fake_run
            rows = setup.registration_report()
        finally:
            setup.subprocess.run = original_run

        claude_rows = [r for r in rows if r["host"] == "claude"]
        self.assertEqual(len(claude_rows), 1)
        self.assertIs(claude_rows[0]["healthy"], True)

    # -- repair_registrations -------------------------------------------------
    def test_repair_registrations_dry_run_changes_nothing(self):
        setup.BROKER_HOME.mkdir(parents=True, exist_ok=True)
        canonical_exe = setup.frozen_broker_exe()
        canonical_exe.write_bytes(b"MZ")

        stale_command = str(self.root / "old" / "agent-switchboard-1.0.25.exe")
        setup.CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"mcpServers": {"agent-switchboard": {"command": stale_command}}})
        setup.CLAUDE_JSON.write_text(payload, encoding="utf-8")
        before = setup.CLAUDE_JSON.read_bytes()

        result = setup.repair_registrations(dry=True)

        self.assertIn("Claude MCP", result)
        self.assertTrue(result["Claude MCP"].startswith("would re-point"), result["Claude MCP"])
        self.assertEqual(setup.CLAUDE_JSON.read_bytes(), before)

    def test_repair_registrations_skips_healthy_hosts(self):
        setup.BROKER_HOME.mkdir(parents=True, exist_ok=True)
        canonical_exe = setup.frozen_broker_exe()
        canonical_exe.write_bytes(b"MZ")

        setup.CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
        setup.CLAUDE_JSON.write_text(
            json.dumps({"mcpServers": {"agent-switchboard": {"command": str(canonical_exe)}}}),
            encoding="utf-8",
        )

        result = setup.repair_registrations(dry=True)

        self.assertNotIn("Claude MCP", result)

    # -- canonical_registration -------------------------------------------------
    def test_canonical_registration_prefers_durable_exe(self):
        setup.BROKER_HOME.mkdir(parents=True, exist_ok=True)
        canonical_exe = setup.frozen_broker_exe()
        canonical_exe.write_bytes(b"MZ")

        self.assertFalse(setup.FROZEN)
        command, args = setup.canonical_registration()
        self.assertEqual(command, str(canonical_exe))
        self.assertEqual(args, ["serve"])

        # No exe present -> falls back to broker_command().
        canonical_exe.unlink()
        command, args = setup.canonical_registration()
        self.assertEqual((command, args), setup.broker_command())


if __name__ == "__main__":
    unittest.main()
