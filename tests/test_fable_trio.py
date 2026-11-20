"""Fable trio decision tests: brain proposal -> Gemini evaluator -> Astra & Fable final verdicts.

Verifies:
(a) env unset -> Fable enabled; on_request -> disabled; include_fable=False disables even with mode default;
(b) a Claude host, complexity architecture, mode default: once a stored evaluator review exists,
    the needs_native_consultation request names model fable, effort "high", and its
    adviser_instructions contain the 250-word limit and the review text;
(c) complexity critical: the Fable effort is "xhigh" while DECISION_COMPLEXITY_EFFORT["critical"] stays "max";
(d) a Codex host with a completed native Astra consultation: the cross-vendor claude call gets
    effort "high" (architecture) and its prompt contains the review and the 250-word limit.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402

EVALUATOR = "gemini-4-neon-high"
EVALUATOR_HEAVY = "gemini-4-neon-xhigh"
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
        "topic": "fable-trio-tests",
        "session_id": "fable-session-1",
        "work_package_id": "WP-FABLE-TEST",
        "host": {"vendor": vendor, "model": model},
        "complexity": complexity,
        "brief": _brief(),
        **extra,
    }


def _native(family: str, status: str = "completed") -> dict:
    return {
        "family": family,
        "model": "gpt-6-astra" if family == "codex" else "fable",
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


class FableTrioTestCase(unittest.TestCase):
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

    def test_case_a_fable_mode_env_and_include_fable_toggle(self):
        # 1. Env unset -> mode is default, Fable enabled by default
        self.assertNotIn("AGENT_BROKER_FABLE_MODE", os.environ)
        self.assertEqual(broker._decision_fable_mode(), "default")
        result, consult, _ = self._run(_args("claude", "opus", "bounded"))
        self.assertEqual(result["status"], "needs_native_consultation")
        self.assertEqual(result["native_request"]["family"], "claude")
        self.assertEqual(result["native_request"]["model"], "fable")
        consult.assert_not_called()

        # 2. Mode on_request (and aliases request, off, none) -> disabled
        for mode_val in ("on_request", "on request", "request", "off", "none"):
            broker._DECISION_EVALUATOR_REVIEWS.clear()
            with mock.patch.dict(os.environ, {"AGENT_BROKER_FABLE_MODE": mode_val}):
                self.assertEqual(broker._decision_fable_mode(), "on_request")
                res, cons, _ = self._run(_args("claude", "opus", "bounded"))
                # With Fable disabled, Claude host doesn't request native Fable; goes to Astra
                self.assertEqual(res["status"], "complete")
                self.assertEqual([c.args[0] for c in cons.call_args_list], ["codex"])

        # 3. include_fable=False disables Fable even when mode is default
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        res, cons, _ = self._run(_args("claude", "opus", "bounded", include_fable=False))
        self.assertEqual(res["status"], "complete")
        self.assertEqual([c.args[0] for c in cons.call_args_list], ["codex"])

        # Also check with explicit AGENT_BROKER_FABLE_MODE=default
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        with mock.patch.dict(os.environ, {"AGENT_BROKER_FABLE_MODE": "default"}):
            res, cons, _ = self._run(_args("claude", "opus", "bounded", include_fable=False))
            self.assertEqual(res["status"], "complete")
            self.assertEqual([c.args[0] for c in cons.call_args_list], ["codex"])

    def test_case_b_claude_host_architecture_stored_review_and_250_word_limit(self):
        result, consult, evaluator = self._run(_args("claude", "opus", "architecture"))
        evaluator.assert_called_once()
        self.assertEqual(evaluator.call_args.args[0], EVALUATOR_HEAVY)
        self.assertEqual(result["status"], "needs_native_consultation")
        req = result["native_request"]
        self.assertEqual(req["family"], "claude")
        self.assertEqual(req["model"], "fable")
        self.assertEqual(req["effort"], "high")
        self.assertIn(broker.FABLE_ANSWER_LIMIT, req["adviser_instructions"])
        self.assertIn("under 250 words", req["adviser_instructions"])
        self.assertIn("Evaluator review (Gemini 4):", req["adviser_instructions"])
        self.assertIn('"verdict":"modify"', req["adviser_instructions"])
        consult.assert_not_called()

    def test_case_c_critical_fable_effort_xhigh_while_astra_critical_stays_max(self):
        self.assertEqual(broker.DECISION_COMPLEXITY_EFFORT["critical"], "max")
        self.assertEqual(broker.FABLE_COMPLEXITY_EFFORT["critical"], "xhigh")

        # Claude native request at critical complexity gets xhigh
        res_claude, _, evaluator = self._run(_args("claude", "opus", "critical"))
        self.assertEqual(evaluator.call_args.args[0], EVALUATOR_HEAVY)
        self.assertEqual(res_claude["status"], "needs_native_consultation")
        self.assertEqual(res_claude["native_request"]["effort"], "xhigh")

        # Codex host: first turn runs evaluator and returns native request for Astra at max
        broker._DECISION_EVALUATOR_REVIEWS.clear()
        first_res, _, _ = self._run(_args("codex", "gpt-5.6-sol", "critical"))
        self.assertEqual(first_res["consultations"][0]["resolved_model"], EVALUATOR_HEAVY)
        self.assertEqual(first_res["status"], "needs_native_consultation")
        self.assertEqual(first_res["native_request"]["effort"], "max")

        # Second turn with native Astra done: cross-vendor claude target gets xhigh
        res_codex, cons_codex, _ = self._run(
            _args("codex", "gpt-5.6-sol", "critical", native_consultation=_native("codex"))
        )
        self.assertEqual(res_codex["status"], "complete")
        claude_calls = [c for c in cons_codex.call_args_list if c.args[0] == "claude"]
        self.assertEqual(len(claude_calls), 1)
        self.assertEqual(claude_calls[0].args[1]["effort"], "xhigh")
        self.assertEqual(broker.DECISION_COMPLEXITY_EFFORT["critical"], "max")

    def test_case_d_codex_host_architecture_cross_vendor_claude_call(self):
        # First turn runs evaluator and returns needs_native_consultation for Astra
        first_result, _, evaluator = self._run(_args("codex", "gpt-5.6-sol", "architecture"))
        self.assertEqual(first_result["status"], "needs_native_consultation")
        evaluator.assert_called_once()
        self.assertEqual(evaluator.call_args.args[0], EVALUATOR_HEAVY)

        # Second turn with completed native Astra consultation:
        result, consult, _ = self._run(
            _args("codex", "gpt-5.6-sol", "architecture", native_consultation=_native("codex"))
        )
        self.assertEqual(result["status"], "complete")
        claude_calls = [c for c in consult.call_args_list if c.args[0] == "claude"]
        self.assertEqual(len(claude_calls), 1)
        call_args = claude_calls[0].args[1]
        self.assertEqual(call_args["target_model"], "fable")
        self.assertEqual(call_args["effort"], "high")
        self.assertIn(broker.FABLE_ANSWER_LIMIT, call_args["prompt"])
        self.assertIn("under 250 words", call_args["prompt"])
        self.assertIn("Evaluator review (Gemini 4):", call_args["prompt"])
        self.assertIn('"verdict":"modify"', call_args["prompt"])

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _pin_live_neon_catalog(monkeypatch):
    """Keep these tests independent of the live agy catalog: with no live Neon found,
    the evaluator falls back to the default constants these tests assert."""
    monkeypatch.setattr(broker, "_newest_live_neon", lambda level: None)
