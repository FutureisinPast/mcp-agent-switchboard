"""Tests for hourly override budget caps and live-remote command classification."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
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
def _enforce_override_cap(monkeypatch):
    """Override cap tests enforce the hourly cap and test the default max budget."""
    monkeypatch.setenv("AGENT_BROKER_OVERRIDE_CAP", "enforce")
    monkeypatch.delenv("AGENT_BROKER_OVERRIDE_MAX_BUDGET", raising=False)


class TestOverrideBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "routing-gate"
        self.evidence_dir = Path(self.tmp.name) / "context-evidence"
        self.db_path = Path(self.tmp.name) / "state.sqlite"
        self.state_patch = mock.patch.object(routing_gate, "STATE_DIR", self.state_dir)
        self.evidence_patch = mock.patch.object(routing_gate, "EVIDENCE_DIR", self.evidence_dir)
        self.db_patch = mock.patch.object(routing_gate, "DB_PATH", self.db_path)
        self.child_patch = mock.patch.dict(os.environ, {"AGENT_BROKER_CHILD": ""})
        self.state_patch.start()
        self.evidence_patch.start()
        self.db_patch.start()
        self.child_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.evidence_patch.stop)
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.child_patch.stop)

    def test_default_max_is_6_and_budget_12_request_grants_6(self):
        session_id = "s-default-max-6"
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-MAX-6",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "12",
            ])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data.get("registered"))
        self.assertEqual(data.get("package"), "WP-MAX-6")
        self.assertEqual(data.get("budget"), 6)
        state = routing_gate._read_state(session_id)
        self.assertEqual(state.get("relief_budget"), 6)
        grants = state.get("override_grants", [])
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0].get("budget"), 6)

    def test_two_6_call_overrides_fit_third_refused_exits_nonzero_counters_unchanged(self):
        session_id = "s-two-fit-third-refused"
        # Consume 1 direct labour call first
        routing_gate.pre_tool_use({
            "session_id": session_id,
            "tool_use_id": "r-1",
            "tool_name": "Read",
            "tool_input": {"path": "src/a.py"},
            "_switchboard_host": "codex",
        })
        state_init = routing_gate._read_state(session_id)
        self.assertEqual(state_init.get("direct_labour_since_relief"), 1)

        # Override 1: 6 calls
        buf1 = io.StringIO()
        with mock.patch("sys.stdout", buf1):
            code1 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-ONE",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "6",
            ])
        self.assertEqual(code1, 0)
        self.assertTrue(json.loads(buf1.getvalue())["registered"])

        # Override 2: 6 calls (total 12)
        buf2 = io.StringIO()
        with mock.patch("sys.stdout", buf2):
            code2 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-TWO",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "6",
            ])
        self.assertEqual(code2, 0)
        self.assertTrue(json.loads(buf2.getvalue())["registered"])

        # Consume 3 direct labour calls under override 2
        for i in range(3):
            routing_gate.pre_tool_use({
                "session_id": session_id,
                "tool_use_id": f"r-during-{i}",
                "tool_name": "Read",
                "tool_input": {"path": f"src/{i}.py"},
                "_switchboard_host": "codex",
            })
        state_before_refused = routing_gate._read_state(session_id)
        direct_since_before = state_before_refused.get("direct_labour_since_relief")
        block_counts_before = dict(state_before_refused.get("direct_labour_block_counts", {}))
        relief_seq_before = state_before_refused.get("labour_relief_sequence")
        self.assertEqual(direct_since_before, 3)

        # Override 3: in the same hour -> must be refused!
        buf3 = io.StringIO()
        with mock.patch("sys.stdout", buf3):
            code3 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-THREE",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "6",
            ])
        self.assertNotEqual(code3, 0)
        data3 = json.loads(buf3.getvalue())
        self.assertFalse(data3["registered"])
        self.assertEqual(data3["package"], "WP-THREE")
        self.assertIn("hourly override budget used (12/12 direct calls in the last hour)", data3["reason"])
        self.assertIn("route this labour to Flash with route_agent_task", data3["reason"])

        # Direct labour counters must be completely unchanged!
        state_after_refused = routing_gate._read_state(session_id)
        self.assertEqual(state_after_refused.get("direct_labour_since_relief"), direct_since_before)
        self.assertEqual(state_after_refused.get("direct_labour_block_counts"), block_counts_before)
        self.assertEqual(state_after_refused.get("labour_relief_sequence"), relief_seq_before)

        # Decision log check: logged as Override event with decision "refused"
        log_path = routing_gate._session_log_path(session_id)
        records = routing_gate._read_session_log(log_path)
        refused_recs = [r for r in records if r.get("event") == "Override" and r.get("decision") == "refused"]
        self.assertEqual(len(refused_recs), 1)
        self.assertEqual(refused_recs[0].get("work_package_id"), "WP-THREE")

    def test_4_call_then_12_call_request_grants_4_then_6_then_refuses(self):
        session_id = "s-4-then-12-refuse"
        # 1. 4-call request -> grants 4
        buf1 = io.StringIO()
        with mock.patch("sys.stdout", buf1):
            code1 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-A",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "4",
            ])
        self.assertEqual(code1, 0)
        data1 = json.loads(buf1.getvalue())
        self.assertTrue(data1["registered"])
        self.assertEqual(data1["budget"], 4)

        # 2. 12-call request -> clamped to 6, available is 8, grants 6
        buf2 = io.StringIO()
        with mock.patch("sys.stdout", buf2):
            code2 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-B",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "12",
            ])
        self.assertEqual(code2, 0)
        data2 = json.loads(buf2.getvalue())
        self.assertTrue(data2["registered"])
        self.assertEqual(data2["budget"], 6)

        # Total used is 4 + 6 = 10. Remaining is 2.
        # Another request for 12 calls grants min(6, 2) = 2.
        buf3 = io.StringIO()
        with mock.patch("sys.stdout", buf3):
            code3 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-C",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "12",
            ])
        self.assertEqual(code3, 0)
        data3 = json.loads(buf3.getvalue())
        self.assertTrue(data3["registered"])
        self.assertEqual(data3["budget"], 2)

        # Now total used is 10 + 2 = 12. Remaining is 0. Next request refuses!
        buf4 = io.StringIO()
        with mock.patch("sys.stdout", buf4):
            code4 = routing_gate.routing_override_cli([
                "--session", session_id,
                "--package", "WP-D",
                "--reason", "valid override reason at least 12 chars",
                "--budget", "6",
            ])
        self.assertNotEqual(code4, 0)
        data4 = json.loads(buf4.getvalue())
        self.assertFalse(data4["registered"])
        self.assertIn("hourly override budget used (12/12 direct calls in the last hour)", data4["reason"])

    def test_after_window_passes_override_granted_again(self):
        session_id = "s-window-passes"
        start_time = 1000000.0

        with mock.patch("time.time", return_value=start_time):
            routing_gate.register_brain_override(session_id, "WP-WIN-1", "valid override reason at least 12 chars", budget=6)
            routing_gate.register_brain_override(session_id, "WP-WIN-2", "valid override reason at least 12 chars", budget=6)

            buf_refused = io.StringIO()
            with mock.patch("sys.stdout", buf_refused):
                code_refused = routing_gate.routing_override_cli([
                    "--session", session_id,
                    "--package", "WP-WIN-3",
                    "--reason", "valid override reason at least 12 chars",
                    "--budget", "6",
                ])
            self.assertNotEqual(code_refused, 0)
            self.assertFalse(json.loads(buf_refused.getvalue())["registered"])

        # Advance time past 3600 seconds
        later_time = start_time + 3601.0
        with mock.patch("time.time", return_value=later_time):
            buf_granted = io.StringIO()
            with mock.patch("sys.stdout", buf_granted):
                code_granted = routing_gate.routing_override_cli([
                    "--session", session_id,
                    "--package", "WP-WIN-4",
                    "--reason", "valid override reason at least 12 chars",
                    "--budget", "6",
                ])
            self.assertEqual(code_granted, 0)
            data_granted = json.loads(buf_granted.getvalue())
            self.assertTrue(data_granted["registered"])
            self.assertEqual(data_granted["budget"], 6)

    def test_warn_mode_grants_and_logs(self):
        session_id = "s-warn-cap"
        with mock.patch.dict(os.environ, {"AGENT_BROKER_OVERRIDE_CAP": "warn"}):
            routing_gate.register_brain_override(session_id, "WP-W1", "valid override reason at least 12 chars", budget=6)
            routing_gate.register_brain_override(session_id, "WP-W2", "valid override reason at least 12 chars", budget=6)

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                code = routing_gate.routing_override_cli([
                    "--session", session_id,
                    "--package", "WP-W3",
                    "--reason", "valid override reason at least 12 chars",
                    "--budget", "6",
                ])
            self.assertEqual(code, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["registered"])
            self.assertEqual(data["budget"], 6)
            self.assertIn("warning", data)
            self.assertIn("hourly override budget used (12/12 direct calls in the last hour)", data["warning"])

            log_path = routing_gate._session_log_path(session_id)
            records = routing_gate._read_session_log(log_path)
            warn_recs = [r for r in records if r.get("event") == "Override" and r.get("decision") == "override-cap-would-refuse"]
            self.assertEqual(len(warn_recs), 1)
            self.assertEqual(warn_recs[0]["budget"], 6)

    def test_off_mode_has_no_hourly_cap(self):
        session_id = "s-off-cap"
        with mock.patch.dict(os.environ, {"AGENT_BROKER_OVERRIDE_CAP": "off"}):
            for i in range(3):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    code = routing_gate.routing_override_cli([
                        "--session", session_id,
                        "--package", f"WP-OFF-{i}",
                        "--reason", "valid override reason at least 12 chars",
                        "--budget", "6",
                    ])
                self.assertEqual(code, 0)
                data = json.loads(buf.getvalue())
                self.assertTrue(data["registered"])
                self.assertNotIn("warning", data)

    def test_flash_credit_does_not_clear_override_grants(self):
        session_id = "s-flash-credit-grants"
        routing_gate.register_brain_override(
            session_id, "WP-GRANT", "valid override reason at least 12 chars", budget=6
        )
        state_before = routing_gate._read_state(session_id)
        self.assertEqual(len(state_before.get("override_grants", [])), 1)

        req_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE consultations (request_id TEXT)")
            conn.execute("INSERT INTO consultations (request_id) VALUES (?)", (req_id,))
            conn.commit()
        finally:
            conn.close()

        dispatch_payload = {
            "session_id": session_id,
            "tool_use_id": "call-credit-1",
            "tool_name": routing_gate.CREDITABLE_DISPATCH_TOOL,
            "tool_input": {"target_agent": "antigravity", "surface": "cli"},
            "tool_response": {
                "receipt": f"broker:{req_id}",
                "outcome": routing_gate.CREDITABLE_OUTCOME,
                "work_package_id": "WP-FLASH-CRED",
            },
            "_switchboard_host": "codex",
        }
        res = routing_gate.post_tool_use(dispatch_payload)
        self.assertIn("Routing credit", str(res))

        state_after = routing_gate._read_state(session_id)
        self.assertNotIn("relief_budget", state_after)
        grants = state_after.get("override_grants", [])
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["budget"], 6)

    def test_live_remote_classifier_free_and_charged_examples(self):
        free_examples = [
            'ssh -i key user@host "cd /srv && ls"',
            r'cmd /c "ssh -i k u@h bash -s < C:\scratch\job.sh"',
            r'scp -i k u@h:/var/log/x.log C:\scratch\x.log',
        ]
        for cmd in free_examples:
            cat = routing_gate._direct_labour_category("bash", {"command": cmd})
            self.assertIsNone(cat, f"Command should be free (None): {cmd}")
            session_id = "s-free-" + uuid.uuid4().hex[:8]
            payload = {
                "session_id": session_id,
                "tool_use_id": "call-free",
                "tool_name": "bash",
                "tool_input": {"command": cmd},
                "_switchboard_host": "codex",
            }
            res = routing_gate.pre_tool_use(payload)
            self.assertEqual(res, {})
            log_path = routing_gate._session_log_path(session_id)
            records = routing_gate._read_session_log(log_path)
            self.assertEqual(records[-1].get("decision"), "live-remote")

        charged_examples = [
            "pytest && ssh box ls",
            "ssh box ls; rm x",
            "ssh -o ProxyCommand=evil box",
            "ssh box ls > out.txt",
            'cmd /c "pytest" && ssh h ls',
        ]
        for cmd in charged_examples:
            cat = routing_gate._direct_labour_category("bash", {"command": cmd})
            self.assertIsNotNone(cat, f"Command should be charged (not None): {cmd}")


if __name__ == "__main__":
    unittest.main()
