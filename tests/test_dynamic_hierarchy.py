"""Focused tests for future-proof role selection and Claude fallbacks/modes."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def claude_stream(model: str, response: str = "ok") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"model": model, "content": [{"text": response}]},
                }
            ),
            json.dumps({"type": "result", "result": response}),
        ]
    )


class DynamicCodexRoleTests(unittest.TestCase):
    def test_live_priority_and_descriptions_select_all_roles(self):
        roles = broker.codex_roles_from_models(
            [
                broker.model_entry(
                    "gpt-8-brain", source="codex-debug", metadata={"priority": 1, "visibility": "list", "description": "frontier"}
                ),
                broker.model_entry(
                    "gpt-8-worker", source="codex-debug", metadata={"priority": 4, "visibility": "list", "description": "balanced everyday workhorse"}
                ),
                broker.model_entry(
                    "gpt-8-reader", source="codex-debug", metadata={"priority": 9, "visibility": "list", "description": "affordable cost-efficient fast"}
                ),
            ]
        )
        self.assertEqual(roles["frontier"]["id"], "gpt-8-brain")
        self.assertEqual(roles["workhorse"]["id"], "gpt-8-worker")
        self.assertEqual(roles["reader"]["id"], "gpt-8-reader")

    def test_cost_policies_use_dynamic_roles(self):
        with mock.patch.object(
            broker,
            "current_codex_role_model",
            side_effect=lambda role: {"reader": "gpt-8-reader", "workhorse": "gpt-8-worker"}[role],
        ):
            cheap = broker.apply_codex_model_policy(
                {"model_policy": "cheap_read"}, "read files", "research", None, None
            )
            balanced = broker.apply_codex_model_policy(
                {"model_policy": "workhorse"}, "write tests", "implementation", None, None
            )
        self.assertEqual(cheap[:2], ("gpt-8-reader", broker.CODEX_CHEAP_EFFORT))
        self.assertEqual(balanced[:2], ("gpt-8-worker", "medium"))

    def test_astra_seed_outranks_live_sol_and_explicit_future_frontier_wins(self):
        seeded = broker.model_roles.seed_codex_frontier(
            {"models": [{"id": "gpt-5.6-sol", "priority": 4, "visibility": "list",
                         "description": "balanced everyday workhorse"}]}
        )
        roles = broker.model_roles.select_codex_roles(seeded)
        self.assertEqual(roles.frontier["id"], "gpt-6-astra")
        self.assertEqual(roles.workhorse["id"], "gpt-5.6-sol")
        seeded["models"].append(
            {"id": "gpt-7-future", "priority": 4, "visibility": "list",
             "capability_tier": "frontier"}
        )
        roles = broker.model_roles.select_codex_roles(seeded)
        self.assertEqual(roles.frontier["id"], "gpt-7-future")

    def test_astra_selection_is_valid_native_expectation_for_live_sol(self):
        models = [
            broker.model_entry(
                "gpt-6-astra", source="static", metadata=broker.model_roles.CODEX_FRONTIER_SEED
            ),
            broker.model_entry(
                "gpt-5.6-sol", source="codex-debug",
                metadata={"priority": 4, "visibility": "list", "description": "balanced workhorse"},
            ),
        ]
        roles = broker.codex_roles_from_models(models)
        self.assertEqual(roles["frontier"]["id"], "gpt-6-astra")
        self.assertEqual(roles["workhorse"]["id"], "gpt-5.6-sol")
        with mock.patch.object(broker, "current_codex_roles", return_value=roles):
            self.assertEqual(
                broker._decision_native_requirement("codex", "gpt-5.6-sol"),
                {"family": "codex", "model": "gpt-6-astra"},
            )


class DynamicAntigravityRoleTests(unittest.TestCase):
    @staticmethod
    def _catalog(*live_slugs: str) -> list[dict]:
        return [
            broker.antigravity_model_entry_from_slug(
                broker.ANTIGRAVITY_DEFAULT_MODEL, "static"
            ),
            *[
                broker.antigravity_model_entry_from_slug(slug, "antigravity-cli")
                for slug in live_slugs
            ],
        ]

    def test_live_37_outranks_36_and_is_never_a_brain(self):
        roles = broker.antigravity_roles_from_models(
            self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        )
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertIsNone(roles["frontier"])
        self.assertFalse(roles["authoritative"])
        self.assertFalse(roles["peer_brain_eligible"])
        self.assertEqual(roles["capability_tier"], "workhorse")

    def test_live_cli_tabular_catalog_is_parsed_and_drives_workhorse(self):
        stdout = "\n".join(
            [
                "gemini-3.7-flash-high\tGemini 3.7 Flash (High)",
                "gemini-3.6-flash-high\tGemini 3.6 Flash (High)",
            ]
        )
        proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE", None), \
             mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE_AT", 0.0), \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "_should_probe_antigravity_models", return_value=True), \
             mock.patch.object(broker, "_load_antigravity_catalog", return_value=[]), \
             mock.patch.object(broker.subprocess, "run", return_value=proc), \
             mock.patch.object(broker.shutil, "which", return_value=None):
            models = broker.discover_antigravity_models()
        roles = broker.antigravity_roles_from_models(models)
        by_id = {item["id"]: item for item in models}
        self.assertEqual(by_id["gemini-3.7-flash-high"]["source"], "antigravity-cli")
        self.assertEqual(by_id["gemini-3.6-flash-high"]["source"], "antigravity-cli")
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertEqual(roles["source"], "antigravity-cli")

    def test_future_versions_sort_numerically_and_preview_never_promotes(self):
        roles = broker.antigravity_roles_from_models(
            self._catalog(
                "gemini-3.9-flash-high",
                "gemini-3.10-flash-high",
                "gemini-4-flash-high",
                "gemini-99.0-flash-high-preview",
                "gemini-99-flash-lite-high",
                "gemini-99-image-flash-high",
                "gemini-99-pro-high",
            )
        )
        self.assertEqual(roles["workhorse"]["id"], "gemini-4-flash-high")

    def test_static_36_is_offline_fallback_only(self):
        roles = broker.antigravity_roles_from_models(self._catalog())
        self.assertEqual(roles["workhorse"]["id"], broker.ANTIGRAVITY_DEFAULT_MODEL)
        self.assertEqual(roles["source"], "offline-fallback")

    def test_generic_flash_moves_to_latest_but_explicit_version_stays_exact(self):
        catalog = self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        common = {
            "target_agent": "antigravity",
            "project": "p",
            "topic": "routing-test",
        }
        with mock.patch.object(broker, "discover_antigravity_models", return_value=catalog) as discover, \
             mock.patch.object(broker, "find_model_default", return_value=None), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")):
            bare = broker.resolve_model_request(common)
            generic = broker.resolve_model_request({**common, "target_model": "gemini flash"})
            exact = broker.resolve_model_request(
                {**common, "target_model": "gemini-3.6-flash-high"}
            )
        self.assertEqual(bare["target_model"], "gemini-3.7-flash-high")
        self.assertEqual(bare["source"], "family_workhorse")
        self.assertEqual(generic["target_model"], "gemini-3.7-flash-high")
        self.assertEqual(generic["source"], "family_workhorse")
        self.assertEqual(exact["target_model"], "gemini-3.6-flash-high")
        self.assertEqual(exact["source"], "explicit_request")
        self.assertGreaterEqual(
            sum(call.kwargs.get("force_live") is True for call in discover.call_args_list),
            2,
        )

    def test_catalog_and_guide_expose_non_authoritative_workhorse_role(self):
        models = self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        with mock.patch.object(broker, "discover_antigravity_models", return_value=models):
            catalog = broker.list_agent_models("antigravity")
            guide = broker.get_model_routing_guide("antigravity")
        roles = catalog["catalogs"]["antigravity"]["roles"]
        policy = guide["defaults"]["antigravity_cli"]
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertFalse(roles["peer_brain_eligible"])
        self.assertEqual(policy["role"], "workhorse")
        self.assertFalse(policy["authoritative"])
        self.assertFalse(policy["peer_brain_eligible"])
        self.assertFalse(policy["native_child_agent"])
        self.assertIn("bounded search/read/extraction/summaries/drafting", policy["recommended_for"])
        self.assertEqual(policy["failure_fallback"]["codex"], ["explorer", "worker"])
        self.assertEqual(policy["failure_fallback"]["claude"], ["Explore", "economy-worker"])

        self.assertTrue(policy["failure_fallback"]["record_fallback"])
        self.assertIn("automatic external labour default only for Codex and Claude", policy["rule"])
        self.assertIn("not Gemini/Antigravity or unknown hosts", policy["rule"])
        self.assertIn("Every non-creditable Flash outcome", policy["rule"])
        self.assertIn("never auto-launches native work", policy["rule"])
        self.assertIn("structured native reader/workhorse handoff", policy["rule"])
        self.assertIn("concurrently only on independent packages", policy["rule"])
        self.assertIn("writes are serial unless demonstrably isolated", policy["rule"])
        self.assertIn("brain reviews evidence/diffs", policy["rule"])

        examples = guide["caller_examples"]
        read_args = examples["antigravity_external_read"]["args"]
        write_args = examples["antigravity_external_implementation"]["args"]
        for args in (read_args, write_args):
            self.assertEqual(args["target_agent"], "antigravity")
            self.assertEqual(args["surface"], "cli")
            self.assertEqual(args["target_model"], "gemini flash")
            self.assertEqual(args["effort"], "high")
        self.assertEqual(read_args["mode"], "plan")
        self.assertEqual(write_args["mode"], "accept-edits")
        self.assertIn("approved isolated package", write_args["prompt"])
        self.assertEqual(write_args["work_package_id"], "WP-1")
        self.assertLessEqual(len(write_args["allowed_files"]), broker.FLASH_WORKHORSE_MAX_ALLOWED_FILES)
        self.assertTrue(write_args["acceptance_criteria"])
        self.assertIn("one work package per call", " ".join(policy["hard_requirements"]))
        self.assertIn("schema-enforced JSON", " ".join(policy["hard_requirements"]))

    def test_explicit_flash_pin_does_not_force_live_refresh_or_upgrade(self):
        catalog = self._catalog("gemini-3.8-flash-high")
        with mock.patch.object(broker, "discover_antigravity_models", return_value=catalog) as discover, \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")):
            result = broker.resolve_model_request(
                {
                    "project": "p",
                    "target_agent": "antigravity",
                    "target_model": "gemini-3.6-flash-high",
                }
            )
        self.assertEqual(result["target_model"], "gemini-3.6-flash-high")
        self.assertFalse(any(call.kwargs.get("force_live") for call in discover.call_args_list))

    @staticmethod
    def _flash_package() -> dict:
        return broker.prepare_flash_work_package(
            {
                "work_package_id": "WP-TEST",
                "allowed_files": ["src/worker.py", "tests/test_worker.py"],
                "acceptance_criteria": ["Focused tests pass."],
            },
            "implementation",
            "Implement the approved bounded change.",
        )

    @staticmethod
    def _flash_output(package_id: str, **overrides) -> dict:
        structured = {
            "package_id": package_id,
            "status": "completed",
            "summary": "Implemented the bounded package.",
            "acceptance_criteria": [
                {"criterion": "Focused tests pass.", "status": "passed", "evidence": ["pytest: passed"]}
            ],
            "files_changed": [{"path": "src/worker.py", "change": "Applied bounded fix."}],
            "checks": [
                {"command": "pytest tests/test_worker.py", "status": "passed", "exit_code": 0, "output_excerpt": "1 passed"}
            ],
            "evidence": [
                {"claim": "Change is present", "path": "src/worker.py", "line": "12", "observation": "Guard added."}
            ],
            "claims": [
                {"statement": "Guard is present.", "basis": "observed", "evidence": ["src/worker.py:12"]}
            ],
            "research_coverage": [],
            "ambiguities": [],
            "risks": [],
            "next_action": "Brain verifies diff and test output.",
            "brain_verification_required": "required",
        }
        structured.update(overrides)
        return {
            "conversation_id": "conv-1",
            "status": "SUCCESS",
            "structured_output": structured,
            "duration_seconds": 2.5,
            "num_turns": 1,
            "usage": {"total_tokens": 100},
        }

    def test_flash_implementation_requires_a_bounded_envelope(self):
        with self.assertRaisesRegex(ValueError, "work_package_id"):
            broker.prepare_flash_work_package({}, "implementation", "Implement everything.")
        with self.assertRaisesRegex(ValueError, "1-5 allowed_writes"):
            broker.prepare_flash_work_package(
                {"work_package_id": "WP-X", "acceptance_criteria": ["tests pass"]},
                "implementation",
                "Implement everything.",
            )
        with self.assertRaisesRegex(ValueError, "acceptance_criteria"):
            broker.prepare_flash_work_package(
                {"work_package_id": "WP-X", "allowed_files": ["src/x.py"]},
                "implementation",
                "Implement everything.",
            )

    @staticmethod
    def _research_package(*questions: str) -> dict:
        return broker.prepare_flash_work_package(
            {"work_package_id": "WP-RESEARCH", "research_questions": list(questions)},
            "research",
            "Research only the declared questions.",
        )

    @staticmethod
    def _research_evidence(source_kind: str = "code", *, primary: bool = True) -> dict:
        if source_kind == "web":
            return {
                "source_kind": "web",
                "location": "https://example.com/primary",
                "locator": "Models section",
                "observation": "The primary page states the relevant behavior.",
                "primary": primary,
            }
        if source_kind == "document":
            return {
                "source_kind": "document",
                "location": "docs/design.md",
                "locator": "section 2.1",
                "observation": "The design section documents the constraint.",
                "primary": primary,
            }
        if source_kind == "command":
            return {
                "source_kind": "command",
                "location": "pytest tests/test_worker.py",
                "locator": "exit 0",
                "observation": "The focused check passed.",
                "primary": primary,
            }
        return {
            "source_kind": "code",
            "location": "src/worker.py",
            "locator": "L12-L18",
            "observation": "The implementation contains the relevant guard.",
            "primary": primary,
        }

    def _research_outer(self, package: dict, coverage: list, **overrides) -> dict:
        return self._flash_output(
            package["package_id"],
            acceptance_criteria=[],
            files_changed=[],
            checks=[],
            research_coverage=coverage,
            **overrides,
        )

    def test_research_requires_one_to_three_unique_bounded_questions(self):
        with self.assertRaisesRegex(ValueError, "1-3 research_questions"):
            self._research_package()
        with self.assertRaisesRegex(ValueError, "1-3 research_questions"):
            self._research_package("q1", "q2", "q3", "q4")
        with self.assertRaisesRegex(ValueError, "unique"):
            self._research_package("What changed?", " what changed? ")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self._research_package(" ")
        with self.assertRaisesRegex(ValueError, "at most 500"):
            self._research_package("x" * 501)

    def test_research_accepts_mcp_iterable_proxy_and_json_array_string(self):
        class ArrayProxy:
            def __iter__(self):
                return iter(["Q1", "Q2"])

        proxied = broker.prepare_flash_work_package(
            {"research_questions": ArrayProxy()}, "research", "Research."
        )
        encoded = broker.prepare_flash_work_package(
            {"research_questions": '["Q1","Q2"]'}, "research", "Research."
        )
        self.assertEqual(proxied["research_questions"], ["Q1", "Q2"])
        self.assertEqual(encoded["research_questions"], ["Q1", "Q2"])

    def test_route_agent_task_forwards_research_questions_to_flash_consult(self):
        resolved = {
            "status": "resolved",
            "target_agent": "antigravity_cli",
            "target_model": "gemini-3.8-flash-high",
            "effort": "high",
            "source": "explicit_request",
        }
        args = {
            "prompt": "Investigate the bounded questions.",
            "target_agent": "antigravity",
            "surface": "cli",
            "target_model": "gemini flash",
            "effort": "high",
            "mode": "plan",
            "task_kind": "research",
            "research_questions": ["Q1", "Q2"],
        }
        with mock.patch.object(broker, "resolve_model_request", return_value=resolved), \
             mock.patch.object(broker, "consult", return_value={"status": "ok"}) as consult, \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None):
            broker.route_agent_task(args)
        forwarded = consult.call_args.args[1]
        self.assertEqual(forwarded["research_questions"], ["Q1", "Q2"])

    def test_search_alias_and_nonresearch_backward_compatibility(self):
        self.assertEqual(broker.normalize_task_kind("search"), "research")
        package = broker.prepare_flash_work_package(
            {"research_questions": ["ignored for compatibility"]}, "quick_check", "Check one fact."
        )
        self.assertEqual(package["research_questions"], [])
        schema = broker.flash_workhorse_output_schema(package)
        coverage = schema["properties"]["research_coverage"]
        self.assertEqual((coverage["minItems"], coverage["maxItems"]), (0, 0))
        _, errors = broker.validate_flash_workhorse_result(
            self._flash_output(package["package_id"]), package
        )
        self.assertEqual(errors, [])

    def test_research_schema_prompt_and_tool_inputs_are_explicit(self):
        questions = ["What does the code do?", "What evidence contradicts it?"]
        package = self._research_package(*questions)
        schema = broker.flash_workhorse_output_schema(package)
        self.assertIn("research_coverage", schema["required"])
        coverage = schema["properties"]["research_coverage"]
        self.assertEqual((coverage["minItems"], coverage["maxItems"]), (2, 2))
        prompt = broker.wrap_flash_workhorse_prompt("Investigate.", package)
        self.assertIn("Never stop at the first plausible result", prompt)
        self.assertIn("surface summary", prompt)
        self.assertIn("competing explanation", prompt)
        self.assertIn("Never invent line numbers for web sources", prompt)
        self.assertIn("8,000 characters", prompt)
        self.assertLess(prompt.index(questions[0]), prompt.index(questions[1]))
        for name in ("consult_antigravity", "route_agent_task"):
            tool = next(item for item in broker.TOOLS if item["name"] == name)
            properties = tool["inputSchema"]["properties"]
            self.assertIn("research", properties["task_kind"]["enum"])
            self.assertIn("search", properties["task_kind"]["enum"])
            self.assertEqual(properties["research_questions"]["maxItems"], 3)
        rules = " ".join(broker.COST_AWARE_ROUTING_RULES)
        self.assertIn("native child-agent mechanism", rules)
        self.assertIn("compact native result to consult_decision", rules)
        self.assertIn("must never replace this with a nested same-vendor CLI call", rules)
        self.assertIn("never substitute Flash for flagship judgment", rules)
        self.assertIn("Every factual investigation sent to Flash MUST use task_kind=research", rules)
        self.assertIn("beyond the first match", rules)
        self.assertIn("NOT FOUND with searched boundaries", rules)
        self.assertIn("in-app/extension surface is retired", rules)
        self.assertIn("needs_model_selection", rules)


class ProgressiveDecisionConsultTests(unittest.TestCase):
    _flash_package = staticmethod(DynamicAntigravityRoleTests._flash_package)
    _flash_output = staticmethod(DynamicAntigravityRoleTests._flash_output)
    _research_package = staticmethod(DynamicAntigravityRoleTests._research_package)
    _research_evidence = staticmethod(DynamicAntigravityRoleTests._research_evidence)
    _research_outer = DynamicAntigravityRoleTests._research_outer

    def setUp(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()

    @staticmethod
    def _args(vendor: str, model: str, complexity: str = "architecture", **extra):
        return {
            "project": "p",
            "topic": "decision-tests",
            "session_id": "session-1",
            "work_package_id": "WP-DECISION-1",
            "host": {"vendor": vendor, "model": model},
            "complexity": complexity,
            "brief": {
                "decision": "Choose a routing design.",
                "constraints": ["Preserve compatibility."],
                "options": [{"id": "A", "summary": "Add one orchestrator."}],
                "questions": ["Is A the safest option?"],
                "evidence": [{"ref": "router.py:10", "claim": "The old route is fixed."}],
            },
            **extra,
        }

    @staticmethod
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

    @staticmethod
    def _native(family: str, status: str = "completed"):
        return {
            "family": family,
            "model": "gpt-6-astra" if family == "codex" else "claude-fable-5",
            "status": status,
            "attestation": "verified",
            "agent_id": "native-17",
            "summary": "Use option A." if status == "completed" else "Native provider unavailable.",
        }

    def _run(self, args, side_effect=None):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "consult", side_effect=side_effect or self._ok_consult) as consult, \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 17}):
            return broker.consult_decision(args), consult

    def test_missing_native_descriptor_returns_request_without_dispatch(self):
        result, consult = self._run(self._args("codex", "gpt-5.6-sol"))
        self.assertEqual(result["status"], "needs_native_consultation")
        self.assertEqual(result["native_request"]["model"], "gpt-6-astra")
        self.assertEqual(result["native_request"]["effort"], "xhigh")
        self.assertEqual(result["native_request"]["lane"], "native_same_vendor")
        self.assertEqual(result["native_request"]["return_contract"]["family"], "codex")
        self.assertIn("at most", result["native_request"]["return_contract"]["summary"])
        consult.assert_not_called()

    def test_sol_architecture_uses_native_astra_then_cross_vendor_fable(self):
        result, consult = self._run(
            self._args(
                "codex",
                "gpt-5.6-sol",
                native_consultation=self._native("codex"),
            )
        )
        self.assertEqual([item["resolved_model"] for item in result["consultations"]], ["gpt-6-astra", "fable"])
        self.assertEqual(
            [item["lane"] for item in result["consultations"]],
            ["native_same_vendor", "switchboard_cross_vendor"],
        )
        self.assertEqual([call.args[0] for call in consult.call_args_list], ["claude"])
        self.assertTrue(all(call.args[1]["effort"] == "xhigh" for call in consult.call_args_list))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["ledger_ref"], "event:17")

    def test_flagship_hosts_are_not_sent_to_themselves(self):
        astra, astra_consult = self._run(self._args("codex", "gpt-6-astra"))
        fable, fable_consult = self._run(self._args("claude", "claude-fable-5"))
        self.assertEqual([call.args[0] for call in astra_consult.call_args_list], ["claude"])
        self.assertEqual([call.args[0] for call in fable_consult.call_args_list], ["codex"])
        self.assertEqual(astra["consultations"][0]["resolved_model"], "fable")
        self.assertEqual(fable["consultations"][0]["resolved_model"], "gpt-6-astra")

    def test_bounded_opus_uses_only_native_fable_and_gemini_uses_both(self):
        opus, opus_consult = self._run(
            self._args(
                "claude",
                "opus",
                "bounded",
                native_consultation=self._native("claude"),
            )
        )
        _, gemini_consult = self._run(self._args("gemini", "gemini-3.8-flash-high", "bounded"))
        opus_consult.assert_not_called()
        self.assertEqual([item["lane"] for item in opus["consultations"]], ["native_same_vendor"])
        self.assertEqual([call.args[0] for call in gemini_consult.call_args_list], ["codex", "claude"])

    def test_gemini_complexity_routes_both_flagships_at_progressive_effort(self):
        for complexity, expected_effort in (
            ("bounded", "high"),
            ("architecture", "xhigh"),
            ("critical", "max"),
        ):
            with self.subTest(complexity=complexity):
                result, consult = self._run(
                    self._args("gemini", "gemini-3.8-flash-high", complexity)
                )
            self.assertEqual([call.args[0] for call in consult.call_args_list], ["codex", "claude"])
            self.assertEqual([call.args[1]["effort"] for call in consult.call_args_list], [expected_effort, expected_effort])
            self.assertEqual([item["requested_effort"] for item in result["consultations"]], [expected_effort, expected_effort])

    def test_gemini_bounded_critical_risk_promotes_both_calls_to_max(self):
        result, consult = self._run(
            self._args(
                "gemini", "gemini-3.8-flash-high", "bounded", risk_flags=["data-loss"]
            )
        )
        self.assertEqual(result["complexity"], "critical")
        self.assertTrue(result["complexity_escalated"])
        self.assertEqual([call.args[0] for call in consult.call_args_list], ["codex", "claude"])
        self.assertEqual([call.args[1]["effort"] for call in consult.call_args_list], ["max", "max"])

    def test_risk_flag_raises_effort_to_max(self):
        result, consult = self._run(
            self._args(
                "codex",
                "gpt-5.6-sol",
                "bounded",
                risk_flags=["migration"],
                native_consultation=self._native("codex"),
            )
        )
        self.assertEqual(result["complexity"], "critical")
        self.assertTrue(result["complexity_escalated"])
        self.assertTrue(all(call.args[1]["effort"] == "max" for call in consult.call_args_list))

    def test_native_unavailable_is_not_retried_through_switchboard(self):
        result, consult = self._run(
            self._args(
                "codex",
                "gpt-5.6-sol",
                "bounded",
                native_consultation=self._native("codex", "unavailable"),
            )
        )
        consult.assert_not_called()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["consultations"][0]["lane"], "native_same_vendor")
        self.assertTrue(result["handoff_notices"])

    def test_native_failure_still_allows_only_opposite_vendor_architecture_consult(self):
        result, consult = self._run(
            self._args(
                "codex",
                "gpt-5.6-sol",
                native_consultation=self._native("codex", "failed"),
            )
        )
        self.assertEqual([call.args[0] for call in consult.call_args_list], ["claude"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["handoff_notices"])

    def test_completed_native_descriptor_requires_bounded_summary(self):
        native = self._native("codex")
        native["summary"] = ""
        with self.assertRaisesRegex(ValueError, "nonempty summary"):
            self._run(
                self._args(
                    "codex",
                    "gpt-5.6-sol",
                    native_consultation=native,
                )
            )

    def test_ledger_metadata_distinguishes_native_and_cross_vendor_lanes(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "consult", side_effect=self._ok_consult), \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 18}) as record:
            broker.consult_decision(
                self._args(
                    "codex",
                    "gpt-5.6-sol",
                    native_consultation=self._native("codex"),
                )
            )
        metadata = json.loads(record.call_args.args[5])
        self.assertEqual(
            [item["lane"] for item in metadata["targets"]],
            ["native_same_vendor", "switchboard_cross_vendor"],
        )

    def test_quota_failure_is_latched_and_returned_as_handoff_notice(self):
        calls = []

        def outcome(family, args):
            calls.append(family)
            if family == "codex":
                return {"status": "error", "response": "Usage quota exceeded"}
            return self._ok_consult(family, args)

        first, _ = self._run(self._args("gemini", "gemini-3.8-flash-high"), outcome)
        second, _ = self._run(self._args("gemini", "gemini-3.8-flash-high"), outcome)
        self.assertEqual(calls, ["codex", "claude", "claude"])
        self.assertEqual(first["consultations"][0]["status"], "skipped_quota")
        self.assertEqual(second["consultations"][0]["attestation"], "not_run")
        self.assertTrue(first["handoff_notices"])

    def test_assembled_provider_prompt_budget_rejects_before_dispatch(self):
        args = self._args("gemini", "gemini-3.8-flash-high", "bounded")
        with mock.patch.object(broker, "DECISION_PROVIDER_PROMPT_MAX_BYTES", 100), \
             mock.patch.object(broker, "consult") as consult:
            with self.assertRaisesRegex(ValueError, "assembled flagship request exceeds the provider budget"):
                broker.consult_decision(args)
        consult.assert_not_called()

    def test_oversized_provider_advice_respects_per_advice_and_combined_caps(self):
        calls = []

        def oversized(family, args):
            calls.append((family, args["max_response_chars"]))
            result = self._ok_consult(family, args)
            result["response"] = family + ":" + ("x" * 20_000)
            return result

        result, _ = self._run(
            self._args(
                "gemini", "gemini-3.8-flash-high", "architecture", max_response_chars=1200
            ),
            oversized,
        )
        self.assertEqual(calls, [("codex", 1200), ("claude", 1200)])
        self.assertEqual([item["target"] for item in result["consultations"]], ["codex", "claude"])
        self.assertTrue(all(len(item["advice"]) <= 1200 for item in result["consultations"]))
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")),
            broker.DECISION_COMBINED_MAX_BYTES,
        )
        self.assertIsInstance(result["consultations"], list)

    def test_nonquota_unavailability_is_isolated_latched_and_noticed(self):
        calls = []

        def outcome(family, args):
            calls.append(family)
            if family == "codex":
                return {"status": "error", "response": "Connection refused by provider"}
            return self._ok_consult(family, args)

        first, _ = self._run(self._args("gemini", "gemini-3.8-flash-high"), outcome)
        second, _ = self._run(self._args("gemini", "gemini-3.8-flash-high"), outcome)
        self.assertEqual(calls, ["codex", "claude", "claude"])
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["consultations"][0]["status"], "skipped_unavailable")
        self.assertEqual(second["consultations"][0]["status"], "skipped_unavailable")
        self.assertEqual(second["consultations"][0]["attestation"], "not_run")
        self.assertTrue(first["handoff_notices"])
        self.assertTrue(second["handoff_notices"])

    def test_both_gemini_flagships_unavailable_returns_unavailable_with_notices(self):
        calls = []

        def unavailable(family, _args):
            calls.append(family)
            return {"status": "error", "response": "Authentication failed for provider"}

        result, _ = self._run(
            self._args("gemini", "gemini-3.8-flash-high", "critical"), unavailable
        )
        self.assertEqual(calls, ["codex", "claude"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            [item["status"] for item in result["consultations"]],
            ["skipped_unavailable", "skipped_unavailable"],
        )
        self.assertEqual(len(result["handoff_notices"]), 2)

    def test_unicode_excerpt_budget_is_enforced_before_dispatch(self):
        args = self._args("codex", "gpt-5.6-sol")
        args["brief"]["evidence"] = [
            {"ref": f"r{i}", "claim": "c", "excerpt": "é" * 1100} for i in range(4)
        ]
        with mock.patch.object(broker, "consult") as consult:
            with self.assertRaisesRegex(ValueError, "evidence excerpts use"):
                broker.consult_decision(args)
        consult.assert_not_called()

    def test_research_coverage_omission_reordering_and_substitution_reject(self):
        package = self._research_package("Q1", "Q2")
        missing = self._research_outer(package, [])
        _, missing_errors = broker.validate_flash_workhorse_result(missing, package)
        self.assertTrue(any("exactly match the dispatched order" in error for error in missing_errors))
        base = {
            "status": "blocked",
            "answer": "",
            "confidence": "low",
            "evidence": [],
            "competing_explanations_checked": [],
            "gaps": ["Evidence unavailable."],
        }
        wrong = self._research_outer(
            package,
            [{"question": "Q2", **base}, {"question": "substitute", **base}],
            status="blocked",
        )
        _, wrong_errors = broker.validate_flash_workhorse_result(wrong, package)
        self.assertTrue(any("exactly match the dispatched order" in error for error in wrong_errors))

    def test_completed_research_rejects_shallow_answer_variants(self):
        package = self._research_package("Q1")
        strong = {
            "question": "Q1",
            "status": "answered",
            "answer": "The guard controls the behavior.",
            "confidence": "high",
            "evidence": [self._research_evidence(), self._research_evidence("command", primary=False)],
            "competing_explanations_checked": ["Configuration was checked and ruled out."],
            "gaps": [],
        }
        variants = {
            "blocked": {**strong, "status": "blocked"},
            "empty answer": {**strong, "answer": ""},
            "low confidence": {**strong, "confidence": "low"},
            "one probe": {**strong, "evidence": strong["evidence"][:1]},
            "no competitor": {**strong, "competing_explanations_checked": []},
            "no primary": {
                **strong,
                "evidence": [
                    {**strong["evidence"][0], "primary": False},
                    {**strong["evidence"][1], "primary": False},
                ],
            },
        }
        for name, coverage in variants.items():
            with self.subTest(name=name):
                _, errors = broker.validate_flash_workhorse_result(
                    self._research_outer(package, [coverage]), package
                )
                self.assertTrue(errors)

    def test_research_rejects_bad_web_and_code_document_citations(self):
        package = self._research_package("Q1")
        evidence = [
            {**self._research_evidence("web"), "location": "example.com/no-scheme"},
            {**self._research_evidence(), "locator": "somewhere nearby", "primary": False},
            {**self._research_evidence("document"), "locator": "vague", "primary": False},
        ]
        coverage = [{
            "question": "Q1", "status": "answered", "answer": "Answer", "confidence": "high",
            "evidence": evidence, "competing_explanations_checked": ["Alternative checked."], "gaps": [],
        }]
        _, errors = broker.validate_flash_workhorse_result(
            self._research_outer(package, coverage), package
        )
        joined = " | ".join(errors)
        self.assertIn("web location must be an http(s) URL", joined)
        self.assertIn("code locator is invalid", joined)
        self.assertIn("document locator is invalid", joined)
        self.assertIn(broker.RESEARCH_LOCATOR_DESCRIPTION, joined)

    def test_callable_schemas_expose_attestation_and_locator_limits(self):
        decision_tool = next(tool for tool in broker.TOOLS if tool["name"] == "consult_decision")
        attestation = decision_tool["inputSchema"]["properties"]["native_consultation"]["properties"]["attestation"]
        self.assertEqual(attestation["maxLength"], 80)
        self.assertIn("80 characters", attestation["description"])

        schema = broker.flash_workhorse_output_schema(self._research_package("Q1"))
        locator = schema["properties"]["research_coverage"]["items"]["properties"]["evidence"]["items"]["properties"]["locator"]
        self.assertEqual(locator["description"], broker.RESEARCH_LOCATOR_DESCRIPTION)
        self.assertIn("lines 12-18", locator["description"])
        self.assertIn("pages 3-4", locator["description"])

    def test_valid_code_web_and_well_supported_not_found_research(self):
        questions = ("Code question", "Web question", "Missing question")
        package = self._research_package(*questions)
        coverage = [
            {
                "question": questions[0], "status": "answered", "answer": "The guard is active.",
                "confidence": "high",
                "evidence": [self._research_evidence(), self._research_evidence("command", primary=False)],
                "competing_explanations_checked": ["Configuration override ruled out."], "gaps": [],
            },
            {
                "question": questions[1], "status": "answered", "answer": "The primary page confirms it.",
                "confidence": "medium",
                "evidence": [self._research_evidence("web"), self._research_evidence("document", primary=False)],
                "competing_explanations_checked": ["Cached documentation ruled out."], "gaps": [],
            },
            {
                "question": questions[2], "status": "not_found", "answer": "",
                "confidence": "low",
                "evidence": [
                    self._research_evidence("web", primary=False),
                    {**self._research_evidence("web", primary=False), "location": "https://example.org/search"},
                ],
                "competing_explanations_checked": [],
                "gaps": ["Searched both official indexes; no matching record."],
            },
        ]
        _, errors = broker.validate_flash_workhorse_result(
            self._research_outer(package, coverage), package
        )
        self.assertEqual(errors, [])

    # NOTE: staging/write-back coverage (agy schema+staging dispatch, plus the
    # three-way write-back apply/conflict/deletion tests that used to live
    # here) moved to tests/test_flash_manifest.py when the old directory-walk
    # staging helpers in agent_broker_mcp.py were replaced by the new
    # flash_manifest module. Not dropped -- relocated.

    def test_agy_cli_never_runs_in_the_real_project_tree(self):
        """The worker must never be pointed at the user's own files.

        With permission checks disabled a stray write or command would otherwise land
        on the real project, so this asserts the working directory is a staging copy
        and not the resolved project root.
        """
        import shutil as _shutil
        import tempfile as _tempfile

        # A real workspace, because staging now copies exactly the declared files:
        # pointing this at paths that do not exist would prove nothing about the
        # working directory, only that the manifest refused the package.
        workspace = _tempfile.mkdtemp(prefix="flash-real-project-")
        self.addCleanup(_shutil.rmtree, workspace, True)
        target = broker.Path(workspace) / "src" / "worker.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n", encoding="utf-8")

        package = self._flash_package()
        package["allowed_files"] = [str(target)]
        package["allowed_writes"] = [str(target)]
        stdout = json.dumps(self._flash_output(package["package_id"]))
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", workspace)), \
             mock.patch.object(broker, "run_process", return_value=(0, stdout, "")) as run:
            broker.consult_antigravity_cli(
                "p", "bounded prompt", "plan", "gemini-3.7-flash-high", "high", 60, package
            )
        cwd = broker.Path(str(run.call_args.args[1])).resolve()
        self.assertNotEqual(cwd, broker.Path(workspace).resolve())
        self.assertIn("flash-manifest-", str(cwd))
        # The real file must still be exactly as it was: a plan package writes
        # nothing back, whatever the worker did to its copy.
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")

    def test_flash_danger_full_access_is_rejected_before_agy_starts(self):
        package = self._flash_package()
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "run_process") as run:
            response = broker.consult_antigravity_cli(
                "p", "deploy everything", "danger-full-access", "gemini-3.7-flash-high", "high", 60, package
            )
        self.assertTrue(response.startswith("Antigravity CLI Flash safety policy rejected"))
        run.assert_not_called()

    def test_direct_accept_edits_cannot_bypass_the_package_envelope(self):
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "run_process") as run:
            with self.assertRaisesRegex(ValueError, "work_package_id"):
                broker.consult_antigravity_cli(
                    "p", "edit the project", "accept-edits", "gemini-3.7-flash-high", "high", 60
                )
        run.assert_not_called()

    def test_flash_validation_rejects_contradiction_scope_and_design_rationalization(self):
        package = self._flash_package()
        outer = self._flash_output(
            package["package_id"],
            ambiguities=["Plan does not specify retry semantics."],
            files_changed=[{"path": "src/outside.py", "change": "Expanded scope."}],
            acceptance_criteria=[{"criterion": "A different easier criterion.", "status": "passed", "evidence": []}],
            claims=[{"statement": "The duplicate query is intentional by design.", "basis": "assumption", "evidence": []}],
        )
        _, errors = broker.validate_flash_workhorse_result(outer, package)
        joined = " | ".join(errors)
        self.assertIn("completed contradicts non-empty ambiguities", joined)
        self.assertIn("out-of-scope file reported", joined)
        self.assertIn("acceptance criteria do not exactly match", joined)
        self.assertIn("intentional/by-design claim lacks observed primary evidence", joined)

    def test_consult_marks_flash_completion_pending_brain_verification(self):
        package = self._flash_package()
        normalized = json.dumps(
            {
                "package_id": package["package_id"],
                "worker_status": "completed",
                "structured_output": self._flash_output(package["package_id"])["structured_output"],
                "cli": {},
            }
        )
        args = {
            "prompt": "Implement the approved bounded change.",
            "task_kind": "implementation",
            "mode": "accept-edits",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": package["package_id"],
            "allowed_files": package["allowed_files"],
            "acceptance_criteria": package["acceptance_criteria"],
        }
        with mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "consult_antigravity_cli", return_value=normalized), \
             mock.patch.object(broker, "store_consultation"):
            result = broker.consult("antigravity", args)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["brain_verification"]["status"], "pending")
        self.assertTrue(result["structured_output_enforced"])

    def test_flash_completed_plan_returns_terminal_progress_not_live_updates(self):
        package = broker.prepare_flash_work_package(
            {"work_package_id": "WP-PLAN-PROGRESS"}, "quick_check", "Inspect the bounded package."
        )
        normalized = json.dumps(
            {
                "package_id": package["package_id"],
                "worker_status": "completed",
                "structured_output": self._flash_output(package["package_id"])["structured_output"],
                "cli": {"duration_seconds": 2.5},
            }
        )
        args = {
            "prompt": "Inspect the bounded package.", "task_kind": "quick_check", "mode": "plan",
            "target_model": "gemini-3.7-flash-high", "effort": "high",
            "work_package_id": package["package_id"],
        }
        with mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "consult_antigravity_cli", return_value=normalized), \
             mock.patch.object(broker, "store_consultation"):
            result = broker.consult("antigravity", args)
        progress = result["progress"]
        self.assertEqual(progress["state"], "completed")
        self.assertEqual(progress["current_phase"], "structured_validation")
        self.assertFalse(progress["live_updates_available"])
        self.assertEqual(progress["elapsed_seconds"], 2.5)
        self.assertEqual(
            [(item["phase"], item["status"]) for item in progress["phases"]],
            [("model_resolution", "completed"), ("containment_staging", "completed"),
             ("worker_execution", "completed"), ("structured_validation", "completed"),
             ("workspace_apply", "not_applicable")],
        )

    def test_flash_rejection_and_unavailability_return_truthful_terminal_progress(self):
        package = self._flash_package()
        args = {
            "prompt": "Implement the approved bounded change.", "task_kind": "implementation",
            "mode": "accept-edits", "target_model": "gemini-3.7-flash-high", "effort": "high",
            "work_package_id": package["package_id"], "allowed_files": package["allowed_files"],
            "acceptance_criteria": package["acceptance_criteria"],
        }
        cases = {
            "rejected": "Antigravity CLI structured-output validation failed: missing field",
            "unavailable_pre_mutation": "Antigravity CLI was not found. test",
        }
        for outcome, response in cases.items():
            with self.subTest(outcome=outcome), \
                 mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
                 mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
                 mock.patch.object(broker, "consult_antigravity_cli", return_value=response), \
                 mock.patch.object(broker, "store_consultation"):
                result = broker.consult("antigravity", args)
            progress = result["progress"]
            self.assertEqual(progress["state"], outcome)
            self.assertFalse(progress["live_updates_available"])
            statuses = {item["phase"]: item["status"] for item in progress["phases"]}
            if outcome == "rejected":
                self.assertEqual(progress["current_phase"], "structured_validation")
                self.assertEqual(statuses["structured_validation"], "failed")
                self.assertEqual(statuses["workspace_apply"], "not_applied")
                self.assertEqual(result["artifact_disposition"], None)
            else:
                self.assertEqual(progress["current_phase"], "model_resolution")
                self.assertEqual(statuses["worker_execution"], "not_started")
                self.assertEqual(statuses["structured_validation"], "not_started")

    def test_consult_attaches_native_handoff_for_each_noncreditable_flash_outcome(self):
        package = self._flash_package()
        args = {
            "prompt": "Implement the approved bounded change.",
            "task_kind": "implementation",
            "mode": "accept-edits",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": package["package_id"],
            "allowed_files": package["allowed_files"],
            "acceptance_criteria": package["acceptance_criteria"],
        }
        responses = {
            "unavailable_pre_mutation": "Antigravity CLI was not found. test",
            "rejected": "Antigravity CLI structured-output validation failed: missing field",
            "blocked": json.dumps({"worker_status": "blocked", "structured_output": {"status": "blocked"}, "cli": {}}),
            "failed_pre_mutation": json.dumps({"worker_status": "failed", "structured_output": {"status": "failed"}, "cli": {}}),
        }
        for expected_outcome, response in responses.items():
            with self.subTest(outcome=expected_outcome), \
                 mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), \
                 mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
                 mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
                 mock.patch.object(broker, "consult_antigravity_cli", return_value=response), \
                 mock.patch.object(broker, "store_consultation"), \
                 mock.patch.object(broker, "current_codex_role_model", return_value="gpt-live-terra"):
                result = broker.consult("antigravity", args)
            self.assertEqual(result["outcome"], expected_outcome)
            handoff = result["native_handoff"]
            self.assertEqual(handoff["semantic_lane"], "workhorse")
            self.assertEqual(handoff["native"]["role"], "worker")
            self.assertEqual(handoff["native"]["model"], "gpt-live-terra")
            self.assertTrue(handoff["flash_skip_reason"].endswith(result["receipt"]))
            self.assertFalse(handoff["auto_launch"])


class ClaudeFrontierFallbackTests(unittest.TestCase):
    def _consult(self, runs):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value="claude"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "claude_empty_mcp_config_path", return_value=Path(tmpdir) / "empty.json"), \
             mock.patch.object(broker, "run_process", side_effect=runs) as run:
            result = broker.consult_claude(tmpdir, "check", model_name="best", effort="max")
            return result, run

    def test_best_alias_attests_any_structured_claude_model(self):
        self.assertTrue(broker.claude_model_attested("best", "claude-fable-5"))
        self.assertTrue(broker.claude_model_attested("best", "claude-opus-6-1"))
        self.assertFalse(broker.claude_model_attested("best", "unknown-provider-model"))

    def test_best_unavailable_falls_back_to_fable(self):
        result, run = self._consult(
            [
                (1, "", "Invalid model name: model 'best' is not available"),
                (0, claude_stream("claude-fable-5", "approved"), ""),
            ]
        )
        self.assertEqual(result.initial_model, "best")
        self.assertEqual(result.requested_model, "fable")
        self.assertEqual(result.actual_model, "claude-fable-5")
        self.assertEqual(result.attempted_models, ("best", "fable"))
        self.assertTrue(result.model_attested)
        self.assertEqual(run.call_count, 2)

    def test_best_and_fable_unavailable_fall_back_to_opus(self):
        result, run = self._consult(
            [
                (1, "", "Unknown model: best"),
                (1, "", "Model fable is unavailable for this subscription"),
                (0, claude_stream("claude-opus-6-1", "approved"), ""),
            ]
        )
        self.assertEqual(result.requested_model, "opus")
        self.assertEqual(result.attempted_models, ("best", "fable", "opus"))
        self.assertEqual(run.call_count, 3)

    def test_general_failure_does_not_retry(self):
        result, run = self._consult([(1, "", "Network connection reset")])
        self.assertFalse(result.model_attested)
        self.assertEqual(result.attempted_models, ("best",))
        self.assertEqual(run.call_count, 1)


class ClaudeQueuedModeTests(unittest.TestCase):
    def test_queue_persists_implementation_mode(self):
        # sqlite3's context manager commits but does not close the connection;
        # tolerate delayed handle release on Windows test cleanup.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            broker_home = root / "broker"
            db_path = broker_home / "state.sqlite"
            with mock.patch.object(broker, "BROKER_DIR", broker_home), \
                 mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "CONFIG_PATH", broker_home / "config.json"):
                result = broker.queue_claude_request(
                    str(root),
                    "implement the approved patch",
                    target_model="sonnet",
                    cli_model="sonnet",
                    autorun=False,
                    mode="acceptEdits",
                )
                with sqlite3.connect(db_path) as conn:
                    stored = conn.execute(
                        "SELECT mode FROM claude_requests WHERE id = ?", (result["id"],)
                    ).fetchone()[0]
            self.assertEqual(result["mode"], "acceptEdits")
            self.assertEqual(stored, "acceptEdits")

    def test_worker_reuses_persisted_implementation_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            broker_home = root / "broker"
            db_path = broker_home / "state.sqlite"
            with mock.patch.object(broker, "BROKER_DIR", broker_home), \
                 mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "CONFIG_PATH", broker_home / "config.json"):
                queued = broker.queue_claude_request(
                    str(root),
                    "implement the approved patch",
                    target_model="sonnet",
                    cli_model="sonnet",
                    autorun=False,
                    mode="acceptEdits",
                )
                response = broker.ClaudeConsultResult(
                    response="implemented",
                    requested_model="sonnet",
                    actual_model="claude-sonnet-5",
                    model_attested=True,
                    initial_model="sonnet",
                    attempted_models=("sonnet",),
                )
                with mock.patch.object(broker, "consult_claude", return_value=response) as consult, \
                     mock.patch.object(broker, "store_consultation"), \
                     mock.patch.object(broker, "record_agent_event"), \
                     mock.patch.object(broker, "render_request_ledger"):
                    result = broker.run_claude_request_worker(queued["id"])
            self.assertEqual(consult.call_args.args[2], "acceptEdits")
            self.assertEqual(result["mode"], "acceptEdits")
            self.assertEqual(result["actual_model"], "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
