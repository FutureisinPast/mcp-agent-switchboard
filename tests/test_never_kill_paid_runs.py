"""Tests for WP-ASYNC-I1: Never kill paid runs.

Verifies:
1. AGENT_BROKER_INLINE_WAIT_SECONDS clamping [15..220s], default 200s, and module-level export.
2. Claude consults for all non-quick tasks run via queue_claude_request + worker (cap 1800s),
   wait inline up to INLINE_WAIT, return completed if finished, pending if slow; process is never killed.
3. Claude quick_check/sanity_check stay synchronous with 240s kill.
4. consult_decision Claude legs at high effort set call_args['async'] = True.
5. Codex inline wait respects INLINE_WAIT and returns pending with poll 180s.
6. Gemini consult for non-quick tasks runs via flash_requests with task_kind='gemini_consult',
   bridge verb 'run-gemini-consult', cap 1800s, inline wait, and pending contract; quick_check/search stay sync.
7. Antigravity consult for non-quick tasks runs through Flash async lane with inline wait; quick_check/search stay sync;
   worker re-entry runs consult_antigravity_cli directly.
8. Late results for all providers are preserved and accessible via request_result.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


class NeverKillPaidRunsBase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.orig_child = os.environ.get("AGENT_BROKER_CHILD")
        os.environ["AGENT_BROKER_CHILD"] = ""
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp_path = Path(self.tmp_dir.name)
        self.broker_dir = self.tmp_path / "broker_home"
        self.broker_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.broker_dir / "test_state.sqlite"

        self.patches = [
            mock.patch.object(broker, "BROKER_DIR", self.broker_dir),
            mock.patch.object(broker, "DB_PATH", self.db_path),
            mock.patch.object(broker, "CLAUDE_QUEUE_AUTORUN", True),
            mock.patch.object(broker, "CODEX_QUEUE_AUTORUN", True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        broker.init_db()
        broker.register_project("synthetic-proj", str(self.tmp_path))

    def tearDown(self):
        if self.orig_child is not None:
            os.environ["AGENT_BROKER_CHILD"] = self.orig_child
        else:
            os.environ.pop("AGENT_BROKER_CHILD", None)
        super().tearDown()


class InlineWaitConfigurationTests(NeverKillPaidRunsBase):
    def test_default_inline_wait_seconds(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_BROKER_INLINE_WAIT_SECONDS", None)
            self.assertEqual(broker.get_inline_wait_seconds(), 200)
            self.assertEqual(broker.INLINE_WAIT, 200)

    def test_inline_wait_within_bounds(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_INLINE_WAIT_SECONDS": "50"}):
            self.assertEqual(broker.get_inline_wait_seconds(), 50)
            self.assertEqual(broker.INLINE_WAIT, 50)

    def test_inline_wait_clamped_to_min(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_INLINE_WAIT_SECONDS": "5"}):
            self.assertEqual(broker.get_inline_wait_seconds(), 15)
            self.assertEqual(broker.INLINE_WAIT, 15)

    def test_inline_wait_clamped_to_max(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_INLINE_WAIT_SECONDS": "500"}):
            self.assertEqual(broker.get_inline_wait_seconds(), 220)
            self.assertEqual(broker.INLINE_WAIT, 220)

    def test_inline_wait_invalid_string_defaults(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_INLINE_WAIT_SECONDS": "not-an-int"}):
            self.assertEqual(broker.get_inline_wait_seconds(), 200)
            self.assertEqual(broker.INLINE_WAIT, 200)

    def test_inline_wait_respects_smaller_timeout_seconds(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_INLINE_WAIT_SECONDS": "200"}):
            self.assertEqual(broker.get_inline_wait_seconds(30), 30)
            self.assertEqual(broker.get_inline_wait_seconds(240), 200)


class ClaudeNeverKillTests(NeverKillPaidRunsBase):
    def test_claude_consult_finishes_inline_returns_completed(self):
        kill_mock = mock.Mock()

        def fake_start_worker(rid):
            now = broker.utc_now()
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE claude_requests
                    SET status = 'completed', response = 'Fast Claude Answer',
                        responder = 'claude-cli-worker', responder_model = 'claude:claude-3-7-sonnet',
                        completed_at = ?, worker_completed_at = ?
                    WHERE id = ?
                    """,
                    (now, now, rid),
                )
            return {"started": True, "pid": 999}

        with mock.patch.object(broker, "start_claude_request_worker", side_effect=fake_start_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=5.0), \
             mock.patch.object(broker, "kill_process_tree", kill_mock):
            res = broker.consult(
                "claude",
                {
                    "project": "synthetic-proj",
                    "prompt": "Evaluate this architecture",
                    "task_kind": "consult",
                    "target_model": "claude-3-7-sonnet",
                },
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["response"], "Fast Claude Answer")
        self.assertEqual(res["model"], "claude:claude-3-7-sonnet")
        self.assertTrue(res.get("request_id"))
        kill_mock.assert_not_called()

    def test_claude_consult_slow_returns_pending_and_never_kills_process(self):
        kill_mock = mock.Mock()
        captured_rid = []

        def fake_start_worker(rid):
            captured_rid.append(rid)
            now = broker.utc_now()
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE claude_requests
                    SET status = 'running', worker_started_at = ?, worker_pid = 1234
                    WHERE id = ?
                    """,
                    (now, rid),
                )
            return {"started": True, "pid": 1234}

        with mock.patch.object(broker, "start_claude_request_worker", side_effect=fake_start_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=0.05), \
             mock.patch.object(broker, "kill_process_tree", kill_mock):
            res = broker.consult(
                "claude",
                {
                    "project": "synthetic-proj",
                    "prompt": "Long running consult",
                    "task_kind": "implementation_plan",
                    "target_model": "claude-3-7-sonnet",
                },
            )

        self.assertEqual(res["status"], "pending")
        self.assertTrue(res.get("request_id"))
        rid = res["request_id"]
        self.assertEqual(res["id"], rid)
        self.assertEqual(res["poll"], {"tool": "request_result", "request_id": rid, "wait_seconds": 180})
        self.assertIn("never killed by the call window", res["note"])
        kill_mock.assert_not_called()

        # Simulate worker completing later
        now = broker.utc_now()
        with broker.db_connect() as conn:
            conn.execute(
                """
                UPDATE claude_requests
                SET status = 'completed', response = 'Late Claude Answer',
                    responder = 'claude-cli-worker', responder_model = 'claude:claude-3-7-sonnet',
                    completed_at = ?, worker_completed_at = ?
                WHERE id = ?
                """,
                (now, now, rid),
            )

        # request_result retrieves the finished answer
        result_payload = broker.request_result(rid)
        self.assertEqual(result_payload["status"], "ok")
        self.assertEqual(result_payload["response"], "Late Claude Answer")

    def test_claude_quick_check_and_sanity_check_stay_synchronous(self):
        queue_mock = mock.Mock()
        mock_result = broker.ClaudeConsultResult(
            response="Quick Claude Result",
            requested_model="claude-3-7-sonnet",
            actual_model="claude-3-7-sonnet",
            model_attested=True,
            initial_model="claude-3-7-sonnet",
            attempted_models=("claude-3-7-sonnet",),
            fallback_reason=None,
        )
        with mock.patch.object(broker, "queue_claude_request", queue_mock), \
             mock.patch.object(broker, "consult_claude", return_value=mock_result) as sync_mock:
            res1 = broker.consult(
                "claude",
                {
                    "project": "synthetic-proj",
                    "prompt": "Quick check syntax",
                    "task_kind": "quick_check",
                    "target_model": "claude-3-7-sonnet",
                },
            )
            res2 = broker.consult(
                "claude",
                {
                    "project": "synthetic-proj",
                    "prompt": "Sanity check imports",
                    "task_kind": "sanity_check",
                    "target_model": "claude-3-7-sonnet",
                },
            )

        queue_mock.assert_not_called()
        self.assertEqual(sync_mock.call_count, 2)
        self.assertEqual(res1["response"], "Quick Claude Result")
        self.assertEqual(res2["response"], "Quick Claude Result")

    def test_claude_explicit_force_sync_stays_synchronous(self):
        queue_mock = mock.Mock()
        mock_result = broker.ClaudeConsultResult(
            response="Forced Sync Claude Result",
            requested_model="claude-3-7-sonnet",
            actual_model="claude-3-7-sonnet",
            model_attested=True,
            initial_model="claude-3-7-sonnet",
            attempted_models=("claude-3-7-sonnet",),
            fallback_reason=None,
        )
        with mock.patch.object(broker, "queue_claude_request", queue_mock), \
             mock.patch.object(broker, "consult_claude", return_value=mock_result) as sync_mock:
            res = broker.consult(
                "claude",
                {
                    "project": "synthetic-proj",
                    "prompt": "Forced sync consult",
                    "task_kind": "consult",
                    "target_model": "claude-3-7-sonnet",
                    "force_sync": True,
                },
            )
        queue_mock.assert_not_called()
        sync_mock.assert_called_once()
        self.assertEqual(res["response"], "Forced Sync Claude Result")


class ConsultDecisionClaudeLegTests(NeverKillPaidRunsBase):
    def test_consult_decision_claude_leg_at_high_effort_sets_async(self):
        brief = {
            "decision": "Choose the compatibility-safe option.",
            "context": "Context",
            "question": "Which option?",
            "options": [{"id": "A", "summary": "Choice A"}, {"id": "B", "summary": "Choice B"}],
            "proposed_choice": "A",
            "choice_rationale": "Better",
            "tradeoffs": "None",
            "risk_if_wrong": "Low",
        }
        consult_calls = []

        def fake_consult(family, call_args):
            consult_calls.append((family, dict(call_args)))
            if family == "claude":
                return {
                    "status": "pending",
                    "async": True,
                    "request_id": "claude-pending-req",
                    "poll": {"tool": "request_result", "request_id": "claude-pending-req", "wait_seconds": 180},
                }
            return {"status": "ok", "response": "ok"}

        evaluator_review = json.dumps({"verdict": "agree", "critique": "Solid choice"})
        with mock.patch.object(broker, "consult", side_effect=fake_consult), \
             mock.patch.object(broker, "_run_decision_evaluator", return_value=evaluator_review), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 1}):
            result = broker.consult_decision(
                {
                    "project": "synthetic-proj",
                    "work_package_id": "WP-TEST-DECISION",
                    "host": {"vendor": "codex", "model": "gpt-6-astra"},
                    "complexity": "bounded",
                    "brief": brief,
                    "include_fable": True,
                }
            )

        self.assertEqual(result["status"], "pending")
        claude_calls = [c for c in consult_calls if c[0] == "claude"]
        self.assertTrue(claude_calls)
        claude_args = claude_calls[0][1]
        self.assertTrue(claude_args.get("async"))
        self.assertTrue(claude_args.get("new_chat"))
        self.assertEqual(claude_args.get("effort"), "high")


class CodexInlineWaitTests(NeverKillPaidRunsBase):
    def test_codex_inline_wait_returns_pending_and_stores_late_answer(self):
        rid_holder = []

        def fake_start_worker(rid):
            rid_holder.append(rid)
            now = broker.utc_now()
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE codex_requests
                    SET status = 'running', worker_started_at = ?, worker_pid = 4321
                    WHERE id = ?
                    """,
                    (now, rid),
                )
            return {"started": True, "pid": 4321}

        with mock.patch.object(broker, "start_codex_request_worker", side_effect=fake_start_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=0.05):
            res = broker.consult(
                "codex",
                {
                    "project": "synthetic-proj",
                    "prompt": "Analyze code structure",
                    "task_kind": "consult",
                    "effort": "medium",
                    "target_model": "gpt-6.1-sol",
                },
            )

        self.assertEqual(res["status"], "pending")
        rid = res["request_id"]
        self.assertEqual(res["id"], rid)
        self.assertEqual(res["poll"]["wait_seconds"], 180)
        self.assertIn("never killed by the call window", res["note"])

        # Complete worker late
        now = broker.utc_now()
        with broker.db_connect() as conn:
            conn.execute(
                """
                UPDATE codex_requests
                SET status = 'completed', response = 'Late Codex Response',
                    responder_model = 'codex:gpt-6.1-sol [medium]', completed_at = ?, worker_completed_at = ?
                WHERE id = ?
                """,
                (now, now, rid),
            )

        out = broker.request_result(rid)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["response"], "Late Codex Response")


