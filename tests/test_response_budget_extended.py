"""WP-SB8B: the response-envelope budget applied to every route_agent_task/
consult result (item 1), the opt-in extended prompt_budget tier (item 2), the
Flash no-length-pressure schema change (item 3), and the sharpened
research_questions length error (item 4).

Stdlib-only. Every sqlite/DB touch is redirected to a TemporaryDirectory via
DB_PATH/BROKER_DIR patches, mirroring tests/test_consult_child_guard.py.
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
import routing_gate  # noqa: E402


class _TempBrokerHomeTestCase(unittest.TestCase):
    """Every test that stores/retrieves shared context needs a real (but
    disposable) sqlite DB -- store_shared_context/retrieve_shared_context are
    exercised for real, not mocked, so the "retrievable by ref" claim is
    actually checked end to end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        tmpdir = Path(self._tmp.name)
        db_patch = mock.patch.object(broker, "DB_PATH", tmpdir / "state.sqlite")
        dir_patch = mock.patch.object(broker, "BROKER_DIR", tmpdir)
        db_patch.start()
        dir_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(dir_patch.stop)
        broker.init_db()


class ResponseEnvelopeBudgetTests(_TempBrokerHomeTestCase):
    def _big_structured_result(self) -> dict:
        big_answer = "A" * 25_000
        return {
            "status": "ok",
            "outcome": "creditable",
            "accepted": False,
            "credit_eligible": True,
            "worker_status": "completed",
            "disposition": None,
            "receipt": "broker:abc-123",
            "work_package_id": "WP-TEST",
            "model": "antigravity:gemini-flash-high",
            "attested_model": "gemini-flash-high",
            "attestation": "verified",
            "model_attested": True,
            "elapsed_seconds": 12.5,
            # The raw "response" duplicates structured_output verbatim -- exactly
            # the defect described in Problem A (max_response_chars=8000 caller
            # actually got 24,640 chars because this duplicate was never capped).
            "response": json.dumps({"summary": big_answer}),
            "structured_output": {
                "package_id": "WP-TEST",
                "status": "completed",
                "summary": big_answer,
                "research_coverage": [
                    {"question": "Q1?", "answer": big_answer, "confidence": "high"},
                ],
            },
        }

    def test_25k_structured_result_fits_8000_char_budget_with_header_intact(self):
        result = self._big_structured_result()
        trimmed = broker.apply_response_envelope_budget(dict(result), 8000, None, "t", "test")
        serialized = json.dumps(trimmed, ensure_ascii=False)
        self.assertLessEqual(len(serialized), 8000)
        # Header fields survive whole -- these are what the routing gate and the
        # completion audit read; they must never be trimmed away.
        for key in (
            "status", "outcome", "accepted", "credit_eligible", "worker_status",
            "receipt", "work_package_id", "model", "attested_model", "attestation",
            "model_attested", "elapsed_seconds",
        ):
            self.assertEqual(trimmed[key], result[key], key)
        self.assertTrue(trimmed["truncated"])
        self.assertIn("response_ref", trimmed)
        self.assertIsNotNone(trimmed["response_ref"])

    def test_full_untrimmed_result_is_retrievable_by_ref(self):
        result = self._big_structured_result()
        trimmed = broker.apply_response_envelope_budget(dict(result), 8000, None, "t", "test")
        fetched = broker.retrieve_shared_context(trimmed["response_ref"], limit=80000)
        self.assertIn("structured_output", fetched["content"])
        # The full 25k-char answer is recoverable even though the envelope trimmed it.
        self.assertIn("A" * 25_000, fetched["content"])

    def test_no_duplication_when_structured_output_present(self):
        result = self._big_structured_result()
        trimmed = broker.apply_response_envelope_budget(dict(result), 8000, None, "t", "test")
        # The raw "response" string (a duplicate of structured_output) is dropped;
        # only the ref remains.
        self.assertNotIn("response", trimmed)

    def test_default_budget_is_6000_when_unset(self):
        result = self._big_structured_result()
        trimmed = broker.apply_response_envelope_budget(dict(result), None, None, "t", "test")
        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False)), 6000)
        trimmed_small = broker.apply_response_envelope_budget(
            {"status": "ok", "work_package_id": "WP-1"}, None, None, "t", "test"
        )
        self.assertFalse(trimmed_small["truncated"])

    def test_result_already_within_budget_is_unchanged_bar_truncated_flag(self):
        small = {"status": "ok", "work_package_id": "WP-1", "response": "short"}
        out = broker.apply_response_envelope_budget(dict(small), 8000, None, "t", "test")
        self.assertEqual(out["response"], "short")
        self.assertFalse(out["truncated"])


