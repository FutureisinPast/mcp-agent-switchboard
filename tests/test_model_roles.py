"""Focused stdlib-only tests for model_roles.py (WP3a)."""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import model_roles  # noqa: E402


class StandaloneImportTests(unittest.TestCase):
    def test_module_source_never_imports_agent_broker_mcp(self):
        source = Path(model_roles.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("agent_broker_mcp", imports)

    def test_module_has_no_reference_to_agent_broker_mcp_at_runtime(self):
        self.assertFalse(hasattr(model_roles, "agent_broker_mcp"))


class VisibilityFilterTests(unittest.TestCase):
    def _entries(self, models):
        return model_roles._visible_entries({"models": models})

    def test_absent_visibility_is_kept(self):
        out = self._entries([{"id": "a", "priority": 1}])
        self.assertEqual([e["id"] for e in out], ["a"])

    def test_visibility_list_is_kept(self):
        out = self._entries([{"id": "a", "priority": 1, "visibility": "list"}])
        self.assertEqual([e["id"] for e in out], ["a"])

    def test_visibility_hidden_is_filtered(self):
        out = self._entries([{"id": "a", "priority": 1, "visibility": "hidden"}])
        self.assertEqual(out, [])

    def test_visibility_experimental_is_filtered(self):
        out = self._entries([{"id": "a", "priority": 1, "visibility": "experimental"}])
        self.assertEqual(out, [])

    def test_mixed_visibility_keeps_only_listed_and_absent(self):
        out = self._entries(
            [
                {"id": "a", "priority": 1, "visibility": "hidden"},
                {"id": "b", "priority": 2, "visibility": "list"},
                {"id": "c", "priority": 3},
            ]
        )
        self.assertEqual(sorted(e["id"] for e in out), ["b", "c"])


class MetadataRetentionTests(unittest.TestCase):
    def test_description_priority_reasoning_retained(self):
        entry = {
            "id": "gpt-x",
            "priority": 2,
            "description": "flagship reasoning model",
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [{"effort": "high"}],
            "visibility": "list",
        }
        out = model_roles._visible_entries({"models": [entry]})
        self.assertEqual(len(out), 1)
        norm = out[0]
        self.assertEqual(norm["description"], "flagship reasoning model")
        self.assertEqual(norm["priority"], 2.0)
        self.assertEqual(norm["default_reasoning_level"], "high")
        self.assertEqual(norm["supported_reasoning_levels"], [{"effort": "high"}])
        self.assertEqual(norm["visibility"], "list")


class SelectCodexRolesTests(unittest.TestCase):
    def test_empty_catalog_returns_all_none_for_caller_fallback(self):
        roles = model_roles.select_codex_roles({})
        self.assertIsNone(roles.frontier)
        self.assertIsNone(roles.workhorse)
        self.assertIsNone(roles.reader)

    def test_frontier_is_lowest_priority(self):
        models = [
            {"id": "mid", "priority": 5},
            {"id": "top", "priority": 1},
            {"id": "low", "priority": 9},
        ]
        roles = model_roles.select_codex_roles({"models": models})
        self.assertEqual(roles.frontier["id"], "top")

    def test_workhorse_chosen_by_keyword(self):
        models = [
            {"id": "top", "priority": 1, "description": "flagship"},
            {"id": "mid", "priority": 5, "description": "balanced everyday driver"},
            {"id": "low", "priority": 9, "description": "affordable fast option"},
        ]
        roles = model_roles.select_codex_roles({"models": models})
        self.assertEqual(roles.workhorse["id"], "mid")

    def test_reader_chosen_by_keyword(self):
        models = [
            {"id": "top", "priority": 1, "description": "flagship"},
            {"id": "mid", "priority": 5, "description": "balanced everyday driver"},
            {"id": "low", "priority": 9, "description": "affordable cost-efficient fast"},
        ]
        roles = model_roles.select_codex_roles({"models": models})
        self.assertEqual(roles.reader["id"], "low")

    def test_workhorse_falls_back_to_next_priority_without_keyword_hit(self):
        models = [
            {"id": "top", "priority": 1, "description": "flagship"},
            {"id": "mid", "priority": 5, "description": "no keyword hit here"},
        ]
        roles = model_roles.select_codex_roles({"models": models})
        self.assertEqual(roles.workhorse["id"], "mid")

    def test_deterministic_tie_break_by_id_on_equal_priority(self):
        models = [
            {"id": "zeta", "priority": 1},
            {"id": "alpha", "priority": 1},
        ]
        roles_a = model_roles.select_codex_roles({"models": models})
        roles_b = model_roles.select_codex_roles({"models": list(reversed(models))})
        self.assertEqual(roles_a.frontier["id"], "alpha")
        self.assertEqual(roles_a.frontier["id"], roles_b.frontier["id"])

    def test_hidden_entries_excluded_from_role_selection(self):
        models = [
            {"id": "secret", "priority": 0, "visibility": "hidden"},
            {"id": "top", "priority": 1},
        ]
        roles = model_roles.select_codex_roles({"models": models})
        self.assertEqual(roles.frontier["id"], "top")


class SelectClaudeRolesTests(unittest.TestCase):
    def test_frontier_chain_is_fable_then_opus(self):
        out = model_roles.select_claude_roles()
        self.assertEqual(out["frontier"], ["fable", "opus"])

    def test_workhorse_and_reader_aliases(self):
        out = model_roles.select_claude_roles()
        self.assertEqual(out["workhorse"], "sonnet")
        self.assertEqual(out["reader"], "haiku")

    def test_reader_is_future_proof_alias_not_pinned_id(self):
        out = model_roles.select_claude_roles()
        self.assertNotIn("reader_pinned_id", out)


if __name__ == "__main__":
    unittest.main()
