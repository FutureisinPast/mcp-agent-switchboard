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
from unittest import mock

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
        legacy_dir = self.root / "appdata" / "Antigravity IDE" / "User"
        authoritative = self.root / "home" / ".gemini" / "config" / "mcp_config.json"
        self._patch("APPDATA", self.root / "appdata")
        self._patch("ANTIGRAVITY_USER_DIRS", [legacy_dir])
        self._patch("ANTIGRAVITY_MCP", authoritative)
        self._patch("ANTIGRAVITY_LEGACY_MCP_CANDIDATES", [legacy_dir / "mcp_config.json"])
        self._patch("ANTIGRAVITY_MCP_CANDIDATES", [authoritative, legacy_dir / "mcp_config.json"])
        self._patch("VSCODE_MCP", self.root / "appdata" / "Code" / "User" / "mcp.json")
        self._patch("BROKER_HOME", self.root / "broker")
        self._patch("_backup_root", None)
        self._patch("FROZEN", False)
        self._patch("which", lambda name: None)
        self._patch("antigravity_cli", lambda: None)
        self._patch("vscode_cli", lambda: None)
        self._patch("claude_desktop_installed", lambda: False)

    def _patch(self, name, value):
        original = getattr(setup, name)
        setattr(setup, name, value)
        self.addCleanup(setattr, setup, name, original)

    @staticmethod
    def _owned_entry(command=r"C:\broker\agent-switchboard.exe"):
        return {"command": command, "args": ["serve"], "env": {"KEEP_ENV": "1"}}

    @staticmethod
    def _write_json(path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

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

    # -- Antigravity authoritative registration and legacy migration ---------
    def test_antigravity_writer_uses_authoritative_schema_safe_shape_and_preserves_data(self):
        setup.ANTIGRAVITY_MCP.parent.mkdir(parents=True)
        self._write_json(setup.ANTIGRAVITY_MCP, {
            "keepTop": {"value": 1},
            "mcpServers": {"other": {"type": "stdio", "command": "other.exe"}},
        })
        with mock.patch.object(setup, "antigravity_schema", return_value="file:///valid-schema.json"):
            result = setup.register_antigravity(r"C:\broker\agent-switchboard.exe", ["serve"], False)

        self.assertTrue(result.startswith("registered"), result)
        data = json.loads(setup.ANTIGRAVITY_MCP.read_text(encoding="utf-8"))
        self.assertEqual(data["keepTop"], {"value": 1})
        self.assertEqual(data["$schema"], "file:///valid-schema.json")
        self.assertEqual(data["mcpServers"]["other"]["command"], "other.exe")
        entry = data["mcpServers"][setup.MCP_KEY]
        self.assertEqual(set(entry), {"command", "args", "env"})
        self.assertEqual(entry["command"], r"C:\broker\agent-switchboard.exe")
        self.assertEqual(entry["args"], ["serve"])
        self.assertEqual(entry["env"]["AGENT_BROKER_CALLER"], "antigravity")

    def test_bare_gemini_config_directory_does_not_enable_antigravity_registration(self):
        setup.ANTIGRAVITY_MCP.parent.mkdir(parents=True)

        result = setup.register_antigravity("python", ["broker.py"], False)

        self.assertEqual(result, "skipped (not installed)")
        self.assertFalse(setup.ANTIGRAVITY_MCP.exists())

    def test_existing_authoritative_file_or_antigravity_profile_enables_registration(self):
        self._write_json(setup.ANTIGRAVITY_MCP, {"keep": True})
        first = setup.register_antigravity("python", ["broker.py"], False)
        self.assertTrue(first.startswith("registered"), first)

        setup.ANTIGRAVITY_MCP.unlink()
        setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0].parent.mkdir(parents=True)
        second = setup.register_antigravity("python", ["broker.py"], False)
        self.assertTrue(second.startswith("registered"), second)

    def test_antigravity_repairs_zero_byte_and_whitespace_authoritative_configs_after_backup(self):
        for index, original in enumerate((b"", b" \r\n\t")):
            with self.subTest(original=original):
                path = self.root / f"empty-{index}" / ".gemini" / "config" / "mcp_config.json"
                path.parent.mkdir(parents=True)
                path.write_bytes(original)
                backup_home = self.root / f"broker-{index}"
                with mock.patch.object(setup, "ANTIGRAVITY_MCP", path), \
                     mock.patch.object(setup, "BROKER_HOME", backup_home), \
                     mock.patch.object(setup, "_backup_root", None):
                    result = setup.register_antigravity("python", ["broker.py"], False)

                self.assertTrue(result.startswith("registered"), result)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn(setup.MCP_KEY, data["mcpServers"])
                backups = list((backup_home / "setup-backups").rglob("mcp_config*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), original)

    def test_antigravity_rejects_nonempty_malformed_authoritative_config_unchanged(self):
        original = b"{not valid json"
        setup.ANTIGRAVITY_MCP.parent.mkdir(parents=True)
        setup.ANTIGRAVITY_MCP.write_bytes(original)

        result = setup.register_antigravity("python", ["broker.py"], False)

        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertIn("left untouched", result)
        self.assertEqual(setup.ANTIGRAVITY_MCP.read_bytes(), original)
        self.assertFalse((setup.BROKER_HOME / "setup-backups").exists())

    def test_claude_and_vscode_keep_their_existing_type_formats(self):
        self._write_json(setup.CLAUDE_JSON, {"mcpServers": {"other": {"command": "other"}}})
        setup.VSCODE_MCP.parent.mkdir(parents=True)

        self.assertEqual(setup.register_claude("python", ["broker.py"], False), "registered")
        self.assertEqual(setup.register_vscode("python", ["broker.py"], False), "registered")

        claude = json.loads(setup.CLAUDE_JSON.read_text(encoding="utf-8"))
        vscode = json.loads(setup.VSCODE_MCP.read_text(encoding="utf-8"))
        self.assertEqual(claude["mcpServers"][setup.MCP_KEY]["type"], "stdio")
        self.assertEqual(vscode["servers"][setup.MCP_KEY]["type"], "stdio")
        self.assertIn("other", claude["mcpServers"])

    def test_install_migrates_only_owned_legacy_entry_after_backup(self):
        legacy = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        self._write_json(legacy, {
            "legacyTop": True,
            "mcpServers": {
                setup.MCP_KEY: self._owned_entry(),
                "other": {"command": "other.exe", "custom": 7},
            },
        })

        result = setup.register_antigravity(r"C:\broker\agent-switchboard.exe", ["serve"], False)

        self.assertIn("registered", result)
        self.assertIn("removed", result)
        legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertTrue(legacy_data["legacyTop"])
        self.assertNotIn(setup.MCP_KEY, legacy_data["mcpServers"])
        self.assertEqual(legacy_data["mcpServers"]["other"]["custom"], 7)
        backups = list((setup.BROKER_HOME / "setup-backups").rglob("mcp_config*.json"))
        self.assertTrue(backups)

    def test_install_preserves_unowned_and_malformed_legacy_files(self):
        unowned = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        malformed = self.root / "appdata" / "Antigravity" / "User" / "mcp_config.json"
        setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES = [unowned, malformed]
        unowned_payload = {
            "mcpServers": {setup.MCP_KEY: {"command": "different-product.exe"}, "other": {"command": "x"}}
        }
        self._write_json(unowned, unowned_payload)
        malformed.parent.mkdir(parents=True)
        malformed.write_text("{not json", encoding="utf-8")

        result = setup.register_antigravity(r"C:\broker\agent-switchboard.exe", ["serve"], False)

        self.assertIn("preserved (unrecognized command)", result)
        self.assertIn("skipped (invalid JSON)", result)
        self.assertEqual(json.loads(unowned.read_text(encoding="utf-8")), unowned_payload)
        self.assertEqual(malformed.read_text(encoding="utf-8"), "{not json")

    def test_repair_migrates_legacy_only_registration_to_authoritative_path(self):
        setup.BROKER_HOME.mkdir(parents=True)
        setup.frozen_broker_exe().write_bytes(b"MZ")
        legacy = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        self._write_json(legacy, {"mcpServers": {setup.MCP_KEY: self._owned_entry()}})

        result = setup.repair_registrations(dry=False)

        self.assertIn("Antigravity MCP", result)
        self.assertEqual(setup.registered_command("antigravity"), str(setup.frozen_broker_exe()))
        legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertNotIn(setup.MCP_KEY, legacy_data["mcpServers"])

    def test_uninstall_removes_owned_authoritative_and_legacy_but_preserves_others(self):
        legacy = setup.ANTIGRAVITY_LEGACY_MCP_CANDIDATES[0]
        for path in (setup.ANTIGRAVITY_MCP, legacy):
            self._write_json(path, {
                "top": "keep",
                "mcpServers": {setup.MCP_KEY: self._owned_entry(), "other": {"command": "other.exe"}},
            })

        result = setup.unregister_antigravity(False)

        self.assertIn("authoritative: removed", result)
        self.assertIn("legacy", result)
        for path in (setup.ANTIGRAVITY_MCP, legacy):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["top"], "keep")
            self.assertNotIn(setup.MCP_KEY, data["mcpServers"])
            self.assertIn("other", data["mcpServers"])

    def test_uninstall_preserves_unrecognized_authoritative_entry(self):
        payload = {"mcpServers": {setup.MCP_KEY: {"command": "different-product.exe"}}}
        self._write_json(setup.ANTIGRAVITY_MCP, payload)

        result = setup.unregister_antigravity(False)

        self.assertIn("preserved (unrecognized command)", result)
        self.assertEqual(json.loads(setup.ANTIGRAVITY_MCP.read_text(encoding="utf-8")), payload)

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