class EnvelopeHeaderGuaranteeTests(_TempBrokerHomeTestCase):
    """WP-SB8D item 2: the header used to be kept whole, so a large
    native_handoff (or any other bulky header object/string) could push the
    final serialized result past max_response_chars, and the final fallback
    returned without re-checking size at all. apply_response_envelope_budget
    now bounds every header field individually and guarantees
    len(json.dumps(result)) <= max_response_chars in every case."""

    def _big_native_handoff(self) -> dict:
        return {
            "work_package_id": "WP-TEST",
            "semantic_lane": "workhorse",
            "native": {
                "family": "codex", "role": "worker", "model": "gpt-6-sol",
                "effort": "medium", "mechanism": "managed_native_subagent",
            },
            "flash_outcome": "rejected",
            "flash_skip_reason": "flash-failed:broker:abc-123",
            "record_requirement": "x" * 6_000,
            "action": "Start the named current native role with the same bounded package.",
            "brain_review": {"required": True, "action": "y" * 6_000},
        }

    def test_oversized_native_handoff_still_fits_budget(self):
        result = {
            "status": "skipped_unavailable",
            "outcome": "rejected",
            "accepted": False,
            "credit_eligible": False,
            "receipt": "broker:abc-123",
            "work_package_id": "WP-TEST",
            "native_handoff": self._big_native_handoff(),
            "response": "short body",
        }
        trimmed = broker.apply_response_envelope_budget(dict(result), 2000, None, "t", "test")
        serialized = json.dumps(trimmed, ensure_ascii=False)
        self.assertLessEqual(len(serialized), 2000)
        # The essential native_handoff scalars survive, condensed to a small stub.
        handoff = trimmed.get("native_handoff") or {}
        if handoff and "note" not in handoff:
            self.assertEqual(handoff.get("family"), "codex")
            self.assertEqual(handoff.get("role"), "worker")
            self.assertEqual(handoff.get("model"), "gpt-6-sol")
            self.assertEqual(handoff.get("flash_skip_reason"), "flash-failed:broker:abc-123")
        # The full object is always recoverable via response_ref.
        self.assertIn("response_ref", trimmed)
        fetched = broker.retrieve_shared_context(trimmed["response_ref"], limit=80000)
        self.assertIn("record_requirement", fetched["content"])

    def test_native_handoff_within_cap_is_kept_intact(self):
        small_handoff = {"family": "codex", "role": "worker", "model": "gpt-6-sol", "action": "go"}
        result = {
            "status": "ok",
            "native_handoff": small_handoff,
            "structured_output": {"summary": "A" * 20_000},
        }
        trimmed = broker.apply_response_envelope_budget(dict(result), 6000, None, "t", "test")
        self.assertEqual(trimmed["native_handoff"], small_handoff)

    def test_pathological_every_field_huge_still_fits_budget(self):
        huge = "Z" * 50_000
        result = {
            "status": "ok",
            "outcome": huge,
            "accepted": True,
            "credit_eligible": True,
            "worker_status": huge,
            "disposition": huge,
            "receipt": huge,
            "work_package_id": huge,
            "model": huge,
            "attested_model": huge,
            "attestation": huge,
            "model_attested": True,
            "elapsed_seconds": 1.0,
            "native_handoff": {"blob": huge, "nested": {"more": huge}},
            "caveats": [huge, huge, huge],
            "structured_output": {"summary": huge, "detail": huge},
            "response": huge,
        }
        for budget in (800, 2000, 8000):
            trimmed = broker.apply_response_envelope_budget(dict(result), budget, None, "t", "test")
            serialized = json.dumps(trimmed, ensure_ascii=False)
            self.assertLessEqual(len(serialized), budget, f"budget={budget}")
            self.assertIn("status", trimmed)

    def test_guarantee_holds_even_at_the_minimum_800_char_budget(self):
        # max_response_chars is clamped to a minimum of 800 regardless of what
        # the caller asked for -- the guarantee must still hold there.
        result = {
            "status": "skipped_quota",
            "outcome": "rejected",
            "accepted": False,
            "credit_eligible": False,
            "receipt": "X" * 5000,
            "work_package_id": "Y" * 5000,
            "native_handoff": {"a": "b" * 5000},
            "response": "Z" * 5000,
        }
        trimmed = broker.apply_response_envelope_budget(dict(result), 1, None, "t", "test")
        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False)), 800)


