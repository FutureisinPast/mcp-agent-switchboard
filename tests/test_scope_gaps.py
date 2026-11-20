"""WP-SCOPE-GAPS-1071: a read-only package may report files outside its read scope.

The worker lists them in `scope_gaps` and finishes with status=completed; the broker
accepts that only for a package that was DISPATCHED read-only, rejects it for an
implementation package, and grades the result `completed_with_gaps` (same relief as
`completed_verified`, brain verification still required). Everything here is
synthetic: agy and the model catalog are fakes.
"""

from __future__ import annotations

import copy
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

CRITERION = "The answer is grounded in the given files."
MODEL = "gemini-3.7-flash-high"


def _gap(path="outside/helper.py", affects="criterion 1", why="Needed to confirm the helper contract."):
    return {"path": path, "affects": affects, "why_needed": why}


class ScopeGapsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "ws"
        self.ctx_a = self.ws / "src" / "a.py"
        self.ctx_b = self.ws / "src" / "b.py"
        for path in (self.ctx_a, self.ctx_b):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
        self.package = self.read_only_package()

    def read_only_package(self, **extra):
        args = {
            "work_package_id": "WP-GAPS-RO",
            "read_context": [str(self.ctx_a), str(self.ctx_b)],
            "workspace_root": str(self.ws),
            "acceptance_criteria": [CRITERION],
        }
        args.update(extra)
        return {**broker.prepare_flash_work_package(args, "quick_check", "Answer from the files."), "mode": "plan"}

    def implementation_package(self):
        package = broker.prepare_flash_work_package(
            {
                "work_package_id": "WP-GAPS-IMPL",
                "allowed_writes": [str(self.ctx_a)],
                "read_context": [str(self.ctx_b)],
                "workspace_root": str(self.ws),
                "acceptance_criteria": [CRITERION],
            },
            "implementation",
            "Change the file.",
        )
        return {**package, "mode": "accept-edits"}

    def structured(self, package, **overrides):
        payload = {
            "package_id": package["package_id"],
            "status": "completed",
            "summary": "Answered from the given files.",
            "acceptance_criteria": [
                {"criterion": item, "status": "passed", "evidence": ["src/a.py:1"]}
                for item in package["acceptance_criteria"]
            ],
            "files_changed": [],
            "checks": [],
            "evidence": [
                {"claim": "a is 1", "path": "src/a.py", "line": "1", "observation": "x = 1"}
            ],
            "claims": [],
            "research_coverage": [],
            "scope_gaps": [_gap()],
            "ambiguities": [],
            "risks": ["No runtime test was run."],
            "next_action": "Brain verifies.",
            "brain_verification_required": "required",
        }
        payload.update(overrides)
        return payload

    def validate(self, package, structured):
        outer = {"status": "SUCCESS", "structured_output": structured}
        return broker.validate_flash_workhorse_result(outer, package)

    def assertAccepted(self, package, structured):
        _, errors = self.validate(package, structured)
        self.assertEqual(errors, [])

    def assertRejected(self, package, structured, fragment):
        _, errors = self.validate(package, structured)
        self.assertTrue(any(fragment in error for error in errors), errors)


