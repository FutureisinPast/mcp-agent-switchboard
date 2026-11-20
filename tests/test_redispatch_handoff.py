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


class RedispatchHandoffBase(unittest.TestCase):
    """Shared fixtures for fix-and-redispatch handoff and not_found 1-probe tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        (self.tmp_root / "src").mkdir(parents=True, exist_ok=True)
        (self.tmp_root / "src" / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.broker_dir = self.tmp_root / ".agent-broker"
        self.broker_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.broker_dir / "state.sqlite"
        self.patches = [
            mock.patch.object(broker, "BROKER_DIR", self.broker_dir),
            mock.patch.object(broker, "DB_PATH", self.db_path),
            mock.patch.object(broker, "LOG_PATH", self.broker_dir / "agent-broker.log"),
            mock.patch.object(broker, "CONFIG_PATH", self.broker_dir / "config.json"),
            mock.patch.object(
                broker, "ANTIGRAVITY_CATALOG_PATH", self.broker_dir / "antigravity-models.json"
            ),
            mock.patch.object(broker, "WORKER_LOG_DIR", self.broker_dir / "flash-logs"),
            mock.patch.object(broker, "QUARANTINE_ROOT", self.broker_dir / "flash-quarantine"),
            mock.patch.object(
                broker,
                "QUARANTINE_LOCK_PATH",
                self.broker_dir / "flash-quarantine.lock",
            ),
            mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"),
        ]
        for patcher in self.patches:
            patcher.start()
        broker.init_db()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self._tmp.cleanup()

    def read_only_package(
        self,
        *,
        package_id: str = "WP-NF-1",
        read_context: list[str] | None = None,
        questions: list[str] | None = None,
    ) -> dict:
        return {
            "package_id": package_id,
            "task_kind": "research",
            "mode": "plan",
            "allowed_files": [],
            "allowed_writes": [],
            "allowed_creates": [],
            "read_context": list(read_context if read_context is not None else ["src/alpha.py"]),
            "workspace_root": str(self.tmp_root),
            "acceptance_criteria": ["Answer research questions with primary evidence"],
            "forbidden_actions": ["Do not modify files"],
            "research_questions": list(questions if questions is not None else ["Where is target_fn defined?"]),
        }

    def implementation_args(
        self,
        *,
        package_id: str = "WP-REDISPATCH-1",
        prompt: str = "Update src/alpha.py to validate inputs.",
    ) -> dict:
        return {
            "work_package_id": package_id,
            "task_kind": "implementation",
            "mode": "accept-edits",
            "allowed_files": ["src/alpha.py"],
            "acceptance_criteria": ["Input validation rejects empty values"],
            "forbidden_actions": ["Do not edit unrelated modules"],
            "prompt": prompt,
            "workspace_root": str(self.tmp_root),
        }

    def not_found_payload(
        self,
        package: dict,
        *,
        evidence: list[dict],
        scope_gaps: list[dict] | None = None,
        gaps: list[str] | None = None,
    ) -> dict:
        structured = {
            "package_id": package["package_id"],
            "status": "completed",
            "summary": "Target symbol was not found in the supplied read_context.",
            "acceptance_criteria": [
                {
                    "criterion": package["acceptance_criteria"][0],
                    "status": "passed",
                    "evidence": ["src/alpha.py:L1:L20"],
                }
            ],
            "files_changed": [],
            "checks": [],
            "evidence": [
                {
                    "claim": "Inspected supplied file.",
                    "path": "src/alpha.py",
                    "line": "L1:L20",
                    "observation": "target_fn is imported but not defined here.",
                }
            ],
            "claims": [],
            "ambiguities": [],
            "risks": ["Missing outside module prevents full trace."],
            "next_action": "Review scope_gaps and re-dispatch if needed.",
            "brain_verification_required": "required",
            "research_coverage": [
                {
                    "question": package["research_questions"][0],
                    "status": "not_found",
                    "answer": "NOT FOUND in supplied files.",
                    "confidence": "medium",
                    "evidence": evidence,
                    "competing_explanations_checked": [],
                    "gaps": list(gaps if gaps is not None else ["Checked supplied files; target_fn resides elsewhere."]),
                }
            ],
            "scope_gaps": list(scope_gaps or []),
        }
        return {"status": "SUCCESS", "structured_output": structured}

    def _record_prior_consultation(
        self,
        *,
        work_package_id: str,
        prompt: str,
        outcome: str,
        request_id: str = "prior-req-1",
        response: str = "Antigravity CLI structured-output validation failed: missing fields: summary",
    ) -> None:
        info = broker.ProjectInfo("demo-proj", str(self.tmp_root))
        with mock.patch.object(broker, "run_git", return_value="main"):
            broker.store_consultation(
                info,
                "antigravity:gemini-hydrogen-high",
                "accept-edits",
                prompt,
                response,
                "error" if outcome == "rejected" else outcome,
                response,
                "2026-01-01T00:00:00Z",
                request_id=request_id,
                receipt_meta={
                    "outcome": outcome,
                    "work_package_id": work_package_id,
                    "requested_model": "hydrogen",
                    "attested_model": "hydrogen",
                    "attestation": "backend",
                    "mode": "accept-edits",
                },
            )


class TestRedispatchHandoff(RedispatchHandoffBase):
    def test_availability_failure_keeps_native_handoff(self) -> None:
        handoff = broker.native_handoff_for_flash_outcome(
            self.implementation_args(),
            "unavailable_pre_mutation",
            "broker:avail-1",
        )
        self.assertIsNotNone(handoff)
        self.assertIn("native", handoff)
        self.assertEqual(handoff["native"]["role"], "worker")
        self.assertEqual(handoff["flash_skip_reason"], "flash-unavailable:broker:avail-1")
        self.assertEqual(handoff["auto_launch"], False)
        self.assertNotEqual(handoff["action"], "fix_and_redispatch")

    def test_first_rejection_returns_fix_and_redispatch_without_native(self) -> None:
        handoff = broker.native_handoff_for_flash_outcome(
            self.implementation_args(),
            "rejected",
            "broker:rej-1",
            missing_context=["src/helper.py"],
            validation_failures=["missing fields: next_action"],
            structured_output={
                "ambiguities": [],
                "checks": [],
                "scope_gaps": [{"path": "src/other.py", "affects": "criterion 1", "why_needed": "Defines constant"}],
            },
        )
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["action"], "fix_and_redispatch")
        self.assertEqual(
            handoff["target"],
            {"target_agent": "antigravity", "surface": "cli", "target_model": "hydrogen"},
        )
        self.assertEqual(handoff["validation_failures"], ["missing fields: next_action"])
        self.assertEqual(handoff["suggested_read_context"], ["src/other.py", "src/helper.py"])
        self.assertIn("proposal for the brain", handoff["suggested_read_context_note"].lower())
        self.assertIs(handoff["stage_repo_hint"], False)
        self.assertIs(handoff["brain_adjudication_required"], False)
        self.assertNotIn("native", handoff)
        self.assertNotIn("flash_skip_reason", handoff)

    def test_non_creditable_outcomes_return_fix_and_redispatch(self) -> None:
        for outcome in ("blocked", "failed_pre_mutation"):
            with self.subTest(outcome=outcome):
                handoff = broker.native_handoff_for_flash_outcome(
                    self.implementation_args(package_id=f"WP-{outcome}"),
                    outcome,
                    f"broker:{outcome}",
                )
                self.assertEqual(handoff["action"], "fix_and_redispatch")
                self.assertNotIn("do_not_replay", handoff)
                self.assertNotIn("native", handoff)
                self.assertTrue(handoff["brain_review"]["required"])

        for outcome in broker.FLASH_APPLIED_OUTCOMES:
            with self.subTest(outcome=outcome):
                handoff = broker.native_handoff_for_flash_outcome(
                    self.implementation_args(package_id=f"WP-{outcome}"),
                    outcome,
                    f"broker:{outcome}",
                )
                self.assertEqual(handoff["action"], "review_applied_files_first")
                self.assertIs(handoff["do_not_replay"], True)
                self.assertTrue(handoff["do_not_replay_note"])
                self.assertTrue(handoff["brain_review"]["required"])
                self.assertNotIn("native", handoff)

    def test_failed_pre_mutation_infrastructure_and_timeout_keep_native_handoff(self) -> None:
        cases = [
            ("timeout_during_execution kind", None, "timeout_during_execution"),
            ("timeout kind", None, "timeout"),
            ("timeout_exceeded kind", None, "timeout_exceeded"),
            ("worker_dead kind", None, "worker_dead"),
            ("worker_never_started kind", None, "worker_never_started"),
            ("agy missing prefix", "Antigravity CLI was not found on PATH.", None),
            ("timeout prefix", "Antigravity CLI timed out after 120 seconds.", None),
            ("exit code prefix", "Antigravity CLI exited with code 1: transient failure", None),
            ("dead worker message", "Antigravity CLI async worker (pid 1234) died before recording a result after 15 seconds.", None),
            ("quota error", "HTTP 429 rate limit / quota exceeded", None),
            ("json quota stderr", json.dumps({"status": "error", "cli": {"stderr": "429 Too Many Requests"}}), None),
        ]
        for label, response_text, failure_kind in cases:
            with self.subTest(label=label):
                handoff = broker.native_handoff_for_flash_outcome(
                    self.implementation_args(package_id="WP-INFRA-1"),
                    "failed_pre_mutation",
                    "broker:infra-1",
                    response_text=response_text,
                    failure_kind=failure_kind,
                )
                self.assertIn("native", handoff)
                self.assertEqual(handoff["native"]["role"], "worker")
                self.assertEqual(handoff["flash_skip_reason"], "flash-failed:broker:infra-1")
                self.assertIn("flash-failed:broker:infra-1", handoff["record_requirement"])
                self.assertFalse(handoff["auto_launch"])
                self.assertNotEqual(handoff["action"], "fix_and_redispatch")

        # Applied outcomes never bypass the workspace-review safety rule, even after a timeout.
        applied_timeout = broker.native_handoff_for_flash_outcome(
            self.implementation_args(package_id="WP-APPLIED-TO"),
            "applied_then_rejected",
            "broker:applied-to",
            response_text="Antigravity CLI timed out after 120 seconds.",
            failure_kind="timeout_during_execution",
        )
        self.assertEqual(applied_timeout["action"], "review_applied_files_first")
        self.assertIs(applied_timeout["do_not_replay"], True)
        self.assertNotIn("native", applied_timeout)

    def test_module_not_found_and_import_errors_set_stage_repo_hint(self) -> None:
        cases = [
            "ModuleNotFoundError: No module named 'demo_pkg'",
            "ImportError: cannot import name 'Widget' from 'demo_pkg'",
            "No module named 'helper_mod'",
            "tests that could not run in minimal staging manifest",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                handoff = broker.native_handoff_for_flash_outcome(
                    self.implementation_args(),
                    "rejected",
                    "broker:mod-1",
                    validation_failures=[msg],
                )
                self.assertIs(handoff["stage_repo_hint"], True)
                self.assertIs(handoff["brain_adjudication_required"], False)

    def test_adjudication_triggers_set_brain_adjudication_required(self) -> None:
        cases = [
            ("ambiguity text", ["status=completed contradicts non-empty ambiguities"], None),
            ("scope violation", ["out-of-scope file reported: src/unrelated.py"], None),
            (
                "model attestation mismatch",
                ["requested model 'hydrogen' but the backend attested 'flash'; resolved-model mismatch"],
                None,
            ),
            ("failed check failure", ["status=completed contradicts a failed check"], None),
            (
                "structured failed check",
                [],
                {"ambiguities": [], "checks": [{"command": "python -m unittest", "status": "failed"}]},
            ),
            (
                "structured ambiguity",
                [],
                {"ambiguities": ["Spec unclear on error code"], "checks": []},
            ),
        ]
        for label, failures, structured in cases:
            with self.subTest(label=label):
                handoff = broker.native_handoff_for_flash_outcome(
                    self.implementation_args(),
                    "rejected" if failures else "blocked",
                    "broker:adj-1",
                    validation_failures=failures,
                    structured_output=structured,
                )
                self.assertIs(handoff["brain_adjudication_required"], True)

    def test_second_rejection_with_different_prompt_includes_native_block(self) -> None:
        wp_id = "WP-DIFF-1"
        self._record_prior_consultation(
            work_package_id=wp_id,
            prompt="Initial prompt without helper context",
            outcome="rejected",
            request_id="prior-diff-1",
        )
        handoff = broker.native_handoff_for_flash_outcome(
            self.implementation_args(
                package_id=wp_id,
                prompt="Corrected prompt with extra constraints",
            ),
            "rejected",
            "broker:curr-diff-2",
            validation_failures=["missing fields: summary"],
        )
        self.assertEqual(handoff["action"], "fix_and_redispatch")
        self.assertIn("native", handoff)
        self.assertEqual(handoff["native"]["role"], "worker")
        self.assertEqual(handoff["flash_skip_reason"], "flash-failed:broker:curr-diff-2")
        self.assertIn("flash-failed:broker:curr-diff-2", handoff["record_requirement"])
        # Brain adjudication remains computed from the current failure, not forced True.
        self.assertIs(handoff["brain_adjudication_required"], False)

    def test_second_rejection_with_identical_prompt_omits_native_block(self) -> None:
        wp_id = "WP-SAME-1"
        same_prompt = "Unchanged prompt for WP-SAME-1"
        wrapped = (
            "<flash_workhorse_contract>\nPackage ID: WP-SAME-1\n</flash_workhorse_contract>\n\n"
            f"<bounded_request>\nShared context pack for this topic:\n\nGenerated: 2026-01-01\n\n"
            f"Current request:\n\n[Agent Switchboard] task=implementation\n\nRequest:\n\n{same_prompt}\n</bounded_request>"
        )
        self._record_prior_consultation(
            work_package_id=wp_id,
            prompt=wrapped,
            outcome="rejected",
            request_id="prior-same-1",
        )
        handoff = broker.native_handoff_for_flash_outcome(
            self.implementation_args(package_id=wp_id, prompt=same_prompt),
            "rejected",
            "broker:curr-same-2",
            validation_failures=["missing fields: summary"],
        )
        self.assertEqual(handoff["action"], "fix_and_redispatch")
        self.assertNotIn("native", handoff)
        self.assertNotIn("flash_skip_reason", handoff)

    def test_end_to_end_consult_redispatch_progression(self) -> None:
        wp_id = "WP-E2E-REDISPATCH"
        base_args = {
            **self.implementation_args(package_id=wp_id, prompt="First attempt"),
            "project": "demo-proj",
            "target_model": "hydrogen",
            "effort": "high",
            "force_sync": True,
        }
        info = broker.ProjectInfo("demo-proj", str(self.tmp_root))
        rejection = "Antigravity CLI structured-output validation failed: missing fields: risks"
        with (
            mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3-flash-high", "high")),
            mock.patch.object(broker, "antigravity_model_for_effort", return_value="gemini-3-flash-high"),
            mock.patch.object(broker, "_antigravity_effective_effort", return_value="high"),
            mock.patch.object(broker, "resolve_project", return_value=info),
            mock.patch.object(broker, "run_git", return_value="main"),
            mock.patch.object(broker, "consult_antigravity_cli", return_value=rejection),
        ):
            first = broker.consult("antigravity", dict(base_args))
            second_same = broker.consult("antigravity", dict(base_args))
            third_diff = broker.consult(
                "antigravity",
                {**base_args, "prompt": "Corrected second attempt with updated instructions"},
            )
        self.assertEqual(first["native_handoff"]["action"], "fix_and_redispatch")
        self.assertNotIn("native", first["native_handoff"])
        self.assertEqual(second_same["native_handoff"]["action"], "fix_and_redispatch")
        self.assertNotIn("native", second_same["native_handoff"])
        self.assertEqual(third_diff["native_handoff"]["action"], "fix_and_redispatch")
        self.assertIn("native", third_diff["native_handoff"])
        self.assertEqual(third_diff["native_handoff"]["flash_skip_reason"], f"flash-failed:{third_diff['receipt']}")

    def test_not_found_one_probe_with_linked_gap_and_all_context_cited_is_accepted(self) -> None:
        pkg = self.read_only_package(read_context=["src/alpha.py"])
        outer = self.not_found_payload(
            pkg,
            evidence=[
                {
                    "source_kind": "code",
                    "location": "src/alpha.py",
                    "locator": "L1:L20",
                    "observation": "Inspected all lines of src/alpha.py; target_fn absent.",
                    "primary": True,
                }
            ],
            scope_gaps=[
                {
                    "path": "src/beta.py",
                    "affects": "question 1",
                    "why_needed": "src/alpha.py imports target_fn from src/beta.py",
                }
            ],
        )
        structured, errors = broker.validate_flash_workhorse_result(outer, pkg)
        self.assertEqual(errors, [])
        self.assertIsNotNone(structured)
        self.assertEqual(structured["research_coverage"][0]["status"], "not_found")
        self.assertEqual(len(structured["scope_gaps"]), 1)
        self.assertEqual(broker.classify_flash_outcome("{}", "ok", structured), "completed_with_gaps")

    def test_not_found_one_probe_missing_read_context_file_is_rejected(self) -> None:
        pkg = self.read_only_package(read_context=["src/alpha.py", "src/extra.py"])
        outer = self.not_found_payload(
            pkg,
            evidence=[
                {
                    "source_kind": "code",
                    "location": "src/alpha.py",
                    "locator": "L1:L20",
                    "observation": "Checked src/alpha.py only.",
                    "primary": True,
                }
            ],
            scope_gaps=[
                {
                    "path": "src/beta.py",
                    "affects": "question 1",
                    "why_needed": "target_fn resides outside declared read_context",
                }
            ],
        )
        weak_answers: list = []
        structured, errors = broker.validate_flash_workhorse_result(
            outer, pkg, weak_answers_out=weak_answers
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            weak_answers,
            [
                {
                    "question": 1,
                    "requirement": "not_found status requires at least 2 distinct search/evidence probes",
                    "follow_up": "confirm with a second independent probe before relying on it",
                }
            ],
        )
        self.assertEqual(broker.classify_flash_outcome("{}", "ok", structured), "completed_with_gaps")

    def test_not_found_one_probe_without_linked_gap_is_rejected(self) -> None:
        pkg = self.read_only_package(read_context=["src/alpha.py"])
        outer_no_gap = self.not_found_payload(
            pkg,
            evidence=[
                {
                    "source_kind": "code",
                    "location": "src/alpha.py",
                    "locator": "L1:L20",
                    "observation": "Checked src/alpha.py only.",
                    "primary": True,
                }
            ],
            scope_gaps=[],
        )
        weak_no_gap: list = []
        structured_no_gap, errors_no_gap = broker.validate_flash_workhorse_result(
            outer_no_gap, pkg, weak_answers_out=weak_no_gap
        )
        self.assertEqual(errors_no_gap, [])
        self.assertEqual(
            weak_no_gap,
            [
                {
                    "question": 1,
                    "requirement": "not_found status requires at least 2 distinct search/evidence probes",
                    "follow_up": "confirm with a second independent probe before relying on it",
                }
            ],
        )
        self.assertEqual(
            broker.classify_flash_outcome("{}", "ok", structured_no_gap),
            "completed_with_gaps",
        )

        outer_wrong_target = self.not_found_payload(
            pkg,
            evidence=[
                {
                    "source_kind": "code",
                    "location": "src/alpha.py",
                    "locator": "L1:L20",
                    "observation": "Checked src/alpha.py only.",
                    "primary": True,
                }
            ],
            scope_gaps=[
                {
                    "path": "src/beta.py",
                    "affects": "criterion 1",
                    "why_needed": "Unlinked gap targets criterion rather than question 1",
                }
            ],
        )
        weak_wrong_target: list = []
        structured_wrong_target, errors_wrong_target = broker.validate_flash_workhorse_result(
            outer_wrong_target, pkg, weak_answers_out=weak_wrong_target
        )
        self.assertEqual(errors_wrong_target, [])
        self.assertEqual(
            weak_wrong_target,
            [
                {
                    "question": 1,
                    "requirement": "not_found status requires at least 2 distinct search/evidence probes",
                    "follow_up": "confirm with a second independent probe before relying on it",
                }
            ],
        )
        self.assertEqual(
            broker.classify_flash_outcome("{}", "ok", structured_wrong_target),
            "completed_with_gaps",
        )

    def test_cost_aware_rules_and_version_updated(self) -> None:
        joined = "\n".join(broker.COST_AWARE_ROUTING_RULES)
        self.assertIn("stage_repo=true", joined)
        gpu_sentence = (
            "For GPU or real-data work, the workhorse writes the code and tests with synthetic data; "
            "only the final real-hardware run happens in the brain's own session."
        )
        self.assertIn(gpu_sentence, joined)
        added_entries = [rule for rule in broker.COST_AWARE_ROUTING_RULES if "stage_repo=true" in rule]
        self.assertEqual(len(added_entries), 1)
        self.assertLess(len(added_entries[0]), 300)
        self.assertGreaterEqual(tuple(int(x) for x in str(switchboard_version.BROKER_VERSION).split(".")), (1, 0, 74))


if __name__ == "__main__":
    unittest.main()
