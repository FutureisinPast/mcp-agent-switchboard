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