class ScopeGapsValidatorTests(ScopeGapsBase):
    def test_read_only_completed_with_valid_gap_is_accepted(self):
        self.assertAccepted(self.package, self.structured(self.package))

    def test_missing_scope_gaps_field_is_treated_as_empty(self):
        payload = self.structured(self.package)
        del payload["scope_gaps"]
        structured, errors = self.validate(self.package, payload)
        self.assertEqual(errors, [])
        self.assertEqual(broker._scope_gaps_of(structured), [])
        self.assertEqual(broker.classify_flash_outcome("{}", "ok", structured), "completed_verified")

    def test_completed_with_ambiguities_still_rejected_with_new_hint(self):
        payload = self.structured(self.package, scope_gaps=[], ambiguities=["helper.py might differ"])
        self.assertRejected(self.package, payload, "status=completed contradicts non-empty ambiguities")
        self.assertRejected(self.package, payload, "report it in scope_gaps instead")

    def test_accept_edits_completed_with_scope_gaps_rejected(self):
        package = self.implementation_package()
        self.assertRejected(
            package, self.structured(package), "scope_gaps on an implementation package requires status=blocked"
        )

    def test_package_with_allowed_writes_is_not_read_only_even_when_mode_says_plan(self):
        package = {**self.implementation_package(), "mode": "plan"}
        self.assertFalse(broker.package_is_read_only(package))
        self.assertRejected(package, self.structured(package), "requires status=blocked")

    def test_gap_naming_in_scope_read_context_file_rejected(self):
        payload = self.structured(self.package, scope_gaps=[_gap(path=str(self.ctx_a))])
        self.assertRejected(self.package, payload, f"scope_gaps entry names an in-scope file: {self.ctx_a}")

    def test_in_scope_check_ignores_case_and_relative_spelling(self):
        payload = self.structured(self.package, scope_gaps=[_gap(path=str(self.ctx_b).upper())])
        self.assertRejected(self.package, payload, "names an in-scope file")

    def test_gap_with_empty_path_rejected(self):
        payload = self.structured(self.package, scope_gaps=[{**_gap(), "path": "", "symbol": "helper"}])
        self.assertRejected(self.package, payload, "a symbol alone cannot show a dependency is outside scope")

    def test_four_gaps_rejected(self):
        gaps = [_gap(path=f"outside/f{i}.py") for i in range(4)]
        self.assertRejected(self.package, self.structured(self.package, scope_gaps=gaps), "at most 3")

    def test_zero_evidence_rejected(self):
        payload = self.structured(self.package, evidence=[])
        self.assertRejected(self.package, payload, "completed_with_gaps requires in-scope evidence")

    def test_criterion_index_must_exist(self):
        payload = self.structured(self.package, scope_gaps=[_gap(affects="criterion 4")])
        self.assertRejected(self.package, payload, "affects criterion 4")

    def test_malformed_affects_rejected(self):
        payload = self.structured(self.package, scope_gaps=[_gap(affects="the whole thing")])
        self.assertRejected(self.package, payload, "affects must look like")

    def _research_package(self):
        package = broker.prepare_flash_work_package(
            {
                "work_package_id": "WP-GAPS-RQ",
                "read_context": [str(self.ctx_a)],
                "workspace_root": str(self.ws),
                "research_questions": ["What does a.py define?"],
            },
            "research",
            "Investigate.",
        )
        return {**package, "mode": "plan"}

    def _research_payload(self, package, gaps):
        evidence = [
            {
                "source_kind": "code", "location": "src/a.py", "locator": "L1",
                "observation": "x = 1", "primary": True,
            },
            {
                "source_kind": "code", "location": "src/a.py", "locator": "L1:L1",
                "observation": "only one assignment", "primary": False,
            },
        ]
        payload = self.structured(
            package,
            acceptance_criteria=[],
            research_coverage=[
                {
                    "question": package["research_questions"][0],
                    "status": "answered",
                    "answer": "It defines x.",
                    "confidence": "medium",
                    "evidence": evidence,
                    "competing_explanations_checked": ["Another definition elsewhere: not in given files."],
                    "gaps": gaps,
                }
            ],
            scope_gaps=[_gap(affects="question 1")],
        )
        return payload

    def test_question_gap_requires_a_listed_research_gap(self):
        package = self._research_package()
        self.assertRejected(
            package,
            self._research_payload(package, gaps=[]),
            "scope_gaps entry affects question 1 but research_coverage lists no gap for it",
        )

    def test_question_gap_accepted_when_research_gap_is_listed(self):
        package = self._research_package()
        self.assertAccepted(package, self._research_payload(package, gaps=["helper.py was not provided"]))

    def test_question_index_out_of_range_rejected(self):
        package = self._research_package()
        payload = self._research_payload(package, gaps=["helper.py was not provided"])
        payload["scope_gaps"] = [_gap(affects="question 2")]
        self.assertRejected(package, payload, "affects question 2 but research_coverage lists no gap")


