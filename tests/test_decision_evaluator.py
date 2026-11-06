"""Decision pipeline: brain proposal -> Gemini evaluator -> final Astra verdict.

Fable is opt-in. The evaluator function and consult() are always mocked; no real
agy/codex/claude CLI is ever started here.
"""

from __future__ import annotations

import hashlib
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

_REAL_START = broker.start_flash_request_worker  # captured before tests/conftest.py stubs it
EVALUATOR = "gemini-4-argon-high"
REVIEW = {
    "verdict": "modify",
    "issues": ["Rollback path is unstated."],
    "missing_evidence": [],
    "suggested_change": "Add a rollback note.",
}


def _brief() -> dict:
    return {
        "decision": "Choose a routing design.",
        "constraints": ["Preserve compatibility."],
        "options": [{"id": "A", "summary": "Add one orchestrator."}],
        "proposed_choice": "A",
        "questions": ["Is A the safest option?"],
        "evidence": [{"ref": "router.py:10", "claim": "The old route is fixed."}],
    }


def _args(vendor: str, model: str, complexity: str = "bounded", **extra) -> dict:
    return {
        "project": "p",
        "topic": "evaluator-tests",
        "session_id": "evaluator-session-1",
        "work_package_id": "WP-EVAL-TEST",
        "host": {"vendor": vendor, "model": model},
        "complexity": complexity,
        "brief": _brief(),
        **extra,
    }


def _native(family: str, status: str = "completed") -> dict:
    return {
        "family": family,
        "model": "gpt-6-astra" if family == "codex" else "claude-fable-5",
        "status": status,
        "attestation": "verified",
        "agent_id": "native-1",
        "summary": "Use option A." if status == "completed" else "Native provider unavailable.",
    }


def _ok_consult(family, args):
    return {
        "status": "ok",
        "response": '{"recommendation":"A"}',
        "effort": args["effort"],
        "actual_effort": args["effort"],
        "actual_model": "gpt-6-astra" if family == "codex" else "claude-fable-5",
        "model_attested": True,
    }


def _ok_evaluator(model, brief, *, package_id, complexity, **_kw):
    return {"status": "completed", "review": dict(REVIEW)}


class EvaluatorTestCase(unittest.TestCase):
    def setUp(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        self._home_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._home_dir.cleanup)
        home = Path(self._home_dir.name)
        home_patch = mock.patch.object(Path, "home", return_value=home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        env = {"HOME": str(home), "USERPROFILE": str(home), "AGENT_BROKER_CHILD": ""}
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in (
            "AGENT_BROKER_DECISION_EVALUATOR_MODEL",
            "AGENT_BROKER_FABLE_MODE",
        ):
            os.environ.pop(name, None)

    def tearDown(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        broker._DECISION_EVALUATOR_REVIEWS.clear()

    def _run(self, args, consult_effect=None, evaluator=None):
        """Call consult_decision the way a host does. The detached worker is simulated by a
        start stub that runs the worker body in-process (agy runner mocked). A 'pending'
        first answer is followed by the documented second call, whose result is returned."""
        evaluator_mock = mock.Mock(side_effect=evaluator or _ok_evaluator)

        def start(rid):
            with mock.patch.object(broker, "_run_decision_evaluator", evaluator_mock):
                broker.run_decision_evaluator_worker(rid)
            return {"started": True, "pid": 1}

        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "_run_decision_evaluator", evaluator_mock), \
             mock.patch.object(broker, "start_flash_request_worker", side_effect=start), \
             mock.patch.object(broker, "consult", side_effect=consult_effect or _ok_consult) as consult, \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 1}):
            result = broker.consult_decision(args)
            if result.get("status") == "pending":
                result = broker.consult_decision(args)
        return result, consult, evaluator_mock


