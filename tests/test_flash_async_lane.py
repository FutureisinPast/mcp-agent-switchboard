"""WP-SB10: the async Flash (agy) lane.

Covers the default async decision, queue -> worker -> result, timeout bounds, live
progress, stale/dead-worker classification, cancellation, the accept-edits overlap
guard, gate relief on start (once, deduped, none for failed starts) and the explicit
async=false sync path. agy, subprocess and every process probe are mocked; nothing here
starts a real worker, touches ~/.agent-broker, or reads a real agy transcript.
"""
from __future__ import annotations

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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import routing_gate  # noqa: E402

# tests/conftest.py stubs the worker start suite-wide; this module restores the real one
# (captured at import time) and mocks subprocess.Popen instead.
REAL_START_WORKER = broker.start_flash_request_worker

RESOLVED = {
    "status": "resolved",
    "target_agent": "antigravity_cli",
    "target_model": "gemini-3.7-flash-high",
    "effort": "high",
    "source": "explicit_request",
}


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _fake_popen(pid: int = 4242) -> mock.MagicMock:
    proc = mock.MagicMock()
    proc.pid = pid
    return mock.MagicMock(return_value=proc)


class FlashAsyncBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.ws = root / "workspace"
        self.ws.mkdir()
        self.file_a = self.ws / "a.py"
        self.file_b = self.ws / "b.py"
        self.file_a.write_text("x = 1\n", encoding="utf-8")
        self.file_b.write_text("y = 2\n", encoding="utf-8")
        self.agy_home = root / "agy-home"
        env = {k: v for k, v in os.environ.items() if k != "AGENT_BROKER_CALLER"}
        env["AGENT_BROKER_AGY_HOME"] = str(self.agy_home)
        env.pop("AGENT_BROKER_CHILD", None)
        patches = [
            mock.patch.multiple(broker, DB_PATH=root / "broker.db", BROKER_DIR=root / "broker"),
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(broker, "start_flash_request_worker", REAL_START_WORKER),
            mock.patch.object(broker, "run_git", return_value=""),
            mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-cli"),
            mock.patch.object(broker, "current_codex_role_model", return_value="gpt-live-sol"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.popen = _fake_popen()
        popen_patch = mock.patch.object(broker.subprocess, "Popen", self.popen)
        popen_patch.start()
        self.addCleanup(popen_patch.stop)
        broker.init_db()

    # -- helpers ---------------------------------------------------------
    def impl_args(self, path: Path | None = None, wp: str = "WP-A", **extra) -> dict:
        args = {
            "project": str(self.ws),
            "prompt": "Implement the bounded change.",
            "task_kind": "implementation",
            "mode": "accept-edits",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": wp,
            "allowed_writes": [str(path or self.file_a)],
            "acceptance_criteria": ["the file still parses"],
            "max_response_chars": 1500,
        }
        args.update(extra)
        return args

    def read_args(self, wp: str = "WP-R", **extra) -> dict:
        args = {
            "project": str(self.ws),
            "prompt": "Investigate the bounded question.",
            "task_kind": "research",
            "mode": "plan",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": wp,
            "research_questions": ["What does a.py define?"],
            "read_context": [str(self.file_a)],
            "max_response_chars": 1500,
        }
        args.update(extra)
        return args

    def row(self, rid: str) -> dict:
        with broker.db_connect() as conn:
            conn.row_factory = sqlite3.Row
            found = conn.execute("SELECT * FROM flash_requests WHERE id = ?", (rid,)).fetchone()
        return dict(found)

    def set_row(self, rid: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with broker.db_connect() as conn:
            conn.execute(f"UPDATE flash_requests SET {cols} WHERE id = ?", (*fields.values(), rid))

    def write_transcript(self, package_id: str, steps: list[str]) -> Path:
        transcript = self.agy_home / "brain" / "session-1" / ".system_generated" / "logs" / "transcript.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            "\n".join([f"Package ID: {package_id}"] + [json.dumps({"toolAction": s}) for s in steps]) + "\n",
            encoding="utf-8",
        )
        return transcript

    def envelope_json(self, package_id: str, summary: str = "done") -> str:
        return json.dumps(
            {
                "package_id": package_id,
                "worker_status": "completed",
                "structured_output": {"package_id": package_id, "status": "completed", "summary": summary},
                "caveats": [],
                "disposition": "accepted",
                "cli": {
                    "conversation_id": "conv-1",
                    "duration_seconds": 12,
                    "model": "gemini-3.7-flash-high",
                    "requested_model": "gemini-3.7-flash-high",
                    "attestation": "backend",
                    "model_attested": True,
                },
            }
        )

    def run_worker(self, rid: str, agy_response: str | None = None, package_id: str = "WP-A") -> tuple[dict, mock.Mock]:
        agy = mock.Mock(return_value=agy_response or self.envelope_json(package_id))
        with mock.patch.object(broker, "consult_antigravity_cli", agy), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", side_effect=lambda model, effort: model), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}):
            result = broker.run_flash_request_worker(rid)
        return result, agy


class DefaultAsyncDecisionTests(FlashAsyncBase):
    def test_default_decision_matrix(self):
        decide = broker.flash_async_decision
        self.assertEqual(decide({"task_kind": "implementation"}, "plan"), (True, "default_implementation"))
        self.assertEqual(decide({"task_kind": "quick_check"}, "accept-edits")[0], True)
        self.assertEqual(decide({"task_kind": "research"}, "plan")[0], True)
        self.assertEqual(decide({"task_kind": "quick_check"}, "plan")[0], False)
        self.assertEqual(decide({"task_kind": "search"}, "plan")[0], False)
        self.assertEqual(decide({}, "plan")[0], False)

    def test_explicit_async_always_wins(self):
        decide = broker.flash_async_decision
        self.assertEqual(decide({"task_kind": "implementation", "async": False}, "accept-edits"), (False, "explicit"))
        self.assertEqual(decide({"task_kind": "implementation", "async": "false"}, "accept-edits")[0], False)
        self.assertEqual(decide({"task_kind": "quick_check", "async": True}, "plan"), (True, "explicit"))

    def test_schema_async_is_no_longer_claude_only(self):
        route = next(t for t in broker.TOOLS if t["name"] == "route_agent_task")
        props = route["inputSchema"]["properties"]
        self.assertNotIn("For Claude targets, queue", props["async"]["description"])
        self.assertIn("Flash", props["async"]["description"])
        self.assertEqual(props["timeout_seconds"]["maximum"], 10800)


class TimeoutBoundTests(FlashAsyncBase):
    def test_bounds_and_default(self):
        bound = broker.bounded_flash_async_timeout
        self.assertEqual(bound(None), 3600)
        self.assertEqual(bound(10), 240)
        self.assertEqual(bound(99999), 10800)
        self.assertEqual(bound(7200), 7200)
        self.assertEqual(bound("not-a-number"), 3600)
        self.assertEqual(bound(0), 3600)

    def test_env_default_is_clamped_into_bounds(self):
        self.assertGreaterEqual(broker.FLASH_ASYNC_DEFAULT_TIMEOUT_SECONDS, 240)
        self.assertLessEqual(broker.FLASH_ASYNC_DEFAULT_TIMEOUT_SECONDS, 10800)
        with mock.patch.object(broker, "FLASH_ASYNC_DEFAULT_TIMEOUT_SECONDS", 1800):
            self.assertEqual(broker.bounded_flash_async_timeout(None), 1800)

    def test_queue_stores_bounded_timeout_and_worker_passes_it_to_agy(self):
        queued = broker.queue_flash_request(self.impl_args(timeout_seconds=50))
        self.assertEqual(queued["timeout_seconds"], 240)
        self.assertEqual(queued["async_worker"]["timeout_seconds"], 240)
        queued2 = broker.queue_flash_request(self.impl_args(self.file_b, "WP-B", timeout_seconds=7200))
        self.assertEqual(self.row(queued2["request_id"])["timeout_seconds"], 7200)
        _, agy = self.run_worker(queued2["request_id"], package_id="WP-B")
        self.assertEqual(agy.call_args.args[5], 7200)  # consult_antigravity_cli(timeout) positional


class QueueWorkerResultTests(FlashAsyncBase):
    def test_immediate_return_shape_and_worker_start(self):
        queued = broker.queue_flash_request(self.impl_args())
        rid = queued["request_id"]
        self.assertEqual(queued["status"], "running")
        self.assertEqual(queued["receipt"], f"broker:{rid}")
        self.assertEqual(queued["work_package_id"], "WP-A")
        self.assertEqual(queued["async_worker"]["started"], True)
        self.assertEqual(queued["async_worker"]["pid"], 4242)
        self.assertEqual(queued["poll"], {"tool": "request_result", "request_id": rid, "wait_seconds": 180})
        cmd = self.popen.call_args.args[0]
        self.assertIn("run-flash-request", cmd)
        self.assertEqual(cmd[-1], rid)
        stored = self.row(rid)
        self.assertEqual(json.loads(stored["write_paths"]), [str(self.file_a)])
        args = json.loads(stored["args_json"])
        self.assertEqual(args["acceptance_criteria"], ["the file still parses"])
        self.assertEqual(json.loads(stored["package_json"])["package_id"], "WP-A")
        self.assertEqual(json.loads(stored["manifest_json"])["writes"], [str(self.file_a)])

    def test_invalid_envelope_is_refused_at_queue_time(self):
        with self.assertRaises(ValueError):
            broker.queue_flash_request(self.impl_args(acceptance_criteria=[]))
        with self.assertRaises(ValueError):
            broker.queue_flash_request(self.impl_args(self.ws / "missing.py"))
        with self.assertRaises(ValueError):
            broker.queue_flash_request(self.impl_args(mode="danger-full-access"))
        with broker.db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM flash_requests").fetchone()[0], 0)

    def test_failed_worker_start_queues_nothing_creditable(self):
        self.popen.side_effect = OSError("no such exe")
        queued = broker.queue_flash_request(self.impl_args())
        self.assertEqual(queued["status"], "queued")
        self.assertFalse(queued["async_worker"]["started"])
        self.assertEqual(self.row(queued["request_id"])["status"], "error")

    def test_queue_worker_result_roundtrip_under_budget(self):
        queued = broker.queue_flash_request(self.impl_args())
        rid = queued["request_id"]
        result, agy = self.run_worker(rid, self.envelope_json("WP-A", summary="s" * 5000))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "completed_verified")
        self.assertEqual(result["receipt"], f"broker:{rid}")
        agy.assert_called_once()
        final = broker.request_result(rid)
        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["answered"])
        self.assertEqual(final["outcome"], "completed_verified")
        self.assertEqual(final["receipt"], f"broker:{rid}")
        self.assertEqual(final["request_id"], rid)
        self.assertTrue(final["truncated"])
        self.assertTrue(final["response_ref"])
        self.assertLessEqual(len(json.dumps(final, ensure_ascii=False)), 1500)
        # The full result is kept under response_ref on the row and resolves in the ledger.
        self.assertTrue(self.row(rid)["response_ref"])
        with broker.db_connect() as conn:
            hit = conn.execute("SELECT COUNT(*) FROM consultations WHERE request_id = ?", (rid,)).fetchone()[0]
        self.assertEqual(hit, 1)
        status = broker.request_status(rid)
        self.assertEqual(status["kind"], "flash")
        self.assertTrue(status["terminal"])
        self.assertNotIn("progress", status)

    def test_worker_is_idempotent_and_skips_terminal_rows(self):
        rid = broker.queue_flash_request(self.impl_args())["request_id"]
        self.run_worker(rid)
        again, agy = self.run_worker(rid)
        self.assertTrue(again.get("skipped"))
        agy.assert_not_called()

    def test_worker_exception_becomes_failed_envelope(self):
        rid = broker.queue_flash_request(self.impl_args())["request_id"]
        with mock.patch.object(broker, "consult", side_effect=RuntimeError("boom")):
            result = broker.run_flash_request_worker(rid)
        self.assertEqual(result["status"], "error")
        final = broker.request_result(rid)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["outcome"], "failed_pre_mutation")
        self.assertIn("native_handoff", final)

    def test_route_default_is_async_and_fits_envelope_budget(self):
        args = self.impl_args(target_agent="antigravity", surface="cli")
        consult = mock.Mock()
        with mock.patch.object(broker, "resolve_model_request", return_value=dict(RESOLVED)), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None), \
             mock.patch.object(broker, "consult", consult):
            result = broker.route_agent_task(args)
        consult.assert_not_called()
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["route"], "antigravity_cli")
        self.assertEqual(result["receipt"], f"broker:{result['request_id']}")
        self.assertTrue(result["async_worker"]["started"])
        self.assertEqual(result["poll"]["tool"], "request_result")
        self.assertEqual(result["async_reason"], "default_implementation_mode")
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 1500)

    def test_explicit_async_false_keeps_sync_path(self):
        args = self.impl_args(target_agent="antigravity", surface="cli", **{"async": False})
        consult = mock.Mock(return_value={"status": "ok", "outcome": "completed_verified"})
        queue = mock.Mock()
        with mock.patch.object(broker, "resolve_model_request", return_value=dict(RESOLVED)), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None), \
             mock.patch.object(broker, "queue_flash_request", queue), \
             mock.patch.object(broker, "consult", consult):
            result = broker.route_agent_task(args)
        queue.assert_not_called()
        consult.assert_called_once()
        self.assertEqual(result["route"], "antigravity_cli")
        self.popen.assert_not_called()

    def test_quick_check_stays_sync_by_default(self):
        args = {"project": str(self.ws), "prompt": "look", "target_agent": "antigravity", "surface": "cli",
                "task_kind": "quick_check", "mode": "plan"}
        consult = mock.Mock(return_value={"status": "ok"})
        queue = mock.Mock()
        with mock.patch.object(broker, "resolve_model_request", return_value=dict(RESOLVED)), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None), \
             mock.patch.object(broker, "queue_flash_request", queue), \
             mock.patch.object(broker, "consult", consult):
            broker.route_agent_task(args)
        queue.assert_not_called()
        consult.assert_called_once()


