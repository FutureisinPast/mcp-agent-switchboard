"""Focused stdlib-only tests for routing_gate.py."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routing_gate  # noqa: E402


class RoutingGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "routing-gate"
        self.db_path = Path(self.tmp.name) / "state.sqlite"
        self.state_patch = mock.patch.object(routing_gate, "STATE_DIR", self.state_dir)
        self.db_patch = mock.patch.object(routing_gate, "DB_PATH", self.db_path)
        self.state_patch.start()
        self.db_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.db_patch.stop)

    @staticmethod
    def payload(message=""):
        return {"session_id": "session-1", "last_assistant_message": message}

    def test_no_mutation_allows(self):
        self.assertEqual(routing_gate.stop(self.payload()), {})

    def test_user_prompt_resets_prior_turn(self):
        routing_gate.mark_mutated("session-1")
        routing_gate.mark_blocked("session-1")
        routing_gate.user_prompt_submit({"session_id": "session-1"})
        self.assertFalse(routing_gate.has_mutation("session-1"))
        self.assertFalse(routing_gate.already_blocked("session-1"))

    def test_exact_override_line_allows(self):
        routing_gate.mark_mutated("session-1")
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            result = routing_gate.stop(
                self.payload("override: brain - coordination costs more than this tiny edit")
            )
        self.assertEqual(result, {})

    def test_broker_unavailable_fails_open(self):
        routing_gate.mark_mutated("session-1")
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=False):
            self.assertEqual(routing_gate.stop(self.payload()), {})

    def test_blocks_only_once_for_missing_audit(self):
        routing_gate.mark_mutated("session-1")
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            first = routing_gate.stop(self.payload("implementation complete"))
            second = routing_gate.stop(self.payload("implementation complete"))
        self.assertEqual(first.get("decision"), "block")
        self.assertEqual(second, {})

    def test_stop_hook_active_fails_open(self):
        routing_gate.mark_mutated("session-1")
        payload = self.payload()
        payload["stop_hook_active"] = True
        self.assertEqual(routing_gate.stop(payload), {})

    def test_valid_receipt_allows(self):
        routing_gate.mark_mutated("session-1")
        rid = "e53e5d2b-dcb7-4e2d-8c03-20009a336399"
        message = f"## Routing audit\n- request: {rid}\n"
        fake = types.SimpleNamespace(
            request_status=lambda _rid: {
                "found": True,
                "answered": True,
                "responder_model": "claude:claude-sonnet-5 [medium]",
            }
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True), mock.patch.dict(
            sys.modules, {"agent_broker_mcp": fake}
        ):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

    def test_unverified_receipt_blocks(self):
        routing_gate.mark_mutated("session-1")
        rid = "e53e5d2b-dcb7-4e2d-8c03-20009a336399"
        message = f"## Routing audit\n- request: {rid}\n"
        fake = types.SimpleNamespace(
            request_status=lambda _rid: {
                "found": True,
                "answered": True,
                "responder_model": "codex:unverified",
            }
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True), mock.patch.dict(
            sys.modules, {"agent_broker_mcp": fake}
        ):
            self.assertEqual(routing_gate.stop(self.payload(message)).get("decision"), "block")

    def test_receipt_validation_error_fails_open(self):
        routing_gate.mark_mutated("session-1")
        rid = "e53e5d2b-dcb7-4e2d-8c03-20009a336399"
        message = f"## Routing audit\n- request: {rid}\n"
        fake = types.SimpleNamespace(request_status=mock.Mock(side_effect=RuntimeError("down")))
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True), mock.patch.dict(
            sys.modules, {"agent_broker_mcp": fake}
        ):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

    def test_mutation_classification(self):
        positives = [
            ("Edit", {}),
            ("Bash", {"command": "npm install"}),
            ("Bash", {"command": "ssh box systemctl restart nginx"}),
            ("PowerShell", {"command": "Set-Content file.txt value"}),
        ]
        for tool, tool_input in positives:
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertTrue(routing_gate._is_mutating(tool, tool_input))
        negatives = [
            ("Read", {"path": "x"}),
            ("Bash", {"command": "git status"}),
            ("Bash", {"command": "ssh box cat /etc/nginx/nginx.conf"}),
        ]
        for tool, tool_input in negatives:
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertFalse(routing_gate._is_mutating(tool, tool_input))

    def test_hook_merge_preserves_unrelated_entry(self):
        existing = [{"type": "command", "command": "shutdown-if-armed.ps1"}]
        merged = routing_gate.merge_hook_entry(existing, "agent-switchboard routing-hook Stop")
        self.assertEqual(merged[0], existing[0])
        self.assertEqual(len(merged), 2)
        merged_again = routing_gate.merge_hook_entry(merged, "agent-switchboard routing-hook Stop")
        self.assertEqual(merged_again, merged)
        self.assertEqual(routing_gate.remove_owned_hook_entries(merged), existing)

    def test_cli_always_emits_json_on_bad_input(self):
        with mock.patch("sys.stdin.read", return_value="not-json"), mock.patch(
            "sys.stdout.write"
        ) as write:
            self.assertEqual(routing_gate.main(["UnknownEvent"]), 0)
        json.loads(write.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