class PromptBudgetTierTests(_TempBrokerHomeTestCase):
    def test_standard_default_matches_existing_frontier_cap(self):
        tier, reason = broker._resolve_prompt_budget({})
        self.assertEqual(tier, "standard")
        self.assertIsNone(reason)
        max_bytes, max_tokens = broker._prompt_budget_provider_limits(tier)
        self.assertEqual(max_bytes, broker.DECISION_PROVIDER_PROMPT_MAX_BYTES)
        self.assertEqual(max_bytes, routing_gate.FLAGSHIP_PROMPT_MAX_BYTES)
        self.assertEqual(max_tokens, broker.DECISION_PROVIDER_PROMPT_MAX_TOKENS)

    def test_extended_without_reason_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "budget_reason is required"):
            broker._resolve_prompt_budget({"prompt_budget": "extended"})
        with self.assertRaisesRegex(ValueError, "budget_reason is required"):
            broker._resolve_prompt_budget({"prompt_budget": "extended", "budget_reason": "   "})

    def test_extended_reason_over_200_chars_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at most 200 characters"):
            broker._resolve_prompt_budget(
                {"prompt_budget": "extended", "budget_reason": "x" * 201}
            )

    def test_invalid_tier_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "standard.*extended"):
            broker._resolve_prompt_budget({"prompt_budget": "huge"})

    def test_extended_tier_triples_provider_prompt_budget(self):
        tier, reason = broker._resolve_prompt_budget(
            {"prompt_budget": "extended", "budget_reason": "large repro log needed"}
        )
        self.assertEqual(tier, "extended")
        self.assertEqual(reason, "large repro log needed")
        max_bytes, max_tokens = broker._prompt_budget_provider_limits(tier)
        self.assertEqual(max_bytes, routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES)
        self.assertEqual(max_bytes, broker.DECISION_PROVIDER_PROMPT_MAX_BYTES * 3)
        self.assertEqual(max_tokens, broker.DECISION_PROVIDER_PROMPT_MAX_TOKENS * 3)

    def test_decision_limits_extended_triples_every_byte_token_budget_not_counts(self):
        standard = broker._prompt_budget_decision_limits("standard")
        extended = broker._prompt_budget_decision_limits("extended")
        for key in (
            "brief_bytes", "brief_tokens", "evidence_excerpt_bytes",
            "provider_prompt_bytes", "provider_prompt_tokens", "combined_bytes",
        ):
            self.assertEqual(extended[key], standard[key] * 3, key)

    def test_route_agent_task_frontier_cap_standard_rejects_over_14000_bytes(self):
        big_prompt = "x" * (broker.DECISION_PROVIDER_PROMPT_MAX_BYTES + 500)
        with self.assertRaisesRegex(ValueError, "exceeds the provider budget") as ctx:
            broker._enforce_frontier_prompt_cap("codex", "gpt-6-astra", big_prompt, {})
        self.assertIn("prompt_budget='extended'", str(ctx.exception))

    def test_route_agent_task_frontier_cap_extended_allows_up_to_42000_bytes(self):
        prompt = "x" * (broker.DECISION_PROVIDER_PROMPT_MAX_BYTES + 500)
        tier, reason = broker._enforce_frontier_prompt_cap(
            "codex", "gpt-6-astra", prompt,
            {"prompt_budget": "extended", "budget_reason": "bounded large diff review"},
        )
        self.assertEqual(tier, "extended")
        self.assertEqual(reason, "bounded large diff review")

    def test_route_agent_task_frontier_cap_extended_still_rejects_over_42000_bytes(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_EXTENDED_MAX_BYTES + 500)
        with self.assertRaisesRegex(ValueError, "exceeds the provider budget"):
            broker._enforce_frontier_prompt_cap(
                "codex", "gpt-6-astra", big_prompt,
                {"prompt_budget": "extended", "budget_reason": "still bounded"},
            )