class ScopeGapsSchemaAndPromptTests(ScopeGapsBase):
    def test_schema_has_required_scope_gaps_with_max_three(self):
        schema = broker.flash_workhorse_output_schema(self.package)
        self.assertIn("scope_gaps", schema["required"])
        gaps = schema["properties"]["scope_gaps"]
        self.assertEqual(gaps["maxItems"], 3)
        item = gaps["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(sorted(item["required"]), ["affects", "path", "why_needed"])
        self.assertEqual(item["properties"]["path"]["maxLength"], 300)
        self.assertEqual(item["properties"]["why_needed"]["minLength"], 10)
        self.assertEqual(item["properties"]["symbol"]["maxLength"], 200)
        # The format is enforced by the validator, not by a schema "pattern" keyword
        # (agy has never been sent one).
        self.assertNotIn("pattern", item["properties"]["affects"])
        self.assertIn("question N", item["properties"]["affects"]["description"])
        self.assertTrue(broker.SCOPE_GAP_AFFECTS_RE.fullmatch("question 2"))
        self.assertFalse(broker.SCOPE_GAP_AFFECTS_RE.fullmatch("q2"))
        self.assertIn("scope_gaps", schema["properties"]["ambiguities"]["description"])

    def test_prompt_tells_read_only_workers_to_use_scope_gaps(self):
        text = broker.wrap_flash_workhorse_prompt("Inspect.", self.package)
        self.assertIn("In a read-only package (mode plan)", text)
        self.assertIn("scope_gaps", text)
        self.assertIn("still means status=blocked", text)


class ScopeGapsOutcomeTests(ScopeGapsBase):
    def test_classify_gives_completed_with_gaps(self):
        structured = self.structured(self.package)
        self.assertEqual(broker.classify_flash_outcome("{}", "ok", structured), "completed_with_gaps")
        self.assertIn("completed_with_gaps", broker.FLASH_CREDITABLE_OUTCOMES)

    def test_gate_treats_completed_with_gaps_as_creditable(self):
        self.assertIn("completed_with_gaps", routing_gate.CREDITABLE_OUTCOMES)
        self.assertIn(routing_gate.CREDITABLE_OUTCOME, routing_gate.CREDITABLE_OUTCOMES)

    def test_no_native_handoff_for_completed_with_gaps(self):
        self.assertIsNone(
            broker.native_handoff_for_flash_outcome({}, "completed_with_gaps", "broker:abc")
        )

    def _consult(self, structured):
        outer = {
            "package_id": self.package["package_id"],
            "status": "SUCCESS",
            "model": MODEL,
            "structured_output": structured,
            "duration_seconds": 1.0,
            "num_turns": 1,
            "usage": {"total_tokens": 10},
        }
        catalog = [broker.model_entry(MODEL, "Gemini 3.7 Flash (High)")]
        args = {
            "prompt": "Answer from the given files.",
            "task_kind": "quick_check",
            "mode": "plan",
            "target_model": MODEL,
            "effort": "high",
            "work_package_id": self.package["package_id"],
            "read_context": list(self.package["read_context"]),
            "workspace_root": str(self.ws),
            "acceptance_criteria": list(self.package["acceptance_criteria"]),
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "discover_antigravity_models", return_value=catalog), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", return_value=(0, json.dumps(outer), "")), \
             mock.patch.object(broker, "store_consultation"):
            return broker.consult("antigravity", args)

    def test_end_to_end_envelope_for_valid_gaps(self):
        result = self._consult(self.structured(self.package))
        self.assertEqual(result["outcome"], "completed_with_gaps", result.get("response"))
        self.assertTrue(result["credit_eligible"])
        self.assertTrue(result["brain_verification"]["required"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["suggested_read_context"], ["outside/helper.py"])
        self.assertEqual(result["scope_gaps"][0]["affects"], "criterion 1")
        self.assertIn("never applied automatically", result["suggested_read_context_note"])
        self.assertNotIn("native_handoff", result)

    def test_end_to_end_without_gaps_stays_completed_verified(self):
        result = self._consult(self.structured(self.package, scope_gaps=[]))
        self.assertEqual(result["outcome"], "completed_verified", result.get("response"))
        self.assertTrue(result["credit_eligible"])
        self.assertNotIn("suggested_read_context", result)

    def test_end_to_end_in_scope_gap_is_rejected_and_not_credited(self):
        payload = self.structured(self.package, scope_gaps=[_gap(path=str(self.ctx_a))])
        with tempfile.TemporaryDirectory() as quarantine:
            with mock.patch.object(broker, "QUARANTINE_ROOT", Path(quarantine) / "rejected"), \
                 mock.patch.object(broker, "QUARANTINE_LOCK_PATH", Path(quarantine) / ".prune.lock"):
                result = self._consult(copy.deepcopy(payload))
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(result["credit_eligible"])


if __name__ == "__main__":
    unittest.main()