class ProgressAndStaleTests(FlashAsyncBase):
    def _running(self, wp: str = "WP-A") -> str:
        rid = broker.queue_flash_request(self.impl_args(wp=wp))["request_id"]
        self.set_row(rid, worker_started_at=_iso(time.time() - 30), max_response_chars=20000)
        return rid

    def test_progress_while_running_reads_the_agy_transcript(self):
        rid = self._running()
        self.write_transcript("WP-A", ["read a.py", "editing a.py to add the fix"])
        with mock.patch.object(broker, "_pid_alive", return_value=True):
            result = broker.request_result(rid)
            status = broker.request_status(rid)
        self.assertEqual(result["state"], "running")
        self.assertFalse(result["answered"])
        self.assertEqual(result["progress"]["steps"], 2)
        self.assertEqual(result["progress"]["last_action"], "editing a.py to add the fix")
        self.assertTrue(result["progress"]["transcript_found"])
        self.assertGreaterEqual(result["progress"]["elapsed_seconds"], 30)
        self.assertEqual(result["poll"]["wait_seconds"], 180)
        self.assertEqual(status["progress"]["steps"], 2)
        self.assertEqual(status["work_package_id"], "WP-A")

    def test_progress_without_transcript_is_zero_steps(self):
        rid = self._running()
        with mock.patch.object(broker, "_pid_alive", return_value=True):
            result = broker.request_result(rid)
        self.assertEqual(result["progress"]["steps"], 0)
        self.assertFalse(result["progress"]["transcript_found"])

    def test_dead_worker_after_steps_fails_with_flash_failed_handoff(self):
        rid = self._running()
        self.write_transcript("WP-A", ["read a.py"])
        with mock.patch.object(broker, "_pid_alive", return_value=False):
            result = broker.request_result(rid)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["outcome"], "failed_pre_mutation")
        self.assertEqual(result["failure_kind"], "timeout_during_execution")
        self.assertEqual(result["timeout_evidence"]["steps"], 1)
        self.assertFalse(result["credit_eligible"])
        handoff = result["native_handoff"]
        self.assertEqual(handoff["flash_skip_reason"], f"flash-failed:broker:{rid}")
        self.assertIn("died", result["response"])

    def test_dead_worker_without_transcript_is_flash_unavailable(self):
        rid = self._running()
        with mock.patch.object(broker, "_pid_alive", return_value=False):
            result = broker.request_result(rid)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["outcome"], "unavailable_pre_mutation")
        self.assertEqual(result["native_handoff"]["flash_skip_reason"], f"flash-unavailable:broker:{rid}")

    def test_timeout_exceeded_kills_worker_and_classifies(self):
        rid = self._running()
        self.set_row(rid, worker_started_at=_iso(time.time() - 5000), worker_pid=999)
        self.write_transcript("WP-A", ["still editing"])
        with mock.patch.object(broker, "_pid_alive", return_value=True), \
             mock.patch.object(broker, "_kill_pid_tree") as kill:
            result = broker.request_status(rid)
        kill.assert_called_once_with(999)
        self.assertEqual(result["state"], "failed")
        final = broker.request_result(rid)
        self.assertTrue(final["response"].startswith("Antigravity CLI timed out after"))
        self.assertEqual(final["failure_kind"], "timeout_during_execution")
        self.assertEqual(final["native_handoff"]["flash_skip_reason"], f"flash-failed:broker:{rid}")

    def test_healthy_running_row_is_left_alone(self):
        rid = self._running()
        with mock.patch.object(broker, "_pid_alive", return_value=True):
            self.assertEqual(broker.request_status(rid)["state"], "running")

    def test_never_started_queued_row_expires_as_unavailable(self):
        self.popen.side_effect = None
        with mock.patch.object(broker, "start_flash_request_worker", return_value={"started": False, "reason": "x"}):
            rid = broker.queue_flash_request(self.impl_args())["request_id"]
        self.set_row(rid, created_at=_iso(time.time() - 4000))
        self.assertEqual(broker.request_status(rid)["state"], "failed")
        self.assertEqual(broker.request_result(rid)["outcome"], "unavailable_pre_mutation")