class ConsultDecisionExtendedBudgetTests(_TempBrokerHomeTestCase):
    def _small_brief(self) -> dict:
        return {"decision": "x" * 200}

    def _maxed_brief(self) -> dict:
        # Spreads bytes across every field (each item under its own fixed
        # per-item cap, which the extended tier never changes) so the total
        # lands comfortably between the standard (12,000) and extended
        # (36,000) aggregate byte budgets -- roughly 23-24KB serialized.
        return {
            "decision": "d" * 2000,
            "constraints": ["c" * 500 for _ in range(8)],
            "options": [{"id": f"opt{i}", "summary": "s" * 1000} for i in range(4)],
            "proposed_choice": "p" * 500,
            "questions": ["q" * 500 for _ in range(3)],
            "uncertainties": ["u" * 500 for _ in range(6)],
            "evidence": [{"ref": "r" * 500, "claim": "c" * 500} for _ in range(8)],
        }

    def _base_args(self, **overrides) -> dict:
        args = {
            "work_package_id": "WP-DEC-1",
            "host": {"vendor": "gemini", "model": "gemini-3.5"},
            "complexity": "bounded",
            "brief": self._small_brief(),
        }
        args.update(overrides)
        return args

    def test_standard_brief_over_12000_bytes_is_rejected_with_opt_in_hint(self):
        args = self._base_args(brief=self._maxed_brief())
        with mock.patch.object(broker, "decision_consult_targets", return_value=[]):
            with self.assertRaisesRegex(ValueError, "exceeds the preflight budget") as ctx:
                broker.consult_decision(args)
        self.assertIn("prompt_budget='extended'", str(ctx.exception))

    def test_extended_brief_up_to_36000_bytes_is_accepted(self):
        args = self._base_args(
            brief=self._maxed_brief(),
            prompt_budget="extended",
            budget_reason="large migration decision brief",
        )
        with mock.patch.object(broker, "decision_consult_targets", return_value=[]):
            result = broker.consult_decision(args)
        self.assertEqual(result["prompt_budget"], "extended")
        self.assertEqual(result["budget_reason"], "large migration decision brief")

    def test_extended_requires_nonempty_budget_reason(self):
        args = self._base_args(prompt_budget="extended")
        with self.assertRaisesRegex(ValueError, "budget_reason is required"):
            broker.consult_decision(args)

    def test_standard_result_echoes_prompt_budget_standard(self):
        args = self._base_args()
        with mock.patch.object(broker, "decision_consult_targets", return_value=[]):
            result = broker.consult_decision(args)
        self.assertEqual(result["prompt_budget"], "standard")
        self.assertNotIn("budget_reason", result)


class FlashNoLengthPressureTests(unittest.TestCase):
    def test_research_answer_schema_has_no_tight_length_cap(self):
        package = {
            "package_id": "WP-1", "task_kind": "research",
            "research_questions": ["Q1?"],
        }
        schema = broker.flash_workhorse_output_schema(package)
        answer_schema = schema["properties"]["research_coverage"]["items"]["properties"]["answer"]
        self.assertGreaterEqual(answer_schema.get("maxLength", 10**9), 20_000)

    def test_worker_contract_tells_flash_not_to_trim(self):
        package = broker.prepare_flash_work_package(
            {"research_questions": ["Q1?"]}, "research", "Investigate."
        )
        prompt = broker.wrap_flash_workhorse_prompt("Investigate.", package)
        self.assertIn("no length limit on your answer", prompt)
        self.assertIn("Do not measure, trim or shorten your output", prompt)
        self.assertNotIn("8,000 characters", prompt)


class ResearchQuestionLengthErrorTests(unittest.TestCase):
    def test_over_500_chars_names_item_length_and_limit(self):
        with self.assertRaisesRegex(
            ValueError, r"research_questions item 1 is 501 characters; the limit is 500"
        ):
            broker._bounded_research_questions(["x" * 501])
        with self.assertRaisesRegex(ValueError, "Split it into smaller"):
            broker._bounded_research_questions(["x" * 501])

    def test_second_item_reports_its_own_index(self):
        with self.assertRaisesRegex(ValueError, r"research_questions item 2 is 600 characters"):
            broker._bounded_research_questions(["short question", "y" * 600])


if __name__ == "__main__":
    unittest.main()
