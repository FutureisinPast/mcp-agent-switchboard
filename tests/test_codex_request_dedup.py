"""Tests for WP-SB8C: queue_codex_request's resend/dedup bug.

Regression covered: a request held by the outbound screen, approved by the
owner, and resent with outbound_reviewed=true was being deduped against the
PRIOR terminal (failed/error) request row for the same project/topic/prompt/
mode and returned `deduped: true, "request already terminal"` without ever
running. The dedup query must:
  - never match a request in a real failure terminal state (error, cancelled,
    canceled, expired, failed);
  - include outbound_reviewed in the dedup key, so a resend carrying a
    different reviewed flag than a stored row is never silently matched to
    it;
  - still dedupe normally against a live (queued/running) or completed row
    that has the SAME outbound_reviewed value, so ordinary repeat calls keep
    their existing idempotency.

Standard-library only. Uses a TemporaryDirectory-backed sqlite DB via
broker.db_connect/init_db -- no real Codex process is started (autorun is
disabled per-call).
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

import agent_broker_mcp as broker  # noqa: E402


class CodexRequestDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmpdir.name)
        self.paths = {
            "DB_PATH": root / "broker.db",
            "BROKER_DIR": root / "broker",
        }
        self.stack = mock.patch.multiple(broker, **self.paths)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        broker.init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _mark_status(self, request_id: str, status: str) -> None:
        with broker.db_connect() as conn:
            conn.execute(
                "UPDATE codex_requests SET status = ? WHERE id = ?",
                (status, request_id),
            )

    def test_resend_after_failed_request_is_never_deduped(self):
        first = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t1", autorun=False,
            outbound_reviewed=False,
        )
        self.assertNotIn("deduped", first)
        self._mark_status(first["id"], "failed")

        resend = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t1", autorun=False,
            outbound_reviewed=True,
        )
        self.assertNotIn("deduped", resend)
        self.assertNotEqual(resend["id"], first["id"])
        self.assertTrue(resend["outbound_reviewed"])

    def test_resend_after_error_request_is_never_deduped(self):
        first = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t2", autorun=False,
        )
        self._mark_status(first["id"], "error")

        resend = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t2", autorun=False,
            outbound_reviewed=True,
        )
        self.assertNotIn("deduped", resend)
        self.assertNotEqual(resend["id"], first["id"])

    def test_resend_with_different_outbound_reviewed_never_matches_live_row(self):
        first = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t3", autorun=False,
            outbound_reviewed=False,
        )
        # First row is still 'queued' (a live, non-terminal state).
        resend = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t3", autorun=False,
            outbound_reviewed=True,
        )
        self.assertNotIn("deduped", resend)
        self.assertNotEqual(resend["id"], first["id"])

    def test_same_outbound_reviewed_still_dedupes_against_a_live_row(self):
        first = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t4", autorun=False,
            outbound_reviewed=False,
        )
        again = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t4", autorun=False,
            outbound_reviewed=False,
        )
        self.assertTrue(again.get("deduped"))
        self.assertEqual(again["id"], first["id"])

    def test_same_outbound_reviewed_still_dedupes_against_a_completed_row(self):
        first = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t5", autorun=False,
            outbound_reviewed=True,
        )
        self._mark_status(first["id"], "completed")

        again = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t5", autorun=False,
            outbound_reviewed=True,
        )
        self.assertTrue(again.get("deduped"))
        self.assertEqual(again["id"], first["id"])

    def test_different_outbound_reviewed_never_matches_a_completed_row(self):
        first = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t6", autorun=False,
            outbound_reviewed=False,
        )
        self._mark_status(first["id"], "completed")

        resend = broker.queue_codex_request(
            "dedup-project", "do the thing", topic="t6", autorun=False,
            outbound_reviewed=True,
        )
        self.assertNotIn("deduped", resend)
        self.assertNotEqual(resend["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