class ClaudeHostPipelineTests(EvaluatorTestCase):
    def test_bounded_runs_evaluator_then_astra_at_high_with_review_in_prompt(self):
        result, consult, evaluator = self._run(_args("claude", "opus", "bounded"))
        evaluator.assert_called_once()
        self.assertEqual(evaluator.call_args.args[0], EVALUATOR)
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        call_args = consult.call_args_list[0].args[1]
        self.assertEqual(call_args["effort"], "high")
        self.assertEqual(call_args["target_model"], "gpt-6-astra")
        self.assertIn("Evaluator review (Gemini 4):", call_args["prompt"])
        self.assertIn('"verdict":"modify"', call_args["prompt"])
        self.assertIn("Give the final verdict.", call_args["prompt"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            [item["lane"] for item in result["consultations"]],
            ["switchboard_evaluator", "switchboard_cross_vendor"],
        )
        self.assertEqual(result["consultations"][0]["resolved_model"], EVALUATOR)
        self.assertEqual(result["consultations"][0]["target"], "antigravity")

    def test_architecture_and_risk_flag_efforts(self):
        _, consult, _ = self._run(_args("claude", "opus", "architecture"))
        self.assertEqual(consult.call_args_list[0].args[1]["effort"], "xhigh")
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        result, consult, _ = self._run(_args("claude", "opus", "bounded", risk_flags=["security"]))
        self.assertEqual(consult.call_args_list[0].args[1]["effort"], "max")
        self.assertTrue(result["complexity_escalated"])

    def test_include_fable_requests_native_fable_first(self):
        result, consult, evaluator = self._run(_args("claude", "opus", include_fable=True))
        self.assertEqual(result["status"], "needs_native_consultation")
        self.assertEqual(result["native_request"]["family"], "claude")
        consult.assert_not_called()
        evaluator.assert_not_called()

    def test_fable_mode_default_requests_native_fable_first(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_FABLE_MODE": "default"}):
            result, consult, evaluator = self._run(_args("claude", "opus"))
        self.assertEqual(result["status"], "needs_native_consultation")
        self.assertEqual(result["native_request"]["family"], "claude")
        consult.assert_not_called()
        evaluator.assert_not_called()

    def test_fable_native_done_then_evaluator_then_astra(self):
        result, consult, evaluator = self._run(
            _args("claude", "opus", include_fable=True, native_consultation=_native("claude"))
        )
        evaluator.assert_called_once()
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertEqual(
            [item["lane"] for item in result["consultations"]],
            ["native_same_vendor", "switchboard_evaluator", "switchboard_cross_vendor"],
        )

    def test_evaluator_failure_adds_notice_and_astra_still_called(self):
        def failing(model, brief, *, package_id, complexity, **_kw):
            return {"status": "error", "response": "agy exited with code 1: boom"}

        result, consult, _ = self._run(_args("claude", "opus"), evaluator=failing)
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertNotIn("Evaluator review", consult.call_args_list[0].args[1]["prompt"])
        self.assertEqual(result["consultations"][0]["status"], "failed")
        self.assertTrue(any("Decision evaluator" in n for n in result["handoff_notices"]))
        self.assertEqual(result["consultations"][1]["status"], "completed")

    def test_evaluator_plan_failure_latches_and_second_decision_skips_it(self):
        def credits(model, brief, *, package_id, complexity, **_kw):
            return {"status": "error", "response": "requires usage credits (HTTP 429)"}

        result, _, evaluator = self._run(_args("claude", "opus"), evaluator=credits)
        self.assertEqual(result["consultations"][0]["status"], "skipped_unavailable")
        self.assertEqual(evaluator.call_count, 1)
        second, _, evaluator2 = self._run(_args("claude", "opus", work_package_id="WP-EVAL-TEST-2"), evaluator=credits)
        evaluator2.assert_not_called()
        self.assertEqual(second["consultations"][0]["status"], "skipped_unavailable")
        third, _, evaluator3 = self._run(
            _args("claude", "opus", work_package_id="WP-EVAL-TEST-3", retry_unavailable=True)
        )
        evaluator3.assert_called_once()
        self.assertEqual(third["consultations"][0]["status"], "completed")

    def test_astra_unavailable_without_fable_is_partial_with_review_and_notice(self):
        def unavailable(family, _args):
            return {"status": "error", "response": "Authentication failed for provider"}

        result, consult, _ = self._run(_args("claude", "opus"), consult_effect=unavailable)
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["consultations"][0]["lane"], "switchboard_evaluator")
        self.assertIn('"verdict":"modify"', result["consultations"][0]["response"])
        self.assertIn(broker.DECISION_FINAL_UNAVAILABLE_NOTICE, result["handoff_notices"])

    def test_evaluator_off_is_skipped(self):
        for value in ("off", ""):
            with self.subTest(value=value):
                broker._DECISION_EVALUATOR_REVIEWS.clear()
                with mock.patch.dict(os.environ, {"AGENT_BROKER_DECISION_EVALUATOR_MODEL": value}):
                    result, consult, evaluator = self._run(_args("claude", "opus"))
                evaluator.assert_not_called()
                self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
                self.assertNotIn("Evaluator review", consult.call_args_list[0].args[1]["prompt"])
                self.assertEqual(
                    [item["lane"] for item in result["consultations"]], ["switchboard_cross_vendor"]
                )

    def test_evaluator_model_env_override(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_DECISION_EVALUATOR_MODEL": "gemini-9-test"}):
            _, _, evaluator = self._run(_args("claude", "opus"))
        self.assertEqual(evaluator.call_args.args[0], "gemini-9-test")


class CodexHostPipelineTests(EvaluatorTestCase):
    def test_sol_host_runs_evaluator_then_requests_native_astra_with_review(self):
        result, consult, evaluator = self._run(_args("codex", "gpt-5.6-sol"))
        evaluator.assert_called_once()
        consult.assert_not_called()
        self.assertEqual(result["status"], "needs_native_consultation")
        native = result["native_request"]
        self.assertEqual(native["model"], "gpt-6-astra")
        self.assertIn("Evaluator review (Gemini 4):", native["adviser_instructions"])
        self.assertIn("You give the final verdict.", native["adviser_instructions"])
        self.assertTrue(native["adviser_instructions"].startswith(broker._DECISION_ADVISER_INSTRUCTIONS))
        self.assertEqual(result["consultations"][0]["lane"], "switchboard_evaluator")

    def test_second_call_with_completed_native_completes_without_rerunning_evaluator(self):
        self._run(_args("codex", "gpt-5.6-sol"))
        result, consult, evaluator = self._run(
            _args("codex", "gpt-5.6-sol", native_consultation=_native("codex"))
        )
        evaluator.assert_not_called()
        consult.assert_not_called()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            [item["lane"] for item in result["consultations"]],
            ["switchboard_evaluator", "native_same_vendor"],
        )

    def test_native_astra_unavailable_without_fable_is_partial_with_notice(self):
        self._run(_args("codex", "gpt-5.6-sol"))
        result, consult, evaluator = self._run(
            _args("codex", "gpt-5.6-sol", native_consultation=_native("codex", "unavailable"))
        )
        evaluator.assert_not_called()
        consult.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertIn(broker.DECISION_FINAL_UNAVAILABLE_NOTICE, result["handoff_notices"])

    def test_native_astra_unavailable_with_fable_goes_cross_vendor(self):
        self._run(_args("codex", "gpt-5.6-sol", include_fable=True))
        result, consult, _ = self._run(
            _args("codex", "gpt-5.6-sol", include_fable=True, native_consultation=_native("codex", "unavailable"))
        )
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["claude"])
        self.assertNotIn(broker.DECISION_FINAL_UNAVAILABLE_NOTICE, result["handoff_notices"])

    def test_astra_host_gets_evaluator_only(self):
        result, consult, evaluator = self._run(_args("codex", "gpt-6-astra"))
        evaluator.assert_called_once()
        consult.assert_not_called()
        self.assertEqual(result["status"], "complete")
        self.assertEqual([i["lane"] for i in result["consultations"]], ["switchboard_evaluator"])
        self.assertEqual(result["handoff_notices"], [])

    def test_astra_host_with_fable_adds_cross_vendor_fable(self):
        _, consult, evaluator = self._run(_args("codex", "gpt-6-astra", include_fable=True))
        evaluator.assert_called_once()
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["claude"])


