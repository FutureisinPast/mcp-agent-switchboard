"""WP-1073-NO-WASTED-RESULTS: weak_answers, condensed handoff preservation,
pretty-printed redacted storage, disposition labels, and version 1.0.74.
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
import switchboard_version  # noqa: E402

MODEL = "gemini-3.7-flash-high"


class WeakAnswersBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.ws = self.tmp_root / "ws"
        self.src_a = self.ws / "src" / "a.py"
        self.src_a.parent.mkdir(parents=True, exist_ok=True)
        self.src_a.write_text("VALUE = 1\n", encoding="utf-8")
        self.broker_dir = self.tmp_root / ".agent-broker"
        self.broker_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.broker_dir / "state.sqlite"
        for patcher in (
            mock.patch.object(broker, "BROKER_DIR", self.broker_dir),
            mock.patch.object(broker, "DB_PATH", self.db_path),
            mock.patch.object(broker, "LOG_PATH", self.broker_dir / "agent-broker.log"),
            mock.patch.object(broker, "CONFIG_PATH", self.broker_dir / "config.json"),
            mock.patch.object(broker, "WORKER_LOG_DIR", self.broker_dir / "flash-logs"),
            mock.patch.object(broker, "QUARANTINE_ROOT", self.broker_dir / "flash-quarantine"),
            mock.patch.object(broker, "QUARANTINE_LOCK_PATH", self.broker_dir / "flash-quarantine.lock"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        broker.init_db()

    def read_only_package(self, *questions: str, **extra) -> dict:
        args = {
            "work_package_id": "WP-WEAK-RO",
            "read_context": [str(self.src_a)],
            "workspace_root": str(self.ws),
            "research_questions": list(questions or ("What does VALUE equal?",)),
        }
        args.update(extra)
        pkg = broker.prepare_flash_work_package(args, "research", "Investigate.")
        return {**pkg, "mode": "plan"}

    def accept_edits_package(self, *questions: str) -> dict:
        pkg = broker.prepare_flash_work_package(
            {
                "work_package_id": "WP-WEAK-IMPL",
                "allowed_files": [str(self.src_a)],
                "workspace_root": str(self.ws),
                "acceptance_criteria": ["Update VALUE safely."],
                "research_questions": list(questions or ("What does VALUE equal?",)),
            },
            "implementation",
            "Implement after checking.",
        )
        # Keep research_questions on the package to test strict accept-edits validation.
        return {
            **pkg,
            "mode": "accept-edits",
            "research_questions": list(questions or ("What does VALUE equal?",)),
        }

    @staticmethod
    def evidence_item(*, primary: bool = True, locator: str = "L1", obs: str = "VALUE = 1") -> dict:
        return {
            "source_kind": "code",
            "location": "src/a.py",
            "locator": locator,
            "observation": obs,
            "primary": primary,
        }

    def coverage_answered(self, question: str, evidence: list[dict], **overrides) -> dict:
        item = {
            "question": question,
            "status": "answered",
            "answer": "VALUE is 1.",
            "confidence": "high",
            "evidence": evidence,
            "competing_explanations_checked": ["Checked for override in same file."],
            "gaps": [],
        }
        item.update(overrides)
        return item

    def coverage_not_found(self, question: str, evidence: list[dict], **overrides) -> dict:
        item = {
            "question": question,
            "status": "not_found",
            "answer": "NOT FOUND in src/a.py.",
            "confidence": "medium",
            "evidence": evidence,
            "competing_explanations_checked": [],
            "gaps": ["Searched src/a.py; helper module was not in scope."],
        }
        item.update(overrides)
        return item

    def outer_payload(self, package: dict, coverage: list[dict], **overrides) -> dict:
        structured = {
            "package_id": package["package_id"],
            "status": "completed",
            "summary": "Completed investigation.",
            "acceptance_criteria": [
                {"criterion": c, "status": "passed", "evidence": ["src/a.py:L1"]}
                for c in (package.get("acceptance_criteria") or [])
            ],
            "files_changed": [],
            "checks": [],
            "evidence": [
                {"claim": "VALUE is 1", "path": "src/a.py", "line": "1", "observation": "VALUE = 1"}
            ],
            "claims": [],
            "research_coverage": coverage,
            "scope_gaps": [],
            "ambiguities": [],
            "risks": ["Static read only."],
            "next_action": "Brain verifies cited lines.",
            "brain_verification_required": "required",
        }
        structured.update(overrides)
        return {
            "conversation_id": "conv-weak-1",
            "status": "SUCCESS",
            "model": MODEL,
            "structured_output": structured,
            "duration_seconds": 1.2,
            "num_turns": 1,
            "usage": {"total_tokens": 50},
        }

    def run_consult(self, package: dict, outer: dict, *, mode: str = "plan") -> dict:
        catalog = [broker.model_entry(MODEL, "Gemini 3.7 Flash (High)")]
        args = {
            "prompt": "Investigate.",
            "task_kind": "research" if package.get("research_questions") else "quick_check",
            "mode": mode,
            "target_model": MODEL,
            "effort": "high",
            "work_package_id": package["package_id"],
            "read_context": list(package.get("read_context") or []),
            "workspace_root": str(self.ws),
            "research_questions": list(package.get("research_questions") or []),
            "acceptance_criteria": list(package.get("acceptance_criteria") or []),
            "async": False,
        }
        if package.get("allowed_files"):
            args["allowed_files"] = list(package["allowed_files"])
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "discover_antigravity_models", return_value=catalog), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", return_value=(0, json.dumps(outer), "")), \
             mock.patch.object(broker, "store_consultation"):
            return broker.consult("antigravity", args)


class WeakAnswersValidationAndOutcomeTests(WeakAnswersBase):
    def test_one_probe_answered_read_only_returns_completed_with_gaps_and_weak_answers(self):
        pkg = self.read_only_package("Q1?")
        outer = self.outer_payload(pkg, [self.coverage_answered("Q1?", [self.evidence_item(primary=True)])])
        result = self.run_consult(pkg, outer)
        self.assertEqual(result["outcome"], "completed_with_gaps", result.get("response"))
        self.assertTrue(result["credit_eligible"])
        self.assertTrue(result["brain_verification"]["required"])
        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["weak_answers"],
            [
                {
                    "question": 1,
                    "requirement": "answered status requires at least 2 distinct evidence probes",
                    "follow_up": "confirm with a second independent probe before relying on it",
                }
            ],
        )
        self.assertNotIn("scope_gaps", result)
        self.assertNotIn("suggested_read_context", result)

    def test_one_probe_not_found_read_only_returns_completed_with_gaps_and_weak_answers(self):
        pkg = self.read_only_package("Where is helper?")
        outer = self.outer_payload(
            pkg,
            [self.coverage_not_found("Where is helper?", [self.evidence_item(primary=True)])],
        )
        result = self.run_consult(pkg, outer)
        self.assertEqual(result["outcome"], "completed_with_gaps", result.get("response"))
        self.assertTrue(result["credit_eligible"])
        self.assertEqual(
            result["weak_answers"],
            [
                {
                    "question": 1,
                    "requirement": "not_found status requires at least 2 distinct search/evidence probes",
                    "follow_up": "confirm with a second independent probe before relying on it",
                }
            ],
        )

    def test_every_question_one_probe_still_completes_with_gaps(self):
        pkg = self.read_only_package("Q1?", "Q2?")
        outer = self.outer_payload(
            pkg,
            [
                self.coverage_answered("Q1?", [self.evidence_item(primary=True)]),
                self.coverage_not_found("Q2?", [self.evidence_item(primary=True)]),
            ],
        )
        result = self.run_consult(pkg, outer)
        self.assertEqual(result["outcome"], "completed_with_gaps", result.get("response"))
        self.assertTrue(result["credit_eligible"])
        self.assertEqual(len(result["weak_answers"]), 2)
        self.assertEqual([item["question"] for item in result["weak_answers"]], [1, 2])

    def test_mixed_two_probe_and_one_probe_records_only_weak_question(self):
        pkg = self.read_only_package("Q1?", "Q2?")
        outer = self.outer_payload(
            pkg,
            [
                self.coverage_answered(
                    "Q1?",
                    [
                        self.evidence_item(primary=True, locator="L1"),
                        self.evidence_item(primary=False, locator="L1:L2", obs="Confirmed once more."),
                    ],
                ),
                self.coverage_answered("Q2?", [self.evidence_item(primary=True, locator="L1")]),
            ],
        )
        result = self.run_consult(pkg, outer)
        self.assertEqual(result["outcome"], "completed_with_gaps", result.get("response"))
        self.assertEqual([item["question"] for item in result["weak_answers"]], [2])

    def test_zero_evidence_question_still_rejects(self):
        pkg = self.read_only_package("Q1?")
        for cov in (
            self.coverage_answered("Q1?", []),
            self.coverage_not_found("Q1?", []),
        ):
            with self.subTest(status=cov["status"]):
                outer = self.outer_payload(pkg, [cov])
                result = self.run_consult(pkg, outer)
                self.assertEqual(result["outcome"], "rejected")
                self.assertEqual(result["disposition"], "rejected")
                self.assertFalse(result["credit_eligible"])

    def test_missing_primary_citation_and_unsupported_by_design_claim_still_reject(self):
        pkg = self.read_only_package("Q1?")
        no_primary = self.outer_payload(
            pkg, [self.coverage_answered("Q1?", [self.evidence_item(primary=False)])]
        )
        _, errs_primary = broker.validate_flash_workhorse_result(no_primary, pkg)
        self.assertTrue(any("requires a primary citation" in e for e in errs_primary), errs_primary)

        by_design = self.outer_payload(
            pkg,
            [self.coverage_answered("Q1?", [self.evidence_item(primary=True)])],
            claims=[{"statement": "This fallback is intentional by design.", "basis": "assumption", "evidence": []}],
        )
        _, errs_claim = broker.validate_flash_workhorse_result(by_design, pkg)
        self.assertTrue(any("intentional/by-design claim lacks observed primary evidence" in e for e in errs_claim), errs_claim)

    def test_not_found_without_gaps_still_rejects(self):
        pkg = self.read_only_package("Where is helper?")
        outer = self.outer_payload(
            pkg,
            [self.coverage_not_found("Where is helper?", [self.evidence_item(primary=True)], gaps=[])],
        )
        result = self.run_consult(pkg, outer)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["disposition"], "rejected")
        self.assertTrue(any("searched-boundary gaps" in e for e in result["validation_failures"]))

    def test_accept_edits_one_probe_still_rejects(self):
        pkg = self.accept_edits_package("Q1?")
        for cov in (
            self.coverage_answered("Q1?", [self.evidence_item(primary=True)]),
            self.coverage_not_found("Q1?", [self.evidence_item(primary=True)]),
        ):
            with self.subTest(status=cov["status"]):
                outer = self.outer_payload(pkg, [cov])
                _, errors = broker.validate_flash_workhorse_result(outer, pkg)
                self.assertTrue(any("at least 2 distinct" in e for e in errors), errors)


class CondensedEnvelopeAndRedactionTests(WeakAnswersBase):
    def test_condensed_envelope_keeps_handoff_keys_weak_answers_and_scope_gaps(self):
        native_handoff = {
            "work_package_id": "WP-CONDENSE",
            "semantic_lane": "workhorse",
            "action": "review_applied_files_first",
            "target": {"target_agent": "antigravity", "surface": "cli", "target_model": "hydrogen"},
            "brain_adjudication_required": True,
            "stage_repo_hint": True,
            "suggested_read_context": [f"src/file_{i}.py" for i in range(7)],
            "validation_failures": [
                "failure 1: " + ("x" * 500),
                "failure 2: " + ("y" * 500),
                "failure 3: short",
                "failure 4: dropped",
            ],
            "brain_review": {"required": True, "action": "Inspect applied files before replaying."},
            "do_not_replay": True,
            "do_not_replay_note": "Do NOT replay before checking git diff.",
            "native": {
                "family": "codex",
                "role": "worker",
                "model": "gpt-6-sol",
                "effort": "medium",
                "mechanism": "managed_native_subagent",
            },
            "flash_skip_reason": "flash-failed:broker:req-1",
            "extra_verbose_blob": "Z" * 5_000,
        }
        weak_answers = [
            {
                "question": 1,
                "requirement": "answered status requires at least 2 distinct evidence probes",
                "follow_up": "confirm with a second independent probe before relying on it",
            }
        ]
        scope_gaps = [
            {"path": f"outside/mod_{i}.py", "affects": "criterion 1", "why_needed": "Needed for check."}
            for i in range(6)
        ]
        result = {
            "status": "ok",
            "outcome": "rejected",
            "accepted": False,
            "credit_eligible": False,
            "receipt": "broker:req-1",
            "work_package_id": "WP-CONDENSE",
            "request_id": "req-1",
            "native_handoff": native_handoff,
            "weak_answers": weak_answers,
            "scope_gaps": scope_gaps,
            "structured_output": {"summary": "B" * 20_000},
            "response": "C" * 20_000,
        }
        trimmed = broker.apply_response_envelope_budget(dict(result), 4500, None, "t", "test")
        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False)), 4500)
        self.assertTrue(trimmed["truncated"])
        handoff = trimmed["native_handoff"]
        for key in (
            "action",
            "target",
            "brain_adjudication_required",
            "stage_repo_hint",
            "suggested_read_context",
            "validation_failures",
            "brain_review",
            "do_not_replay",
            "do_not_replay_note",
            "native",
            "flash_skip_reason",
        ):
            self.assertIn(key, handoff, key)
        self.assertEqual(len(handoff["suggested_read_context"]), 5)
        self.assertEqual(len(handoff["validation_failures"]), 3)
        for item in handoff["validation_failures"]:
            self.assertLessEqual(len(item), 300)
        self.assertEqual(trimmed["weak_answers"], weak_answers)
        self.assertEqual(len(trimmed["scope_gaps"]), 5)
        self.assertNotIn("extra_verbose_blob", handoff)

    def test_stored_envelope_with_secret_line_keeps_other_fields_after_redaction(self):
        result = {
            "status": "error",
            "outcome": "failed_pre_mutation",
            "accepted": False,
            "credit_eligible": False,
            "work_package_id": "WP-REDACT-TEST",
            "summary": "Worker failed after checking config.",
            "error_detail": "api_key=demo-not-a-real-secret-value-12345",
            "structured_output": {"summary": "Useful findings " + ("D" * 10_000)},
        }
        trimmed = broker.apply_response_envelope_budget(dict(result), 2000, None, "t", "test")
        fetched = broker.retrieve_shared_context(trimmed["response_ref"], limit=80000)
        content = fetched["content"]
        self.assertTrue(fetched["contains_redaction_placeholders"])
        self.assertIn("[redacted possible secret line]", content)
        self.assertNotIn("demo-not-a-real-secret-value-12345", content)
        self.assertIn('"work_package_id": "WP-REDACT-TEST"', content)
        self.assertIn('"summary": "Worker failed after checking config."', content)
        self.assertIn("Useful findings", content)

    def test_tight_budget_keeps_handoff_before_body_and_honours_800_char_cap(self):
        result = {
            "status": "error",
            "outcome": "rejected",
            "accepted": False,
            "credit_eligible": False,
            "receipt": "broker:req-tight",
            "work_package_id": "WP-TIGHT",
            "native_handoff": {
                "action": "fix_and_redispatch",
                "target": {"target_agent": "antigravity", "surface": "cli", "target_model": "hydrogen"},
                "brain_adjudication_required": False,
                "stage_repo_hint": True,
                "suggested_read_context": ["src/a.py"],
                "validation_failures": ["missing fields: next_action"],
                "brain_review": {"required": True, "action": "Inspect failure."},
                "native": {"family": "codex", "role": "worker", "model": "gpt-6-sol"},
                "flash_skip_reason": "flash-failed:broker:req-tight",
            },
            "weak_answers": [
                {
                    "question": 1,
                    "requirement": "answered status requires at least 2 distinct evidence probes",
                    "follow_up": "confirm with a second independent probe before relying on it",
                }
            ],
            "structured_output": {"items": [{"k": "v"} for _ in range(120)]},
            "fallback_advice": "F" * 400,
        }
        tight = broker.apply_response_envelope_budget(dict(result), 1800, None, "t", "test")
        self.assertLessEqual(len(json.dumps(tight, ensure_ascii=False)), 1800)
        self.assertIn("native_handoff", tight)
        self.assertEqual(tight["native_handoff"]["action"], "fix_and_redispatch")
        self.assertIn("weak_answers", tight)

        min_budget = broker.apply_response_envelope_budget(dict(result), 800, None, "t", "test")
        self.assertLessEqual(len(json.dumps(min_budget, ensure_ascii=False)), 800)


class DispositionAndVersionTests(WeakAnswersBase):
    def test_blocked_worker_gets_disposition_blocked_even_with_caveats(self):
        pkg = self.read_only_package("Q1?")
        outer = self.outer_payload(
            pkg,
            [
                {
                    "question": "Q1?",
                    "status": "blocked",
                    "answer": "",
                    "confidence": "low",
                    "evidence": [],
                    "competing_explanations_checked": [],
                    "gaps": ["Missing external spec."],
                }
            ],
            status="blocked",
            ambiguities=["Blocked on missing spec."],
        )
        outer["status"] = "ERROR"  # triggers cli_exit_status_mismatch caveat
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", return_value=(0, json.dumps(outer), "")):
            raw = broker.consult_antigravity_cli("p", "Investigate.", "plan", MODEL, "high", 60, pkg)
        normalized = json.loads(raw)
        self.assertEqual(normalized["worker_status"], "blocked")
        self.assertEqual(normalized["disposition"], "blocked")
        self.assertTrue(normalized["caveats"])

        result = self.run_consult(pkg, outer)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["disposition"], "blocked")

    def test_failed_worker_and_rejected_outcome_get_disposition_rejected(self):
        pkg = self.read_only_package("Q1?")
        failed_outer = self.outer_payload(
            pkg,
            [
                {
                    "question": "Q1?",
                    "status": "blocked",
                    "answer": "",
                    "confidence": "low",
                    "evidence": [],
                    "competing_explanations_checked": [],
                    "gaps": ["Tool crashed."],
                }
            ],
            status="failed",
        )
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", return_value=(0, json.dumps(failed_outer), "")):
            raw = broker.consult_antigravity_cli("p", "Investigate.", "plan", MODEL, "high", 60, pkg)
        self.assertEqual(json.loads(raw)["disposition"], "rejected")

        rejected_outer = self.outer_payload(pkg, [self.coverage_answered("Q1?", [])])
        rejected_result = self.run_consult(pkg, rejected_outer)
        self.assertEqual(rejected_result["outcome"], "rejected")
        self.assertEqual(rejected_result["disposition"], "rejected")

    def test_version_is_1_0_73(self):
        self.assertGreaterEqual(tuple(int(x) for x in str(switchboard_version.BROKER_VERSION).split(".")), (1, 0, 74))


if __name__ == "__main__":
    unittest.main()
