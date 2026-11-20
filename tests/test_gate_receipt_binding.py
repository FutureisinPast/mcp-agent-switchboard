"""Tests for Flash receipt binding in routing_gate.py."""
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


class GateReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "routing-gate"
        self.evidence_dir = Path(self.tmp.name) / "context-evidence"
        self.db_path = Path(self.tmp.name) / "state.sqlite"
        self.state_patch = mock.patch.object(routing_gate, "STATE_DIR", self.state_dir)
        self.evidence_patch = mock.patch.object(
            routing_gate, "EVIDENCE_DIR", self.evidence_dir
        )
        self.db_patch = mock.patch.object(routing_gate, "DB_PATH", self.db_path)
        # Clear AGENT_BROKER_CHILD and enforce native flash_skip even when run without pytest.
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "AGENT_BROKER_CHILD": "",
                "AGENT_BROKER_NATIVE_FLASH_SKIP": "enforce",
            },
        )
        self.state_patch.start()
        self.evidence_patch.start()
        self.db_patch.start()
        self.env_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.evidence_patch.stop)
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.env_patch.stop)

    @staticmethod
    def pre_payload(
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict | None = None,
        host: str = "codex",
        **extra,
    ) -> dict:
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
    ) -> dict:
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

    @classmethod
    def _dispatch_payload(
        cls,
        tool_use_id: str,
        receipt: str,
        outcome: str,
        work_package_id: str,
        prompt: str = "Execute package",
        read_context: list[str] | None = None,
        allowed_files: list[str] | None = None,
        session_id: str = "session-1",
    ) -> dict:
        tool_input: dict = {
            "target_agent": "antigravity",
            "surface": "cli",
            "target_model": "hydrogen",
            "work_package_id": work_package_id,
            "prompt": prompt,
        }
        if read_context is not None:
            tool_input["read_context"] = read_context
        if allowed_files is not None:
            tool_input["allowed_files"] = allowed_files
        bare_id = receipt.split(":", 1)[1] if receipt.startswith("broker:") else receipt
        return cls.post_payload(
            session_id,
            tool_use_id,
            "mcp__agent_switchboard__route_agent_task",
            tool_response={
                "receipt": receipt,
                "request_id": bare_id,
                "outcome": outcome,
                "work_package_id": work_package_id,
            },
            tool_input=tool_input,
            host="codex",
        )

    @classmethod
    def _spawn_payload(
        cls,
        tool_use_id: str,
        text: str,
        host: str = "codex",
        session_id: str = "session-1",
        tool_name: str | None = None,
        agent_type: str | None = None,
        name: str | None = None,
    ) -> dict:
        if host == "claude":
            t_name = tool_name or "Agent"
            role = agent_type or "economy-worker"
            tool_input: dict = {"subagent_type": role, "prompt": text}
        else:
            t_name = tool_name or "collaborationspawn_agent"
            role = agent_type or "worker"
            tool_input = {"agent_type": role, "instructions": text}
        if name:
            tool_input["name"] = name
        return cls.pre_payload(
            session_id,
            tool_use_id,
            t_name,
            tool_input,
            host=host,
        )

    @staticmethod
    def _deny_reason(result: dict) -> str:
        return (
            result.get("hookSpecificOutput", {}).get("permissionDecisionReason")
            or ""
        )

    @staticmethod
    def _decision(result: dict) -> str:
        return (
            result.get("hookSpecificOutput", {}).get("permissionDecision")
            or ""
        )

    def _read_log_entries(self, session_id: str = "session-1") -> list[dict]:
        path = routing_gate._session_log_path(session_id)
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

    def test_receipt_from_package_a_rejected_for_package_b_spawn(self):
        receipt_a = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-1", receipt_a, "unavailable_pre_mutation", "WP-PKG-A"
            )
        )

        denied = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-1",
                f"Handle WP-PKG-B\nflash_skip: flash-failed:{receipt_a}",
            )
        )
        self.assertEqual(self._decision(denied), "deny")
        reason = self._deny_reason(denied)
        self.assertIn("wrong package", reason.lower())
        self.assertIn("WP-PKG-A", reason)
        self.assertIn("target_model='hydrogen'", reason)
        self.assertIn("stage_repo=true", reason)

    def test_single_rejection_denied_with_fix_and_resend_message(self):
        receipt = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-1",
                receipt,
                "rejected",
                "WP-PKG-A",
                prompt="First attempt",
                read_context=["a.py"],
                allowed_files=["b.py"],
            )
        )

        denied = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-1",
                f"Handle WP-PKG-A\nflash_skip: flash-failed:{receipt}",
            )
        )
        self.assertEqual(self._decision(denied), "deny")
        reason = self._deny_reason(denied)
        self.assertIn("only one rejection so far", reason)
        self.assertIn("fix and resend the package to the workhorse first", reason)
        self.assertIn("target_model='hydrogen'", reason)
        self.assertIn("stage_repo=true", reason)

    def test_two_rejections_different_digests_allowed_once_then_denied(self):
        receipt_1 = f"broker:{uuid.uuid4()}"
        receipt_2 = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-1",
                receipt_1,
                "rejected",
                "WP-PKG-A",
                prompt="Attempt 1",
                read_context=["a.py"],
                allowed_files=["x.py"],
            )
        )
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-2",
                receipt_2,
                "blocked",
                "WP-PKG-A",
                prompt="Attempt 2 with narrowed scope",
                read_context=["a.py", "c.py"],
                allowed_files=["x.py"],
            )
        )

        first = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-1",
                f"Implement WP-PKG-A.\nflash_skip: flash-failed:{receipt_2}",
            )
        )
        self.assertEqual(first, {})

        second = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-2",
                f"Retry WP-PKG-A.\nflash_skip: flash-failed:{receipt_2}",
            )
        )
        self.assertEqual(self._decision(second), "deny")
        reason = self._deny_reason(second)
        self.assertIn("already used", reason.lower())
        self.assertIn("target_model='hydrogen'", reason)

        # Bare UUID alias of the same consumed receipt is also rejected.
        bare_2 = receipt_2.split(":", 1)[1]
        third = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-3",
                f"Retry WP-PKG-A.\nflash_skip: flash-failed:{bare_2}",
            )
        )
        self.assertEqual(self._decision(third), "deny")
        self.assertIn("already used", self._deny_reason(third).lower())

    def test_two_rejections_same_digest_denied(self):
        receipt_1 = f"broker:{uuid.uuid4()}"
        receipt_2 = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-1",
                receipt_1,
                "rejected",
                "WP-PKG-SAME",
                prompt="Unchanged prompt",
                read_context=["b.py", "a.py"],
                allowed_files=["z.py", "y.py"],
            )
        )
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-2",
                receipt_2,
                "failed_pre_mutation",
                "WP-PKG-SAME",
                prompt="Unchanged prompt",
                read_context=["a.py", "b.py"],
                allowed_files=["y.py", "z.py"],
            )
        )

        denied = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-1",
                f"Work on WP-PKG-SAME\nflash_skip: flash-failed:{receipt_2}",
            )
        )
        self.assertEqual(self._decision(denied), "deny")
        reason = self._deny_reason(denied)
        self.assertIn("fix and resend the package to the workhorse first", reason)
        self.assertIn("target_model='hydrogen'", reason)

    def test_availability_failure_allowed_on_first_failure_for_package_once(self):
        for outcome in ("unavailable_pre_mutation", "timeout", "quota"):
            session_id = f"session-{outcome}"
            receipt = f"broker:{uuid.uuid4()}"
            routing_gate.post_tool_use(
                self._dispatch_payload(
                    "d-avail",
                    receipt,
                    outcome,
                    "WP-PKG-AVAIL",
                    session_id=session_id,
                )
            )
            allowed = routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-avail-1",
                    f"Run WP-PKG-AVAIL\nflash_skip: flash-failed:{receipt}",
                    session_id=session_id,
                )
            )
            self.assertEqual(allowed, {}, msg=f"expected allow for {outcome}")

            reused = routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-avail-2",
                    f"Run WP-PKG-AVAIL again\nflash_skip: flash-failed:{receipt}",
                    session_id=session_id,
                )
            )
            self.assertEqual(self._decision(reused), "deny")
            self.assertIn("already used", self._deny_reason(reused).lower())

    def test_flash_unavailable_valid_for_other_package_within_30_minutes_and_invalid_after(self):
        receipt = f"broker:{uuid.uuid4()}"
        t0 = 1_000_000.0
        with mock.patch("time.time", return_value=t0):
            routing_gate.post_tool_use(
                self._dispatch_payload(
                    "d-unavail",
                    receipt,
                    "unavailable_pre_mutation",
                    "WP-PKG-A",
                )
            )

        with mock.patch("time.time", return_value=t0 + 29 * 60):
            allow_b = routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-b",
                    f"Work on WP-PKG-B\nflash_skip: flash-unavailable:{receipt}",
                )
            )
            self.assertEqual(allow_b, {})

            # Not consumed: another package can also use it within 30 minutes.
            allow_c = routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-c",
                    f"Work on WP-PKG-C\nflash_skip: flash-unavailable:{receipt}",
                )
            )
            self.assertEqual(allow_c, {})

        with mock.patch("time.time", return_value=t0 + 30 * 60 + 1):
            expired = routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-d",
                    f"Work on WP-PKG-D\nflash_skip: flash-unavailable:{receipt}",
                )
            )
            self.assertEqual(self._decision(expired), "deny")
            self.assertIn("expired", self._deny_reason(expired).lower())

    def test_flash_unavailable_rejects_non_availability_outcome(self):
        receipt = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload("d-rej", receipt, "rejected", "WP-PKG-A")
        )
        denied = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-1",
                f"Work on WP-PKG-B\nflash_skip: flash-unavailable:{receipt}",
            )
        )
        self.assertEqual(self._decision(denied), "deny")
        reason = self._deny_reason(denied).lower()
        self.assertIn("non-availability outcome", reason)
        self.assertIn("'rejected'", reason)

    def test_unknown_and_creditable_receipts_denied_with_exact_reason(self):
        unknown_receipt = f"broker:{uuid.uuid4()}"
        unknown = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-unk",
                f"Work on WP-PKG-A\nflash_skip: flash-failed:{unknown_receipt}",
            )
        )
        self.assertEqual(self._decision(unknown), "deny")
        self.assertIn("unknown receipt", self._deny_reason(unknown).lower())
        self.assertIn("target_model='hydrogen'", self._deny_reason(unknown))

        good_receipt = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-ok", good_receipt, "completed_verified", "WP-PKG-A"
            )
        )
        creditable = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-ok",
                f"Work on WP-PKG-A\nflash_skip: flash-failed:{good_receipt}",
            )
        )
        self.assertEqual(self._decision(creditable), "deny")
        self.assertIn("completed_verified", self._deny_reason(creditable))

    def test_followup_to_authorized_agent_allowed_without_new_receipt(self):
        # Codex path: SubagentStart exposes agent_id; followup_task targets id.
        r1 = f"broker:{uuid.uuid4()}"
        r2 = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload("d-1", r1, "rejected", "WP-PKG-FU", prompt="First")
        )
        routing_gate.post_tool_use(
            self._dispatch_payload("d-2", r2, "rejected", "WP-PKG-FU", prompt="Second")
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "call-spawn-1",
                    f"Implement WP-PKG-FU\nflash_skip: flash-failed:{r2}",
                    host="codex",
                )
            ),
            {},
        )
        routing_gate.subagent_start(
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "agent_id": "worker-auth-1",
                "agent_type": "worker",
            }
        )

        # Follow-up to authorized agent needs no flash_skip receipt.
        followup_ok = routing_gate.pre_tool_use(
            self.pre_payload(
                "session-1",
                "fu-1",
                "collaborationfollowup_task",
                {
                    "id": "worker-auth-1",
                    "message": "Continue checking the edge case.",
                },
                host="codex",
            )
        )
        self.assertEqual(followup_ok, {})

        # Follow-up to an unauthorized agent still requires flash_skip.
        followup_denied = routing_gate.pre_tool_use(
            self.pre_payload(
                "session-1",
                "fu-2",
                "collaborationfollowup_task",
                {
                    "id": "worker-unauth-99",
                    "message": "Continue checking without a receipt.",
                },
                host="codex",
            )
        )
        self.assertEqual(self._decision(followup_denied), "deny")

        # Claude path: PostToolUse tool_response exposes agentId; SendMessage targets it.
        r3 = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-3",
                r3,
                "unavailable_pre_mutation",
                "WP-PKG-CLAUDE",
                session_id="session-claude",
            )
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "toolu-agent-1",
                    f"Implement WP-PKG-CLAUDE\nflash_skip: flash-failed:{r3}",
                    host="claude",
                    session_id="session-claude",
                )
            ),
            {},
        )
        routing_gate.post_tool_use(
            self.post_payload(
                "session-claude",
                "toolu-agent-1",
                "Agent",
                tool_response={"agentId": "claude-auth-agent-1"},
                tool_input={"subagent_type": "economy-worker", "prompt": "..."},
                host="claude",
            )
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self.pre_payload(
                    "session-claude",
                    "toolu-msg-1",
                    "SendMessage",
                    {
                        "to": "claude-auth-agent-1",
                        "message": "Please run one more verification step.",
                    },
                    host="claude",
                )
            ),
            {},
        )
        logs = self._read_log_entries("session-claude")
        self.assertTrue(
            any(e.get("decision") == "followup-authorized-allow" for e in logs)
        )

    def test_state_survives_reload_from_disk_and_turn_reset(self):
        receipt_1 = f"broker:{uuid.uuid4()}"
        receipt_2 = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-1", receipt_1, "rejected", "WP-PERSIST", prompt="First try"
            )
        )
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-2", receipt_2, "rejected", "WP-PERSIST", prompt="Second try"
            )
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "call-persist-1",
                    f"Work WP-PERSIST\nflash_skip: flash-failed:{receipt_2}",
                )
            ),
            {},
        )
        routing_gate.post_tool_use(
            self.post_payload(
                "session-1",
                "call-persist-1",
                "collaborationspawn_agent",
                tool_response={"agent_id": "persisted-worker-1"},
                tool_input={"agent_type": "worker", "instructions": "..."},
                host="codex",
            )
        )

        state_file = routing_gate._session_path("session-1")
        self.assertTrue(state_file.exists())
        disk_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn(receipt_2, disk_state["flash_receipts"])
        bare_2 = receipt_2.split(":", 1)[1]
        self.assertTrue(disk_state["flash_receipt_records"][bare_2]["consumed"])
        self.assertEqual(
            disk_state["flash_receipt_records"][bare_2]["consumed_agent_id"],
            "persisted-worker-1",
        )
        self.assertEqual(
            disk_state["authorized_native_agents"]["persisted-worker-1"]["work_package_id"],
            "WP-PERSIST",
        )

        # Turn reset simulates a new turn after reload/restart; records survive.
        routing_gate.user_prompt_submit({"session_id": "session-1", "prompt": "Next turn"})

        reused = routing_gate.pre_tool_use(
            self._spawn_payload(
                "call-persist-2",
                f"Work WP-PERSIST\nflash_skip: flash-failed:{receipt_2}",
            )
        )
        self.assertEqual(self._decision(reused), "deny")
        self.assertIn("already used", self._deny_reason(reused).lower())

        followup_after_reset = routing_gate.pre_tool_use(
            self.pre_payload(
                "session-1",
                "fu-persist",
                "collaborationfollowup_task",
                {"id": "persisted-worker-1", "message": "Next turn follow-up"},
                host="codex",
            )
        )
        self.assertEqual(followup_after_reset, {})

    def test_async_dispatch_followed_by_request_result_preserves_attempt_digest(self):
        req_id = str(uuid.uuid4())
        receipt = f"broker:{req_id}"
        # Async route_agent_task starts Attempt 1
        routing_gate.post_tool_use(
            self.post_payload(
                "session-1",
                "async-1",
                "mcp__agent_switchboard__route_agent_task",
                tool_input={
                    "target_agent": "antigravity",
                    "surface": "cli",
                    "target_model": "hydrogen",
                    "work_package_id": "WP-ASYNC",
                    "prompt": "Async attempt 1",
                },
                tool_response={
                    "status": "queued",
                    "request_id": req_id,
                    "receipt": receipt,
                    "work_package_id": "WP-ASYNC",
                    "async_worker": {"started": True},
                },
                host="codex",
            )
        )
        # Cannot use while still queued/in-progress
        too_early = routing_gate.pre_tool_use(
            self._spawn_payload(
                "s-early",
                f"Work WP-ASYNC\nflash_skip: flash-failed:{receipt}",
            )
        )
        self.assertEqual(self._decision(too_early), "deny")

        # request_result reports rejection without re-sending prompt in tool_input
        routing_gate.post_tool_use(
            self.post_payload(
                "session-1",
                "res-1",
                "mcp__agent_switchboard__request_result",
                tool_input={"request_id": req_id},
                tool_response={
                    "request_id": req_id,
                    "receipt": receipt,
                    "work_package_id": "WP-ASYNC",
                    "outcome": "rejected",
                },
                host="codex",
            )
        )

        # Second distinct attempt rejected -> now allowed
        receipt_2 = f"broker:{uuid.uuid4()}"
        routing_gate.post_tool_use(
            self._dispatch_payload(
                "d-async-2",
                receipt_2,
                "rejected",
                "WP-ASYNC",
                prompt="Async attempt 2 fixed",
            )
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-async-ok",
                    f"Work WP-ASYNC\nflash_skip: flash-failed:{receipt_2}",
                )
            ),
            {},
        )

    def test_implementation_hint_mentions_stage_repo(self):
        snippet = routing_gate._suggested_dispatch_snippet("tests")
        self.assertIn("target_model:'hydrogen'", snippet)
        self.assertIn("stage_repo=true", snippet)

    def test_other_flash_skip_kinds_unchanged(self):
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-host",
                    "Use MCP.\nflash_skip: host-tools:mcp__custom__inspect_state",
                )
            ),
            {},
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-unshared",
                    "Needs context.\nflash_skip: unshared-state:in-memory debug trace from earlier turn",
                )
            ),
            {},
        )
        self.assertEqual(
            routing_gate.pre_tool_use(
                self._spawn_payload(
                    "s-oversize",
                    "Atomic edit.\nflash_skip: atomic-oversize:WP-1072 touches 8 tightly coupled files",
                )
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
