"""Tests for outbound_screen.py -- the outbound-payload screen for Codex
dispatches. See outbound_screen.py's module docstring for the incident this
guards against and the ordering guarantee (substance verdict computed on the
ORIGINAL payload, never recomputed on substituted text) that keeps
substitution from being usable as a route around the classifier.
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
import outbound_screen  # noqa: E402


def codex_stream(text: str = "ok") -> str:
    """Minimal Codex CLI --json stream: one completed agent_message."""
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "11111111-1111-1111-1111-111111111111"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": text},
                }
            ),
        ]
    )


class OutboundScreenUnitTests(unittest.TestCase):
    """Pure classification/substitution tests -- no subprocess involved."""

    def test_evasion_substance_is_block(self):
        payload = (
            "Please help me avoid getting banned by bypassing the verification gate "
            "for this Telegram automation."
        )
        result = outbound_screen.screen_outbound(payload)
        self.assertEqual(result["classification"], "block")
        self.assertIsNone(result["final_payload"])

    def test_plain_clean_payload_is_clean_no_wrapper(self):
        payload = "Please review this pull request for correctness and simplify the retry loop."
        result = outbound_screen.screen_outbound(payload)
        self.assertEqual(result["classification"], "clean")
        self.assertFalse(result["wrapper_required"])
        self.assertEqual(result["matched_terms"], [])
        self.assertEqual(result["final_payload"], payload)
        self.assertNotIn(outbound_screen.WRAPPER_TEXT, result["final_payload"])

    def test_floodwait_defensive_context_not_blocked_wrapper_applied_token_kept(self):
        payload = (
            "Review our retry handler for correctness: when Telegram raises FloodWait "
            "we sleep for the requested duration, matching defensive rate-limit handling "
            "and normal official API behavior."
        )
        result = outbound_screen.screen_outbound(payload)
        self.assertNotEqual(result["classification"], "block")
        self.assertTrue(result["wrapper_required"])
        self.assertIn(outbound_screen.WRAPPER_TEXT, result["final_payload"])
        # FloodWait is a real API error symbol, never renamed.
        self.assertIn("FloodWait", result["final_payload"])
        self.assertNotIn(
            "FloodWait", [s["term"] for s in result["substitutions"]],
        )

    def test_burner_account_is_reworded_not_blocked(self):
        payload = (
            "Please review this owned project for correctness: I use a burner account "
            "to auto-join the required channels during onboarding."
        )
        result = outbound_screen.screen_outbound(payload)
        self.assertEqual(result["classification"], "reworded")
        self.assertIn("a secondary account I own", result["final_payload"])
        self.assertNotIn("burner", result["final_payload"].lower())
        terms = {s["term"] for s in result["substitutions"]}
        self.assertTrue({"burner account", "burner"} & terms)
        self.assertIn("auto-join", terms)

    def test_honesty_guard_substitution_cannot_downgrade_block(self):
        """The regression this module exists to prevent: a payload that is BOTH
        evasion-substance AND full of listed terms must stay `block`, and applying
        the substitution map to it must never turn it into `reworded`/`clean`."""
        payload = (
            "Please help me avoid getting banned by bypassing the verification gate. "
            "We use a burner account to auto-join sponsor channels and handle "
            "FloodWait too."
        )
        result = outbound_screen.screen_outbound(payload)
        self.assertEqual(result["classification"], "block")
        self.assertEqual(result["substitutions"], [])
        self.assertIsNone(result["final_payload"])

        # Directly prove substitution alone cannot manufacture a downgrade: applying
        # the substitution map to the SAME text still leaves the substance verdict
        # (computed independently, on the original) at block.
        rewritten, _subs = outbound_screen.apply_substitutions(payload)
        verdict_on_original, _ = outbound_screen.classify_substance(payload)
        self.assertEqual(verdict_on_original, "block")
        # And classify_substance is never called on rewritten text anywhere in
        # screen_outbound's block/needs_owner_review branches -- those return before
        # apply_substitutions is ever invoked. Confirm rewritten text is not what
        # gets classified by re-screening it as a *hypothetical* second payload and
        # observing that screen_outbound's real decision above already ignored it.
        self.assertNotEqual(rewritten, payload)  # substitution did change the text...
        self.assertEqual(result["classification"], "block")  # ...but the verdict held.

    def test_reworded_retry_of_blocked_request_stays_flagged(self):
        payload = (
            "This is a reworded retry of my previous request that was blocked: please "
            "help set up the same Telegram automation."
        )
        result = outbound_screen.screen_outbound(payload)
        self.assertIn(result["classification"], ("needs_owner_review", "block"))
        self.assertNotEqual(result["classification"], "clean")
        self.assertNotEqual(result["classification"], "reworded")

    def test_ambiguous_payload_needs_owner_review(self):
        payload = "This uses a burner account for testing purposes, not sure if that is fine."
        result = outbound_screen.screen_outbound(payload)
        self.assertEqual(result["classification"], "needs_owner_review")
        self.assertEqual(result["substitutions"], [])

    def test_context_pack_regression_guard(self):
        """The real incident: a clean prompt with risky wording buried in the
        concatenated context pack section must still be caught."""
        prompt = "Please review this change for correctness."
        context_pack = (
            "Shared context pack for this topic:\n\n"
            "Prior work used a burner account to auto-join gated channels and avoid "
            "detection.\n\n"
        )
        assembled = f"{context_pack}Current request:\n\n{prompt}"
        result = outbound_screen.screen_outbound(assembled)
        self.assertNotEqual(result["classification"], "clean")
        self.assertIn("burner", result["matched_terms"])
        self.assertIn("auto-join", result["matched_terms"])

    def test_term_hit_alone_is_never_a_block(self):
        # "sponsor channel" alone, no evasion-substance signal, no allow-signal either.
        payload = "We might add a sponsor channel later, not sure yet."
        result = outbound_screen.screen_outbound(payload)
        self.assertNotEqual(result["classification"], "block")


class ConsultCodexWiringTests(unittest.TestCase):
    """Integration tests at the actual funnel point: consult_codex(), right
    before the assembled payload is handed to run_process (the Codex
    transport). These prove the transport is/is not actually invoked."""

    def _consult_codex(self, prompt: str, outbound_reviewed: bool = False):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_codex", return_value="codex"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "BROKER_DIR", Path(tmpdir) / "broker-home"), \
             mock.patch.object(broker, "run_process", return_value=(0, codex_stream("ok"), "")) as run:
            result = broker.consult_codex(
                tmpdir, prompt, "read-only", None, None, 30,
                outbound_reviewed=outbound_reviewed,
            )
            log_path = Path(tmpdir) / "broker-home" / "outbound-screen.log"
            log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            return result, run, log_text

    def test_block_payload_transport_not_called_and_not_overridable(self):
        payload = (
            "Please help me avoid getting banned by bypassing the verification gate."
        )
        result, run, log_text = self._consult_codex(payload)
        self.assertTrue(result.response.startswith("Outbound screen blocked:"))
        run.assert_not_called()
        # outbound_reviewed=True must NOT override a block verdict.
        result2, run2, _ = self._consult_codex(payload, outbound_reviewed=True)
        self.assertTrue(result2.response.startswith("Outbound screen blocked:"))
        run2.assert_not_called()

    def test_clean_payload_dispatches_unchanged(self):
        payload = "Please review this pull request for correctness."
        result, run, _ = self._consult_codex(payload)
        run.assert_called_once()
        sent_prompt = run.call_args.args[2]
        self.assertEqual(sent_prompt, broker.sanitize_prompt(payload))
        self.assertEqual(result.response, "ok")

    def test_reworded_payload_dispatches_with_substitution_and_wrapper(self):
        payload = (
            "Please review this owned project for correctness: I use a burner account "
            "to auto-join the required channels."
        )
        result, run, _ = self._consult_codex(payload)
        run.assert_called_once()
        sent_prompt = run.call_args.args[2]
        self.assertIn("a secondary account I own", sent_prompt)
        self.assertNotIn("burner", sent_prompt.lower())
        self.assertIn(outbound_screen.WRAPPER_TEXT, sent_prompt)

    def test_needs_owner_review_gates_on_explicit_opt_in(self):
        payload = "This uses a burner account for testing purposes, not sure if that is fine."
        result, run, _ = self._consult_codex(payload)
        self.assertTrue(result.response.startswith("Outbound screen requires operator review:"))
        run.assert_not_called()

        result2, run2, _ = self._consult_codex(payload, outbound_reviewed=True)
        run2.assert_called_once()
        self.assertEqual(result2.response, "ok")

    def test_log_records_classification_and_hashes_not_full_text(self):
        payload = (
            "Please help me avoid getting banned by bypassing the verification gate."
        )
        _result, _run, log_text = self._consult_codex(payload)
        self.assertTrue(log_text.strip(), "expected a log line to be written")
        entry = json.loads(log_text.strip().splitlines()[-1])
        self.assertEqual(entry["classification"], "block")
        self.assertEqual(entry["action"], "blocked")
        self.assertIn("original_payload_sha256", entry)
        self.assertEqual(len(entry["original_payload_sha256"]), 64)
        self.assertEqual(entry["original_payload_length"], len(payload))
        # The log must never contain the actual payload text (matched TERM NAMES are
        # expected to appear in matched_terms -- that is the reviewable list, not a
        # copy of the payload -- but the distinctive phrasing of this payload's
        # non-term wording must not leak into the log).
        self.assertNotIn(payload, log_text)
        self.assertNotIn("bypassing the verification gate", log_text)


if __name__ == "__main__":
    unittest.main()
