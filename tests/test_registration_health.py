"""Tests for setup.py's registration-visibility helpers added alongside WP1:
`host_is_installed`, `missing_registrations`, and `antigravity_profile_report`.

Everything here operates on temp paths patched onto the `setup` module's globals --
never on the real ~/.codex, ~/.claude.json, %APPDATA%, or ~/.agent-broker. Fixture
style copied from tests/test_registration_repair.py.
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
        self._patch("ANTIGRAVITY_USER_DIRS", [self.root / "antigravity_missing" / "User"])
        self._patch("VSCODE_MCP", self.root / "vscode_missing" / "mcp.json")
        self._patch("BROKER_HOME", self.root / "broker")
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

    def test_antigravity_profile_report_both_dirs_selects_first(self):
        # With two profile roots present, exactly one row must be marked selected,
        # and it must be the FIRST candidate -- matching antigravity_user_dir()'s
        # first-match-wins behavior.
        first = self.root / "antigravity_ide" / "User"
        second = self.root / "antigravity_old" / "User"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        setup.ANTIGRAVITY_USER_DIRS = [first, second]

        rows = setup.antigravity_profile_report()

        self.assertEqual(len(rows), 2)
        selected_rows = [r for r in rows if r["selected"]]
        self.assertEqual(len(selected_rows), 1)
        self.assertEqual(selected_rows[0]["directory"], str(first))

    def test_antigravity_profile_report_shows_asymmetry_when_selected_dir_is_unregistered(self):
        # The exact trap this function exists to catch: the SELECTED (first-match)
        # profile dir has no broker entry, but the OTHER (unused) dir does. A reader
        # that only opens the selected dir would wrongly conclude the IDE is unregistered.
        selected_dir = self.root / "antigravity_ide" / "User"
        other_dir = self.root / "antigravity_old" / "User"
        selected_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        setup.ANTIGRAVITY_USER_DIRS = [selected_dir, other_dir]

        self._write_json(
            other_dir / "mcp_config.json",
            {"mcpServers": {"agent-switchboard": {"command": r"C:\path\agent-switchboard.exe"}}},
        )

        rows = setup.antigravity_profile_report()

        by_dir = {r["directory"]: r for r in rows}
        self.assertIsNone(by_dir[str(selected_dir)]["registered"])
        self.assertTrue(by_dir[str(selected_dir)]["selected"])
        self.assertEqual(by_dir[str(other_dir)]["registered"], r"C:\path\agent-switchboard.exe")
        self.assertFalse(by_dir[str(other_dir)]["selected"])

    def test_antigravity_profile_report_single_dir_returns_one_selected_row(self):
        # Only one candidate dir exists on this box: report exactly one row, and it
        # must still be marked selected (no false "second profile" noise).
        only_dir = self.root / "antigravity_ide" / "User"
        missing_dir = self.root / "antigravity_old" / "User"
        only_dir.mkdir(parents=True)
        setup.ANTIGRAVITY_USER_DIRS = [only_dir, missing_dir]

        rows = setup.antigravity_profile_report()

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["selected"])
        self.assertEqual(rows[0]["directory"], str(only_dir))

    # -- host_is_installed --------------------------------------------------

    def test_host_is_installed_false_for_all_hosts_when_nothing_present(self):
        # With every config path pointed at an empty temp dir and every CLI probe
        # neutered, no host should ever read as "installed" -- otherwise
        # missing_registrations() would nag about apps the user does not have.
        for host, _ in setup.REGISTRATION_HOSTS:
            self.assertFalse(setup.host_is_installed(host), host)


if __name__ == "__main__":
    unittest.main()