class CancelTests(FlashAsyncBase):
    def test_cancel_kills_worker_tree_and_blocks_late_finalize(self):
        rid = broker.queue_flash_request(self.impl_args())["request_id"]
        self.assertEqual(self.row(rid)["worker_pid"], 4242)
        with mock.patch.object(broker, "_pid_alive", return_value=True), \
             mock.patch.object(broker, "_kill_pid_tree", return_value=True) as kill:
            cancelled = broker.cancel_request(rid, "owner stop")
        kill.assert_called_once_with(4242)
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["kind"], "flash")
        self.assertEqual(cancelled["worker_killed"], {"pid": 4242, "was_alive": True, "killed": True})
        self.assertEqual(broker.request_status(rid)["state"], "cancelled")
        # A worker that finishes in the race window cannot overwrite the cancellation.
        self.assertFalse(broker._flash_finalize_row(rid, "completed", "{}", None, None, None))
        again = broker.cancel_request(rid)
        self.assertFalse(again["cancelled"])

    def test_cancel_of_dead_worker_does_not_kill(self):
        rid = broker.queue_flash_request(self.impl_args())["request_id"]
        with mock.patch.object(broker, "_pid_alive", return_value=False), \
             mock.patch.object(broker, "_kill_pid_tree") as kill:
            cancelled = broker.cancel_request(rid)
        kill.assert_not_called()
        self.assertTrue(cancelled["cancelled"])

    def test_windows_kill_uses_taskkill_tree(self):
        if os.name != "nt":
            self.skipTest("taskkill path is Windows-only")
        with mock.patch.object(broker.subprocess, "run") as run:
            self.assertTrue(broker._kill_pid_tree(777))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["taskkill", "/PID"])
        self.assertIn("/T", cmd)
        self.assertIn("/F", cmd)