class GeminiHostPipelineTests(EvaluatorTestCase):
    def test_gemini_host_runs_evaluator_then_astra_only(self):
        result, consult, evaluator = self._run(_args("gemini", "gemini-3.8-flash-high"))
        evaluator.assert_called_once()
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertIn("Evaluator review (Gemini 4):", consult.call_args_list[0].args[1]["prompt"])
        self.assertEqual(result["status"], "complete")

    def test_gemini_host_with_fable_adds_claude(self):
        _, consult, _ = self._run(_args("gemini", "gemini-3.8-flash-high", include_fable=True))
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex", "claude"])

    def test_gemini_host_that_is_the_evaluator_model_skips_evaluator(self):
        result, consult, evaluator = self._run(_args("gemini", EVALUATOR))
        evaluator.assert_not_called()
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertNotIn("Evaluator review", consult.call_args_list[0].args[1]["prompt"])
        self.assertEqual([i["lane"] for i in result["consultations"]], ["switchboard_cross_vendor"])


class EvaluatorProcessTests(unittest.TestCase):
    def test_cli_runs_in_fresh_temp_dir_removed_afterwards_with_schema_args(self):
        seen: dict = {}

        def fake_run(command, cwd, stdin_text=None, timeout=0, **_kw):
            seen["command"] = command
            seen["cwd"] = cwd
            seen["existed"] = Path(cwd).is_dir()
            seen["empty"] = not any(Path(cwd).iterdir())
            seen["timeout"] = timeout
            payload = {"status": "SUCCESS", "model": EVALUATOR, "structured_output": dict(REVIEW)}
            return 0, json.dumps(payload), ""

        brief = broker._normalized_decision_brief(_brief())
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy-test-binary"), \
             mock.patch.object(broker, "run_process", side_effect=fake_run):
            result = broker._run_decision_evaluator(
                EVALUATOR, brief, package_id="WP-EVAL-PROC", complexity="bounded"
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review"]["verdict"], "modify")
        self.assertTrue(result["model_attested"])
        cwd = Path(seen["cwd"])
        self.assertTrue(seen["existed"] and seen["empty"])
        self.assertFalse(cwd.exists())
        self.assertNotEqual(cwd.resolve(), REPO_ROOT.resolve())
        self.assertNotEqual(cwd.resolve(), Path.cwd().resolve())
        command = seen["command"]
        self.assertEqual(command[0], "agy-test-binary")
        self.assertEqual(command[command.index("--model") + 1], EVALUATOR)
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertEqual(command[command.index("--print-timeout") + 1], "1800s")
        schema = json.loads(command[command.index("--json-schema") + 1])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            sorted(schema["required"]),
            ["issues", "missing_evidence", "suggested_change", "verdict"],
        )
        prompt = command[command.index("--print") + 1]
        self.assertTrue(prompt.startswith("You are the decision evaluator."))
        self.assertIn("Proposed choice: A", prompt)

    def test_invalid_output_and_nonzero_exit_are_errors(self):
        brief = broker._normalized_decision_brief(_brief())
        cases = [
            (0, "not json", ""),
            (0, json.dumps({"status": "SUCCESS", "structured_output": {"verdict": "maybe"}}), ""),
            (3, "", "boom"),
            (124, "", ""),
        ]
        for outcome in cases:
            with self.subTest(code=outcome[0]):
                with mock.patch.object(broker, "load_config", return_value={}), \
                     mock.patch.object(broker, "discover_antigravity_cli", return_value="agy-test-binary"), \
                     mock.patch.object(broker, "run_process", return_value=outcome):
                    result = broker._run_decision_evaluator(
                        EVALUATOR, brief, package_id="WP-EVAL-PROC", complexity="bounded"
                    )
                self.assertEqual(result["status"], "error")

    def test_missing_cli_is_reported_unavailable(self):
        brief = broker._normalized_decision_brief(_brief())
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value=None):
            result = broker._run_decision_evaluator(
                EVALUATOR, brief, package_id="WP-EVAL-PROC", complexity="bounded"
            )
        self.assertEqual(broker._decision_failure_kind(result)[0], "skipped_unavailable")

    def test_review_is_capped(self):
        review = {
            "verdict": "reject",
            "issues": ["x" * 200] * 3,
            "missing_evidence": ["y" * 200] * 2,
            "suggested_change": "z" * 300,
        }
        self.assertLessEqual(
            len(broker._compact_evaluator_review(review)), broker.DECISION_EVALUATOR_REVIEW_MAX_CHARS
        )


