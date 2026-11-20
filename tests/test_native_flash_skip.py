"""Tests for Codex collaboration delegation, flash_skip parsing, and native relief."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routing_gate  # noqa: E402


@pytest.fixture(autouse=True)
def _enforce_native_flash_skip(monkeypatch):
    """conftest turns the check off for older tests; this file tests the default."""
    monkeypatch.setenv("AGENT_BROKER_NATIVE_FLASH_SKIP", "enforce")


class TestNativeFlashSkip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "routing-gate"
        self.evidence_dir = Path(self.tmp.name) / "context-evidence"
        self.db_path = Path(self.tmp.name) / "state.sqlite"
        self.state_patch = mock.patch.object(routing_gate, "STATE_DIR", self.state_dir)
        self.evidence_patch = mock.patch.object(routing_gate, "EVIDENCE_DIR", self.evidence_dir)
        self.db_patch = mock.patch.object(routing_gate, "DB_PATH", self.db_path)
        # Clear AGENT_BROKER_CHILD so test harness exercises brain-level PreToolUse / PostToolUse
        self.child_patch = mock.patch.dict(os.environ, {"AGENT_BROKER_CHILD": ""})
        self.state_patch.start()
        self.evidence_patch.start()
        self.db_patch.start()
        self.child_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.evidence_patch.stop)
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.child_patch.stop)

    @staticmethod
    def pre_payload(
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict | None = None,
        host: str = "codex",
        **extra,
    ):
        payload = {
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input if tool_input is not None else {},
            "_switchboard_host": host,
        }
        payload.update(extra)
        return payload

    @staticmethod
    def post_payload(
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_response: object,
        tool_input: dict | None = None,
        host: str = "codex",
        **extra,
    ):
        payload = {
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "tool_response": tool_response,
            "_switchboard_host": host,
        }
        payload.update(extra)
        return payload

    def test_codex_collaboration_spawn_worker_no_flash_skip_denied(self):
        session_id = "s-deny-1"
        payload = self.pre_payload(
            session_id,
            "call-spawn-1",
            "collaborationspawn_agent",
            {"agent_type": "worker", "prompt": "Implement the feature."},
            host="codex",
        )
        res = routing_gate.pre_tool_use(payload)
        self.assertEqual(res.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        reason = res.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        self.assertIn("flash_skip", reason)
        self.assertIn("route_agent_task", reason)
        state = routing_gate._read_state(session_id)
        self.assertFalse(state.get("pending_native_relief", False))

    def test_codex_collaboration_spawn_worker_valid_unshared_state_allowed_and_relieves(self):
        session_id = "s-allow-unshared"
        # Consume some labour calls first
        for i in range(2):
            routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
        state_before = routing_gate._read_state(session_id)
        self.assertEqual(state_before.get("direct_labour_since_relief"), 2)

        payload = self.pre_payload(
            session_id,
            "call-spawn-2",
            "collaborationspawn_agent",
            {
                "agent_type": "worker",
                "message": "Implement feature\nflash_skip: unshared-state: session state not visible to Flash",
            },
            host="codex",
        )
        res = routing_gate.pre_tool_use(payload)
        self.assertEqual(res, {})
        state_mid = routing_gate._read_state(session_id)
        self.assertTrue(state_mid.get("pending_native_relief"))
        self.assertEqual(state_mid.get("flash_skip_counts", {}).get("unshared-state"), 1)

        # subagent_start grants relief
        routing_gate.subagent_start({
            "session_id": session_id,
            "agent_id": "sub-worker-1",
            "agent_type": "worker",
            "turn_id": "t-1",
        })
        state_after = routing_gate._read_state(session_id)
        self.assertEqual(state_after.get("direct_labour_since_relief"), 0)
        self.assertFalse(state_after.get("pending_native_relief", False))
        self.assertEqual(state_after.get("labour_relief_sequence"), 1)

    def test_flash_failed_unknown_vs_recorded_receipt(self):
        session_id = "s-flash-failed"
        unknown_payload = self.pre_payload(
            session_id,
            "call-spawn-failed-unk",
            "collaborationspawn_agent",
            {
                "agent_type": "worker",
                "instructions": (
                    "Implement WP-1072-TEST\n"
                    "flash_skip: flash-failed: broker:00000000-0000-0000-0000-000000000000 timeout"
                ),
            },
            host="codex",
        )
        res_unk = routing_gate.pre_tool_use(unknown_payload)
        self.assertEqual(res_unk.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn(
            "unknown receipt",
            res_unk.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower(),
        )

        # Recorded receipt with no work_package_id -> deny with exact reason
        no_wp_id = str(uuid.uuid4())
        no_wp_receipt = f"broker:{no_wp_id}"
        routing_gate.post_tool_use(
            self.post_payload(
                session_id,
                "call-flash-post-nowp",
                "mcp__agent_switchboard__route_agent_task",
                tool_response={"receipt": no_wp_receipt, "request_id": no_wp_id, "status": "failed"},
            )
        )
        self.assertIn(no_wp_id, routing_gate._read_state(session_id).get("flash_receipts", []))
        res_nowp = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-nowp",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-TEST\nflash_skip: flash-failed: {no_wp_receipt} returned failure",
                },
                host="codex",
            )
        )
        self.assertEqual(res_nowp.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn(
            "no recorded work_package_id",
            res_nowp.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower(),
        )

        # Recorded rejection for WP-1072-TEST: missing/wrong package in prompt -> deny
        rej1_id = str(uuid.uuid4())
        rej1_receipt = f"broker:{rej1_id}"
        routing_gate.post_tool_use(
            self.post_payload(
                session_id,
                "call-flash-post-rej1",
                "mcp__agent_switchboard__route_agent_task",
                tool_input={
                    "target_agent": "antigravity",
                    "work_package_id": "WP-1072-TEST",
                    "prompt": "Attempt 1",
                },
                tool_response={
                    "receipt": rej1_receipt,
                    "request_id": rej1_id,
                    "status": "failed",
                    "work_package_id": "WP-1072-TEST",
                },
            )
        )
        res_no_pkg_in_prompt = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-nopkg",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Task brief\nflash_skip: flash-failed: {rej1_receipt} returned failure",
                },
                host="codex",
            )
        )
        self.assertEqual(
            res_no_pkg_in_prompt.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )
        no_pkg_reason = res_no_pkg_in_prompt.get("hookSpecificOutput", {}).get(
            "permissionDecisionReason", ""
        )
        self.assertIn("wrong package", no_pkg_reason.lower())
        self.assertIn("WP-1072-TEST", no_pkg_reason)

        # Same package after only one rejection -> deny
        res_one_rej = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-one-rej",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-TEST\nflash_skip: flash-failed: {rej1_receipt} returned failure",
                },
                host="codex",
            )
        )
        self.assertEqual(res_one_rej.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn(
            "only one rejection so far",
            res_one_rej.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower(),
        )

        # Second rejection with identical attempt digest -> still deny
        rej_same_id = str(uuid.uuid4())
        rej_same_receipt = f"broker:{rej_same_id}"
        routing_gate.post_tool_use(
            self.post_payload(
                session_id,
                "call-flash-post-same",
                "mcp__agent_switchboard__route_agent_task",
                tool_input={
                    "target_agent": "antigravity",
                    "work_package_id": "WP-1072-TEST",
                    "prompt": "Attempt 1",
                },
                tool_response={
                    "receipt": rej_same_receipt,
                    "request_id": rej_same_id,
                    "status": "failed",
                    "work_package_id": "WP-1072-TEST",
                },
            )
        )
        res_same_digest = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-same-digest",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-TEST\nflash_skip: flash-failed: {rej_same_receipt} returned failure",
                },
                host="codex",
            )
        )
        self.assertEqual(
            res_same_digest.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )
        self.assertIn(
            "only one distinct attempt so far",
            res_same_digest.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower(),
        )

        # Second distinct attempt digest rejected -> allowed once, then denied on reuse
        rej2_id = str(uuid.uuid4())
        rej2_receipt = f"broker:{rej2_id}"
        routing_gate.post_tool_use(
            self.post_payload(
                session_id,
                "call-flash-post-rej2",
                "mcp__agent_switchboard__route_agent_task",
                tool_input={
                    "target_agent": "antigravity",
                    "work_package_id": "WP-1072-TEST",
                    "prompt": "Attempt 2 with revised scope",
                },
                tool_response={
                    "receipt": rej2_receipt,
                    "request_id": rej2_id,
                    "status": "failed",
                    "work_package_id": "WP-1072-TEST",
                },
            )
        )
        res_rej_ok = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-failed-valid",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-TEST\nflash_skip: flash-failed: {rej2_receipt} returned failure",
                },
                host="codex",
            )
        )
        self.assertEqual(res_rej_ok, {})
        state2 = routing_gate._read_state(session_id)
        self.assertTrue(state2.get("pending_native_relief"))
        self.assertEqual(state2.get("flash_skip_counts", {}).get("flash-failed"), 1)

        res_rej_reuse = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-failed-reuse",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-TEST\nflash_skip: flash-failed: {rej2_receipt} reused",
                },
                host="codex",
            )
        )
        self.assertEqual(res_rej_reuse.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn(
            "already used",
            res_rej_reuse.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower(),
        )

        # Availability failure for the named package -> allowed once on first failure, then denied
        avail_id = str(uuid.uuid4())
        avail_receipt = f"broker:{avail_id}"
        routing_gate.post_tool_use(
            self.post_payload(
                session_id,
                "call-flash-post-avail",
                "mcp__agent_switchboard__route_agent_task",
                tool_input={
                    "target_agent": "antigravity",
                    "work_package_id": "WP-1072-AVAIL",
                    "prompt": "Attempt on unavailable workhorse",
                },
                tool_response={
                    "receipt": avail_receipt,
                    "request_id": avail_id,
                    "status": "timeout",
                    "work_package_id": "WP-1072-AVAIL",
                },
            )
        )
        res_avail_ok = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-avail-valid",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-AVAIL\nflash_skip: flash-failed: {avail_receipt} timed out",
                },
                host="codex",
            )
        )
        self.assertEqual(res_avail_ok, {})
        state3 = routing_gate._read_state(session_id)
        self.assertTrue(state3.get("pending_native_relief"))
        self.assertEqual(state3.get("flash_skip_counts", {}).get("flash-failed"), 2)

        res_avail_reuse = routing_gate.pre_tool_use(
            self.pre_payload(
                session_id,
                "call-spawn-avail-reuse",
                "collaborationspawn_agent",
                {
                    "agent_type": "worker",
                    "instructions": f"Implement WP-1072-AVAIL\nflash_skip: flash-failed: {avail_receipt} reused",
                },
                host="codex",
            )
        )
        self.assertEqual(
            res_avail_reuse.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )
        self.assertIn(
            "already used",
            res_avail_reuse.get("hookSpecificOutput", {}).get("permissionDecisionReason", "").lower(),
        )

    def test_host_tools_rejects_standard_tools(self):
        session_id = "s-host-tools"
        for standard in [
            "bash", "shell", "powershell", "read", "grep", "glob",
            "edit", "write", "apply_patch", "exec", "execute", "view_image", "search"
        ]:
            payload = self.pre_payload(
                session_id,
                f"call-ht-{standard}",
                "collaborationspawn_agent",
                {
                    "agent_type": "explorer",
                    "task": f"Run command\nflash_skip: host-tools: {standard} needed for testing",
                },
                host="codex",
            )
            res = routing_gate.pre_tool_use(payload)
            self.assertEqual(
                res.get("hookSpecificOutput", {}).get("permissionDecision"),
                "deny",
                f"Standard tool {standard} should have been denied",
            )

        # Custom non-standard tool should be allowed
        valid_payload = self.pre_payload(
            session_id,
            "call-ht-custom",
            "collaborationspawn_agent",
            {
                "agent_type": "explorer",
                "task": "Run query\nflash_skip: host-tools: custom_mcp_database_tool needed",
            },
            host="codex",
        )
        res_val = routing_gate.pre_tool_use(valid_payload)
        self.assertEqual(res_val, {})
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("flash_skip_counts", {}).get("host-tools"), 1)

    def test_followup_task_requires_own_flash_skip(self):
        session_id = "s-followup"
        # Followup task with no flash_skip is denied
        payload_denied = self.pre_payload(
            session_id,
            "call-fup-1",
            "collaborationfollowup_task",
            {"prompt": "Continue working on the next steps."},
            host="codex",
        )
        res1 = routing_gate.pre_tool_use(payload_denied)
        self.assertEqual(res1.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

        # Bare followup_task name is also caught
        payload_bare = self.pre_payload(
            session_id,
            "call-fup-bare",
            "followup_task",
            {"prompt": "Continue working on the next steps."},
            host="codex",
        )
        res_bare = routing_gate.pre_tool_use(payload_bare)
        self.assertEqual(res_bare.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

        # Followup task with its own valid flash_skip is allowed
        payload_allowed = self.pre_payload(
            session_id,
            "call-fup-2",
            "collaborationfollowup_task",
            {
                "prompt": "Continue\nflash_skip: atomic-oversize: six indivisible files in this package",
            },
            host="codex",
        )
        res2 = routing_gate.pre_tool_use(payload_allowed)
        self.assertEqual(res2, {})
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("flash_skip_counts", {}).get("atomic-oversize"), 1)

    def test_spawn_with_no_text_field_fails_open_and_grants_no_relief(self):
        session_id = "s-no-text"
        for i in range(2):
            routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )

        payload = self.pre_payload(
            session_id,
            "call-spawn-notext",
            "collaborationspawn_agent",
            {"agent_type": "worker"},  # No text fields at all
            host="codex",
        )
        res = routing_gate.pre_tool_use(payload)
        self.assertEqual(res, {})  # Fail open
        state = routing_gate._read_state(session_id)
        self.assertFalse(state.get("pending_native_relief", False))

        log_path = routing_gate._session_log_path(session_id)
        records = routing_gate._read_session_log(log_path)
        unknown_recs = [r for r in records if r.get("decision") == "flash-skip-unknown-shape"]
        self.assertEqual(len(unknown_recs), 1)

        # subagent_start does NOT grant relief because pending_native_relief is False
        routing_gate.subagent_start({
            "session_id": session_id,
            "agent_id": "sub-notext-1",
            "agent_type": "worker",
            "turn_id": "t-1",
        })
        state_after = routing_gate._read_state(session_id)
        self.assertEqual(state_after.get("direct_labour_since_relief"), 2)

    def test_send_message_untouched(self):
        session_id = "s-send-msg"
        payload = self.pre_payload(
            session_id,
            "call-send-1",
            "collaborationsend_message",
            {"message": "Status update to worker agent."},
            host="codex",
        )
        res = routing_gate.pre_tool_use(payload)
        self.assertEqual(res, {})
        # Verify it is not considered direct labour
        cat = routing_gate._direct_labour_category(
            "collaborationsend_message", {"message": "Status update"}
        )
        self.assertIsNone(cat)

    def test_warn_mode_allows_and_logs(self):
        session_id = "s-warn-mode"
        with mock.patch.dict(os.environ, {"AGENT_BROKER_NATIVE_FLASH_SKIP": "warn"}):
            payload = self.pre_payload(
                session_id,
                "call-spawn-warn",
                "collaborationspawn_agent",
                {"agent_type": "worker", "prompt": "Worker task without flash skip."},
                host="codex",
            )
            res = routing_gate.pre_tool_use(payload)
            self.assertEqual(res.get("hookSpecificOutput", {}).get("permissionDecision"), "allow")
            self.assertIn("flash_skip", res.get("hookSpecificOutput", {}).get("additionalContext", ""))
            state = routing_gate._read_state(session_id)
            self.assertFalse(state.get("pending_native_relief", False))

            log_path = routing_gate._session_log_path(session_id)
            records = routing_gate._read_session_log(log_path)
            warn_recs = [r for r in records if r.get("decision") == "flash-skip-would-deny"]
            self.assertEqual(len(warn_recs), 1)

    def test_off_mode_restores_old_behaviour(self):
        session_id = "s-off-mode"
        for i in range(2):
            routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
        with mock.patch.dict(os.environ, {"AGENT_BROKER_NATIVE_FLASH_SKIP": "off"}):
            payload = self.pre_payload(
                session_id,
                "call-spawn-off",
                "collaborationspawn_agent",
                {"agent_type": "worker", "prompt": "Worker task without flash skip."},
                host="codex",
            )
            res = routing_gate.pre_tool_use(payload)
            self.assertEqual(res, {})

            # subagent_start grants relief unconditionally in off mode
            routing_gate.subagent_start({
                "session_id": session_id,
                "agent_id": "sub-off-1",
                "agent_type": "worker",
                "turn_id": "t-1",
            })
            state = routing_gate._read_state(session_id)
            self.assertEqual(state.get("direct_labour_since_relief"), 0)

    def test_default_and_flagship_spawn_consult_decision_check(self):
        session_id = "s-consult"
        # No recent consult -> denied
        payload_default = self.pre_payload(
            session_id,
            "call-spawn-def",
            "collaborationspawn_agent",
            {"agent_type": "default", "prompt": "Architecture question."},
            host="codex",
        )
        res_denied = routing_gate.pre_tool_use(payload_default)
        self.assertEqual(res_denied.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn("consult_decision", res_denied.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""))

        # Spawning with flagship model gpt-6-astra without consult -> denied
        payload_flag = self.pre_payload(
            session_id,
            "call-spawn-flag",
            "collaborationspawn_agent",
            {"agent_type": "", "model": "gpt-6-astra", "prompt": "Hard decision."},
            host="codex",
        )
        res_flag_denied = routing_gate.pre_tool_use(payload_flag)
        self.assertEqual(res_flag_denied.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

        # Call consult_decision PreToolUse
        consult_payload = self.pre_payload(
            session_id,
            "call-consult-1",
            "mcp__agent_switchboard__consult_decision",
            {"decision": "DB architecture choice"},
            host="codex",
        )
        routing_gate.pre_tool_use(consult_payload)
        state = routing_gate._read_state(session_id)
        self.assertGreater(state.get("last_consult_decision_ts", 0), 0)

        # Right after consult_decision -> allowed!
        res_def_allowed = routing_gate.pre_tool_use(payload_default)
        self.assertEqual(res_def_allowed, {})

        # Flagship model spawn is now also allowed
        res_flag_allowed = routing_gate.pre_tool_use(payload_flag)
        self.assertEqual(res_flag_allowed, {})

        # On Claude host, non-default agent type (e.g. web-fetch) is untouched even without consult_decision
        claude_payload = self.pre_payload(
            session_id + "-claude",
            "call-claude-fetch",
            "agent",
            {"agent_type": "web-fetch", "prompt": "Fetch external doc"},
            host="claude",
        )
        res_claude = routing_gate.pre_tool_use(claude_payload)
        self.assertEqual(res_claude, {})

    def test_subagent_stop_attests_model_from_transcript(self):
        session_id = "s-transcript"
        transcript_file = Path(self.tmp.name) / "transcript.jsonl"
        transcript_lines = [
            json.dumps({"step": 1, "model": "gpt-6-sol"}),
            json.dumps({"step": 2, "model": "gpt-6-astra"}),
        ]
        transcript_file.write_text("\n".join(transcript_lines), encoding="utf-8")

        # Start agent
        routing_gate.subagent_start({
            "session_id": session_id,
            "agent_id": "sub-attest-1",
            "agent_type": "worker",
            "turn_id": "turn-100",
            "model": "",
        })

        # Stop agent with empty model, pointing to transcript
        routing_gate.subagent_stop({
            "session_id": session_id,
            "agent_id": "sub-attest-1",
            "agent_type": "worker",
            "turn_id": "turn-100",
            "model": "",
            "agent_transcript_path": str(transcript_file),
            "last_assistant_message": "Completed architecture review.",
        })

        state = routing_gate._read_state(session_id)
        agent_record = state.get("native_agents", {}).get("sub-attest-1", {})
        self.assertEqual(agent_record.get("model"), "gpt-6-astra")
        self.assertTrue(agent_record.get("completed"))
        # Flagship model gpt-6-astra records decision receipt
        receipts = state.get("decision_receipts", [])
        self.assertIn("native:sub-attest-1", receipts)


if __name__ == "__main__":
    unittest.main()
