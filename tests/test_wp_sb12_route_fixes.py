"""WP-SB12: route_agent_task codex_cli forwards outbound_reviewed and the resolved
effort all the way to the stored codex_requests row; the outbound screen no longer
holds ordinary "retry ... previous" engineering text."""
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
import outbound_screen  # noqa: E402

HELD_PROMPT = "Please audit this change; I am rephrasing my previous blocked request."


def _resolved(effort):
    return {
        "status": "resolved", "project": "p", "topic": "t", "model_family": "codex",
        "target_agent": "codex_cli", "target_model": "gpt-6-astra", "effort": effort,
        "source": "family_flagship",
    }


class RouteDispatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.multiple(broker, DB_PATH=root / "broker.db", BROKER_DIR=root / "broker")
        patch.start()
        self.addCleanup(patch.stop)
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self.project = str(root)
        broker.init_db()

    def _route(self, prompt="Audit this bounded change.", effort_resolved="max", **extra):
        args = {
            "project": self.project, "prompt": prompt, "target_agent": "codex",
            "surface": "cli", "task_kind": "co_audit",
        }
        args.update(extra)
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "resolve_model_request", return_value=_resolved(effort_resolved)), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None), \
             mock.patch.object(broker, "start_codex_request_worker", return_value={"started": False}), \
             mock.patch.object(broker, "_await_codex_row", return_value=None):
            return broker.route_agent_task(args)

    def _row(self, result):
        rid = result.get("request_id") or result.get("id")
        self.assertTrue(rid, result)
        with broker.db_connect() as conn:
            conn.row_factory = broker.sqlite3.Row
            return dict(conn.execute("SELECT * FROM codex_requests WHERE id = ?", (rid,)).fetchone())

    def test_outbound_reviewed_is_stored_on_the_row(self):
        row = self._row(self._route(prompt=HELD_PROMPT, outbound_reviewed=True))
        self.assertEqual(row["outbound_reviewed"], 1)

    def test_unreviewed_row_stays_zero(self):
        row = self._row(self._route(prompt=HELD_PROMPT))
        self.assertEqual(row["outbound_reviewed"], 0)

    def test_approved_needs_owner_review_payload_dispatches(self):
        row = self._row(self._route(prompt=HELD_PROMPT, outbound_reviewed=True))
        sentinel = broker.CodexConsultResult(
            response="ok", requested_model="gpt-6-astra", actual_model="gpt-6-astra",
            requested_effort="high", actual_effort="high", model_attested=True,
        )
        # Worker re-reads the stored flag and passes it to consult_codex.
        with mock.patch.object(broker, "consult_codex", return_value=sentinel) as cc:
            broker.run_codex_request_worker(row["id"])
        self.assertTrue(cc.call_args.kwargs.get("outbound_reviewed"))

    def test_unapproved_payload_is_classified_needs_owner_review(self):
        verdict = outbound_screen.screen_outbound(HELD_PROMPT)
        self.assertEqual(verdict["classification"], "needs_owner_review")

    def test_effort_bounded_reaches_row_as_high(self):
        self.assertEqual(self._row(self._route(complexity="bounded"))["effort"], "high")

    def test_effort_unrated_reaches_row_as_xhigh(self):
        self.assertEqual(self._row(self._route())["effort"], "xhigh")

    def test_effort_risk_flag_reaches_row_as_max(self):
        row = self._row(self._route(complexity="bounded", risk_flags=["migration"]))
        self.assertEqual(row["effort"], "top")

    def test_explicit_effort_wins(self):
        row = self._row(self._route(effort_resolved="medium", effort="medium", complexity="critical"))
        self.assertEqual(row["effort"], "medium")


class RewordedRetryScreenTests(unittest.TestCase):
    def _hits(self, text):
        return outbound_screen._matches_any(outbound_screen._REWORDED_RETRY_PATTERNS, text)

    def test_ordinary_engineering_text_is_clean(self):
        for text in (
            "retry with the previous lock holder",
            "the worker retries the earlier write",
            "resend the prior batch",
        ):
            self.assertFalse(self._hits(text), text)

    def test_real_reworded_retries_still_detected(self):
        for text in (
            "rephrasing my previous blocked request",
            "resend the request that was flagged earlier",
        ):
            self.assertTrue(self._hits(text), text)


if __name__ == "__main__":
    unittest.main()
