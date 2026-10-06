"""Unit tests for Switchboard code_graph tool exemption in routing_gate.py.

Covers WP-CG3 acceptance criteria:
- code_graph with op in {locate, expand, path, stats, health} is not counted.
- code_graph with missing op is not counted.
- code_graph with op == 'refresh' is counted as evidence.
- each recognised host name form is supported.
- code_graph locate never grants relief and never resets direct_labour_since_relief.
- existing exempt probes (e.g. retrieve_shared_context) remain exempt.
- plain Grep is still counted.
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

import routing_gate  # noqa: E402


class CodeGraphGateTests(unittest.TestCase):
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

        # Ensure environment does not enforce child-guard during brain gate tests
        self.env_patch = mock.patch.dict(os.environ, clear=False)
        self.env_patch.start()
        os.environ.pop("AGENT_BROKER_CHILD", None)
        self.addCleanup(self.env_patch.stop)

    @staticmethod
    def pre_payload(tool_use_id: str, tool_name: str, tool_input: dict | None = None, host: str = "codex", **extra):
        payload = {
            "session_id": "session-1",
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input if tool_input is not None else {},
            "_switchboard_host": host,
        }
        payload.update(extra)
        return payload

    def test_code_graph_locate_not_counted(self):
        """code_graph with op == 'locate' is exempt and does not consume labour allowance."""
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for i in range(5):
                payload = self.pre_payload(
                    f"locate-{i}",
                    "mcp__agent_switchboard__code_graph",
                    {"op": "locate", "symbol": "foo"},
                )
                result = routing_gate.pre_tool_use(payload)
                self.assertEqual(result, {}, f"call {i} should be allowed")

        self.assertIsNone(
            routing_gate._direct_labour_category(
                "mcp__agent_switchboard__code_graph", {"op": "locate"}
            )
        )
        state = routing_gate._read_state("session-1")
        self.assertEqual(int(state.get("direct_labour_count") or 0), 0)
        self.assertEqual(int(state.get("direct_labour_since_relief") or 0), 0)

    def test_code_graph_all_exempt_ops_not_counted(self):
        """All exempt ops: locate, expand, path, stats, health are non-labour probes."""
        exempt_ops = ["locate", "expand", "path", "stats", "health"]
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for op in exempt_ops:
                with self.subTest(op=op):
                    self.assertIsNone(
                        routing_gate._direct_labour_category(
                            "mcp__agent_switchboard__code_graph", {"op": op}
                        )
                    )
                    payload = self.pre_payload(
                        f"op-{op}",
                        "mcp__agent_switchboard__code_graph",
                        {"op": op},
                    )
                    result = routing_gate.pre_tool_use(payload)
                    self.assertEqual(result, {})

        state = routing_gate._read_state("session-1")
        self.assertEqual(int(state.get("direct_labour_count") or 0), 0)

    def test_code_graph_op_missing_not_counted(self):
        """When op is missing or empty, code_graph is treated as an exempt probe."""
        inputs = [
            {},
            {"path": "source.py"},
            {"query": "my_function"},
            {"op": None},
            {"op": ""},
            {"op": "   "},
        ]
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for idx, tool_input in enumerate(inputs):
                with self.subTest(tool_input=tool_input):
                    self.assertIsNone(
                        routing_gate._direct_labour_category(
                            "mcp__agent_switchboard__code_graph", tool_input
                        )
                    )
                    payload = self.pre_payload(
                        f"missing-op-{idx}",
                        "mcp__agent_switchboard__code_graph",
                        tool_input,
                    )
                    self.assertEqual(routing_gate.pre_tool_use(payload), {})

        # Also test with tool_input=None directly to _direct_labour_category
        self.assertIsNone(
            routing_gate._direct_labour_category(
                "mcp__agent_switchboard__code_graph", None
            )
        )
        state = routing_gate._read_state("session-1")
        self.assertEqual(int(state.get("direct_labour_count") or 0), 0)

    def test_code_graph_refresh_counted(self):
        """code_graph with op == 'refresh' is NOT exempt; classified as evidence and counted."""
        self.assertEqual(
            routing_gate._direct_labour_category(
                "mcp__agent_switchboard__code_graph", {"op": "refresh"}
            ),
            "evidence",
        )
        # Case insensitive test
        self.assertEqual(
            routing_gate._direct_labour_category(
                "mcp__agent_switchboard__code_graph", {"op": "REFRESH"}
            ),
            "evidence",
        )

        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            first = self.pre_payload(
                "refresh-1",
                "mcp__agent_switchboard__code_graph",
                {"op": "refresh"},
            )
            self.assertEqual(routing_gate.pre_tool_use(first), {})

            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_counts"), {"evidence": 1})
            self.assertEqual(state.get("direct_labour_count"), 1)
            self.assertEqual(state.get("direct_labour_since_relief"), 1)

            # Second call must be denied as limit is 1
            second = self.pre_payload(
                "refresh-2",
                "mcp__agent_switchboard__code_graph",
                {"op": "refresh"},
            )
            denied = routing_gate.pre_tool_use(second)
            self.assertEqual(
                denied.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
            )
            self.assertIn(
                "direct labour calls",
                denied.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""),
            )

    def test_recognised_host_name_forms(self):
        """Both Codex (underscore) and Claude (hyphen) Switchboard namespace forms match."""
        host_forms = [
            ("codex", "mcp__agent_switchboard__code_graph"),
            ("claude", "mcp__agent-switchboard__code_graph"),
            ("codex-hyphen-tool", "mcp__agent_switchboard__code-graph"),
            ("claude-hyphen-tool", "mcp__agent-switchboard__code-graph"),
        ]
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for label, tool_name in host_forms:
                with self.subTest(label=label, tool_name=tool_name):
                    # Locate is exempt across all host forms
                    self.assertIsNone(
                        routing_gate._direct_labour_category(
                            tool_name, {"op": "locate"}
                        )
                    )
                    payload_locate = self.pre_payload(
                        f"locate-{label}", tool_name, {"op": "locate"}
                    )
                    self.assertEqual(routing_gate.pre_tool_use(payload_locate), {})

                    # Missing op is exempt across all host forms
                    self.assertIsNone(
                        routing_gate._direct_labour_category(tool_name, {})
                    )
                    payload_missing = self.pre_payload(
                        f"missing-{label}", tool_name, {}
                    )
                    self.assertEqual(routing_gate.pre_tool_use(payload_missing), {})

                    # Refresh is classified as evidence across all host forms
                    self.assertEqual(
                        routing_gate._direct_labour_category(
                            tool_name, {"op": "refresh"}
                        ),
                        "evidence",
                    )

    def test_no_relief_granted_counter_unchanged_after_locate(self):
        """code_graph locate never grants relief and never resets direct_labour_since_relief."""
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 5):
            # 1. Perform a direct labour read call
            read_payload = self.pre_payload("read-1", "Read", {"path": "foo.py"})
            self.assertEqual(routing_gate.pre_tool_use(read_payload), {})
            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_since_relief"), 1)
            self.assertEqual(state.get("direct_labour_count"), 1)
            relief_seq_before = int(state.get("labour_relief_sequence") or 0)

            # 2. Invoke code_graph locate
            locate_payload = self.pre_payload(
                "locate-probe",
                "mcp__agent_switchboard__code_graph",
                {"op": "locate", "symbol": "foo"},
            )
            self.assertEqual(routing_gate.pre_tool_use(locate_payload), {})

            # 3. Verify counter is unchanged: still 1, total still 1, relief seq unchanged
            state_after = routing_gate._read_state("session-1")
            self.assertEqual(
                state_after.get("direct_labour_since_relief"), 1,
                "direct_labour_since_relief must remain unchanged after code_graph probe"
            )
            self.assertEqual(
                state_after.get("direct_labour_count"), 1,
                "direct_labour_count must not increment on code_graph probe"
            )
            self.assertEqual(
                int(state_after.get("labour_relief_sequence") or 0), relief_seq_before,
                "labour_relief_sequence must not increase (no relief granted)"
            )

    def test_no_relief_granted_following_a_relief(self):
        """Counter remains unchanged when code_graph locate is called following a relief."""
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 5):
            # 1. Accumulate some labour
            for i in range(2):
                self.assertEqual(
                    routing_gate.pre_tool_use(self.pre_payload(f"r-{i}", "Read", {"path": "f"})),
                    {},
                )
            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_since_relief"), 2)

            # 2. Grant actual relief via cheap native subagent start
            native = {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "agent_id": "worker-1",
                "agent_type": "worker",
            }
            routing_gate.subagent_start(native)
            routing_gate.subagent_stop(native)
            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_since_relief"), 0)
            self.assertEqual(int(state.get("labour_relief_sequence") or 0), 1)

            # 3. Perform 1 labour call after relief
            self.assertEqual(
                routing_gate.pre_tool_use(self.pre_payload("post-relief-read", "Read", {"path": "f"})),
                {},
            )
            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_since_relief"), 1)

            # 4. Invoke code_graph locate
            locate_payload = self.pre_payload(
                "locate-after-relief",
                "mcp__agent_switchboard__code_graph",
                {"op": "locate"},
            )
            self.assertEqual(routing_gate.pre_tool_use(locate_payload), {})

            # 5. Counter must STILL be 1, relief sequence must STILL be 1
            state_after = routing_gate._read_state("session-1")
            self.assertEqual(
                state_after.get("direct_labour_since_relief"), 1,
                "direct_labour_since_relief must stay 1; code_graph locate must not reset counter"
            )
            self.assertEqual(
                int(state_after.get("labour_relief_sequence") or 0), 1,
                "labour_relief_sequence must stay 1"
            )

    def test_existing_exempt_probe_still_exempt(self):
        """Existing Switchboard probes (retrieve_shared_context, run_evidence_probe, etc.) remain exempt."""
        probes = [
            ("mcp__agent_switchboard__retrieve_shared_context", {"ref": "c:1"}),
            ("mcp__agent-switchboard__retrieve_shared_context", {"ref": "c:1"}),
            ("mcp__agent_switchboard__run_evidence_probe", {"probe": "git_status"}),
            ("mcp__agent_switchboard__request_status", {"request_id": "r1"}),
            ("mcp__agent_switchboard__request_result", {"request_id": "r1"}),
            ("mcp__agent_switchboard__compact_topic", {"topic": "t1"}),
            ("mcp__agent_switchboard__resolve_model_request", {"request": "m"}),
            ("mcp__agent_switchboard__get_topic_status", {}),
            ("mcp__agent_switchboard__list_live_surfaces", {}),
        ]
        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            for idx, (tool_name, tool_input) in enumerate(probes):
                with self.subTest(tool=tool_name):
                    self.assertIsNone(
                        routing_gate._direct_labour_category(tool_name, tool_input)
                    )
                    payload = self.pre_payload(f"probe-{idx}", tool_name, tool_input)
                    self.assertEqual(routing_gate.pre_tool_use(payload), {})

        state = routing_gate._read_state("session-1")
        self.assertEqual(int(state.get("direct_labour_count") or 0), 0)

    def test_plain_grep_still_counted(self):
        """A plain Grep call is classified as searches and counted toward the allowance."""
        self.assertEqual(
            routing_gate._direct_labour_category("Grep", {"path": "src", "pattern": "foo"}),
            "searches",
        )
        self.assertEqual(
            routing_gate._direct_labour_category("grep", {"path": "src", "pattern": "foo"}),
            "searches",
        )

        with mock.patch.object(routing_gate, "DIRECT_LABOUR_LIMIT", 1):
            first = self.pre_payload("grep-1", "Grep", {"path": "src", "pattern": "foo"})
            self.assertEqual(routing_gate.pre_tool_use(first), {})

            state = routing_gate._read_state("session-1")
            self.assertEqual(state.get("direct_labour_counts"), {"searches": 1})
            self.assertEqual(state.get("direct_labour_count"), 1)
            self.assertEqual(state.get("direct_labour_since_relief"), 1)

            # Second Grep call exceeds the limit (1) and must be denied
            second = self.pre_payload("grep-2", "Grep", {"path": "src", "pattern": "bar"})
            denied = routing_gate.pre_tool_use(second)
            self.assertEqual(
                denied.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
            )
            self.assertIn(
                "searches call is blocked",
                denied.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""),
            )


if __name__ == "__main__":
    unittest.main()
