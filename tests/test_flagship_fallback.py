"""Tests for the WP-SB1 flagship fallback chain: per-model latches, the
quota/plan classifier, account-hash voiding, and retry_unavailable.

Standard-library only. Every filesystem lookup that would otherwise hit
Path.home() (``~/.claude.json``, ``~/.codex/auth.json``) is redirected to a
TemporaryDirectory; HOME and USERPROFILE are overridden alongside Path.home
so nothing here can ever touch the real signed-in account files.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def _brief() -> dict:
    return {
        "decision": "Choose a routing design.",
        "constraints": ["Preserve compatibility."],
        "options": [{"id": "A", "summary": "Add one orchestrator."}],
        "questions": ["Is A the safest option?"],
        "evidence": [{"ref": "router.py:10", "claim": "The old route is fixed."}],
    }


def _args(vendor: str, model: str, complexity: str = "architecture", **extra) -> dict:
    return {
        "project": "p",
        "topic": "flagship-fallback-tests",
        "session_id": "session-1",
        "work_package_id": "WP-SB1-TEST",
        "host": {"vendor": vendor, "model": model},
        "complexity": complexity,
        "brief": _brief(),
        **extra,
    }


def _native(family: str, status: str = "completed", model: str | None = None) -> dict:
    return {
        "family": family,
        "model": model or ("gpt-6-astra" if family == "codex" else "claude-fable-5"),
        "status": status,
        "attestation": "verified",
        "agent_id": "native-1",
        "summary": "Use option A." if status == "completed" else "Native provider unavailable.",
    }


def _ok_consult(family, args):
    actual = "gpt-6-astra" if family == "codex" else "claude-fable-5"
    return {
        "status": "ok",
        "response": '{"recommendation":"A"}',
        "effort": args["effort"],
        "actual_effort": args["effort"],
        "actual_model": actual,
        "model_attested": True,
    }


class FlagshipFallbackTestCase(unittest.TestCase):
    """Every test here can trigger a latch write, which reads (never writes)
    ~/.claude.json / $CODEX_HOME/auth.json for the account-hash fingerprint.
    HOME/USERPROFILE and Path.home are redirected to an empty TemporaryDirectory
    for every test in this base class so the real account files are never
    touched, even though no test here asserts on the hash itself."""

    def setUp(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self._home_dir = tempfile.TemporaryDirectory()
        self._home = Path(self._home_dir.name)
        self.addCleanup(self._home_dir.cleanup)
        # Active for the whole test method, not just inside _run, so a direct
        # broker._set_flagship_latch(...) call made from a test body (before
        # calling _run) also never reaches the real account files.
        home_patch = mock.patch.object(Path, "home", return_value=self._home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        env_patch = mock.patch.dict(
            "os.environ", {"HOME": str(self._home), "USERPROFILE": str(self._home)}
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def tearDown(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()

    def _run(self, args, side_effect=None):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "consult", side_effect=side_effect or _ok_consult) as consult, \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 1}):
            return broker.consult_decision(args), consult


class OpusHostFallbackTests(FlagshipFallbackTestCase):
    def test_opus_host_fable_unavailable_bounded_falls_back_to_astra_at_high(self):
        # Opus is the second (self) entry in the claude chain [fable, opus], so
        # fable is the only native candidate; once it is unavailable the chain
        # is exhausted for an Opus host, and a bounded decision runs the
        # opposite-vendor chain (codex/astra) at the bounded ladder effort.
        result, consult = self._run(
            _args(
                "claude", "opus", "bounded",
                native_consultation=_native("claude", "unavailable"),
            )
        )
        self.assertEqual([call.args[0] for call in consult.call_args_list], ["codex"])
        self.assertEqual(consult.call_args_list[0].args[1]["target_model"], "gpt-6-astra")
        self.assertEqual(consult.call_args_list[0].args[1]["effort"], "high")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["consultations"][0]["lane"], "native_same_vendor")
        self.assertEqual(result["consultations"][1]["lane"], "switchboard_cross_vendor")
        self.assertEqual(result["consultations"][1]["status"], "completed")


class SonnetHostChainTests(FlagshipFallbackTestCase):
    def test_sonnet_host_needs_native_then_completed_has_no_cross_vendor_leg(self):
        first, consult1 = self._run(_args("claude", "sonnet", "bounded"))
        self.assertEqual(first["status"], "needs_native_consultation")
        self.assertEqual(first["native_request"]["model"], "fable")
        consult1.assert_not_called()

        second, consult2 = self._run(
            _args("claude", "sonnet", "bounded", native_consultation=_native("claude", "completed"))
        )
        self.assertEqual(second["status"], "complete")
        self.assertEqual(
            [item["lane"] for item in second["consultations"]], ["native_same_vendor"]
        )
        consult2.assert_not_called()

    def test_sonnet_host_fable_unavailable_offers_opus_at_same_effort(self):
        result, consult = self._run(
            _args(
                "claude", "sonnet", "critical",
                native_consultation=_native("claude", "unavailable"),
            )
        )
        self.assertEqual(result["status"], "needs_native_consultation")
        self.assertEqual(result["native_request"]["model"], "opus")
        self.assertEqual(result["native_request"]["effort"], "max")
        self.assertEqual(result["native_request"]["fallback_from"], "fable")
        consult.assert_not_called()


class SecondConsultSkipsLatchTests(FlagshipFallbackTestCase):
    def test_second_consult_same_session_skips_latched_model_without_asking(self):
        def outcome(family, call_args):
            return {"status": "error", "response": "HTTP 403: organization subscription access disabled"}

        first, consult1 = self._run(
            _args("gemini", "gemini-3.8-flash-high", "bounded"), outcome
        )
        self.assertIn(("session-1", "codex", "gpt 6 astra"), broker._FLAGSHIP_AVAILABILITY_LATCHES)
        first_codex_calls = [c for c in consult1.call_args_list if c.args[0] == "codex"]
        self.assertEqual(len(first_codex_calls), 2)  # astra, then the previous-frontier sol

        # A second call in the same session must not re-ask either latched model.
        second, consult2 = self._run(
            _args("gemini", "gemini-3.8-flash-high", "bounded"), outcome
        )
        second_codex_calls = [c for c in consult2.call_args_list if c.args[0] == "codex"]
        self.assertEqual(second_codex_calls, [])
        self.assertEqual(second["consultations"][0]["attestation"], "not_run")
        self.assertEqual(second["consultations"][0]["status"], "skipped_unavailable")


class RetryUnavailableTests(FlagshipFallbackTestCase):
    def test_retry_unavailable_attempts_latched_model_once_and_clears_on_success(self):
        broker._set_flagship_latch(("session-1", "claude", "fable"), "plan", "skipped_unavailable", "requires usage credits")
        # Host is Astra itself, so no native step applies; only the cross-vendor
        # claude leg (fable, then opus) runs.
        result, consult = self._run(
            _args("codex", "gpt-6-astra", "architecture", retry_unavailable=True),
            _ok_consult,
        )
        claude_calls = [c for c in consult.call_args_list if c.args[0] == "claude"]
        self.assertEqual(len(claude_calls), 1)
        self.assertEqual(claude_calls[0].args[1]["target_model"], "fable")
        self.assertNotIn(("session-1", "claude", "fable"), broker._FLAGSHIP_AVAILABILITY_LATCHES)

    def test_retry_unavailable_refreshes_a_still_failing_latch(self):
        broker._set_flagship_latch(("session-1", "claude", "fable"), "plan", "skipped_unavailable", "requires usage credits")

        def outcome(family, call_args):
            if family == "claude" and call_args["target_model"] == "fable":
                return {"status": "error", "response": "requires usage credits"}
            return _ok_consult(family, call_args)

        self._run(
            _args("codex", "gpt-6-astra", "architecture", retry_unavailable=True), outcome
        )
        self.assertIn(("session-1", "claude", "fable"), broker._FLAGSHIP_AVAILABILITY_LATCHES)

    def test_retry_unavailable_leaves_untried_latches_alone(self):
        broker._set_flagship_latch(("session-1", "claude", "fable"), "plan", "skipped_unavailable", "requires usage credits")
        broker._set_flagship_latch(("session-1", "claude", "opus"), "plan", "skipped_unavailable", "requires usage credits")
        # Host is Opus itself; its only native candidate (fable) is already
        # latched, so the native chain is silently exhausted and only the
        # codex cross-vendor leg runs. Neither claude latch is attempted (or
        # touched) by this call, retry_unavailable notwithstanding.
        result, consult = self._run(_args("claude", "opus", "architecture"), _ok_consult)
        self.assertEqual([c.args[0] for c in consult.call_args_list], ["codex"])
        self.assertIn(("session-1", "claude", "fable"), broker._FLAGSHIP_AVAILABILITY_LATCHES)
        self.assertIn(("session-1", "claude", "opus"), broker._FLAGSHIP_AVAILABILITY_LATCHES)


class QuotaLatchExpiryTests(unittest.TestCase):
    def setUp(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self._home_dir = tempfile.TemporaryDirectory()
        self._home = Path(self._home_dir.name)
        self.addCleanup(self._home_dir.cleanup)
        home_patch = mock.patch.object(Path, "home", return_value=self._home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        env_patch = mock.patch.dict(
            "os.environ", {"HOME": str(self._home), "USERPROFILE": str(self._home)}
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def tearDown(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()

    def test_relative_minutes_window_expires_on_time(self):
        key = ("session-q", "claude", "fable")
        with mock.patch.object(broker.time, "time", return_value=1_000_000.0):
            broker._set_flagship_latch(key, "quota", "skipped_quota", "Usage limit hit. Try again in 5 minutes.")
        latch = broker._FLAGSHIP_AVAILABILITY_LATCHES[key]
        self.assertAlmostEqual(latch["expires_at"], 1_000_000.0 + 300, delta=1)

        with mock.patch.object(broker.time, "time", return_value=1_000_000.0 + 299):
            active, notice = broker._latch_active(key)
        self.assertTrue(active)
        self.assertIsNone(notice)

        with mock.patch.object(broker.time, "time", return_value=1_000_000.0 + 301):
            active, notice = broker._latch_active(key)
        self.assertFalse(active)
        self.assertIn("quota window passed", notice)
        self.assertNotIn(key, broker._FLAGSHIP_AVAILABILITY_LATCHES)

    def test_resets_at_clock_time_rolls_to_tomorrow_when_already_past(self):
        import time as time_module

        now = time_module.mktime((2026, 1, 1, 10, 0, 0, 0, 0, -1))
        with mock.patch.object(broker.time, "time", return_value=now):
            future_today = broker._parse_quota_latch_expiry("Quota resets at 12:30.")
            past_today = broker._parse_quota_latch_expiry("Quota resets at 09:00.")
        expected_today = time_module.mktime((2026, 1, 1, 12, 30, 0, 0, 0, -1))
        expected_tomorrow = time_module.mktime((2026, 1, 2, 9, 0, 0, 0, 0, -1))
        self.assertAlmostEqual(future_today, expected_today, delta=2)
        self.assertAlmostEqual(past_today, expected_tomorrow, delta=2)

    def test_default_window_used_when_no_pattern_matches(self):
        with mock.patch.dict("os.environ", {"AGENT_BROKER_QUOTA_LATCH_MINUTES": "7"}), \
             mock.patch.object(broker.time, "time", return_value=500.0):
            expiry = broker._parse_quota_latch_expiry("Something went wrong.")
        self.assertAlmostEqual(expiry, 500.0 + 7 * 60, delta=1)


class AccountHashVoidingTests(unittest.TestCase):
    def setUp(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self.stack = tempfile.TemporaryDirectory()
        self.home = Path(self.stack.name)

    def tearDown(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self.stack.cleanup()

    def _write_claude_account(self, account_uuid: str) -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": account_uuid}}), encoding="utf-8"
        )

    def test_latch_voided_when_account_hash_changes_but_not_on_mtime_only(self):
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        with mock.patch.object(Path, "home", return_value=self.home), mock.patch.dict("os.environ", env):
            self._write_claude_account("account-aaa")
            key = ("session-h", "claude", "fable")
            broker._set_flagship_latch(key, "plan", "skipped_unavailable", "requires usage credits")
            self.assertIn(key, broker._FLAGSHIP_AVAILABILITY_LATCHES)

            # Rewriting the exact same content (a bare mtime change) must not void it.
            self._write_claude_account("account-aaa")
            active, notice = broker._latch_active(key)
            self.assertTrue(active)
            self.assertIsNone(notice)
            self.assertIn(key, broker._FLAGSHIP_AVAILABILITY_LATCHES)

            # A different signed-in account must void it.
            self._write_claude_account("account-bbb")
            active, notice = broker._latch_active(key)
            self.assertFalse(active)
            self.assertIn("signed-in account changed", notice)
            self.assertNotIn(key, broker._FLAGSHIP_AVAILABILITY_LATCHES)

    def test_missing_account_file_means_expiry_only_no_crash(self):
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        with mock.patch.object(Path, "home", return_value=self.home), mock.patch.dict("os.environ", env):
            self.assertIsNone(broker._account_identity_hash("claude"))
            self.assertIsNone(broker._account_identity_hash("codex"))


class ClassifierTests(unittest.TestCase):
    def test_credit_text_classifies_plan_before_quota(self):
        sample = {
            "status": "error",
            "response": (
                "API Error: Fable 5.1 requires usage credits. Switch to another model, "
                "or manage usage credits at claude.ai/settings/usage (error type rate_limit, HTTP 429)"
            ),
        }
        status, should_latch, kind = broker._decision_failure_kind(sample)
        self.assertEqual(status, "skipped_unavailable")
        self.assertTrue(should_latch)
        self.assertEqual(kind, "plan")

    def test_plain_429_classifies_as_quota(self):
        sample = {"status": "error", "response": "HTTP 429 too many requests"}
        status, should_latch, kind = broker._decision_failure_kind(sample)
        self.assertEqual(status, "skipped_quota")
        self.assertTrue(should_latch)
        self.assertEqual(kind, "quota")

    def test_native_unavailable_classifier_matches_the_same_priority(self):
        self.assertEqual(
            broker._classify_native_unavailable_kind(
                "requires usage credits (error type rate_limit, HTTP 429)"
            ),
            "plan",
        )
        self.assertEqual(broker._classify_native_unavailable_kind("HTTP 429 too many requests"), "quota")
        self.assertEqual(broker._classify_native_unavailable_kind("connection refused"), "plan")


class CrossVendorChainTests(FlagshipFallbackTestCase):
    def test_codex_host_claude_leg_falls_back_from_fable_to_opus(self):
        def outcome(family, call_args):
            if family == "claude" and call_args["target_model"] == "fable":
                return {"status": "error", "response": "HTTP 429 too many requests"}
            return _ok_consult(family, call_args)

        result, consult = self._run(
            _args(
                "codex", "gpt-5.6-terra", "architecture",
                native_consultation=_native("codex", "completed"),
            ),
            outcome,
        )
        claude_calls = [c for c in consult.call_args_list if c.args[0] == "claude"]
        self.assertEqual([c.args[1]["target_model"] for c in claude_calls], ["fable", "opus"])
        self.assertEqual(result["consultations"][-1]["resolved_model"], "opus")
        self.assertEqual(result["consultations"][-1]["status"], "completed")
        self.assertEqual(result["consultations"][-1]["fallback_from"], "fable")


class ExhaustedChainReportTests(FlagshipFallbackTestCase):
    def test_report_for_a_chain_member_is_accepted_even_when_already_latched(self):
        # The host may report on a chain member that has since been latched by
        # another path; _normalized_native_consultation validates chain
        # membership only, never latch state, so this must not raise.
        broker._set_flagship_latch(("session-1", "claude", "fable"), "plan", "skipped_unavailable", "requires usage credits")
        result, consult = self._run(
            _args(
                "claude", "sonnet", "architecture",
                native_consultation=_native("claude", "unavailable"),
            )
        )
        self.assertEqual(result["consultations"][0]["resolved_model"], "fable")
        self.assertEqual(result["consultations"][0]["native_status"], "unavailable")


class ClassifierIgnoresSuccessfulAdviceTextTests(unittest.TestCase):
    """F1 regression: a completed/pending result must never be reclassified as
    a failure by its own advice text discussing quotas/credits/429/subscription."""

    def test_completed_result_mentioning_quota_credits_429_subscription_stays_completed(self):
        sample = {
            "status": "completed",
            "response": (
                "This advice discusses quota policy, usage credits, HTTP 429 handling, "
                "and subscription tiers as background context, not a failure."
            ),
        }
        status, should_latch, kind = broker._decision_failure_kind(sample)
        self.assertIsNone(status)
        self.assertFalse(should_latch)
        self.assertIsNone(kind)

    def test_pending_and_ok_and_async_results_are_never_scanned(self):
        for sample in (
            {"status": "pending", "response": "quota"},
            {"status": "ok", "response": "429 rate limit subscription"},
            {"status": "queued", "response": "usage credits"},
            {"status": "running", "response": "quota exceeded"},
            {"status": "weird", "async": True, "response": "quota exceeded"},
        ):
            with self.subTest(sample=sample):
                status, should_latch, kind = broker._decision_failure_kind(sample)
                self.assertIsNone(status)
                self.assertFalse(should_latch)
                self.assertIsNone(kind)

    def test_error_status_with_credit_text_still_classifies_as_plan(self):
        sample = {
            "status": "error",
            "response": (
                "API Error: Fable 5.1 requires usage credits. Switch to another model, "
                "or manage usage credits at claude.ai/settings/usage (error type rate_limit, HTTP 429)"
            ),
        }
        status, should_latch, kind = broker._decision_failure_kind(sample)
        self.assertEqual(status, "skipped_unavailable")
        self.assertTrue(should_latch)
        self.assertEqual(kind, "plan")

    def test_only_error_bearing_fields_are_scanned_not_the_whole_result(self):
        # A field outside error/stderr/response/message (e.g. an echoed prompt
        # or brief) must never trigger a failure classification.
        sample = {
            "status": "error",
            "prompt": "Please discuss quota, usage credits, 429, and subscription policy.",
            "brief": {"decision": "quota subscription usage credits 429"},
            "error": "connection refused",
        }
        status, should_latch, kind = broker._decision_failure_kind(sample)
        self.assertEqual(status, "skipped_unavailable")
        # "connection refused" alone is not plan/quota wording, so it falls
        # into the generic unavailable bucket (kind defaults to plan).
        self.assertEqual(kind, "plan")


class ReconciliationIgnoresSuccessfulAdviceTextTests(unittest.TestCase):
    """F1 regression for the async/detached reconciliation path."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmpdir.name)
        self.paths = {
            "DB_PATH": root / "broker.db",
            "BROKER_DIR": root / "broker",
            "LOG_PATH": root / "broker" / "broker.log",
        }
        self.stack = mock.patch.multiple(broker, **self.paths)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        # Defense in depth: no code path in this test should reach the real
        # account files (the fix means no latch is written), but redirect
        # HOME/USERPROFILE/Path.home anyway per this file's isolation policy.
        home = root / "home"
        home_patch = mock.patch.object(Path, "home", return_value=home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        env_patch = mock.patch.dict("os.environ", {"HOME": str(home), "USERPROFILE": str(home)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        broker.init_db()

    def tearDown(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self.tmpdir.cleanup()

    def test_completed_async_row_mentioning_quota_stays_completed(self):
        args = {
            "project": "project-a",
            "topic": "flagship-fallback-f1",
            "session_id": "session-f1",
            "work_package_id": "WP-F1-ASYNC",
            "host": {"vendor": "codex", "model": "gpt-6-astra"},
            "complexity": "architecture",
            "brief": _brief(),
        }
        with mock.patch.object(
            broker, "consult",
            return_value={"status": "pending", "request_id": "async-f1", "async": True},
        ):
            parent = broker.consult_decision(args)
        self.assertEqual(parent["status"], "pending")

        with broker.db_connect() as conn:
            conn.execute(
                """
                INSERT INTO claude_requests (
                    id, project, root_path, topic, prompt, status, response, error,
                    created_by, created_at, completed_at, responder, responder_model, target_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "async-f1", "project-a", str(Path.cwd()), "flagship-fallback-f1", "decision brief",
                    "completed",
                    "This response mentions quota, usage credits, HTTP 429, and subscription as context.",
                    None, "agent-switchboard", broker.utc_now(), broker.utc_now(),
                    "claude-cli-worker", "claude:fable", "fable",
                ),
            )
        result = broker.request_result("async-f1")
        update = result["decision_update"]
        self.assertEqual(update["updated_target_status"], "completed")
        self.assertEqual(update["terminal_overall_status"], "complete")
        self.assertNotIn(("session-f1", "claude", "fable"), broker._FLAGSHIP_AVAILABILITY_LATCHES)


class NativeChainExcludesPreviousFrontierTests(FlagshipFallbackTestCase):
    def test_codex_host_sol_astra_unavailable_goes_cross_vendor_not_native_sol(self):
        # gpt-5.6-sol is the cross-vendor resilience fallback, never a native
        # escalation target: once astra is unavailable, a Sol-hosted session
        # must not be asked to natively consult Sol (itself, via the chain).
        first, consult1 = self._run(_args("codex", "gpt-5.6-sol", "bounded"))
        self.assertEqual(first["status"], "needs_native_consultation")
        self.assertEqual(first["native_request"]["model"], "gpt-6-astra")
        consult1.assert_not_called()

        second, consult2 = self._run(
            _args(
                "codex", "gpt-5.6-sol", "bounded",
                native_consultation=_native("codex", "unavailable"),
            )
        )
        self.assertNotEqual(second.get("status"), "needs_native_consultation")
        claude_calls = [c for c in consult2.call_args_list if c.args[0] == "claude"]
        self.assertEqual(len(claude_calls), 1)
        self.assertEqual(claude_calls[0].args[1]["effort"], "high")


class NativeReportAtOrBelowHostTierTests(FlagshipFallbackTestCase):
    def test_opus_host_reporting_opus_itself_is_not_counted_native_completed(self):
        result, consult = self._run(
            _args(
                "claude", "opus", "bounded",
                native_consultation=_native("claude", "completed", model="opus"),
            )
        )
        self.assertEqual(result["consultations"][0]["resolved_model"], "opus")
        self.assertTrue(
            any("at or below the host's own tier" in notice for notice in result["handoff_notices"])
        )
        # Not counted as native_completed, so the bounded suppression rule
        # does not apply and the cross-vendor leg still runs.
        codex_calls = [c for c in consult.call_args_list if c.args[0] == "codex"]
        self.assertEqual(len(codex_calls), 1)
        self.assertEqual(result["consultations"][-1]["lane"], "switchboard_cross_vendor")


class FallbackHistoryNoticeTests(FlagshipFallbackTestCase):
    def test_native_report_carries_forward_earlier_latched_candidate_notice(self):
        # Simulate a prior turn in this session already having latched fable
        # (e.g. from an earlier needs_native_consultation round for a Sonnet
        # host), then the host reports on the next chain member, opus.
        broker._set_flagship_latch(
            ("session-1", "claude", "fable"), "plan", "skipped_unavailable", "requires usage credits"
        )
        result, consult = self._run(
            _args(
                "claude", "sonnet", "architecture",
                native_consultation=_native("claude", "completed", model="opus"),
            )
        )
        self.assertTrue(
            any(
                notice.startswith("Skipped claude:fable earlier this session:")
                for notice in result["handoff_notices"]
            ),
            result["handoff_notices"],
        )


if __name__ == "__main__":
    unittest.main()
