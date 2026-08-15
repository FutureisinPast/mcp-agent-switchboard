"""Focused stdlib-only tests for routing_gate.py."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routing_gate  # noqa: E402

DIRECT_BRAIN_LABOUR = (
    "direct-brain-labour: reads=0 | searches=0 | evidence=0 | "
    "tests=0 | docs=0 | other=0\n"
)


class RoutingGateTests(unittest.TestCase):
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
        self.state_patch.start()
        self.evidence_patch.start()
        self.db_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.evidence_patch.stop)
        self.addCleanup(self.db_patch.stop)

    @staticmethod
    def payload(message=""):
        return {"session_id": "session-1", "last_assistant_message": message}

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

    def test_no_mutation_allows(self):
        self.assertEqual(routing_gate.stop(self.payload()), {})

    def test_user_prompt_resets_prior_turn(self):
        routing_gate.mark_mutated("session-1")
        routing_gate.mark_blocked("session-1")
        routing_gate.user_prompt_submit({"session_id": "session-1"})
        self.assertFalse(routing_gate.has_mutation("session-1"))
        self.assertFalse(routing_gate.already_blocked("session-1"))

    def test_bare_global_override_does_not_bypass_audit(self):
        routing_gate.mark_mutated("session-1")
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            result = routing_gate.stop(
                self.payload("override: brain - coordination costs more than this tiny edit")
            )
        self.assertEqual(result.get("decision"), "block")

    def test_structured_per_work_package_override_inside_audit_allows(self):
        routing_gate.mark_mutated("session-1")
        message = (
            "## Routing audit\n"
            "packages: 1\n"
            f"{DIRECT_BRAIN_LABOUR}"
            "- WP1 | receipt: override: brain - WP1: coordination costs more than this tiny edit\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

    def test_missing_exact_direct_brain_labour_census_blocks(self):
        routing_gate.mark_mutated("session-1")
        message = (
            "## Routing audit\n"
            "packages: 1\n"
            "- WP1 | receipt: override: brain - WP1: retained for architecture risk\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            result = routing_gate.stop(self.payload(message))
        self.assertEqual(result.get("decision"), "block")
        self.assertIn("direct-brain-labour", result.get("reason", ""))

    def test_nonzero_direct_labour_without_matching_package_tag_blocks(self):
        routing_gate.mark_mutated("session-1")
        message = (
            "## Routing audit\n"
            "packages: 1\n"
            "direct-brain-labour: reads=1 | searches=0 | evidence=0 | "
            "tests=0 | docs=0 | other=0\n"
            "- WP1 | receipt: override: brain - WP1: retained for architecture risk\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            result = routing_gate.stop(self.payload(message))
        self.assertEqual(result.get("decision"), "block")

    def test_nonzero_direct_labour_with_matching_package_tag_allows(self):
        routing_gate.mark_mutated("session-1")
        message = (
            "## Routing audit\n"
            "packages: 1\n"
            "direct-brain-labour: reads=1 | searches=0 | evidence=0 | "
            "tests=0 | docs=0 | other=0\n"
            "- WP1 | direct=reads | receipt: override: brain - WP1: "
            "retained for architecture risk\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

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
        message = (
            f"## Routing audit\npackages: 1\n{DIRECT_BRAIN_LABOUR}"
            f"- WP1 | receipt: broker:{rid} | verified\n"
        )
        fake = types.SimpleNamespace(
            request_status=lambda _rid: {
                "found": True,
                "answered": True,
                "state": "completed",
                "model_attested": True,
                "responder_model": "claude:claude-sonnet-5 [medium]",
            }
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True), mock.patch.dict(
            sys.modules, {"agent_broker_mcp": fake}
        ):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

    def test_subagent_lifecycle_records_host_attested_identity_and_completion(self):
        start_payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "agent-reader-1",
            "agent_type": "explorer",
            "model": "gpt-5.6-luna",
        }
        self.assertEqual(routing_gate.subagent_start(start_payload), {})
        started = routing_gate._read_state("session-1")["native_agents"]["agent-reader-1"]
        self.assertEqual(started["agent_id"], "agent-reader-1")
        self.assertEqual(started["agent_type"], "explorer")
        self.assertFalse(started["completed"])

        stop_payload = dict(start_payload, last_assistant_message="evidence returned")
        self.assertEqual(routing_gate.subagent_stop(stop_payload), {})
        completed = routing_gate._read_state("session-1")["native_agents"]["agent-reader-1"]
        self.assertEqual(completed["agent_id"], "agent-reader-1")
        self.assertEqual(completed["agent_type"], "explorer")
        self.assertTrue(completed["completed"])

    def test_subagent_stop_without_matching_start_is_not_a_receipt(self):
        routing_gate.subagent_stop(
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "agent_id": "agent-never-started",
                "agent_type": "worker",
            }
        )
        self.assertFalse(
            routing_gate._native_receipt_valid("session-1", "agent-never-started")
        )

    def test_subagent_stop_from_another_turn_is_not_a_receipt(self):
        start_payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "agent-cross-turn",
            "agent_type": "worker",
        }
        routing_gate.subagent_start(start_payload)
        routing_gate.subagent_stop(dict(start_payload, turn_id="turn-2"))
        self.assertFalse(routing_gate._native_receipt_valid("session-1", "agent-cross-turn"))

    def test_subagent_stop_with_different_role_is_not_a_receipt(self):
        start_payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "agent-role-swap",
            "agent_type": "brain",
        }
        routing_gate.subagent_start(start_payload)
        routing_gate.subagent_stop(dict(start_payload, agent_type="worker"))
        self.assertFalse(routing_gate._native_receipt_valid("session-1", "agent-role-swap"))

    def test_mixed_broker_and_completed_native_receipts_allow(self):
        routing_gate.mark_mutated("session-1")
        native_payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "agent-worker-1",
            "agent_type": "worker",
            "model": "gpt-5.6-terra",
        }
        routing_gate.subagent_start(native_payload)
        routing_gate.subagent_stop(native_payload)
        rid = "e53e5d2b-dcb7-4e2d-8c03-20009a336399"
        message = (
            "## Routing audit\n"
            "packages: 2\n"
            f"{DIRECT_BRAIN_LABOUR}"
            f"- WP1 | receipt: broker:{rid} | consult verified\n"
            "- WP2 | receipt: native:agent-worker-1 | tests passed\n"
        )
        fake = types.SimpleNamespace(
            request_status=lambda _rid: {
                "found": True,
                "answered": True,
                "state": "completed",
                "model_attested": True,
                "responder_model": "claude:claude-fable-5 [max]",
            }
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True), mock.patch.dict(
            sys.modules, {"agent_broker_mcp": fake}
        ):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

    def test_unknown_native_receipt_blocks(self):
        routing_gate.mark_mutated("session-1")
        message = (
            f"## Routing audit\npackages: 1\n{DIRECT_BRAIN_LABOUR}"
            "- WP1 | receipt: native:agent-missing | verified\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            self.assertEqual(routing_gate.stop(self.payload(message)).get("decision"), "block")

    def test_unfinished_native_receipt_blocks(self):
        routing_gate.mark_mutated("session-1")
        routing_gate.subagent_start(
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "agent_id": "agent-worker-pending",
                "agent_type": "worker",
                "model": "gpt-5.6-terra",
            }
        )
        message = (
            f"## Routing audit\npackages: 1\n{DIRECT_BRAIN_LABOUR}"
            "- WP1 | receipt: native:agent-worker-pending | pending\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            self.assertEqual(routing_gate.stop(self.payload(message)).get("decision"), "block")

    def test_every_declared_package_requires_its_own_receipt_row(self):
        routing_gate.mark_mutated("session-1")
        message = (
            "## Routing audit\n"
            "packages: 2\n"
            f"{DIRECT_BRAIN_LABOUR}"
            "- WP1 | receipt: override: brain - WP1: retained for architecture risk\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            self.assertEqual(routing_gate.stop(self.payload(message)).get("decision"), "block")

    def test_multiple_receipts_in_one_package_row_block(self):
        routing_gate.mark_mutated("session-1")
        message = (
            "## Routing audit\n"
            "packages: 1\n"
            f"{DIRECT_BRAIN_LABOUR}"
            "- WP1 | receipt: native:agent-worker-1 | "
            "override: brain - WP1: brain also claims the same package\n"
        )
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True):
            self.assertEqual(routing_gate.stop(self.payload(message)).get("decision"), "block")

    def test_same_vendor_broker_receipt_requires_native_unavailable_reason(self):
        rid = "e53e5d2b-dcb7-4e2d-8c03-20009a336399"
        fake = types.SimpleNamespace(
            request_status=lambda _rid: {
                "found": True,
                "answered": True,
                "state": "completed",
                "model_attested": True,
                "responder_model": "codex:gpt-5.6-terra [medium]",
            }
        )
        without_reason = (
            "## Routing audit\npackages: 1\n"
            f"{DIRECT_BRAIN_LABOUR}"
            f"- WP1 | receipt: broker:{rid} | tests passed\n"
        )
        with_reason = (
            "## Routing audit\npackages: 1\n"
            f"{DIRECT_BRAIN_LABOUR}"
            f"- WP1 | receipt: broker:{rid} | "
            "native-unavailable: native worker failed to start twice\n"
        )
        with mock.patch.dict(sys.modules, {"agent_broker_mcp": fake}):
            self.assertFalse(
                routing_gate._lookup_routing_audit_valid(
                    without_reason, "session-1", "codex"
                )
            )
            self.assertTrue(
                routing_gate._lookup_routing_audit_valid(with_reason, "session-1", "codex")
            )

    def test_pretool_allows_exact_limit_then_denies_next_call(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3):
            for index in range(3):
                self.assertEqual(
                    routing_gate.pre_tool_use(self.pre_payload(f"call-{index}")), {}
                )
            denied = routing_gate.pre_tool_use(self.pre_payload("call-3"))

        output = denied["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        reason = output["permissionDecisionReason"]
        # New message: still reports the exact count and allowance, and still
        # names the override command -- same guarantee, new wording.
        self.assertIn(
            "Routing gate: 3 direct brain labour calls since the last delegation or override",
            reason,
        )
        self.assertIn("the allowance is 3", reason)
        self.assertIn("routing-override", reason)
        state = routing_gate._read_state("session-1")
        self.assertEqual(state["direct_labour_counts"]["reads"], 3)

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

    def test_parallel_pretool_reservations_cannot_exceed_limit(self):
        # _update_state's per-session FileLock deliberately fails OPEN on
        # timeout (routing_gate.py:331-360): wedging the host session is a
        # worse failure than occasionally under-counting a reservation under
        # heavy concurrency. A call that loses the lock race is allowed AND
        # left uncounted, and every such fail-open is logged with
        # fail_open=True. Under 12-way parallelism against a limit of 3, a
        # small number of timed-out reservations can legitimately push
        # `allowed` above 3 -- that is not a bug, so this test must not pin
        # `allowed` to a fixed number (that pin is exactly what made this
        # test flaky). What must still hold, and what this test enforces, is
        # the actual security property: nothing is silently dropped
        # (allowed + denied == total), the *counted* reservations never
        # exceed the limit, and every allow beyond the limit is a real,
        # logged fail-open -- an unexplained extra allow still fails this
        # test.
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3):
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(
                    pool.map(
                        routing_gate.pre_tool_use,
                        [self.pre_payload(f"parallel-{index}") for index in range(12)],
                    )
                )

        allowed = sum(result == {} for result in results)
        denied = sum(
            result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            for result in results
        )
        total_attempts = len(results)
        self.assertEqual(allowed + denied, total_attempts, "no attempt may be silently dropped")

        log_entries = self._read_log_entries()
        fail_open_count = sum(
            1
            for entry in log_entries
            if entry.get("event") == "PreToolUse" and entry.get("fail_open") is True
        )

        state = routing_gate._read_state("session-1")
        counted = int(state.get("direct_labour_count") or 0)
        reservations = len(state.get("direct_labour_reservations") or {})

        # The invariant that actually protects the gate: what got COUNTED
        # never exceeds the limit, ...
        self.assertLessEqual(counted, 3)
        self.assertLessEqual(reservations, 3)
        self.assertEqual(counted, reservations)
        # ...and every allow beyond the limit must be accounted for by a
        # logged fail-open -- not an unexplained extra allow.
        self.assertLessEqual(allowed, 3 + fail_open_count)
        self.assertEqual(allowed, counted + fail_open_count)

    def test_pretool_fail_open_is_allowed_and_logged(self):
        # Force the lock-timeout fail-open path directly (routing_gate.py
        # reserve_direct_labour -> _update_state) instead of relying on
        # timing, so this behaviour is deterministically tested rather than
        # only incidentally observed under the parallel-race test above.
        with mock.patch.object(routing_gate, "_update_state", return_value=None):
            result = routing_gate.pre_tool_use(self.pre_payload("fail-open-call"))

        self.assertEqual(result, {})
        entries = self._read_log_entries()
        fail_open_entries = [
            entry
            for entry in entries
            if entry.get("event") == "PreToolUse"
            and entry.get("decision") == "allow"
            and entry.get("fail_open") is True
        ]
        self.assertEqual(len(fail_open_entries), 1)

    def test_same_tool_use_id_is_idempotent(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            payload = self.pre_payload("same-call")
            self.assertEqual(routing_gate.pre_tool_use(payload), {})
            self.assertEqual(routing_gate.pre_tool_use(payload), {})
            denied = routing_gate.pre_tool_use(self.pre_payload("different-call"))

        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        state = routing_gate._read_state("session-1")
        self.assertEqual(state["direct_labour_count"], 1)
        self.assertEqual(state["direct_labour_counts"]["reads"], 1)

    def test_active_codex_cheap_role_exempts_parent_session_calls(self):
        native = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "codex-worker-active",
            "agent_type": "worker",
        }
        routing_gate.subagent_start(native)
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for index in range(3):
                self.assertEqual(
                    routing_gate.pre_tool_use(self.pre_payload(f"worker-{index}")), {}
                )

        self.assertNotIn("direct_labour_count", routing_gate._read_state("session-1"))

    def test_claude_requires_cheap_agent_identity_for_exemption(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            cheap = self.pre_payload(
                "cheap-call", host="claude", agent_id="claude-reader", agent_type="Explore"
            )
            self.assertEqual(routing_gate.pre_tool_use(cheap), {})
            brain = self.pre_payload(
                "brain-call", host="claude", agent_id="claude-brain", agent_type="general-purpose"
            )
            self.assertEqual(routing_gate.pre_tool_use(brain), {})
            denied = routing_gate.pre_tool_use(
                self.pre_payload(
                    "brain-call-2",
                    host="claude",
                    agent_id="claude-brain",
                    agent_type="general-purpose",
                )
            )

        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            routing_gate._read_state("session-1")["direct_labour_count"], 1
        )

    def test_native_start_opens_only_one_new_bounded_block(self):
        native = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "reader-once",
            "agent_type": "explorer",
        }
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 2):
            for index in range(2):
                self.assertEqual(routing_gate.pre_tool_use(self.pre_payload(f"before-{index}")), {})
            self.assertNotEqual(routing_gate.pre_tool_use(self.pre_payload("before-denied")), {})
            routing_gate.subagent_start(native)
            routing_gate.subagent_stop(native)
            for index in range(2):
                self.assertEqual(routing_gate.pre_tool_use(self.pre_payload(f"after-{index}")), {})
            denied = routing_gate.pre_tool_use(self.pre_payload("after-denied"))

        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        state = routing_gate._read_state("session-1")
        self.assertEqual(state["direct_labour_count"], 4)
        self.assertEqual(state["direct_labour_since_relief"], 2)
        self.assertEqual(state["native_agents"]["reader-once"]["status"], "completed")

    def test_switchboard_controls_are_exempt_but_research_mcp_counts(self):
        controls = (
            "consult_codex",
            "consult_claude",
            "queue_codex_request",
            "queue_claude_request",
            "request_status",
            "request_result",
        )
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for index, suffix in enumerate(controls):
                payload = self.pre_payload(
                    f"control-{index}", tool_name=f"mcp__agent_switchboard__{suffix}"
                )
                self.assertEqual(routing_gate.pre_tool_use(payload), {})
            research = self.pre_payload("research", tool_name="mcp__market__research")
            self.assertEqual(routing_gate.pre_tool_use(research), {})
            denied = routing_gate.pre_tool_use(
                self.pre_payload("research-2", tool_name="mcp__market__research")
            )

        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        state = routing_gate._read_state("session-1")
        self.assertEqual(state["direct_labour_counts"], {"evidence": 1})

    def test_direct_agy_shell_invocations_are_hard_denied_for_codex_and_claude(self):
        commands = (
            ("Bash", "agy --print 'inspect this'"),
            ("PowerShell", "agy.exe --version"),
            ("PowerShell", r"& 'C:\\Program Files\\Antigravity\\agy.exe' --print task"),
            ("Bash", "git status && /usr/local/bin/agy --print task"),
            ("Bash", "echo input | sudo agy --output-format json"),
            ("PowerShell", 'Start-Process -FilePath "C:\\tools\\agy.exe" -ArgumentList "--print"'),
        )
        for host in ("codex", "claude"):
            for index, (tool_name, command) in enumerate(commands):
                with self.subTest(host=host, tool=tool_name, command=command):
                    payload = self.pre_payload(
                        f"direct-agy-{host}-{index}", tool_name=tool_name, host=host
                    )
                    payload["tool_input"] = {"command": command}
                    result = routing_gate.pre_tool_use(payload)
                    output = result["hookSpecificOutput"]
                    self.assertEqual(output["permissionDecision"], "deny")
                    self.assertIn("MCP route_agent_task", output["permissionDecisionReason"])
                    self.assertIn('surface="cli"', output["permissionDecisionReason"])

    def test_agy_prose_paths_and_switchboard_mcp_are_not_blocked(self):
        non_invocations = (
            "Write-Output 'Use agy --print for this example'",
            r"Test-Path C:\\tools\\agy.exe",
            r"Get-Item C:\\tools\\agy.exe",
            "$tool = 'agy'; Write-Output $tool",
            "python explain.py --example 'agy --print task'",
            "Write-Output C:\\docs\\agy-notes.md",
        )
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 20):
            for index, command in enumerate(non_invocations):
                with self.subTest(command=command):
                    payload = self.pre_payload(
                        f"agy-prose-{index}", tool_name="PowerShell", host="claude"
                    )
                    payload["tool_input"] = {"command": command}
                    self.assertEqual(routing_gate.pre_tool_use(payload), {})

        switchboard = self.pre_payload(
            "switchboard-agy",
            tool_name="mcp__agent_switchboard__route_agent_task",
            host="claude",
        )
        switchboard["tool_input"] = {
            "target_agent": "antigravity",
            "surface": "cli",
            "prompt": "bounded package",
        }
        self.assertEqual(routing_gate.pre_tool_use(switchboard), {})

    def test_direct_agy_denied_even_without_host_field(self):
        # The direct-agy deny used to require `_switchboard_host` in {codex, claude};
        # it is now host-independent, so an unrecognized/absent host still gets denied.
        payload = self.pre_payload("backend-agy", tool_name="PowerShell", host="backend")
        payload["tool_input"] = {"command": "agy --print internal-package"}
        result = routing_gate.pre_tool_use(payload)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("MCP route_agent_task", output["permissionDecisionReason"])
        self.assertIn('surface="cli"', output["permissionDecisionReason"])

    def test_direct_agy_denied_without_host_field(self):
        # Same host-independence guarantee, but with `_switchboard_host` absent
        # entirely rather than set to an unrecognized value.
        payload = self.pre_payload("no-host-agy", tool_name="PowerShell")
        del payload["_switchboard_host"]
        payload["tool_input"] = {"command": "agy --print x"}
        result = routing_gate.pre_tool_use(payload)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("MCP route_agent_task", output["permissionDecisionReason"])
        self.assertIn('surface="cli"', output["permissionDecisionReason"])

    def test_oversized_mcp_is_counted_once_by_pretool_before_posttool(self):
        payload = self.pre_payload("large-mcp", tool_name="mcp__market__research")
        payload["tool_response"] = "raw-evidence-" * 30
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3), mock.patch.object(
            routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 100
        ):
            self.assertEqual(routing_gate.pre_tool_use(payload), {})
            replaced = routing_gate.post_tool_use(payload)

        self.assertEqual(replaced.get("decision"), "block")
        state = routing_gate._read_state("session-1")
        self.assertEqual(state["direct_labour_counts"], {"evidence": 1})
        self.assertEqual(state["direct_labour_count"], 1)

    def test_pretool_state_or_lock_failure_fails_open(self):
        with mock.patch.object(routing_gate, "_update_state", return_value=None):
            self.assertEqual(
                routing_gate.pre_tool_use(self.pre_payload("lock-failure")), {}
            )

    def test_registered_override_resets_one_block_and_requires_matching_audit_reason(self):
        reason = "architecture boundary requires direct brain inspection"
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("before")), {})
            self.assertNotEqual(routing_gate.pre_tool_use(self.pre_payload("blocked")), {})
            self.assertTrue(routing_gate.register_brain_override("session-1", "WP2", reason))
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("after")), {})
            self.assertNotEqual(routing_gate.pre_tool_use(self.pre_payload("blocked-again")), {})

        exact = (
            "## Routing audit\npackages: 1\n"
            "direct-brain-labour: reads=2 | searches=0 | evidence=0 | "
            "tests=0 | docs=0 | other=0\n"
            f"- WP2 | direct=reads | override: brain - WP2: {reason}\n"
        )
        wrong = exact.replace(reason, "different architecture reason is claimed here")
        self.assertFalse(routing_gate._lookup_routing_audit_valid(wrong, "session-1"))
        self.assertTrue(routing_gate._lookup_routing_audit_valid(exact, "session-1"))

    def test_routing_override_validation_and_shell_classification(self):
        self.assertFalse(routing_gate.register_brain_override("", "WP1", "valid long reason"))
        self.assertFalse(
            routing_gate.register_brain_override("session-1", "bad package", "valid long reason")
        )
        self.assertFalse(routing_gate.register_brain_override("session-1", "WP1", "short"))

        generated = routing_gate._routing_override_command("session-1")
        self.assertIsNone(
            routing_gate._direct_labour_category("Bash", {"command": generated})
        )
        unrelated = "Write-Output '# routing-override is documented here'"
        self.assertEqual(
            routing_gate._direct_labour_category("Bash", {"command": unrelated}), "other"
        )

    def test_audit_cannot_understate_observed_direct_labour_floor(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 3):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("read-1")), {})
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("read-2")), {})
        understated = (
            "## Routing audit\npackages: 1\n"
            "direct-brain-labour: reads=1 | searches=0 | evidence=0 | "
            "tests=0 | docs=0 | other=0\n"
            "- WP1 | direct=reads | override: brain - WP1: architecture risk retained here\n"
        )
        accurate = understated.replace("reads=1", "reads=2")
        self.assertFalse(routing_gate._lookup_routing_audit_valid(understated, "session-1"))
        self.assertTrue(routing_gate._lookup_routing_audit_valid(accurate, "session-1"))

    def test_unverified_receipt_blocks(self):
        routing_gate.mark_mutated("session-1")
        rid = "e53e5d2b-dcb7-4e2d-8c03-20009a336399"
        message = (
            f"## Routing audit\npackages: 1\n{DIRECT_BRAIN_LABOUR}"
            f"- WP1 | receipt: broker:{rid} | verified\n"
        )
        fake = types.SimpleNamespace(
            request_status=lambda _rid: {
                "found": True,
                "answered": True,
                "state": "completed",
                "model_attested": False,
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
        message = (
            f"## Routing audit\npackages: 1\n{DIRECT_BRAIN_LABOUR}"
            f"- WP1 | receipt: broker:{rid} | verified\n"
        )
        fake = types.SimpleNamespace(request_status=mock.Mock(side_effect=RuntimeError("down")))
        with mock.patch.object(routing_gate, "_ledger_reachable", return_value=True), mock.patch.dict(
            sys.modules, {"agent_broker_mcp": fake}
        ):
            self.assertEqual(routing_gate.stop(self.payload(message)), {})

    def test_oversized_mcp_codex_response_is_quarantined_and_replaced(self):
        secret = "raw-provider-evidence-" * 20
        payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "call-1",
            "tool_name": "mcp__market__report",
            "tool_input": {"fields": ["price", "timestamp"], "limit": 5},
            "tool_response": {"rows": secret},
            "_switchboard_host": "codex",
        }
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 100):
            result = routing_gate.post_tool_use(payload)

        self.assertEqual(result.get("decision"), "block")
        self.assertIn("quarantined", result.get("reason", ""))
        evidence_files = list(self.evidence_dir.glob("*.json"))
        self.assertEqual(len(evidence_files), 1)
        record = json.loads(evidence_files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["tool_input"], payload["tool_input"])
        self.assertEqual(record["tool_name"], "mcp__market__report")
        self.assertGreater(record["response_chars"], 100)
        self.assertIn(secret, record["tool_response_serialized"])

    def test_oversized_mcp_claude_response_uses_updated_output_without_raw_leak(self):
        secret = "never-return-this-raw-payload-" * 20
        payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "call-2",
            "tool_name": "mcp__ledger__query",
            "tool_input": {"projection": ["state"]},
            "tool_response": secret,
            "_switchboard_host": "claude",
        }
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 100):
            result = routing_gate.post_tool_use(payload)

        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PostToolUse")
        self.assertIn("updatedToolOutput", output)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(len(list(self.evidence_dir.glob("*.json"))), 1)

    def test_mcp_response_at_or_below_ingress_cap_is_unchanged(self):
        payload = {
            "session_id": "session-1",
            "tool_name": "mcp__ledger__query",
            "tool_input": {"projection": ["state"]},
            "tool_response": "short result",
            "_switchboard_host": "codex",
        }
        with mock.patch.object(routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 100):
            self.assertEqual(routing_gate.post_tool_use(payload), {})
        self.assertFalse(self.evidence_dir.exists())

    def test_evidence_storage_failure_preserves_original_tool_result(self):
        payload = {
            "session_id": "session-1",
            "tool_name": "mcp__ledger__query",
            "tool_input": {"projection": ["state"]},
            "tool_response": "x" * 200,
            "_switchboard_host": "codex",
        }
        with mock.patch.object(
            routing_gate, "CONTEXT_INGRESS_MAX_CHARS", 100
        ), mock.patch.object(routing_gate, "_store_context_evidence", return_value=None):
            self.assertEqual(routing_gate.post_tool_use(payload), {})

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

        exec_form = {
            "type": "command",
            "command": r"C:\Agent Switchboard\agent-switchboard.exe",
            "args": ["routing-hook", "Stop", "agent-switchboard", "claude"],
        }
        self.assertTrue(routing_gate.is_owned_hook_entry(exec_form))
        self.assertEqual(
            routing_gate.remove_owned_hook_entries([existing[0], exec_form]), existing
        )

    def test_cli_always_emits_json_on_bad_input(self):
        with mock.patch("sys.stdin.read", return_value="not-json"), mock.patch(
            "sys.stdout.write"
        ) as write:
            self.assertEqual(routing_gate.main(["UnknownEvent"]), 0)
        json.loads(write.call_args.args[0])

    def test_normalize_tool_name_canonicalizes_server_segment_only(self):
        self.assertEqual(
            routing_gate.normalize_tool_name(
                "mcp__agent-switchboard__route_agent_task"
            ),
            "mcp__agent_switchboard__route_agent_task",
        )
        # A hyphen in the TOOL segment survives -- only the server segment is
        # canonicalized, never a global hyphen/underscore replacement.
        self.assertEqual(
            routing_gate.normalize_tool_name("mcp__market-data__get-quote"),
            "mcp__market_data__get-quote",
        )
        self.assertEqual(routing_gate.normalize_tool_name("Read"), "read")
        self.assertEqual(routing_gate.normalize_tool_name("PowerShell"), "powershell")
        # Total: never raises on missing/extra `__` separators.
        self.assertEqual(routing_gate.normalize_tool_name("mcp__onlyserver"), "mcp__onlyserver")
        self.assertEqual(routing_gate.normalize_tool_name("mcp__"), "mcp__")
        self.assertEqual(routing_gate.normalize_tool_name(""), "")
        self.assertEqual(routing_gate.normalize_tool_name(None), "")
        self.assertEqual(
            routing_gate.normalize_tool_name("mcp__srv__tool__with__dunders"),
            "mcp__srv__tool__with__dunders",
        )

    def test_hyphenated_switchboard_control_is_exempt(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            hyphenated = self.pre_payload(
                "hyphen-control", tool_name="mcp__agent-switchboard__consult_codex"
            )
            underscore = self.pre_payload(
                "underscore-control", tool_name="mcp__agent_switchboard__consult_codex"
            )
            self.assertEqual(routing_gate.pre_tool_use(hyphenated), {})
            self.assertEqual(routing_gate.pre_tool_use(underscore), {})
        self.assertIsNone(
            routing_gate._direct_labour_category(
                "mcp__agent-switchboard__consult_codex", {}
            )
        )
        self.assertIsNone(
            routing_gate._direct_labour_category(
                "mcp__agent_switchboard__consult_codex", {}
            )
        )
        # Neither spelling consumed any direct-labour allowance.
        self.assertNotIn("direct_labour_count", routing_gate._read_state("session-1"))

    def test_powershell_classified_as_shell_labour(self):
        category = routing_gate._direct_labour_category(
            "PowerShell", {"command": "Get-ChildItem"}
        )
        self.assertIsNotNone(category)
        self.assertFalse(
            routing_gate._is_mutating("PowerShell", {"command": "Get-ChildItem"})
        )
        self.assertTrue(
            routing_gate._is_mutating(
                "PowerShell", {"command": "Set-Content file.txt value"}
            )
        )

    def test_gate_decision_log_records_no_arguments(self):
        secret = "sk-super-secret-token-do-not-log-12345"
        payload = self.pre_payload(
            "secret-call", tool_name="Read", host="claude"
        )
        payload["tool_input"] = {"path": "source.py", "command": secret}
        routing_gate.pre_tool_use(payload)

        log_path = routing_gate.STATE_DIR / "session-1.log.jsonl"
        self.assertTrue(log_path.exists())
        content = log_path.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        self.assertNotIn(secret, content)
        record = json.loads(lines[0])
        self.assertIn("decision", record)
        self.assertIn("tool", record)
        self.assertIn("category", record)

    def test_routing_report_cli_runs_on_empty_session(self):
        with mock.patch("sys.stdout.write") as write:
            result = routing_gate.routing_report_cli(["--session", "no-such-session"])
        self.assertEqual(result, 0)
        self.assertTrue(write.called)

    def test_routing_report_cli_last_n_runs_without_raising(self):
        with mock.patch("sys.stdout.write"):
            self.assertEqual(routing_gate.routing_report_cli(["--last", "3"]), 0)
            self.assertEqual(routing_gate.routing_report_cli([]), 0)

    def test_direct_labour_limit_default_is_four(self):
        self.assertEqual(routing_gate.DIRECT_LABOUR_LIMIT_DEFAULT, 4)

    def test_policy_is_relaxed_flags_weaker_config(self):
        # policy_is_relaxed() is the "no silent disable" property: it flags a
        # weaker-than-shipped configuration (env override above the default, or
        # warn mode) rather than effective_direct_labour_limit() silently
        # clamping it -- the owner-set value is honoured, but loudly.
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", routing_gate.DIRECT_LABOUR_LIMIT_DEFAULT):
            with mock.patch.dict("os.environ", {routing_gate.GATE_MODE_ENV: "enforce"}):
                self.assertFalse(routing_gate.policy_is_relaxed())
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", routing_gate.DIRECT_LABOUR_LIMIT_DEFAULT + 1):
            with mock.patch.dict("os.environ", {routing_gate.GATE_MODE_ENV: "enforce"}):
                self.assertTrue(routing_gate.policy_is_relaxed())
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", routing_gate.DIRECT_LABOUR_LIMIT_DEFAULT):
            with mock.patch.dict("os.environ", {routing_gate.GATE_MODE_ENV: "warn"}):
                self.assertTrue(routing_gate.policy_is_relaxed())

    def test_warn_mode_allows_but_logs(self):
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 2), mock.patch.dict(
            "os.environ", {routing_gate.GATE_MODE_ENV: "warn"}
        ):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("warn-1")), {})
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("warn-2")), {})
            # Past the allowance: warn mode still allows the call...
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("warn-3")), {})
        log_path = routing_gate.STATE_DIR / "session-1.log.jsonl"
        entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        warn_entries = [e for e in entries if e.get("decision") == "warn"]
        self.assertTrue(warn_entries, "expected a 'warn' decision to be logged")
        self.assertTrue(any(e.get("would_deny") for e in warn_entries))

    def _seed_ledger(self, request_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE consultations (request_id TEXT)")
            conn.execute("INSERT INTO consultations (request_id) VALUES (?)", (request_id,))
            conn.commit()
        finally:
            conn.close()

    def _dispatch_response_payload(self, tool_use_id: str, receipt: str, outcome: str, work_package: str):
        return {
            "session_id": "session-1",
            "tool_use_id": tool_use_id,
            "tool_name": routing_gate.CREDITABLE_DISPATCH_TOOL,
            "tool_input": {"target_agent": "antigravity", "surface": "cli"},
            "tool_response": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "receipt": receipt,
                                "outcome": outcome,
                                "work_package_id": work_package,
                            }
                        ),
                    }
                ]
            },
            "_switchboard_host": "claude",
        }

    def test_credit_granted_for_completed_verified_receipt(self):
        request_id = str(uuid.uuid4())
        self._seed_ledger(request_id)
        receipt = f"broker:{request_id}"

        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 2):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("credit-1")), {})
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("credit-2")), {})
            denied = routing_gate.pre_tool_use(self.pre_payload("credit-3"))
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

            credit_result = routing_gate.post_tool_use(
                self._dispatch_response_payload("dispatch-1", receipt, "completed_verified", "WP-X")
            )
            self.assertIn(
                receipt,
                credit_result["hookSpecificOutput"]["additionalContext"],
            )

            allowed = routing_gate.pre_tool_use(self.pre_payload("credit-4"))
            self.assertEqual(allowed, {})

    def test_credit_is_single_use(self):
        request_id = str(uuid.uuid4())
        self._seed_ledger(request_id)
        receipt = f"broker:{request_id}"

        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("su-1")), {})
            routing_gate.post_tool_use(
                self._dispatch_response_payload("dispatch-2", receipt, "completed_verified", "WP-X")
            )
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("su-2")), {})

            # Replaying the same receipt a second time buys nothing: no fresh
            # "Routing credit" grant (a checkpoint nudge may still appear).
            replay = routing_gate.post_tool_use(
                self._dispatch_response_payload("dispatch-3", receipt, "completed_verified", "WP-X")
            )
            self.assertNotIn("Routing credit", json.dumps(replay))

            denied = routing_gate.pre_tool_use(self.pre_payload("su-3"))
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_credit_requires_ledger_row(self):
        # Well-formed receipt (valid UUID shape) but never written to the ledger:
        # _receipt_resolves() must fail closed and grant no credit.
        forged_receipt = f"broker:{uuid.uuid4()}"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE consultations (request_id TEXT)")
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            self.assertEqual(routing_gate.pre_tool_use(self.pre_payload("nl-1")), {})
            result = routing_gate.post_tool_use(
                self._dispatch_response_payload("dispatch-4", forged_receipt, "completed_verified", "WP-X")
            )
            self.assertNotIn("Routing credit", json.dumps(result))

            denied = routing_gate.pre_tool_use(self.pre_payload("nl-2"))
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
