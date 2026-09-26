"""WP-SB8D item 3: since 1.0.50, a Switchboard-launched Codex/Claude child
loads the owner's config and therefore the routing hooks. A live Astra audit
child hit two brain-only mechanisms meant for a completely different role:

  - the 4-call direct-labour gate blocked its 5th read (PreToolUse);
  - the Stop hook demanded a flagship-consultation receipt/routing audit --
    a recursive requirement, since satisfying it would mean the child itself
    spawning/consulting another agent, which PreToolUse's own child guard
    just denied.

Fix: when AGENT_BROKER_CHILD=1, every hook entry point applies ONLY the
narrow behaviour appropriate to a depth-1 adviser/worker:
  - PreToolUse: the existing spawn/consult guard only -- no labour counting,
    no flagship prompt cap, no Claude-circuit-breaker deny.
  - PostToolUse: the ingress quarantine only -- no labour checkpoint notice,
    no credit/decision-receipt bookkeeping.
  - Stop / SubagentStop: never block, never record a consultation receipt.
  - SubagentStart: bookkeeping skipped (a guarded child cannot legitimately
    reach this event).
  - UserPromptSubmit: no brain routing-policy injection; at most one short
    role-reminder line.

Also confirms the NON-child behaviour is completely unchanged by these edits.

Stdlib-only; mirrors tests/test_consult_child_guard.py's GateChildGuardTests
setup (isolated STATE_DIR/EVIDENCE_DIR/DB_PATH).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routing_gate  # noqa: E402


class _IsolatedGateState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_dir = Path(self.tmp.name) / "routing-gate"
        evidence_dir = Path(self.tmp.name) / "context-evidence"
        db_path = Path(self.tmp.name) / "state.sqlite"
        for target, value in (
            ("STATE_DIR", state_dir), ("EVIDENCE_DIR", evidence_dir), ("DB_PATH", db_path),
        ):
            patcher = mock.patch.object(routing_gate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def read_payload(tool_use_id: str, session_id: str = "session-1"):
        return {
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "tool_name": "Read",
            "tool_input": {"path": "source.py"},
            "_switchboard_host": "codex",
        }

    def _child_env(self):
        return mock.patch.dict(routing_gate.os.environ, {"AGENT_BROKER_CHILD": "1"})


class PreToolUseLabourGateBypassTests(_IsolatedGateState):
    def test_child_never_denied_past_the_labour_limit(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3):
            with self._child_env():
                for index in range(10):
                    result = routing_gate.pre_tool_use(self.read_payload(f"call-{index}"))
                    self.assertNotIn(
                        "hookSpecificOutput", result,
                        f"call {index} should never be denied for a Switchboard child",
                    )

    def test_child_labour_is_not_even_counted(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3):
            with self._child_env():
                for index in range(5):
                    routing_gate.pre_tool_use(self.read_payload(f"call-{index}"))
        state = routing_gate._read_state("session-1")
        self.assertEqual(int(state.get("direct_labour_since_relief") or 0), 0)
        self.assertEqual(state.get("direct_labour_counts") or {}, {})

    def test_non_child_still_denied_at_the_same_limit(self):
        # Sibling of test_pretool_allows_exact_limit_then_denies_next_call --
        # proves this edit did not touch the brain's own enforcement.
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3):
            for index in range(3):
                self.assertEqual(routing_gate.pre_tool_use(self.read_payload(f"call-{index}")), {})
            denied = routing_gate.pre_tool_use(self.read_payload("call-3"))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_child_agent_spawn_still_denied(self):
        with self._child_env():
            result = routing_gate.pre_tool_use(
                {
                    "session_id": "session-1", "tool_use_id": "call-1",
                    "tool_name": "Agent", "tool_input": {"prompt": "do x"},
                    "_switchboard_host": "codex",
                }
            )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_child_flagship_prompt_cap_not_applied(self):
        # A DELEGATION_TOOL_NAMES call naming a flagship model with an oversized
        # prompt is denied for the brain, but a child never reaches that check
        # at all -- Agent/Task are already denied by the guard before it runs.
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES + 500)
        with self._child_env():
            result = routing_gate.pre_tool_use(
                {
                    "session_id": "session-1", "tool_use_id": "call-1",
                    "tool_name": "Agent",
                    "tool_input": {"model": "gpt-6-astra", "prompt": big_prompt},
                    "_switchboard_host": "codex",
                }
            )
        # Still denied by the spawn guard, not by (or in addition to) the
        # oversize-prompt path -- same reason text as the plain Agent-deny case.
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecisionReason"],
            "Switchboard children may not spawn agents or start consultations",
        )

    def test_child_claude_circuit_breaker_not_applied(self):
        # Latch Claude unavailable for this session the way _record_claude_unavailable
        # would, then confirm a child's plain Read is never denied by that circuit.
        def latch(state):
            state["claude_unavailable"] = "plan"

        routing_gate._update_state("session-1", latch)
        with self._child_env():
            result = routing_gate.pre_tool_use(self.read_payload("call-1"))
        self.assertNotIn("hookSpecificOutput", result)


class PostToolUseIngressOnlyTests(_IsolatedGateState):
    def test_child_gets_ingress_quarantine_but_no_checkpoint(self):
        huge = "A" * (routing_gate.CONTEXT_INGRESS_MAX_CHARS + 2_000)
        payload = {
            "session_id": "session-1",
            "tool_name": "mcp__agent_switchboard__route_agent_task",
            "tool_input": {},
            "tool_response": huge,
            "_switchboard_host": "codex",
        }
        with self._child_env():
            result = routing_gate.post_tool_use(payload)
        self.assertEqual(result.get("decision"), "block")
        self.assertIn("quarantined", result.get("reason", "").lower())

    def test_child_result_never_carries_labour_checkpoint_notice(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            with self._child_env():
                routing_gate.pre_tool_use(self.read_payload("call-0"))
                result = routing_gate.post_tool_use(
                    {
                        "session_id": "session-1", "tool_name": "Read",
                        "tool_input": {"path": "a.py"}, "tool_response": "ok",
                        "_switchboard_host": "codex",
                    }
                )
        self.assertEqual(result, {})

    def test_child_result_never_carries_credit_bookkeeping(self):
        payload = {
            "session_id": "session-1",
            "tool_name": "mcp__agent_switchboard__route_agent_task",
            "tool_input": {},
            "tool_response": {"status": "completed_verified", "receipt": "broker:abc"},
            "_switchboard_host": "codex",
        }
        with self._child_env():
            result = routing_gate.post_tool_use(payload)
        self.assertEqual(result, {})

    def test_non_child_ingress_quarantine_unchanged(self):
        huge = "A" * (routing_gate.CONTEXT_INGRESS_MAX_CHARS + 2_000)
        payload = {
            "session_id": "session-1",
            "tool_name": "mcp__agent_switchboard__route_agent_task",
            "tool_input": {},
            "tool_response": huge,
            "_switchboard_host": "codex",
        }
        result = routing_gate.post_tool_use(payload)
        self.assertEqual(result.get("decision"), "block")
        self.assertIn("quarantined", result.get("reason", "").lower())


class StopAndSubagentStopNeverBlockChildTests(_IsolatedGateState):
    def test_stop_never_blocks_a_child_even_when_decision_receipt_required(self):
        def require(state):
            state["decision_receipt_required"] = True
            state["decision_requirement_source"] = "task_kind:architecture"

        routing_gate._update_state("session-1", require)
        with self._child_env():
            result = routing_gate.stop({"session_id": "session-1", "last_assistant_message": ""})
        self.assertEqual(result, {})

    def test_stop_never_blocks_a_child_on_unaudited_mutation(self):
        routing_gate.mark_mutated("session-1")
        with self._child_env():
            result = routing_gate.stop({"session_id": "session-1", "last_assistant_message": "did stuff"})
        self.assertEqual(result, {})

    def test_non_child_stop_still_blocks_on_missing_decision_receipt(self):
        def require(state):
            state["decision_receipt_required"] = True
            state["decision_requirement_source"] = "task_kind:architecture"

        routing_gate._update_state("session-1", require)
        result = routing_gate.stop({"session_id": "session-1", "last_assistant_message": ""})
        self.assertEqual(result.get("decision"), "block")

    def test_subagent_stop_never_blocks_and_records_nothing_for_a_child(self):
        with self._child_env():
            result = routing_gate.subagent_stop(
                {
                    "session_id": "session-1", "agent_id": "a1", "agent_type": "worker",
                    "turn_id": "t1", "model": "gpt-6-astra",
                }
            )
        self.assertEqual(result, {})
        state = routing_gate._read_state("session-1")
        self.assertEqual(state.get("native_agents") or {}, {})

    def test_subagent_start_skips_bookkeeping_for_a_child(self):
        with self._child_env():
            result = routing_gate.subagent_start(
                {"session_id": "session-1", "agent_id": "a1", "agent_type": "worker", "turn_id": "t1"}
            )
        self.assertEqual(result, {})
        state = routing_gate._read_state("session-1")
        self.assertEqual(state.get("native_agents") or {}, {})


class UserPromptSubmitChildTests(_IsolatedGateState):
    def test_child_gets_short_reminder_not_the_brain_policy(self):
        with self._child_env():
            result = routing_gate.user_prompt_submit(
                {"session_id": "session-1", "prompt": "do the task"}
            )
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            context,
            "You are a Switchboard child: answer the request; do not delegate or consult.",
        )
        self.assertNotIn("STANDING REQUEST", context)
        self.assertNotIn("route_agent_task", context)

    def test_child_never_sets_decision_receipt_required(self):
        with self._child_env():
            routing_gate.user_prompt_submit(
                {"session_id": "session-1", "prompt": "this is an architecture decision"}
            )
        state = routing_gate._read_state("session-1")
        self.assertFalse(state.get("decision_receipt_required"))

    def test_non_child_still_gets_the_brain_policy_line(self):
        result = routing_gate.user_prompt_submit({"session_id": "session-1", "prompt": "hello"})
        context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("STANDING REQUEST", context)


if __name__ == "__main__":
    unittest.main()
