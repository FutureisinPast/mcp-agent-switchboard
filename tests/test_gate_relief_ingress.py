"""WP-SB8A: relief-behind-quarantine, ingress projection, Switchboard broker-tool
exemptions, routing-override loosening, deny/checkpoint wording, and the tiered
flagship prompt cap. Stdlib-only, mirrors tests/test_routing_gate.py conventions."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routing_gate  # noqa: E402


class GateReliefIngressTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "routing-gate"
        self.evidence_dir = Path(self.tmp.name) / "context-evidence"
        self.db_path = Path(self.tmp.name) / "state.sqlite"
        for patch in (
            mock.patch.object(routing_gate, "STATE_DIR", self.state_dir),
            mock.patch.object(routing_gate, "EVIDENCE_DIR", self.evidence_dir),
            mock.patch.object(routing_gate, "DB_PATH", self.db_path),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def pre_payload(tool_use_id: str, tool_name: str = "Read", host: str = "codex", **extra):
        payload = {
            "session_id": "session-1",
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": {"path": "source.py"},
            "_switchboard_host": host,
        }
        payload.update(extra)
        return payload

    def _seed_ledger(self, request_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE consultations (request_id TEXT)")
            conn.execute("INSERT INTO consultations (request_id) VALUES (?)", (request_id,))
            conn.commit()
        finally:
            conn.close()


class ItemOneReliefBehindQuarantineTests(GateReliefIngressTestBase):
    def _oversize_dispatch_payload(self, receipt: str, extra_response_fields: dict | None = None):
        response = {
            "receipt": receipt,
            "outcome": "completed_verified",
            "work_package_id": "WP-BIG",
            "padding": "x" * 500,
        }
        if extra_response_fields:
            response.update(extra_response_fields)
        return {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "dispatch-big",
            "tool_name": routing_gate.CREDITABLE_DISPATCH_TOOL,
            "tool_input": {"target_agent": "antigravity", "surface": "cli"},
            "tool_response": response,
            "_switchboard_host": "codex",
        }

    def test_oversize_completed_verified_response_still_grants_relief(self):
        request_id = str(uuid.uuid4())
        self._seed_ledger(request_id)
        receipt = f"broker:{request_id}"

        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1), mock.patch.object(
            routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 50
        ):
            # Exhaust the block first.
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("read-1")), {})
            denied = routing_gate.pre_tool_use(self.pre_payload("read-2"))
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

            payload = self._oversize_dispatch_payload(receipt)
            result = routing_gate.post_tool_use(payload)

            # The oversized response was still replaced (quarantine fires)...
            self.assertEqual(result.get("decision"), "block")
            self.assertIn("quarantined", result.get("reason", ""))
            # ...AND the credit notice reached the replacement text.
            self.assertIn(receipt, result.get("reason", ""))
            self.assertIn("Routing credit", result.get("reason", ""))

            # The old bug: relief never happened because the credit function was
            # never called. Prove the block counter actually reset.
            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_since_relief"), 0)
            self.assertEqual(state.get("direct_labour_block_counts"), {})
            self.assertIn(receipt, state.get("credited_receipts") or [])

            allowed_again = routing_gate.pre_tool_use(self.pre_payload("read-3"))
            self.assertEqual(allowed_again, {})

    def test_oversize_response_without_credit_is_unaffected(self):
        # No ledger row seeded -- the receipt cannot resolve, so this must behave
        # exactly like the pre-existing (no-credit) quarantine path.
        payload = self._oversize_dispatch_payload("broker:" + str(uuid.uuid4()))
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 50):
            result = routing_gate.post_tool_use(payload)
        self.assertEqual(result.get("decision"), "block")
        self.assertIn("quarantined", result.get("reason", ""))
        self.assertNotIn("Routing credit", result.get("reason", ""))
        state = routing_gate._read_state("session-1")
        self.assertNotIn("credited_receipts", state)

    def test_claude_host_oversize_credited_response_merges_updated_output(self):
        request_id = str(uuid.uuid4())
        self._seed_ledger(request_id)
        receipt = f"broker:{request_id}"
        payload = self._oversize_dispatch_payload(receipt)
        payload["_switchboard_host"] = "claude"
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 50):
            result = routing_gate.post_tool_use(payload)
        output = result["hookSpecificOutput"]
        self.assertIn("updatedToolOutput", output)
        self.assertIn(receipt, output["updatedToolOutput"])
        self.assertIn("quarantined", output["updatedToolOutput"])


class ItemTwoIngressProjectionTests(GateReliefIngressTestBase):
    def _payload(self, response, host="codex"):
        return {
            "session_id": "session-1",
            "tool_use_id": "call-proj",
            "tool_name": "mcp__market__report",
            "tool_input": {"fields": ["price"]},
            "tool_response": response,
            "_switchboard_host": host,
        }

    def test_projection_includes_named_scalars_and_structured_output(self):
        response = {
            "status": "ok",
            "outcome": "completed_verified",
            "work_package_id": "WP-9",
            "attested_model": "gemini-flash-3.8",
            "structured_output": {"summary": "did the thing", "next_action": "review diff"},
            "padding": "y" * 400,
            "nested_ignored": {"a": 1},
        }
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 50):
            result = routing_gate.post_tool_use(self._payload(response))
        text = result.get("reason", "")
        self.assertIn("projection:", text)
        self.assertIn("status=ok", text)
        self.assertIn("outcome=completed_verified", text)
        self.assertIn("work_package_id=WP-9", text)
        self.assertIn("attested_model=gemini-flash-3.8", text)
        self.assertIn("structured_output.summary=did the thing", text)
        self.assertIn("structured_output.next_action=review diff", text)
        self.assertNotIn("padding", text)
        self.assertNotIn("nested_ignored", text)

    def test_projection_handles_json_nested_in_text_content_envelope(self):
        response = {
            "content": [
                {"type": "text", "text": json.dumps({"status": "unavailable", "receipt": "broker:x"})}
            ]
        }
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 20):
            result = routing_gate.post_tool_use(self._payload(response))
        text = result.get("reason", "")
        self.assertIn("status=unavailable", text)
        self.assertIn("receipt=broker:x", text)

    def test_projection_is_capped_at_1500_chars(self):
        long_value = "z" * 4000
        response = {"status": "ok", "structured_output": {"summary": long_value, "next_action": long_value}}
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 20):
            result = routing_gate.post_tool_use(self._payload(response))
        text = result.get("reason", "")
        idx = text.index("projection:")
        projection_text = text[idx:]
        self.assertLessEqual(len(projection_text), routing_gate.INGRESS_PROJECTION_MAX_CHARS)
        # Each structured_output field is truncated to 300 chars before the cap.
        self.assertNotIn(long_value, text)

    def test_no_projection_when_response_does_not_parse_as_json(self):
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 20):
            result = routing_gate.post_tool_use(self._payload("plain text " * 10))
        self.assertNotIn("projection:", result.get("reason", ""))
        self.assertIn("quarantined", result.get("reason", ""))


class ItemThreeSwitchboardBrokerToolsAreNotLabourTests(unittest.TestCase):
    EXEMPT_TOOLS = (
        "run_evidence_probe",
        "retrieve_shared_context",
        "resolve_model_request",
        "request_status",
        "request_result",
        "compact_topic",
        "get_context_pack",
        "get_work_memory",
        "list_agent_models",
        "list_live_surfaces",
    )

    def test_exempt_tools_are_not_labour_underscore_spelling(self):
        for tool in self.EXEMPT_TOOLS:
            name = f"mcp__agent_switchboard__{tool}"
            with self.subTest(tool=name):
                self.assertIsNone(routing_gate._direct_labour_category(name, {}))

    def test_exempt_tools_are_not_labour_hyphenated_spelling(self):
        for tool in self.EXEMPT_TOOLS:
            name = f"mcp__agent-switchboard__{tool}"
            with self.subTest(tool=name):
                self.assertIsNone(routing_gate._direct_labour_category(name, {}))

    def test_other_switchboard_tools_remain_evidence(self):
        for tool in ("record_agent_event", "store_shared_context", "register_project"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    routing_gate._direct_labour_category(
                        f"mcp__agent_switchboard__{tool}", {}
                    ),
                    "evidence",
                )

    def test_other_mcp_servers_remain_evidence(self):
        self.assertEqual(
            routing_gate._direct_labour_category("mcp__market__get_report", {}), "evidence"
        )
        self.assertEqual(
            routing_gate._direct_labour_category("mcp__ledger__list_rows", {}), "evidence"
        )


class ItemFourRoutingOverrideAnyArgsTests(unittest.TestCase):
    @staticmethod
    def _prefix():
        # `_routing_override_command` already returns "<exe> routing-override <args>";
        # slice just before " routing-override" to get the bare exe/script prefix
        # (which, on Windows, is itself a quoted, space-containing path -- naively
        # splitting on the first space would cut it in the middle).
        generated = routing_gate._routing_override_command("session-1")
        marker = " routing-override"
        idx = generated.index(marker)
        return generated[:idx]

    def test_help_invocation_is_exempt(self):
        command = f"{self._prefix()} routing-override --help"
        self.assertIsNone(routing_gate._direct_labour_category("Bash", {"command": command}))

    def test_bare_invocation_with_no_args_is_exempt(self):
        command = f"{self._prefix()} routing-override"
        self.assertIsNone(routing_gate._direct_labour_category("Bash", {"command": command}))

    def test_full_invocation_still_exempt(self):
        generated = routing_gate._routing_override_command("session-1")
        self.assertIsNone(routing_gate._direct_labour_category("Bash", {"command": generated}))

    def test_chained_command_after_routing_override_gets_no_free_pass(self):
        prefix = self._prefix()
        for chain in (";", "&&", "||", "|", "&"):
            command = f"{prefix} routing-override --help {chain} rm -rf /"
            with self.subTest(chain=chain):
                self.assertEqual(
                    routing_gate._direct_labour_category("Bash", {"command": command}), "other"
                )

    def test_reason_text_with_embedded_semicolon_in_quotes_still_exempt(self):
        command = (
            f'{self._prefix()} routing-override --session s1 --package WP1 '
            '--reason "fix a; b issue found during review"'
        )
        self.assertIsNone(routing_gate._direct_labour_category("Bash", {"command": command}))


class ItemFiveDenyAndCheckpointWordingTests(GateReliefIngressTestBase):
    def test_deny_message_shows_current_block_counts_and_labelled_session_total(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("r1")), {})
            denied = routing_gate.pre_tool_use(self.pre_payload("r2"))
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("direct labour calls in this block", reason)
        self.assertIn("reads=1", reason)
        self.assertIn("Session total since start:", reason)

    def test_deny_message_resets_block_counts_display_after_relief(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("a1")), {})
            self.assertNotEqual(routing_gate.pre_tool_use(self.pre_payload("a2")), {})
            self.assertTrue(
                routing_gate.register_brain_override(
                    "session-1", "WP9", "architecture boundary retained by brain"
                )
            )
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("a3")), {})
            denied_again = routing_gate.pre_tool_use(self.pre_payload("a4"))
        reason = denied_again["hookSpecificOutput"]["permissionDecisionReason"]
        # Only ONE call happened in the new block (a3), not the pre-override total.
        # Denied calls (a2, a4) are never counted, so the session total after a1
        # (counted) + a3 (counted) is 2, not the number of pre_tool_use calls made.
        self.assertIn("1 direct labour calls in this block (reads=1)", reason)
        self.assertIn("Session total since start: reads=2", reason)

    def test_deny_message_suggests_research_task_kind_for_read_category(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("rr1")), {})
            denied = routing_gate.pre_tool_use(self.pre_payload("rr2"))
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("task_kind:'research'", reason)
        self.assertIn("research_questions:", reason)
        self.assertNotIn("task_kind:'quick_check'", reason)

    def test_deny_message_suggests_implementation_or_quick_check_for_other_category(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(
                routing_gate.pre_tool_use(
                    self.pre_payload("o1", tool_name="Bash", tool_input={"command": "echo hi"})
                ),
                {},
            )
            denied = routing_gate.pre_tool_use(
                self.pre_payload("o2", tool_name="Bash", tool_input={"command": "echo bye"})
            )
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("task_kind:'quick_check'", reason)
        self.assertNotIn("task_kind:'research'", reason)

    def test_deny_message_suggests_implementation_for_test_category(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(
                routing_gate.pre_tool_use(
                    self.pre_payload(
                        "t1", tool_name="Bash", tool_input={"command": "pytest tests/"}
                    )
                ),
                {},
            )
            denied = routing_gate.pre_tool_use(
                self.pre_payload("t2", tool_name="Bash", tool_input={"command": "pytest tests/"})
            )
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("task_kind:'implementation'", reason)

    def test_checkpoint_text_shows_block_counts_and_category_guidance(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            routing_gate.pre_tool_use(self.pre_payload("chk-pre"))
            result = routing_gate.post_tool_use(self.pre_payload("chk-pre"))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("direct labour calls in this block", context)
        self.assertIn("reads=1", context)
        self.assertIn("task_kind='research'", context)
        self.assertIn("task_kind='implementation'/'quick_check'", context)


class ItemSixTieredFlagshipPromptCapTests(GateReliefIngressTestBase):
    @staticmethod
    def _payload(model, prompt):
        return {
            "session_id": "session-1",
            "tool_use_id": "call-1",
            "tool_name": "Agent",
            "tool_input": {"model": model, "prompt": prompt},
            "_switchboard_host": "claude",
        }

    def test_constant_value(self):
        self.assertEqual(routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES, 42_000)

    def test_prompt_at_standard_cap_is_silently_allowed(self):
        prompt = "x" * routing_gate.FLAGSHIP_PROMPT_MAX_BYTES
        result = routing_gate.pre_tool_use(self._payload("fable", prompt))
        self.assertNotIn("hookSpecificOutput", result)

    def test_prompt_in_extended_range_is_allowed_and_flagged(self):
        prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1000)
        result = routing_gate.pre_tool_use(self._payload("fable", prompt))
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertIn("extended budget", output["permissionDecisionReason"])

    def test_prompt_at_extended_cap_is_allowed(self):
        prompt = "x" * routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES
        result = routing_gate.pre_tool_use(self._payload("fable", prompt))
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")

    def test_prompt_past_extended_cap_is_denied_naming_both_limits(self):
        prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._payload("fable", prompt))
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        reason = output["permissionDecisionReason"]
        self.assertIn(str(routing_gate.FLAGSHIP_PROMPT_MAX_BYTES), reason)
        self.assertIn(str(routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES), reason)

    def test_extended_allow_logs_flagship_prompt_extended_reason(self):
        prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1000)
        routing_gate.pre_tool_use(self._payload("fable", prompt))
        log_path = routing_gate._session_log_path("session-1")
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(
            any(e.get("reason") == "flagship_prompt_extended" for e in entries),
            entries,
        )


if __name__ == "__main__":
    unittest.main()