class BackgroundEvaluatorTests(EvaluatorTestCase):
    """consult_decision never runs agy inline: it starts (or follows) one detached worker,
    returns 'pending', and a later call picks up the persisted review or failure marker."""

    SESSION = "evaluator-session-1"
    WP = "WP-EVAL-TEST"

    def setUp(self):
        super().setUp()
        os.environ.pop("AGENT_BROKER_DECISION_EVALUATOR_TIMEOUT", None)
        os.environ.pop("AGENT_BROKER_DECISION_EVALUATOR_REUSE_HOURS", None)
        os.environ["AGENT_BROKER_DECISION_EVALUATOR_MODEL"] = EVALUATOR
        os.environ.pop("AGENT_BROKER_HOME", None)
        self.start = mock.Mock(return_value={"started": True, "pid": 4242})
        self.evaluator = mock.Mock(side_effect=_ok_evaluator)

    def _call(self, args, consult_effect=None):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "_run_decision_evaluator", self.evaluator), \
             mock.patch.object(broker, "start_flash_request_worker", self.start), \
             mock.patch.object(broker, "consult", side_effect=consult_effect or _ok_consult) as consult, \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 1}):
            result = broker.consult_decision(args)
        return result, consult

    def _run_worker(self, request_id):
        with mock.patch.object(broker, "_run_decision_evaluator", self.evaluator):
            return broker.run_decision_evaluator_worker(request_id)

    def _first_pending(self, args):
        result, consult = self._call(args)
        self.assertEqual(result["status"], "pending")
        consult.assert_not_called()
        return result, result["consultations"][-1]["request_id"]

    def test_first_call_starts_one_worker_and_returns_pending_with_continue_instruction(self):
        result, consult = self._call(_args("claude", "opus"))
        self.start.assert_called_once()
        self.evaluator.assert_not_called()
        consult.assert_not_called()
        self.assertEqual(result["status"], "pending")
        entry = result["consultations"][0]
        self.assertEqual(entry["target"], "antigravity")
        self.assertEqual(entry["lane"], "switchboard_evaluator")
        self.assertEqual(entry["status"], "pending")
        rid = entry["request_id"]
        self.assertEqual(entry["poll"], {"tool": "request_result", "request_id": rid, "wait_seconds": 180})
        self.assertIn("request_result", result["completion_notice"])
        self.assertIn("consult_decision again", result["completion_notice"])
        self.assertIn("same work_package_id", result["completion_notice"])
        # The worker is a normal, pollable request row.
        polled = broker.request_result(rid)
        self.assertTrue(polled["found"])
        self.assertEqual(polled["state"], "queued")

    def test_second_call_while_running_returns_same_request_id_without_new_worker(self):
        first, rid = self._first_pending(_args("claude", "opus"))
        second, consult = self._call(_args("claude", "opus"))
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["consultations"][0]["request_id"], rid)
        self.assertEqual(self.start.call_count, 1)
        consult.assert_not_called()

    def test_claude_host_continues_to_astra_with_review_after_worker_finishes(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self._run_worker(rid)
        done = broker.request_result(rid)
        self.assertEqual(done["state"], "completed")
        result, consult = self._call(_args("claude", "opus"))
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.evaluator.call_count, 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertIn('"verdict":"modify"', consult.call_args_list[0].args[1]["prompt"])
        self.assertEqual(
            [i["lane"] for i in result["consultations"]], ["switchboard_evaluator", "switchboard_cross_vendor"]
        )

    def test_codex_host_gets_needs_native_consultation_with_review_after_worker_finishes(self):
        _, rid = self._first_pending(_args("codex", "gpt-5.6-sol"))
        self._run_worker(rid)
        result, consult = self._call(_args("codex", "gpt-5.6-sol"))
        consult.assert_not_called()
        self.assertEqual(result["status"], "needs_native_consultation")
        self.assertIn('"verdict":"modify"', result["native_request"]["adviser_instructions"])
        self.assertEqual(self.start.call_count, 1)

    def test_gemini_host_continues_to_astra_after_worker_finishes(self):
        _, rid = self._first_pending(_args("gemini", "gemini-3.8-flash-high"))
        self._run_worker(rid)
        result, consult = self._call(_args("gemini", "gemini-3.8-flash-high"))
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertEqual(result["status"], "complete")

    def test_persisted_review_survives_restart_and_agy_is_not_rerun(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self._run_worker(rid)
        path = broker._decision_evaluator_review_path(self.SESSION, self.WP)
        self.assertTrue(path.is_file())
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["model"], EVALUATOR)
        self.assertEqual(stored["review"]["verdict"], "modify")
        self.assertIn("created_at", stored)
        self.assertEqual(path.parent.name, hashlib.sha256(self.SESSION.encode()).hexdigest()[:16])
        broker._DECISION_EVALUATOR_REVIEWS.clear()  # simulated MCP restart
        result, consult = self._call(_args("claude", "opus"))
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.evaluator.call_count, 1)
        self.assertEqual(result["status"], "complete")
        self.assertIn('"verdict":"modify"', consult.call_args_list[0].args[1]["prompt"])

    def test_agent_broker_home_selects_the_store(self):
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.dict(os.environ, {"AGENT_BROKER_HOME": other}):
                path = broker._decision_evaluator_review_path(self.SESSION, self.WP)
            self.assertTrue(str(path).startswith(str(Path(other))))
            self.assertEqual(path.parent.parent.name, "decision-evaluator")

    def test_review_older_than_reuse_window_is_ignored(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self._run_worker(rid)
        path = broker._decision_evaluator_review_path(self.SESSION, self.WP)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["created_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(stored), encoding="utf-8")
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        result, _ = self._call(_args("claude", "opus"))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.start.call_count, 2)

    def test_reuse_hours_env_extends_window(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self._run_worker(rid)
        path = broker._decision_evaluator_review_path(self.SESSION, self.WP)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 3600))
        path.write_text(json.dumps(stored), encoding="utf-8")
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        with mock.patch.dict(os.environ, {"AGENT_BROKER_DECISION_EVALUATOR_REUSE_HOURS": "48"}):
            result, _ = self._call(_args("claude", "opus"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.start.call_count, 1)

    def test_terminal_failure_marker_continues_to_astra_and_does_not_restart(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self.evaluator.side_effect = lambda *a, **k: {
            "status": "error", "response": "Decision evaluator output failed schema validation."
        }
        self._run_worker(rid)
        marker = broker._decision_evaluator_failure_path(self.SESSION, self.WP)
        self.assertTrue(marker.is_file())
        self.assertEqual(broker.request_result(rid)["state"], "failed")
        for _ in range(2):  # the marker keeps holding on repeated calls
            result, consult = self._call(_args("claude", "opus"))
            self.assertEqual(self.start.call_count, 1)
            self.assertEqual(result["consultations"][0]["status"], "failed")
            self.assertTrue(any("Decision evaluator" in n for n in result["handoff_notices"]))
            self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
            self.assertNotIn("Evaluator review", consult.call_args_list[0].args[1]["prompt"])

    def test_cli_status_failure_and_exception_write_markers(self):
        for index, effect in enumerate(
            (
                lambda *a, **k: {"status": "error", "response": "Decision evaluator CLI status ERROR: bad"},
                RuntimeError("worker blew up"),
            )
        ):
            with self.subTest(index=index):
                wp = f"WP-FAIL-{index}"
                _, rid = self._first_pending(_args("claude", "opus", work_package_id=wp))
                self.evaluator.side_effect = effect
                self._run_worker(rid)
                self.assertTrue(broker._decision_evaluator_failure_path(self.SESSION, wp).is_file())

    def test_worker_start_failure_is_a_terminal_failure_marker(self):
        self.start.return_value = {"started": False, "reason": "spawn denied"}
        result, consult = self._call(_args("claude", "opus"))
        self.assertEqual(result["consultations"][0]["status"], "failed")
        self.assertTrue(broker._decision_evaluator_failure_path(self.SESSION, self.WP).is_file())
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])

    def test_dead_worker_is_reconciled_into_a_failure_marker(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        with broker.db_connect() as conn:
            conn.execute(
                "UPDATE flash_requests SET status='running', worker_pid=999999, worker_started_at=? WHERE id=?",
                (started, rid),
            )
        with mock.patch.object(broker, "_pid_alive", return_value=False):
            result, consult = self._call(_args("claude", "opus"))
        self.assertEqual(result["consultations"][0]["status"], "failed")
        self.assertTrue(broker._decision_evaluator_failure_path(self.SESSION, self.WP).is_file())
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertEqual(self.start.call_count, 1)

    def test_retry_unavailable_clears_marker_and_restarts_once(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self.evaluator.side_effect = lambda *a, **k: {"status": "error", "response": "boom"}
        self._run_worker(rid)
        self.assertEqual(self._call(_args("claude", "opus"))[0]["consultations"][0]["status"], "failed")
        self.assertEqual(self.start.call_count, 1)
        result, consult = self._call(_args("claude", "opus", retry_unavailable=True))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.start.call_count, 2)
        self.assertFalse(broker._decision_evaluator_failure_path(self.SESSION, self.WP).exists())
        consult.assert_not_called()
        again, _ = self._call(_args("claude", "opus", retry_unavailable=True))
        self.assertEqual(again["consultations"][0]["request_id"], result["consultations"][0]["request_id"])
        self.assertEqual(self.start.call_count, 2)

    def test_worker_uses_1800_default_and_clamped_env_timeout(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        self.assertEqual(broker.DECISION_EVALUATOR_DEFAULT_TIMEOUT_SECONDS, 1800)
        self._run_worker(rid)
        self.assertEqual(self.evaluator.call_args.kwargs["timeout"], 1800)
        for raw, expected in (("50", 240), ("99999", 10800), ("3000", 3000), ("junk", 1800)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"AGENT_BROKER_DECISION_EVALUATOR_TIMEOUT": raw}):
                self.assertEqual(broker._decision_evaluator_timeout(), expected)
        with mock.patch.dict(os.environ, {"AGENT_BROKER_DECISION_EVALUATOR_TIMEOUT": "5000"}):
            _, rid2 = self._first_pending(_args("claude", "opus", work_package_id="WP-TIMEOUT"))
            self._run_worker(rid2)
        self.assertEqual(self.evaluator.call_args.kwargs["timeout"], 5000)

    def test_real_starter_launches_the_dedicated_bridge_verb(self):
        _, rid = self._first_pending(_args("claude", "opus"))
        commands = []

        class FakeProc:
            pid = 31337

        def fake_popen(cmd, **_kw):
            commands.append(cmd)
            return FakeProc()

        with mock.patch.object(broker.subprocess, "Popen", side_effect=fake_popen):
            started = _REAL_START(rid)
        self.assertTrue(started["started"])
        self.assertIn("run-decision-evaluator", commands[0])
        self.assertNotIn("run-flash-request", commands[0])

    def test_process_runner_uses_default_cap_and_temp_cwd(self):
        seen = {}

        def fake_run(command, cwd, stdin_text=None, timeout=0, **_kw):
            seen.update(command=command, cwd=cwd, timeout=timeout)
            return 0, json.dumps({"status": "SUCCESS", "structured_output": dict(REVIEW)}), ""

        brief = broker._normalized_decision_brief(_brief())
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy-test-binary"), \
             mock.patch.object(broker, "run_process", side_effect=fake_run):
            broker._run_decision_evaluator(EVALUATOR, brief, package_id="WP-X", complexity="bounded")
            self.assertEqual(seen["command"][seen["command"].index("--print-timeout") + 1], "1800s")
            broker._run_decision_evaluator(EVALUATOR, brief, package_id="WP-X", complexity="bounded", timeout=600)
            self.assertEqual(seen["command"][seen["command"].index("--print-timeout") + 1], "600s")
        self.assertNotEqual(Path(seen["cwd"]).resolve(), REPO_ROOT.resolve())
        self.assertNotEqual(Path(seen["cwd"]).resolve(), Path.cwd().resolve())


if __name__ == "__main__":
    unittest.main()
