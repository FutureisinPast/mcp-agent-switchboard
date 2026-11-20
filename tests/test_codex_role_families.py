"""Focused tests for versioned Codex model family role selection (WP-1075)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import model_roles  # noqa: E402
import switchboard_version  # noqa: E402


class CodexRoleFamilyTests(unittest.TestCase):
    def test_live_catalog_picks_newest_per_family(self):
        catalog = {
            "models": [
                {"id": "gpt-6.2-astra", "priority": 8, "visibility": "list", "description": "frontier"},
                {"id": "gpt-6.2-sol", "priority": 9, "visibility": "list", "description": "affordable fast"},
                {"id": "gpt-6.1-sol", "priority": 2, "visibility": "list", "description": "balanced workhorse"},
                {"id": "gpt-6-sol", "priority": 4, "visibility": "list", "description": "workhorse"},
                {"id": "gpt-6-luna", "priority": 10, "visibility": "list", "description": "general"},
                {"id": "gpt-5.6-sol", "priority": 5, "visibility": "list"},
                {"id": "gpt-5.6-luna", "priority": 6, "visibility": "list", "description": "affordable fast"},
                {"id": "gpt-5.6-terra", "priority": 3, "visibility": "list"},
                {"id": "gpt-6.1-terra", "priority": 1, "visibility": "list", "description": "affordable fast"},
                {"id": "gpt-6-astra", "priority": 5, "visibility": "list"},
            ]
        }
        roles = model_roles.select_codex_roles(catalog)
        self.assertIsInstance(roles, model_roles.CodexRoles)
        self.assertEqual(roles.frontier["id"], "gpt-6.2-astra")
        self.assertEqual(roles.workhorse["id"], "gpt-6.2-sol")
        self.assertEqual(roles.reader["id"], "gpt-6-luna")
        for role in (roles.frontier, roles.workhorse, roles.reader):
            self.assertNotIn("raw", role)

    def test_numeric_tuple_version_ordering_beats_lexicographic(self):
        catalog = {
            "models": [
                {"id": "gpt-6.2-astra", "priority": 5, "visibility": "list"},
                {"id": "gpt-6.10-sol", "priority": 10, "visibility": "list"},
                {"id": "gpt-6.9-sol", "priority": 1, "visibility": "list"},
                {"id": "gpt-6.2-sol", "priority": 2, "visibility": "list"},
                {"id": "gpt-6.1-sol", "priority": 3, "visibility": "list"},
                {"id": "gpt-6-sol", "priority": 4, "visibility": "list"},
                {"id": "gpt-5.6-sol", "priority": 5, "visibility": "list"},
                {"id": "gpt-6-luna", "priority": 6, "visibility": "list"},
            ]
        }
        roles = model_roles.select_codex_roles(catalog)
        self.assertEqual(roles.workhorse["id"], "gpt-6.10-sol")

    def test_hidden_newer_entry_is_ignored(self):
        catalog = {
            "models": [
                {"id": "gpt-7-astra", "priority": 1, "visibility": "hidden"},
                {"id": "gpt-6.2-astra", "priority": 5, "visibility": "list"},
                {"id": "gpt-7-sol", "priority": 1, "visibility": "experimental"},
                {"id": "gpt-6.1-sol", "priority": 5, "visibility": "list"},
                {"id": "gpt-7-luna", "priority": 1, "visibility": "hidden"},
                {"id": "gpt-6-luna", "priority": 5, "visibility": "list"},
            ]
        }
        roles = model_roles.select_codex_roles(catalog)
        self.assertEqual(roles.frontier["id"], "gpt-6.2-astra")
        self.assertEqual(roles.workhorse["id"], "gpt-6.1-sol")
        self.assertEqual(roles.reader["id"], "gpt-6-luna")

    def test_missing_luna_falls_back_to_old_reader_logic_only(self):
        catalog = {
            "models": [
                {"id": "gpt-6.2-astra", "priority": 5, "visibility": "list"},
                {"id": "gpt-6.2-sol", "priority": 4, "visibility": "list"},
                {"id": "gpt-6.1-terra", "priority": 9, "visibility": "list", "description": "affordable fast"},
            ]
        }
        roles = model_roles.select_codex_roles(catalog)
        self.assertEqual(roles.frontier["id"], "gpt-6.2-astra")
        self.assertEqual(roles.workhorse["id"], "gpt-6.2-sol")
        self.assertEqual(roles.reader["id"], "gpt-6.1-terra")

    def test_missing_astra_falls_back_to_old_frontier_logic_only(self):
        catalog = {
            "models": [
                {"id": "gpt-7-custom", "priority": 1, "visibility": "list", "description": "frontier"},
                {"id": "gpt-6.2-sol", "priority": 5, "visibility": "list"},
                {"id": "gpt-6-luna", "priority": 6, "visibility": "list"},
            ]
        }
        roles = model_roles.select_codex_roles(catalog)
        self.assertEqual(roles.frontier["id"], "gpt-7-custom")
        self.assertEqual(roles.workhorse["id"], "gpt-6.2-sol")
        self.assertEqual(roles.reader["id"], "gpt-6-luna")

    def test_unknown_families_are_never_chosen_by_family(self):
        catalog = {
            "models": [
                {"id": "gpt-6-astra", "priority": 10, "visibility": "list"},
                {"id": "gpt-6-sol", "priority": 10, "visibility": "list"},
                {"id": "gpt-6-luna", "priority": 10, "visibility": "list"},
                {"id": "gpt-99-terra", "priority": 1, "visibility": "list"},
                {"id": "gpt-99-mini", "priority": 1, "visibility": "list"},
                {"id": "gpt-99-reserve", "priority": 1, "visibility": "list"},
                {"id": "gpt-99-auto-review", "priority": 1, "visibility": "list"},
            ]
        }
        roles = model_roles.select_codex_roles(catalog)
        self.assertEqual(roles.frontier["id"], "gpt-6-astra")
        self.assertEqual(roles.workhorse["id"], "gpt-6-sol")
        self.assertEqual(roles.reader["id"], "gpt-6-luna")

    def test_family_matching_is_case_insensitive_and_seed_is_fallback_only(self):
        catalog = model_roles.seed_codex_frontier(
            {
                "models": [
                    {"id": "GPT-6.2-ASTRA", "priority": 9, "visibility": "list"},
                    {"id": "gpt-6.2-Sol", "priority": 9, "visibility": "list"},
                    {"id": "gpt-6-LUNA", "priority": 9, "visibility": "list"},
                ]
            }
        )
        roles = model_roles.select_codex_roles(catalog)
        self.assertEqual(roles.frontier["id"], "GPT-6.2-ASTRA")
        self.assertEqual(roles.workhorse["id"], "gpt-6.2-Sol")
        self.assertEqual(roles.reader["id"], "gpt-6-LUNA")

    def test_version_bumped_to_1_0_75(self):
        self.assertGreaterEqual(tuple(int(x) for x in switchboard_version.BROKER_VERSION.split(".")), (1, 0, 75))


if __name__ == "__main__":
    unittest.main()
