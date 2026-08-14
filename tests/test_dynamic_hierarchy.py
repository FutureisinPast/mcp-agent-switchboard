"""Focused tests for future-proof role selection and Claude fallbacks/modes."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def claude_stream(model: str, response: str = "ok") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"model": model, "content": [{"text": response}]},
                }
            ),
            json.dumps({"type": "result", "result": response}),
        ]
    )


class DynamicCodexRoleTests(unittest.TestCase):
    def test_live_priority_and_descriptions_select_all_roles(self):
        roles = broker.codex_roles_from_models(
            [
                broker.model_entry(
                    "gpt-8-brain", source="codex-debug", metadata={"priority": 1, "visibility": "list", "description": "frontier"}
                ),
                broker.model_entry(
                    "gpt-8-worker", source="codex-debug", metadata={"priority": 4, "visibility": "list", "description": "balanced everyday workhorse"}
                ),
                broker.model_entry(
                    "gpt-8-reader", source="codex-debug", metadata={"priority": 9, "visibility": "list", "description": "affordable cost-efficient fast"}
                ),
            ]
        )
        self.assertEqual(roles["frontier"]["id"], "gpt-8-brain")
        self.assertEqual(roles["workhorse"]["id"], "gpt-8-worker")
        self.assertEqual(roles["reader"]["id"], "gpt-8-reader")

    def test_cost_policies_use_dynamic_roles(self):
        with mock.patch.object(
            broker,
            "current_codex_role_model",
            side_effect=lambda role: {"reader": "gpt-8-reader", "workhorse": "gpt-8-worker"}[role],
        ):
            cheap = broker.apply_codex_model_policy(
                {"model_policy": "cheap_read"}, "read files", "research", None, None
            )
            balanced = broker.apply_codex_model_policy(
                {"model_policy": "workhorse"}, "write tests", "implementation", None, None
            )
        self.assertEqual(cheap[:2], ("gpt-8-reader", broker.CODEX_CHEAP_EFFORT))
        self.assertEqual(balanced[:2], ("gpt-8-worker", "medium"))


class DynamicAntigravityRoleTests(unittest.TestCase):
    @staticmethod
    def _catalog(*live_slugs: str) -> list[dict]:
        return [
            broker.antigravity_model_entry_from_slug(
                broker.ANTIGRAVITY_DEFAULT_MODEL, "static"
            ),
            *[
                broker.antigravity_model_entry_from_slug(slug, "antigravity-cli")
                for slug in live_slugs
            ],
        ]

    def test_live_37_outranks_36_and_is_never_a_brain(self):
        roles = broker.antigravity_roles_from_models(
            self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        )
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertIsNone(roles["frontier"])
        self.assertFalse(roles["authoritative"])
        self.assertFalse(roles["peer_brain_eligible"])
        self.assertEqual(roles["capability_tier"], "workhorse")

    def test_live_cli_tabular_catalog_is_parsed_and_drives_workhorse(self):
        stdout = "\n".join(
            [
                "gemini-3.7-flash-high\tGemini 3.7 Flash (High)",
                "gemini-3.6-flash-high\tGemini 3.6 Flash (High)",
            ]
        )
        proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE", None), \
             mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE_AT", 0.0), \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker.subprocess, "run", return_value=proc), \
             mock.patch.object(broker.shutil, "which", return_value=None):
            models = broker.discover_antigravity_models()
        roles = broker.antigravity_roles_from_models(models)
        by_id = {item["id"]: item for item in models}
        self.assertEqual(by_id["gemini-3.7-flash-high"]["source"], "antigravity-cli")
        self.assertEqual(by_id["gemini-3.6-flash-high"]["source"], "antigravity-cli")
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertEqual(roles["source"], "antigravity-cli")

    def test_future_versions_sort_numerically_and_preview_never_promotes(self):
        roles = broker.antigravity_roles_from_models(
            self._catalog(
                "gemini-3.9-flash-high",
                "gemini-3.10-flash-high",
                "gemini-99.0-flash-high-preview",
            )
        )
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.10-flash-high")

    def test_static_36_is_offline_fallback_only(self):
        roles = broker.antigravity_roles_from_models(self._catalog())
        self.assertEqual(roles["workhorse"]["id"], broker.ANTIGRAVITY_DEFAULT_MODEL)
        self.assertEqual(roles["source"], "offline-fallback")

    def test_generic_flash_moves_to_latest_but_explicit_version_stays_exact(self):
        catalog = self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        common = {
            "target_agent": "antigravity",
            "project": "p",
            "topic": "routing-test",
        }
        with mock.patch.object(broker, "discover_antigravity_models", return_value=catalog), \
             mock.patch.object(broker, "find_model_default", return_value=None), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")):
            bare = broker.resolve_model_request(common)
            generic = broker.resolve_model_request({**common, "target_model": "gemini flash"})
            exact = broker.resolve_model_request(
                {**common, "target_model": "gemini-3.6-flash-high"}
            )
        self.assertEqual(bare["target_model"], "gemini-3.7-flash-high")
        self.assertEqual(bare["source"], "family_workhorse")
        self.assertEqual(generic["target_model"], "gemini-3.7-flash-high")
        self.assertEqual(generic["source"], "family_workhorse")
        self.assertEqual(exact["target_model"], "gemini-3.6-flash-high")
        self.assertEqual(exact["source"], "explicit_request")

    def test_catalog_and_guide_expose_non_authoritative_workhorse_role(self):
        models = self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        with mock.patch.object(broker, "discover_antigravity_models", return_value=models):
            catalog = broker.list_agent_models("antigravity")
            guide = broker.get_model_routing_guide("antigravity")
        roles = catalog["catalogs"]["antigravity"]["roles"]
        policy = guide["defaults"]["antigravity_cli"]
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertFalse(roles["peer_brain_eligible"])
        self.assertEqual(policy["role"], "workhorse")
        self.assertFalse(policy["authoritative"])
        self.assertFalse(policy["peer_brain_eligible"])
        self.assertFalse(policy["native_child_agent"])
        self.assertIn("bounded search/read/extraction/summaries/drafting", policy["recommended_for"])
        self.assertEqual(policy["failure_fallback"]["codex"], ["explorer", "worker"])
        self.assertEqual(policy["failure_fallback"]["claude"], ["Explore", "economy-worker"])
        self.assertTrue(policy["failure_fallback"]["record_fallback"])
        self.assertIn("proactively consider", policy["rule"])
        self.assertIn("not a native child agent", policy["rule"])
        self.assertIn("missing, quota-limited, times out, mismatches, or fails", policy["rule"])
        self.assertIn("concurrently only on independent packages", policy["rule"])
        self.assertIn("writes are serial unless demonstrably isolated", policy["rule"])
        self.assertIn("brain reviews evidence/diffs", policy["rule"])

        examples = guide["caller_examples"]
        read_args = examples["antigravity_external_read"]["args"]
        write_args = examples["antigravity_external_implementation"]["args"]
        for args in (read_args, write_args):
            self.assertEqual(args["target_agent"], "antigravity")
            self.assertEqual(args["surface"], "cli")
            self.assertEqual(args["target_model"], "gemini flash")
            self.assertEqual(args["effort"], "high")
        self.assertEqual(read_args["mode"], "plan")
        self.assertEqual(write_args["mode"], "accept-edits")
        self.assertIn("approved isolated package", write_args["prompt"])


class ClaudeFrontierFallbackTests(unittest.TestCase):
    def _consult(self, runs):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value="claude"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "claude_empty_mcp_config_path", return_value=Path(tmpdir) / "empty.json"), \
             mock.patch.object(broker, "run_process", side_effect=runs) as run:
            result = broker.consult_claude(tmpdir, "check", model_name="best", effort="max")
            return result, run

    def test_best_alias_attests_any_structured_claude_model(self):
        self.assertTrue(broker.claude_model_attested("best", "claude-fable-5"))
        self.assertTrue(broker.claude_model_attested("best", "claude-opus-6-1"))
        self.assertFalse(broker.claude_model_attested("best", "unknown-provider-model"))

    def test_best_unavailable_falls_back_to_fable(self):
        result, run = self._consult(
            [
                (1, "", "Invalid model name: model 'best' is not available"),
                (0, claude_stream("claude-fable-5", "approved"), ""),
            ]
        )
        self.assertEqual(result.initial_model, "best")
        self.assertEqual(result.requested_model, "fable")
        self.assertEqual(result.actual_model, "claude-fable-5")
        self.assertEqual(result.attempted_models, ("best", "fable"))
        self.assertTrue(result.model_attested)
        self.assertEqual(run.call_count, 2)

    def test_best_and_fable_unavailable_fall_back_to_opus(self):
        result, run = self._consult(
            [
                (1, "", "Unknown model: best"),
                (1, "", "Model fable is unavailable for this subscription"),
                (0, claude_stream("claude-opus-6-1", "approved"), ""),
            ]
        )
        self.assertEqual(result.requested_model, "opus")
        self.assertEqual(result.attempted_models, ("best", "fable", "opus"))
        self.assertEqual(run.call_count, 3)

    def test_general_failure_does_not_retry(self):
        result, run = self._consult([(1, "", "Network connection reset")])
        self.assertFalse(result.model_attested)
        self.assertEqual(result.attempted_models, ("best",))
        self.assertEqual(run.call_count, 1)


class ClaudeQueuedModeTests(unittest.TestCase):
    def test_queue_persists_implementation_mode(self):
        # sqlite3's context manager commits but does not close the connection;
        # tolerate delayed handle release on Windows test cleanup.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            broker_home = root / "broker"
            db_path = broker_home / "state.sqlite"
            with mock.patch.object(broker, "BROKER_DIR", broker_home), \
                 mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "CONFIG_PATH", broker_home / "config.json"):
                result = broker.queue_claude_request(
                    str(root),
                    "implement the approved patch",
                    target_model="sonnet",
                    cli_model="sonnet",
                    autorun=False,
                    mode="acceptEdits",
                )
                with sqlite3.connect(db_path) as conn:
                    stored = conn.execute(
                        "SELECT mode FROM claude_requests WHERE id = ?", (result["id"],)
                    ).fetchone()[0]
            self.assertEqual(result["mode"], "acceptEdits")
            self.assertEqual(stored, "acceptEdits")

    def test_worker_reuses_persisted_implementation_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            broker_home = root / "broker"
            db_path = broker_home / "state.sqlite"
            with mock.patch.object(broker, "BROKER_DIR", broker_home), \
                 mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "CONFIG_PATH", broker_home / "config.json"):
                queued = broker.queue_claude_request(
                    str(root),
                    "implement the approved patch",
                    target_model="sonnet",
                    cli_model="sonnet",
                    autorun=False,
                    mode="acceptEdits",
                )
                response = broker.ClaudeConsultResult(
                    response="implemented",
                    requested_model="sonnet",
                    actual_model="claude-sonnet-5",
                    model_attested=True,
                    initial_model="sonnet",
                    attempted_models=("sonnet",),
                )
                with mock.patch.object(broker, "consult_claude", return_value=response) as consult, \
                     mock.patch.object(broker, "store_consultation"), \
                     mock.patch.object(broker, "record_agent_event"), \
                     mock.patch.object(broker, "render_request_ledger"):
                    result = broker.run_claude_request_worker(queued["id"])
            self.assertEqual(consult.call_args.args[2], "acceptEdits")
            self.assertEqual(result["mode"], "acceptEdits")
            self.assertEqual(result["actual_model"], "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
