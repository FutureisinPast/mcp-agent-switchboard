"""Tests for installer-managed hierarchy files and hooks."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hierarchy_install  # noqa: E402


CODEX_ROLES = {
    "frontier": {"id": "gpt-next-sol"},
    "workhorse": {"id": "gpt-next-terra"},
    "reader": {"id": "gpt-next-luna"},
}
CLAUDE_ROLES = {"frontier": ["best", "fable", "opus"], "workhorse": "sonnet", "reader": "haiku"}


class HierarchyInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = hierarchy_install.HierarchyPaths(root / "home", root / "broker")
        self.backups = []

    def backup(self, path: Path) -> None:
        self.backups.append(path)

    def refresh(self):
        return hierarchy_install.refresh(
            self.paths,
            CODEX_ROLES,
            CLAUDE_ROLES,
            '"C:\\Agent Switchboard\\agent-switchboard.exe" routing-hook',
            self.backup,
        )

    def seed_legacy_files(self):
        self.paths.codex_agents_md.parent.mkdir(parents=True, exist_ok=True)
        self.paths.codex_agents_md.write_text(
            "# Global\n\n## Cost-aware model routing\nold rules\n\n## Response rules\nkeep me\n",
            encoding="utf-8",
        )
        self.paths.claude_md.parent.mkdir(parents=True, exist_ok=True)
        self.paths.claude_md.write_text(
            "# Cost-aware model routing\n\nold Claude rules\n", encoding="utf-8"
        )
        self.paths.codex_explorer.parent.mkdir(parents=True, exist_ok=True)
        self.paths.codex_explorer.write_text(
            'name = "explorer"\ndescription = "Cost-efficient old role"\n', encoding="utf-8"
        )
        self.paths.codex_worker.write_text(
            'name = "worker"\ndescription = "Cost-efficient old role"\n', encoding="utf-8"
        )
        self.paths.claude_explore.parent.mkdir(parents=True, exist_ok=True)
        self.paths.claude_explore.write_text(
            "---\nname: Explore\ndescription: Cost-efficient old role\n---\n", encoding="utf-8"
        )
        self.paths.claude_worker.write_text(
            "---\nname: economy-worker\ndescription: Cost-efficient old role\n---\n", encoding="utf-8"
        )
        self.paths.claude_settings.write_text(
            json.dumps(
                {
                    "model": "user-selected-brain",
                    "effortLevel": "max",
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "shutdown-if-armed.ps1"}
                                ]
                            }
                        ]
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_refresh_migrates_legacy_preserves_user_settings_and_is_idempotent(self):
        self.seed_legacy_files()
        first = self.refresh()
        self.assertTrue(all(not value.startswith("ERROR") for value in first.values()), first)
        codex_text = self.paths.codex_agents_md.read_text(encoding="utf-8")
        self.assertEqual(codex_text.count("agent-switchboard:cost-routing:begin"), 1)
        self.assertIn("## Response rules\nkeep me", codex_text)
        self.assertNotIn("old rules", codex_text)
        claude_text = self.paths.claude_md.read_text(encoding="utf-8")
        self.assertEqual(claude_text.count("agent-switchboard:cost-routing:begin"), 1)
        self.assertIn("gpt-next-sol", claude_text)

        self.assertIn('model = "gpt-next-luna"', self.paths.codex_explorer.read_text(encoding="utf-8"))
        self.assertIn('model = "gpt-next-terra"', self.paths.codex_worker.read_text(encoding="utf-8"))
        self.assertIn("model: haiku", self.paths.claude_explore.read_text(encoding="utf-8"))
        self.assertIn("model: sonnet", self.paths.claude_worker.read_text(encoding="utf-8"))

        settings = json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "user-selected-brain")
        self.assertEqual(settings["effortLevel"], "max")
        stop_commands = [
            item["command"]
            for group in settings["hooks"]["Stop"]
            for item in group.get("hooks", [])
        ]
        self.assertIn("shutdown-if-armed.ps1", stop_commands)
        self.assertTrue(any("routing-hook Stop agent-switchboard" in item for item in stop_commands))

        before = {path: path.read_bytes() for path in (
            self.paths.codex_agents_md,
            self.paths.claude_md,
            self.paths.codex_explorer,
            self.paths.codex_worker,
            self.paths.claude_explore,
            self.paths.claude_worker,
            self.paths.codex_hooks,
            self.paths.claude_settings,
        )}
        second = self.refresh()
        self.assertTrue(all(value == "unchanged" for value in second.values()), second)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_edited_managed_block_is_refused(self):
        self.seed_legacy_files()
        self.refresh()
        original = self.paths.codex_agents_md.read_text(encoding="utf-8")
        tampered = original.replace("The model selected", "THE model selected", 1)
        self.paths.codex_agents_md.write_text(tampered, encoding="utf-8")
        result = self.refresh()["Codex global hierarchy"]
        self.assertTrue(result.startswith("ERROR"), result)
        self.assertEqual(self.paths.codex_agents_md.read_text(encoding="utf-8"), tampered)

    def test_invalid_settings_json_is_left_untouched(self):
        self.paths.claude_settings.parent.mkdir(parents=True, exist_ok=True)
        self.paths.claude_settings.write_text("{invalid", encoding="utf-8")
        before = self.paths.claude_settings.read_bytes()
        result = self.refresh()["Claude routing hooks"]
        self.assertTrue(result.startswith("ERROR"), result)
        self.assertEqual(self.paths.claude_settings.read_bytes(), before)

    def test_uninstall_removes_owned_content_and_preserves_existing_hook(self):
        self.seed_legacy_files()
        self.refresh()
        result = hierarchy_install.uninstall(self.paths, self.backup)
        self.assertTrue(all(not value.startswith("ERROR") for value in result.values()), result)
        self.assertIn("## Response rules\nkeep me", self.paths.codex_agents_md.read_text(encoding="utf-8"))
        self.assertFalse(self.paths.codex_explorer.exists())
        self.assertFalse(self.paths.codex_worker.exists())
        self.assertFalse(self.paths.claude_explore.exists())
        self.assertFalse(self.paths.claude_worker.exists())
        settings = json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))
        stop_commands = [
            item["command"]
            for group in settings["hooks"]["Stop"]
            for item in group.get("hooks", [])
        ]
        self.assertEqual(stop_commands, ["shutdown-if-armed.ps1"])


if __name__ == "__main__":
    unittest.main()