class OverlapGuardTests(FlashAsyncBase):
    def test_overlapping_accept_edits_is_refused_naming_the_request(self):
        first = broker.queue_flash_request(self.impl_args())
        with mock.patch.object(broker, "_pid_alive", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                broker.queue_flash_request(self.impl_args(wp="WP-A2"))
        self.assertIn(first["request_id"], str(ctx.exception))
        self.assertIn("overlap", str(ctx.exception).lower())
        with broker.db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM flash_requests").fetchone()[0], 1)

    def test_creates_and_parent_directory_overlap(self):
        first = broker.queue_flash_request(
            self.impl_args(allowed_writes=[], allowed_creates=[str(self.ws / "new.py")], wp="WP-N")
        )
        with mock.patch.object(broker, "_pid_alive", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                broker.queue_flash_request(
                    self.impl_args(allowed_writes=[], allowed_creates=[str(self.ws / "new.py")], wp="WP-N2")
                )
        self.assertIn(first["request_id"], str(ctx.exception))

    def test_disjoint_files_and_read_only_packages_are_allowed(self):
        broker.queue_flash_request(self.impl_args())
        with mock.patch.object(broker, "_pid_alive", return_value=True):
            broker.queue_flash_request(self.impl_args(self.file_b, "WP-B"))
            broker.queue_flash_request(self.read_args())  # reads a.py; never a write conflict

    def test_terminal_or_dead_rows_do_not_block(self):
        first = broker.queue_flash_request(self.impl_args())["request_id"]
        with mock.patch.object(broker, "_pid_alive", return_value=False):
            self.set_row(first, worker_started_at=_iso(time.time() - 60))
            second = broker.queue_flash_request(self.impl_args(wp="WP-A2"))
        self.assertEqual(broker.request_status(first)["state"], "failed")
        with mock.patch.object(broker, "_pid_alive", return_value=True), \
             mock.patch.object(broker, "_kill_pid_tree", return_value=True):
            with self.assertRaises(ValueError):
                broker.queue_flash_request(self.impl_args(wp="WP-A3"))
            broker.cancel_request(second["request_id"])
            broker.queue_flash_request(self.impl_args(wp="WP-A3"))


class GateReliefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "state.sqlite"
        for patch in (
            mock.patch.object(routing_gate, "STATE_DIR", root / "routing-gate"),
            mock.patch.object(routing_gate, "EVIDENCE_DIR", root / "context-evidence"),
            mock.patch.object(routing_gate, "DB_PATH", self.db_path),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.request_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE flash_requests (id TEXT)")
            conn.execute("INSERT INTO flash_requests (id) VALUES (?)", (self.request_id,))
            conn.commit()
        finally:
            conn.close()

    def _dispatch(self, response: dict, tool: str = routing_gate.CREDITABLE_DISPATCH_TOOL) -> dict:
        return routing_gate.post_tool_use(
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_use_id": f"call-{uuid.uuid4()}",
                "tool_name": tool,
                "tool_input": {"target_agent": "antigravity", "surface": "cli"},
                "tool_response": response,
                "_switchboard_host": "codex",
            }
        )

    def _started(self, rid: str | None = None, **overrides) -> dict:
        rid = rid or self.request_id
        response = {
            "status": "running",
            "request_id": rid,
            "receipt": f"broker:{rid}",
            "work_package_id": "WP-ASYNC",
            "outcome": "async_queued",
            "async_worker": {"started": True, "pid": 4242, "timeout_seconds": 3600, "log": "x.log"},
            "poll": {"tool": "request_result", "request_id": rid, "wait_seconds": 180},
        }
        response.update(overrides)
        return response

    def _sequence(self) -> int:
        return int(routing_gate._read_state("session-1").get("labour_relief_sequence") or 0)

    def test_started_async_package_earns_exactly_one_relief(self):
        first = self._dispatch(self._started())
        self.assertIn("Routing credit", json.dumps(first))
        state = routing_gate._read_state("session-1")
        self.assertEqual(state.get("direct_labour_since_relief"), 0)
        self.assertEqual(self._sequence(), 1)
        self.assertIn(f"broker:{self.request_id}", state["credited_receipts"])
        # The same request replayed earns nothing.
        self._dispatch(self._started())
        self.assertEqual(self._sequence(), 1)

    def test_queued_status_also_counts_as_started(self):
        self._dispatch(self._started(status="queued"))
        self.assertEqual(self._sequence(), 1)

    def test_later_request_result_for_same_request_does_not_credit_twice(self):
        self._dispatch(self._started())
        completed = {
            "status": "ok",
            "request_id": self.request_id,
            "receipt": f"broker:{self.request_id}",
            "outcome": "completed_verified",
            "work_package_id": "WP-ASYNC",
        }
        self._dispatch(completed, tool="mcp__agent_switchboard__request_result")
        self._dispatch(completed)  # even if it arrived as a route_agent_task-shaped result
        self.assertEqual(self._sequence(), 1)

    def test_failed_starts_earn_nothing(self):
        for response in (
            self._started(async_worker={"started": False, "reason": "spawn failed"}),
            self._started(status="error"),
            self._started(async_worker=None),
            {k: v for k, v in self._started().items() if k != "request_id"},
            self._started(rid=str(uuid.uuid4())),  # request id that is not in the ledger
        ):
            self._dispatch(response)
        self.assertEqual(self._sequence(), 0)


if __name__ == "__main__":
    unittest.main()