class GeminiNeverKillTests(NeverKillPaidRunsBase):
    def test_gemini_consult_fast_returns_completed_inline(self):
        def fake_start_flash_worker(rid):
            # Simulate worker finishing immediately
            now = broker.utc_now()
            full_envelope = json.dumps(
                {
                    "status": "ok",
                    "response": "Fast Gemini Advice",
                    "model": "gemini:gemini-2.5-pro",
                    "project": "synthetic-proj",
                    "request_id": rid,
                    "task_kind": broker.GEMINI_CONSULT_TASK_KIND,
                }
            )
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE flash_requests
                    SET status = 'completed', response = ?, completed_at = ?, worker_completed_at = ?
                    WHERE id = ?
                    """,
                    (full_envelope, now, now, rid),
                )
            return {"started": True, "pid": 777}

        with mock.patch.object(broker, "start_flash_request_worker", side_effect=fake_start_flash_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=5.0):
            res = broker.consult(
                "gemini",
                {
                    "project": "synthetic-proj",
                    "prompt": "Evaluate this plan",
                    "task_kind": "consult",
                    "target_model": "gemini-2.5-pro",
                },
            )

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["response"], "Fast Gemini Advice")
        self.assertTrue(res.get("request_id"))

    def test_gemini_consult_slow_returns_pending_and_late_result_available(self):
        captured_rid = []

        def fake_start_flash_worker(rid):
            captured_rid.append(rid)
            now = broker.utc_now()
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE flash_requests
                    SET status = 'running', worker_started_at = ?, worker_pid = 8888
                    WHERE id = ?
                    """,
                    (now, rid),
                )
            return {"started": True, "pid": 8888}

        with mock.patch.object(broker, "start_flash_request_worker", side_effect=fake_start_flash_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=0.05):
            res = broker.consult(
                "gemini",
                {
                    "project": "synthetic-proj",
                    "prompt": "Complex Gemini Analysis",
                    "task_kind": "consult",
                    "target_model": "gemini-2.5-pro",
                },
            )

        self.assertEqual(res["status"], "pending")
        rid = res["request_id"]
        self.assertEqual(res["id"], rid)
        self.assertEqual(res["poll"], {"tool": "request_result", "request_id": rid, "wait_seconds": 180})
        self.assertIn("never killed by the call window", res["note"])

        # Check request_result while still pending
        pending_result = broker.request_result(rid)
        self.assertEqual(pending_result["status"], "pending")
        self.assertIn("Gemini consult still running", pending_result["note"])

        # Worker finishes later
        with mock.patch.object(broker, "consult_gemini", return_value="Late Gemini Result"):
            broker.run_gemini_consult_worker(rid)

        terminal_result = broker.request_result(rid)
        self.assertEqual(terminal_result["status"], "ok")
        self.assertEqual(terminal_result["response"], "Late Gemini Result")

    def test_gemini_quick_check_and_search_stay_synchronous(self):
        with mock.patch.object(broker, "queue_gemini_consult") as queue_mock, \
             mock.patch.object(broker, "consult_gemini", return_value="Sync Gemini Result") as sync_mock:
            res1 = broker.consult(
                "gemini",
                {
                    "project": "synthetic-proj",
                    "prompt": "Quick check",
                    "task_kind": "quick_check",
                    "target_model": "gemini-2.5-pro",
                },
            )
            res2 = broker.consult(
                "gemini",
                {
                    "project": "synthetic-proj",
                    "prompt": "Search keyword",
                    "task_kind": "search",
                    "target_model": "gemini-2.5-pro",
                },
            )

        queue_mock.assert_not_called()
        self.assertEqual(sync_mock.call_count, 2)
        self.assertEqual(res1["response"], "Sync Gemini Result")
        self.assertEqual(res2["response"], "Sync Gemini Result")


