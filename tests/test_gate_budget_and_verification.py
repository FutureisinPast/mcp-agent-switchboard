"""Tests for budgeted overrides and verification reads in routing_gate.py."""
from __future__ import annotations

import io
import json
import os
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


class GateBudgetAndVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "routing-gate"
        self.evidence_dir = Path(self.tmp.name) / "context-evidence"
        self.db_path = Path(self.tmp.name) / "state.sqlite"
        self.state_patch = mock.patch.object(routing_gate, "STATE_DIR", self.state_dir)
        self.evidence_patch = mock.patch.object(routing_gate, "EVIDENCE_DIR", self.evidence_dir)
        self.db_patch = mock.patch.object(routing_gate, "DB_PATH", self.db_path)
        self.state_patch.start()
        self.evidence_patch.start()
        self.db_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.evidence_patch.stop)
        self.addCleanup(self.db_patch.stop)

    @staticmethod
    def pre_payload(session_id: str, tool_use_id: str, tool_name: str = "Read", tool_input: dict | None = None, host: str = "codex", **extra):
        payload = {
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input if tool_input is not None else {"path": "source.py"},
            "_switchboard_host": host,
        }
        payload.update(extra)
        return payload

    @staticmethod
    def post_payload(session_id: str, tool_use_id: str, tool_name: str, tool_response: object, tool_input: dict | None = None, host: str = "codex", **extra):
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

    def _seed_ledger(self, request_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE consultations (request_id TEXT)")
            conn.execute("INSERT INTO consultations (request_id) VALUES (?)", (request_id,))
            conn.commit()
        finally:
            conn.close()

    def test_override_with_budget_allows_and_then_denies(self):
        session_id = "session-budget-allows"
        ok = routing_gate.register_brain_override(
            session_id, "WP-TEST-8", "investigation reason at least twelve chars", budget=8
        )
        self.assertTrue(ok)
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), 8)

        for i in range(8):
            res = routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
            self.assertEqual(res, {})
        denied = routing_gate.pre_tool_use(
            self.pre_payload(session_id, "r-8", "Read", {"path": "src/8.py"})
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("direct_labour_since_relief"), 8)
        self.assertEqual(state.get("labour_gate_denials"), 1)

    def test_budget_clamped_to_max(self):
        session_id = "session-clamp-max"
        ok = routing_gate.register_brain_override(
            session_id, "WP-CLAMP-1", "investigation reason at least twelve chars", budget=20
        )
        self.assertTrue(ok)
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), 12)

        for i in range(12):
            res = routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
            self.assertEqual(res, {})
        denied = routing_gate.pre_tool_use(
            self.pre_payload(session_id, "r-12", "Read", {"path": "src/12.py"})
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        session_id_2 = "session-clamp-env"
        with mock.patch.dict(os.environ, {"AGENT_BROKER_OVERRIDE_MAX_BUDGET": "15"}):
            ok = routing_gate.register_brain_override(
                session_id_2, "WP-CLAMP-2", "investigation reason at least twelve chars", budget=25
            )
            self.assertTrue(ok)
            state = routing_gate._read_state(session_id_2)
            self.assertEqual(state.get("relief_budget"), 15)

    def test_budget_below_default_raised_to_default(self):
        session_id = "session-clamp-min"
        default_limit = routing_gate.effective_direct_labour_limit()
        ok = routing_gate.register_brain_override(
            session_id, "WP-CLAMP-MIN", "investigation reason at least twelve chars", budget=2
        )
        self.assertTrue(ok)
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), default_limit)

        for i in range(default_limit):
            res = routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
            self.assertEqual(res, {})
        denied = routing_gate.pre_tool_use(
            self.pre_payload(session_id, "r-denied", "Read", {"path": "src/denied.py"})
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_plain_override_and_dispatch_credit_restore_default_limit(self):
        session_id = "session-restore-plain"
        routing_gate.register_brain_override(
            session_id, "WP-BUDGET", "investigation reason at least twelve chars", budget=8
        )
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), 8)

        routing_gate.register_brain_override(
            session_id, "WP-PLAIN", "investigation reason at least twelve chars", budget=None
        )
        state = routing_gate._read_state(session_id)
        self.assertNotIn("relief_budget", state)

        default_limit = routing_gate.effective_direct_labour_limit()
        for i in range(default_limit):
            res = routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
            self.assertEqual(res, {})
        denied = routing_gate.pre_tool_use(
            self.pre_payload(session_id, "r-denied", "Read", {"path": "src/denied.py"})
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        session_id_cred = "session-restore-credit"
        routing_gate.register_brain_override(
            session_id_cred, "WP-BUDGET", "investigation reason at least twelve chars", budget=8
        )
        req_id = str(uuid.uuid4())
        self._seed_ledger(req_id)
        dispatch_payload = {
            "session_id": session_id_cred,
            "tool_use_id": "dispatch-1",
            "tool_name": routing_gate.CREDITABLE_DISPATCH_TOOL,
            "tool_input": {"target_agent": "antigravity", "surface": "cli"},
            "tool_response": {
                "receipt": f"broker:{req_id}",
                "outcome": routing_gate.CREDITABLE_OUTCOME,
                "work_package_id": "WP-FLASH-1",
            },
            "_switchboard_host": "codex",
        }
        result = routing_gate.post_tool_use(dispatch_payload)
        self.assertIn("Routing credit", str(result))
        state = routing_gate._read_state(session_id_cred)
        self.assertNotIn("relief_budget", state)

        # Cheap subagent start also clears relief_budget
        session_id_sub = "session-restore-subagent"
        routing_gate.register_brain_override(
            session_id_sub, "WP-BUDGET", "investigation reason at least twelve chars", budget=8
        )
        routing_gate.subagent_start({
            "session_id": session_id_sub,
            "agent_id": "sub-1",
            "agent_type": "explorer",
            "turn_id": "t1",
        })
        state = routing_gate._read_state(session_id_sub)
        self.assertNotIn("relief_budget", state)

    def test_cli_output_includes_budget(self):
        session_id = "session-cli-test"
        stdout_buf = io.StringIO()
        with mock.patch("sys.stdout", stdout_buf):
            code = routing_gate.routing_override_cli(
                ["--session", session_id, "--package", "WP-CLI-1", "--reason", "investigation reason at least twelve chars", "--budget", "9"]
            )
        self.assertEqual(code, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out, {"registered": True, "package": "WP-CLI-1", "budget": 9})

        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), 9)

        log_path = routing_gate._session_log_path(session_id)
        records = routing_gate._read_session_log(log_path)
        override_recs = [r for r in records if r.get("event") == "Override"]
        self.assertEqual(len(override_recs), 1)
        self.assertEqual(override_recs[0].get("budget"), 9)

        stdout_buf_2 = io.StringIO()
        with mock.patch("sys.stdout", stdout_buf_2):
            code = routing_gate.routing_override_cli(
                ["--session", session_id, "--package", "WP-CLI-2", "--reason", "investigation reason at least twelve chars"]
            )
        self.assertEqual(code, 0)
        out_2 = json.loads(stdout_buf_2.getvalue())
        self.assertEqual(out_2, {"registered": True, "package": "WP-CLI-2", "budget": 4})
        state = routing_gate._read_state(session_id)
        self.assertNotIn("relief_budget", state)

    def test_relief_budget_survives_reset_turn_state(self):
        session_id = "session-reset-turn"
        routing_gate.register_brain_override(
            session_id, "WP-VALID", "investigation reason at least twelve chars", budget=8
        )
        routing_gate.reset_turn_state(session_id)
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), 8)

        for invalid in ["8", True, False, 2, 20, None, [8], {"b": 8}]:
            def update(st: dict):
                st["relief_budget"] = invalid
            routing_gate._update_state(session_id, update)
            routing_gate.reset_turn_state(session_id)
            state = routing_gate._read_state(session_id)
            self.assertNotIn("relief_budget", state)

    def test_verification_read_exempted(self):
        session_id = "session-verify"
        response_data = {
            "status": "completed",
            "structured_output": {
                "research_coverage": [
                    {
                        "evidence": [
                            {"location": "pkg/mod.py"}
                        ]
                    }
                ],
                "claims": [
                    {
                        "evidence": ["other.py:L12-L18"]
                    }
                ],
            },
        }
        post_pl = self.post_payload(
            session_id,
            "call-res-1",
            "mcp__agent_switchboard__request_result",
            response_data,
        )
        routing_gate.post_tool_use(post_pl)

        state = routing_gate._read_state(session_id)
        cited = state.get("cited_paths", [])
        self.assertIn("pkg/mod.py", cited)
        self.assertIn("other.py", cited)

        read_pl = self.pre_payload(
            session_id,
            "read-1",
            "Read",
            {"file_path": "/tmp/x/pkg/mod.py"},
        )
        res = routing_gate.pre_tool_use(read_pl)
        self.assertEqual(res, {})
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("direct_labour_since_relief", 0), 0)

        grep_pl = self.pre_payload(
            session_id,
            "grep-1",
            "Grep",
            {"path": "/tmp/other.py"},
        )
        res = routing_gate.pre_tool_use(grep_pl)
        self.assertEqual(res, {})
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("direct_labour_since_relief", 0), 0)

        log_path = routing_gate._session_log_path(session_id)
        records = routing_gate._read_session_log(log_path)
        decisions = [r.get("decision") for r in records if r.get("event") == "PreToolUse"]
        self.assertEqual(decisions, ["verification-read", "verification-read"])

        read_uncited = self.pre_payload(
            session_id,
            "read-uncited",
            "Read",
            {"file_path": "/tmp/uncited.py"},
        )
        res = routing_gate.pre_tool_use(read_uncited)
        self.assertEqual(res, {})
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("direct_labour_since_relief", 0), 1)

        bash_pl = self.pre_payload(
            session_id,
            "bash-1",
            "Bash",
            {"command": "cat /tmp/x/pkg/mod.py"},
        )
        res = routing_gate.pre_tool_use(bash_pl)
        self.assertEqual(res, {})
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("direct_labour_since_relief", 0), 2)

    def test_cited_paths_capped_at_200_fifo_dedup(self):
        session_id = "session-fifo"
        paths_batch = [f"src/file_{i}.py" for i in range(250)]
        payload = self.post_payload(
            session_id,
            "call-applied-1",
            "request_result",
            {"status": "completed", "applied_files": paths_batch},
        )
        routing_gate.post_tool_use(payload)
        state = routing_gate._read_state(session_id)
        cited = state.get("cited_paths", [])
        self.assertEqual(len(cited), 200)
        self.assertEqual(cited[0], "src/file_50.py")
        self.assertEqual(cited[-1], "src/file_249.py")

        dup_payload = self.post_payload(
            session_id,
            "call-applied-2",
            "request_result",
            {"status": "completed", "applied_files": ["src/file_249.py", "src/file_100.py"]},
        )
        routing_gate.post_tool_use(dup_payload)
        state = routing_gate._read_state(session_id)
        cited = state.get("cited_paths", [])
        self.assertEqual(len(cited), 200)

        routing_gate.reset_turn_state(session_id)
        state = routing_gate._read_state(session_id)
        self.assertEqual(len(state.get("cited_paths", [])), 200)

    def test_malformed_tool_responses_ignored(self):
        session_id = "session-malformed"
        malformed_responses = [
            None,
            "not a json {",
            12345,
            {"structured_output": "not json {"},
            {"structured_output": {"research_coverage": "not a list"}},
            {"structured_output": {"claims": [{"evidence": [None, 123]}]}},
            {"applied_files": [None, 123]},
        ]
        for idx, resp in enumerate(malformed_responses):
            payload = self.post_payload(
                session_id,
                f"call-malformed-{idx}",
                "request_result",
                resp,
            )
            res = routing_gate.post_tool_use(payload)
            self.assertIsInstance(res, dict)

    def test_deny_and_checkpoint_wording_mentions_budget_and_free_verification(self):
        session_id = "session-wording-deny"
        for i in range(4):
            res = routing_gate.pre_tool_use(
                self.pre_payload(session_id, f"r-{i}", "Read", {"path": f"src/{i}.py"})
            )
            self.assertEqual(res, {})
        denied = routing_gate.pre_tool_use(
            self.pre_payload(session_id, "r-4", "Read", {"path": "src/4.py"})
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("--budget", reason)
        self.assertIn("reading lines cited by a delegated result is free", reason)

        session_id_chk = "session-wording-chk"
        checkpoint_result = None
        for i in range(4):
            routing_gate.pre_tool_use(
                self.pre_payload(session_id_chk, f"chk-pre-{i}", "Read", {"path": f"src/{i}.py"})
            )
            res = routing_gate.post_tool_use(
                self.pre_payload(session_id_chk, f"chk-pre-{i}", "Read", {"path": f"src/{i}.py"})
            )
            if res.get("hookSpecificOutput", {}).get("additionalContext"):
                checkpoint_result = res
        self.assertIsNotNone(checkpoint_result)
        context = checkpoint_result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("--budget", context)
        self.assertIn("reading lines cited by a delegated result is free", context)


if __name__ == "__main__":
    unittest.main()



def test_cited_directories_never_become_free_search_scopes():
    import routing_gate as _rg
    assert _rg._normalize_cited_tail("pkg/mod.py") == "pkg/mod.py"
    assert _rg._normalize_cited_tail("docs/HANDOFF.md") == "docs/handoff.md" or _rg._normalize_cited_tail("docs/HANDOFF.md") == "docs/HANDOFF.md"
    for directory in ("tests", "code_graph_runtime", "pkg/sub", ".", "..", "src/.hidden"):
        assert _rg._normalize_cited_tail(directory) is None, directory


def test_recording_project_notes_is_not_direct_labour():
    import routing_gate as _rg
    for name in ("mcp__agent_switchboard__project_brief", "mcp__agent-switchboard__record_work_memory"):
        assert _rg._direct_labour_category(name, {"op": "add", "project": "demo"}) is None, name
