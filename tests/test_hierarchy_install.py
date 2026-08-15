"""Tests for installer-managed hierarchy files and hooks."""
from __future__ import annotations

import json
import re
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
        self.paths.gemini_md.parent.mkdir(parents=True, exist_ok=True)
        self.paths.gemini_md.write_text(
            "# SECTION 1: SCOPE & BOUNDARIES\n"
            "Role: Production-grade Quant Dev for TradingView Pine Script v6\n"
            "legacy Pine-only global instructions\n"
            "# SECTION 7: OUTPUT REQUIREMENTS\n",
            encoding="utf-8",
        )
        self.paths.codex_explorer.parent.mkdir(parents=True, exist_ok=True)
        self.paths.codex_explorer.write_text(
            'name = "explorer"\ndescription = "Cost-efficient old role"\n', encoding="utf-8"
        )
        self.paths.codex_worker.write_text(
            'name = "worker"\ndescription = "Cost-efficient old role"\n', encoding="utf-8"
        )
        self.paths.codex_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SubagentStart": [
                            {
                                "matcher": "user-agent",
                                "hooks": [
                                    {"type": "command", "command": "user-subagent-start.ps1"}
                                ],
                            }
                        ],
                        "SubagentStop": [
                            {
                                "matcher": "user-agent",
                                "hooks": [
                                    {"type": "command", "command": "user-subagent-stop.ps1"}
                                ],
                            }
                        ],
                        "PreToolUse": [
                            {
                                "matcher": "legacy-switchboard",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": '"C:\\Agent Switchboard\\agent-switchboard.exe" routing-hook PreToolUse agent-switchboard claude',
                                    }
                                ],
                            },
                            {
                                "matcher": "user-pre",
                                "hooks": [
                                    {"type": "command", "command": "user-pre-tool.ps1"}
                                ],
                            }
                        ],
                        "PostToolUse": [
                            {
                                "matcher": "user-post",
                                "hooks": [
                                    {"type": "command", "command": "user-post-tool.ps1"}
                                ],
                            }
                        ],
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
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
                        ],
                        "PreToolUse": [
                            {
                                "matcher": "user-pre",
                                "hooks": [
                                    {"type": "command", "command": "user-pre-tool.ps1"}
                                ],
                            }
                        ],
                        "PostToolUse": [
                            {
                                "matcher": "user-post",
                                "hooks": [
                                    {"type": "command", "command": "user-post-tool.ps1"}
                                ],
                            }
                        ],
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
        gemini_text = self.paths.gemini_md.read_text(encoding="utf-8")
        self.assertEqual(gemini_text.count("agent-switchboard:cost-routing:begin"), 1)
        self.assertNotIn("Production-grade Quant Dev", gemini_text)
        self.assertIn("MUST call MCP `route_agent_task`", gemini_text)
        self.assertIn("MUST NOT invoke `agy`", gemini_text)
        self.assertIn("never the brain or router", gemini_text)

        self.assertIn('model = "gpt-next-luna"', self.paths.codex_explorer.read_text(encoding="utf-8"))
        self.assertIn('model = "gpt-next-terra"', self.paths.codex_worker.read_text(encoding="utf-8"))
        self.assertIn("model: haiku", self.paths.claude_explore.read_text(encoding="utf-8"))
        self.assertIn("model: sonnet", self.paths.claude_worker.read_text(encoding="utf-8"))

        settings = json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "user-selected-brain")
        self.assertEqual(settings["effortLevel"], "max")
        stop_handlers = [
            item
            for group in settings["hooks"]["Stop"]
            for item in group.get("hooks", [])
        ]
        self.assertIn("shutdown-if-armed.ps1", [item["command"] for item in stop_handlers])
        owned_stop = [
            item for item in stop_handlers if hierarchy_install.routing_gate.is_owned_hook_entry(item)
        ]
        self.assertEqual(
            owned_stop,
            [
                {
                    "type": "command",
                    "command": "C:\\Agent Switchboard\\agent-switchboard.exe",
                    "args": ["routing-hook", "Stop", "agent-switchboard", "claude"],
                }
            ],
        )
        self.assertTrue(
            any(
                str(group.get("matcher", "")) == ".*"
                for group in settings["hooks"]["PostToolUse"]
            )
        )
        for event, user_command in (
            ("PreToolUse", "user-pre-tool.ps1"),
            ("PostToolUse", "user-post-tool.ps1"),
        ):
            handlers = [
                item
                for group in settings["hooks"][event]
                for item in group.get("hooks", [])
            ]
            self.assertIn(user_command, [item["command"] for item in handlers])
            owned = [
                item for item in handlers if hierarchy_install.routing_gate.is_owned_hook_entry(item)
            ]
            self.assertEqual(
                owned,
                [
                    {
                        "type": "command",
                        "command": "C:\\Agent Switchboard\\agent-switchboard.exe",
                        "args": ["routing-hook", event, "agent-switchboard", "claude"],
                    }
                ],
            )
        self.assertTrue(
            any(
                str(group.get("matcher", "")) == ".*"
                for group in settings["hooks"]["PreToolUse"]
            )
        )

        codex_hooks = json.loads(self.paths.codex_hooks.read_text(encoding="utf-8"))["hooks"]
        for event, user_command in (
            ("SubagentStart", "user-subagent-start.ps1"),
            ("SubagentStop", "user-subagent-stop.ps1"),
        ):
            commands = [
                item["command"]
                for group in codex_hooks[event]
                for item in group.get("hooks", [])
            ]
            self.assertIn(user_command, commands)
            self.assertTrue(
                any(item.endswith(f"routing-hook {event} agent-switchboard codex") for item in commands)
            )
        self.assertTrue(
            any(
                str(group.get("matcher", "")) == ".*"
                for group in codex_hooks["PostToolUse"]
            )
        )
        for event, user_command in (
            ("PreToolUse", "user-pre-tool.ps1"),
            ("PostToolUse", "user-post-tool.ps1"),
        ):
            commands = [
                item["command"]
                for group in codex_hooks[event]
                for item in group.get("hooks", [])
            ]
            self.assertIn(user_command, commands)
            self.assertTrue(
                any(item.endswith(f"routing-hook {event} agent-switchboard codex") for item in commands)
            )
        self.assertTrue(
            any(
                str(group.get("matcher", "")) == ".*"
                for group in codex_hooks["PreToolUse"]
            )
        )

        hierarchy_lower = codex_text.lower()
        self.assertIn("same-vendor", hierarchy_lower)
        self.assertIn("default workhorse", hierarchy_lower)
        self.assertIn("agent switchboard", hierarchy_lower)
        self.assertIn("opposite-vendor", hierarchy_lower)
        self.assertIn("fallback", hierarchy_lower)
        self.assertIn("capability tier outranks model version", hierarchy_lower)
        self.assertIn("gemini flash high is a useful, non-authoritative workhorse-level adviser", hierarchy_lower)
        self.assertIn("does not promote it above sol/fable", hierarchy_lower)
        self.assertIn("quota, reachability, entitlement", hierarchy_lower)
        self.assertIn("codex brain should request a second opinion", hierarchy_lower)
        self.assertIn("newest live antigravity flash high", hierarchy_lower)
        self.assertIn("label it degraded advisory fallback", hierarchy_lower)
        self.assertIn("retain final judgment", hierarchy_lower)
        self.assertIn("external antigravity flash lane", hierarchy_lower)
        self.assertIn("not a native child agent", hierarchy_lower)
        self.assertIn("codex, claude, and gemini brains use the newest live antigravity gemini flash high", hierarchy_lower)
        self.assertIn("bounded search, reading, extraction, summaries, drafting", hierarchy_lower)
        self.assertIn("low-risk implementation/tests from an approved plan", hierarchy_lower)
        self.assertIn("must call mcp `route_agent_task`", hierarchy_lower)
        self.assertIn("must not invoke `agy`", hierarchy_lower)
        self.assertIn("only the switchboard backend may start `agy`", hierarchy_lower)
        self.assertIn("target_agent=\"antigravity\"", hierarchy_lower)
        self.assertIn("surface=\"cli\"", hierarchy_lower)
        self.assertIn("every flash call is exactly one bounded work package", hierarchy_lower)
        self.assertIn("work_package_id", hierarchy_lower)
        self.assertIn("mandatory `--output-format json --json-schema` contract", hierarchy_lower)
        self.assertIn("never receives `danger-full-access`", hierarchy_lower)
        self.assertIn("production ssh", hierarchy_lower)
        self.assertIn("a flash completion is never acceptance", hierarchy_lower)
        self.assertIn("unsupported claims that a defect is intentional/by design keep the investigation open", hierarchy_lower)
        self.assertIn("missing, quota-limited, times out, mismatches", hierarchy_lower)
        self.assertIn("codex `explorer`/`worker`", hierarchy_lower)
        self.assertIn("claude `explore`/`economy-worker`", hierarchy_lower)
        self.assertIn("record the fallback", hierarchy_lower)
        self.assertIn("concurrently only on independent stages/packages", hierarchy_lower)
        self.assertIn("writes run serially unless", hierarchy_lower)
        self.assertIn("brain reviews evidence and actual diffs", hierarchy_lower)
        self.assertIn("imported", hierarchy_lower)
        self.assertIn("semantic", hierarchy_lower)
        self.assertIn("resolve", hierarchy_lower)
        self.assertIn("execution", hierarchy_lower)
        self.assertIn("brain-context ingress", hierarchy_lower)
        self.assertIn("decision premise", hierarchy_lower)
        self.assertIn("reader locates", hierarchy_lower)
        self.assertIn("every planned and unplanned package", hierarchy_lower)
        self.assertIn("direct-brain-labour", hierarchy_lower)
        self.assertIn("pretooluse", hierarchy_lower)
        self.assertIn("allowance is four direct labour calls", hierarchy_lower)
        self.assertIn("each relief opens only the next bounded block", hierarchy_lower)
        self.assertIn("reconcile every claude-managed background", hierarchy_lower)
        self.assertIn("launching or detaching a job is never verification", hierarchy_lower)

        codex_reader = self.paths.codex_explorer.read_text(encoding="utf-8").lower()
        claude_reader = self.paths.claude_explore.read_text(encoding="utf-8").lower()
        for reader_text in (codex_reader, claude_reader):
            self.assertIn("observed fact from interpretation", reader_text)
            self.assertIn("decision premise", reader_text)
            self.assertIn("never adjudicate", reader_text)
            self.assertIn("no more than 8,000 characters", reader_text)

        codex_worker = self.paths.codex_worker.read_text(encoding="utf-8").lower()
        claude_worker = self.paths.claude_worker.read_text(encoding="utf-8").lower()
        for worker_text in (codex_worker, claude_worker):
            self.assertIn("return no more than 8,000 characters", worker_text)
            self.assertIn("large logs/artifacts outside the brain context", worker_text)
        self.assertIn("reconcile every background bash/powershell/monitor job", claude_worker)
        self.assertIn("launching or detaching a job is never verification", claude_worker)

        before = {path: path.read_bytes() for path in (
            self.paths.codex_agents_md,
            self.paths.claude_md,
            self.paths.gemini_md,
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

    def test_refresh_preserves_unrelated_gemini_content(self):
        self.paths.gemini_md.parent.mkdir(parents=True, exist_ok=True)
        self.paths.gemini_md.write_text("# My Gemini rules\nkeep this\n", encoding="utf-8")

        result = self.refresh()["Gemini global hierarchy"]

        self.assertEqual(result, "updated")
        text = self.paths.gemini_md.read_text(encoding="utf-8")
        self.assertIn("# My Gemini rules\nkeep this", text)
        self.assertEqual(text.count("agent-switchboard:cost-routing:begin"), 1)
        self.assertIn(self.paths.gemini_md, self.backups)

    def test_refresh_refuses_near_match_legacy_gemini_content(self):
        self.paths.gemini_md.parent.mkdir(parents=True, exist_ok=True)
        original = (
            "SECTION 1: SCOPE & BOUNDARIES\n"
            "- Role: Production-grade Quant Dev for TradingView Pine Script v6\n"
            "SECTION 7 - OUTPUT REQUIREMENTS\n"
        )
        self.paths.gemini_md.write_text(original, encoding="utf-8")

        result = self.refresh()["Gemini global hierarchy"]

        self.assertTrue(result.startswith("ERROR: possible legacy"), result)
        self.assertEqual(self.paths.gemini_md.read_text(encoding="utf-8"), original)
        self.assertNotIn(self.paths.gemini_md, self.backups)

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

    def test_catalog_failure_keeps_last_known_roles_and_never_installs_stale_ids(self):
        empty_roles = {"frontier": None, "workhorse": None, "reader": None}
        first = hierarchy_install.refresh(
            self.paths,
            empty_roles,
            CLAUDE_ROLES,
            '"C:\\Agent Switchboard\\agent-switchboard.exe" routing-hook',
            self.backup,
        )
        self.assertIn("no stale model installed", first["Codex explorer role"])
        self.assertIn("no stale model installed", first["Codex worker role"])
        self.assertFalse(self.paths.codex_explorer.exists())
        self.assertFalse(self.paths.codex_worker.exists())

        self.refresh()
        explorer_before = self.paths.codex_explorer.read_bytes()
        worker_before = self.paths.codex_worker.read_bytes()
        second = hierarchy_install.refresh(
            self.paths,
            empty_roles,
            CLAUDE_ROLES,
            '"C:\\Agent Switchboard\\agent-switchboard.exe" routing-hook',
            self.backup,
        )
        self.assertIn("kept last-known managed role", second["Codex explorer role"])
        self.assertIn("kept last-known managed role", second["Codex worker role"])
        self.assertEqual(explorer_before, self.paths.codex_explorer.read_bytes())
        self.assertEqual(worker_before, self.paths.codex_worker.read_bytes())

    def test_catalog_failure_never_trusts_tampered_or_user_owned_role(self):
        empty_roles = {"frontier": None, "workhorse": None, "reader": None}
        self.refresh()
        explorer = self.paths.codex_explorer.read_text(encoding="utf-8")
        tampered = explorer.replace('model = "gpt-next-luna"', 'model = "user-edited"')
        self.paths.codex_explorer.write_text(tampered, encoding="utf-8")
        user_worker = 'name = "worker"\nmodel = "user-owned"\n'
        self.paths.codex_worker.write_text(user_worker, encoding="utf-8")

        result = hierarchy_install.refresh(
            self.paths,
            empty_roles,
            CLAUDE_ROLES,
            '"C:\\Agent Switchboard\\agent-switchboard.exe" routing-hook',
            self.backup,
        )

        self.assertTrue(result["Codex explorer role"].startswith("ERROR"))
        self.assertIn("user-owned", result["Codex worker role"])
        self.assertEqual(self.paths.codex_explorer.read_text(encoding="utf-8"), tampered)
        self.assertEqual(self.paths.codex_worker.read_text(encoding="utf-8"), user_worker)

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
        self.assertNotIn(
            "agent-switchboard:cost-routing:begin",
            self.paths.gemini_md.read_text(encoding="utf-8"),
        )
        settings = json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))
        stop_commands = [
            item["command"]
            for group in settings["hooks"]["Stop"]
            for item in group.get("hooks", [])
        ]
        self.assertEqual(stop_commands, ["shutdown-if-armed.ps1"])
        remaining_handlers = [
            item
            for groups in settings.get("hooks", {}).values()
            for group in groups
            for item in group.get("hooks", [])
        ]
        self.assertFalse(
            any(
                hierarchy_install.routing_gate.is_owned_hook_entry(item)
                for item in remaining_handlers
            )
        )


    # -- WP8-HIERARCHY-TEXT: Flash-first routing body + full-tool matcher --

    def test_body_makes_flash_the_default_workhorse(self):
        body = hierarchy_install.routing_rules_body(CODEX_ROLES, CLAUDE_ROLES)
        self.assertIn("route_agent_task", body)
        self.assertIn("gemini flash", body.lower())
        self.assertIn("surface", body.lower())
        self.assertIn("STANDING REQUEST", body)
        self.assertNotIn("Native-first routing order is mandatory", body)
        self.assertNotIn("first ten", body.lower())

    def test_body_keeps_flash_prohibitions(self):
        body = hierarchy_install.routing_rules_body(CODEX_ROLES, CLAUDE_ROLES)
        for phrase in (
            "danger-full-access",
            "production SSH",
            "destructive operations, migrations, or live deployment",
            "exactly one bounded work package",
            "never a frontier consultant",
            "MUST NOT invoke `agy`",
            "Only the Switchboard backend may start `agy`",
            "A Flash completion is never acceptance",
            "Routing audit",
            "broker:<uuid>",
        ):
            self.assertIn(phrase, body, phrase)

    def test_body_has_no_pinned_flash_version(self):
        body = hierarchy_install.routing_rules_body(CODEX_ROLES, CLAUDE_ROLES)
        self.assertNotIn("3.6", body)
        self.assertNotIn("3.7", body)
        self.assertIsNone(re.search(r"[Ff]lash\s*3\.\d", body))

    def test_matcher_intercepts_all_tools(self):
        path = self.paths.claude_settings
        path.parent.mkdir(parents=True, exist_ok=True)
        hierarchy_install.update_hooks(
            path,
            '"C:\\Agent Switchboard\\agent-switchboard.exe" routing-hook',
            "claude",
            self.backup,
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        for event in ("PreToolUse", "PostToolUse"):
            matchers = [group.get("matcher") for group in data["hooks"][event]]
            self.assertIn(".*", matchers)

    def test_refresh_replaces_prior_installer_block_without_edited_refusal(self):
        self.paths.claude_md.parent.mkdir(parents=True, exist_ok=True)
        known_checksum = next(iter(hierarchy_install.KNOWN_HIERARCHY_BODY_CHECKSUMS))
        # Simulate a block written by a prior installer release whose marker
        # checksum does not self-hash the body beneath it (e.g. an older
        # canonicalization). It is still recognized because the checksum is
        # one this installer has shipped before.
        stale_block = (
            f"<!-- agent-switchboard:cost-routing:begin sha256={known_checksum} -->\n"
            "old policy text from a prior installer release\n"
            f"{hierarchy_install.BLOCK_END}\n"
        )
        self.paths.claude_md.write_text(
            "# My CLAUDE.md\nkeep this\n\n" + stale_block, encoding="utf-8"
        )

        first = self.refresh()["Claude global hierarchy"]
        self.assertEqual(first, "updated")
        text = self.paths.claude_md.read_text(encoding="utf-8")
        self.assertNotIn("old policy text from a prior installer release", text)
        self.assertIn("# My CLAUDE.md\nkeep this", text)
        self.assertEqual(text.count("agent-switchboard:cost-routing:begin"), 1)

        second = self.refresh()["Claude global hierarchy"]
        self.assertEqual(second, "unchanged")

    def test_refresh_still_refuses_user_edited_block(self):
        self.paths.claude_md.parent.mkdir(parents=True, exist_ok=True)
        unknown_checksum = "0" * 64
        edited_block = (
            f"<!-- agent-switchboard:cost-routing:begin sha256={unknown_checksum} -->\n"
            "hand-edited routing policy text\n"
            f"{hierarchy_install.BLOCK_END}\n"
        )
        self.paths.claude_md.write_text(edited_block, encoding="utf-8")

        result = self.refresh()["Claude global hierarchy"]

        self.assertTrue(result.startswith("ERROR"), result)
        self.assertEqual(self.paths.claude_md.read_text(encoding="utf-8"), edited_block)

    def test_all_three_globals_get_equivalent_body(self):
        self.refresh()
        bodies = {}
        for name, path in (
            ("codex", self.paths.codex_agents_md),
            ("claude", self.paths.claude_md),
            ("gemini", self.paths.gemini_md),
        ):
            parts = hierarchy_install._block_parts(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(parts, name)
            bodies[name] = parts[2]
        self.assertEqual(bodies["codex"], bodies["claude"])
        self.assertEqual(bodies["claude"], bodies["gemini"])


if __name__ == "__main__":
    unittest.main()