class AntigravityNeverKillTests(NeverKillPaidRunsBase):
    def test_antigravity_consult_fast_returns_completed_inline(self):
        def fake_start_flash_worker(rid):
            now = broker.utc_now()
            envelope = {
                "status": "ok",
                "response": "Fast Antigravity Advice",
                "model": "antigravity:gemini-3.7-flash-high",
                "request_id": rid,
            }
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE flash_requests
                    SET status = 'completed', response = ?, completed_at = ?, worker_completed_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(envelope), now, now, rid),
                )
            return {"started": True, "pid": 555}

        with mock.patch.object(broker, "start_flash_request_worker", side_effect=fake_start_flash_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=5.0), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", return_value="gemini-3.7-flash-high"):
            res = broker.consult(
                "antigravity",
                {
                    "project": "synthetic-proj",
                    "prompt": "Review architecture draft",
                    "task_kind": "consult",
                },
            )

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["response"], "Fast Antigravity Advice")
        self.assertTrue(res.get("request_id"))

    def test_antigravity_consult_slow_returns_pending_and_late_result_available(self):
        def fake_start_flash_worker(rid):
            now = broker.utc_now()
            with broker.db_connect() as conn:
                conn.execute(
                    """
                    UPDATE flash_requests
                    SET status = 'running', worker_started_at = ?, worker_pid = 666
                    WHERE id = ?
                    """,
                    (now, rid),
                )
            return {"started": True, "pid": 666}

        with mock.patch.object(broker, "start_flash_request_worker", side_effect=fake_start_flash_worker), \
             mock.patch.object(broker, "get_inline_wait_seconds", return_value=0.05), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", return_value="gemini-3.7-flash-high"):
            res = broker.consult(
                "antigravity",
                {
                    "project": "synthetic-proj",
                    "prompt": "Draft unit tests",
                    "task_kind": "consult",
                },
            )

        self.assertEqual(res["status"], "pending")
        rid = res["request_id"]
        self.assertEqual(res["id"], rid)
        self.assertEqual(res["poll"], {"tool": "request_result", "request_id": rid, "wait_seconds": 180})

        # Complete row later
        now = broker.utc_now()
        envelope = {
            "status": "ok",
            "response": "Late Antigravity Result",
            "model": "antigravity:gemini-3.7-flash-high",
            "request_id": rid,
        }
        with broker.db_connect() as conn:
            conn.execute(
                """
                UPDATE flash_requests
                SET status = 'completed', response = ?, completed_at = ?, worker_completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(envelope), now, now, rid),
            )

        out = broker.request_result(rid)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["response"], "Late Antigravity Result")

    def test_antigravity_quick_check_and_search_stay_synchronous(self):
        sync_response = json.dumps({
            "status": "ok",
            "structured_output": {"status": "completed"},
            "response": "Sync Antigravity Result",
        })
        with mock.patch.object(broker, "queue_flash_request") as queue_mock, \
             mock.patch.object(broker, "consult_antigravity_cli", return_value=sync_response) as sync_mock, \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", return_value="gemini-3.7-flash-high"):
            res1 = broker.consult(
                "antigravity",
                {
                    "project": "synthetic-proj",
                    "prompt": "Quick check",
                    "task_kind": "quick_check",
                },
            )
            res2 = broker.consult(
                "antigravity",
                {
                    "project": "synthetic-proj",
                    "prompt": "Search code",
                    "task_kind": "search",
                    "research_questions": ["Where is symbol defined?"],
                },
            )

        queue_mock.assert_not_called()
        self.assertEqual(sync_mock.call_count, 2)
        self.assertIn("Sync Antigravity Result", res1["response"])
        self.assertIn("Sync Antigravity Result", res2["response"])

    def test_antigravity_internal_worker_reentry_runs_cli_directly(self):
        sync_response = json.dumps({
            "status": "ok",
            "structured_output": {"status": "completed"},
            "response": "Direct Worker CLI Result",
        })
        with mock.patch.object(broker, "queue_flash_request") as queue_mock, \
             mock.patch.object(broker, "consult_antigravity_cli", return_value=sync_response) as sync_mock, \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", return_value="gemini-3.7-flash-high"):
            res = broker.consult(
                "antigravity",
                {
                    "project": "synthetic-proj",
                    "prompt": "Worker prompt",
                    "task_kind": "consult",
                    "_flash_async_token": broker._FLASH_ASYNC_INTERNAL_TOKEN,
                },
            )

        queue_mock.assert_not_called()
        sync_mock.assert_called_once()
        self.assertEqual(res["response"], sync_response)


if __name__ == "__main__":
    unittest.main()
