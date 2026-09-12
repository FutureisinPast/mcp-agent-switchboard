"""Focused standard-library tests for cost-aware routing (Package D).

Covers: Claude stream parsing/model attestation, Codex stdout parsing across two
observed CLI versions, turn_context model/effort extraction, discover_codex /
resolve_codex_path resolution order, and the required routing contract strings.

Uses only unittest/tempfile/unittest.mock. No real home/config/DB is touched:
every filesystem lookup that would otherwise hit Path.home() or the real broker
home is redirected to a TemporaryDirectory for the duration of each test.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import agent_broker_entry  # noqa: E402
import routing_gate  # noqa: E402
import setup as broker_setup  # noqa: E402
from switchboard_version import BROKER_VERSION  # noqa: E402


class ClaudeStreamParserTests(unittest.TestCase):
    def test_ignores_subagent_model_uses_main_thread_message_model(self):
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "parent_tool_use_id": "sub-1",
                        "message": {"model": "claude-haiku-4-5-20251001", "content": [{"text": "sub work"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"model": "claude-sonnet-5", "content": [{"text": "main answer"}]},
                    }
                ),
                json.dumps({"type": "result", "result": "final response", "modelUsage": {"claude-opus-4-8": {}}}),
            ]
        )
        parsed = broker.parse_claude_stream_output(stdout)
        self.assertEqual(parsed.actual_model, "claude-sonnet-5")
        self.assertEqual(parsed.response, "final response")

    def test_ignores_result_model_usage_entirely(self):
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"model": "claude-sonnet-5", "content": [{"text": "hi"}]},
                    }
                ),
                json.dumps({"type": "result", "result": "ok", "modelUsage": {"claude-opus-4-8": {"tokens": 999}}}),
            ]
        )
        parsed = broker.parse_claude_stream_output(stdout)
        self.assertEqual(parsed.actual_model, "claude-sonnet-5")

    def test_family_alias_matches_dated_concrete_id(self):
        self.assertTrue(broker.claude_model_attested("sonnet", "claude-sonnet-5"))
        self.assertTrue(broker.claude_model_attested("haiku", "claude-haiku-4-5-20251001"))

    def test_wrong_concrete_or_different_dated_id_fails(self):
        self.assertFalse(broker.claude_model_attested("claude-sonnet-5", "claude-sonnet-4-20250514"))
        self.assertFalse(broker.claude_model_attested("haiku", "claude-sonnet-5"))


CODEX_0146_STDOUT = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "aaaaaaaa-0146-4a4a-8a8a-aaaaaaaaaaaa"}),
        json.dumps(
            {
                "type": "token_count",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 30,
                    "cache_write_input_tokens": 12,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 15,
                },
            }
        ),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hello from 0.146"}}),
    ]
)

CODEX_0144_STDOUT = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "bbbbbbbb-0144-4b4b-8b8b-bbbbbbbbbbbb"}),
        json.dumps(
            {
                "type": "token_count",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 35,
                },
            }
        ),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hello from 0.144"}}),
    ]
)


class CodexStreamParserTests(unittest.TestCase):
    def test_parses_0146_stream_with_extra_usage_fields(self):
        parsed = broker.parse_codex_stream_output(CODEX_0146_STDOUT)
        self.assertEqual(parsed.thread_id, "aaaaaaaa-0146-4a4a-8a8a-aaaaaaaaaaaa")
        self.assertEqual(parsed.response, "hello from 0.146")

    def test_parses_0144_stream_without_cache_write_or_reasoning_tokens(self):
        payload = json.loads(CODEX_0144_STDOUT.splitlines()[1])
        self.assertNotIn("cache_write_input_tokens", payload["usage"])
        self.assertNotIn("reasoning_output_tokens", payload["usage"])
        parsed = broker.parse_codex_stream_output(CODEX_0144_STDOUT)
        self.assertEqual(parsed.thread_id, "bbbbbbbb-0144-4b4b-8b8b-bbbbbbbbbbbb")
        self.assertEqual(parsed.response, "hello from 0.144")


class TurnContextExtractionTests(unittest.TestCase):
    def _write_rollout(self, tmpdir: str, lines: list[dict]) -> Path:
        path = Path(tmpdir) / "rollout-test.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        return path

    def test_extracts_model_and_effort_from_first_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_rollout(
                tmpdir,
                [
                    {"type": "session_meta", "payload": {"id": "1"}},
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "medium"}},
                    {"type": "response_item", "payload": {"content": "irrelevant"}},
                ],
            )
            model, effort = broker._codex_turn_context_model_effort(path)
            self.assertEqual(model, "gpt-5.6-terra")
            self.assertEqual(effort, "medium")

    def test_extracts_model_and_effort_from_second_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_rollout(
                tmpdir,
                [
                    {"type": "turn_context", "payload": {"model": "gpt-5.6", "effort": "high"}},
                ],
            )
            model, effort = broker._codex_turn_context_model_effort(path)
            self.assertEqual(model, "gpt-5.6")
            self.assertEqual(effort, "high")

    def test_codex_exact_and_alias_matching(self):
        self.assertTrue(broker.codex_model_attested("gpt-5.6-terra", "gpt-5.6-terra"))
        self.assertTrue(broker.codex_model_attested("terra", "gpt-5.6-terra"))
        self.assertTrue(broker.codex_model_attested("sol", "gpt-5.6-sol"))
        self.assertTrue(broker.codex_model_attested("luna", "gpt-5.6-luna"))

    def test_codex_missing_mismatch_fails(self):
        self.assertFalse(broker.codex_model_attested("gpt-5.6-terra", "gpt-5.6-sol"))
        self.assertFalse(broker.codex_model_attested("terra", None))
        self.assertFalse(broker.codex_model_attested("terra", ""))


class DiscoverCodexOrderTests(unittest.TestCase):
    def test_valid_configured_path_wins_over_marker_and_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            configured = tmp / "configured-codex.exe"
            configured.write_text("stub", encoding="utf-8")

            codex_dir = tmp / "home" / ".codex"
            codex_dir.mkdir(parents=True)
            marker_target = tmp / "marker-codex.exe"
            marker_target.write_text("stub", encoding="utf-8")
            (codex_dir / "config.toml").write_text(
                f'CODEX_CLI_PATH = "{marker_target}"', encoding="utf-8"
            )

            with mock.patch.object(Path, "home", return_value=tmp / "home"), \
                 mock.patch.object(broker.shutil, "which", return_value=str(tmp / "path-codex.exe")):
                result = broker.discover_codex({"codex_path": str(configured)})
            self.assertEqual(result, str(configured))

    def test_consult_codex_selects_cli_for_the_requested_model(self):
        config = {"codex_path": "configured-codex.exe"}
        with mock.patch.object(broker, "load_config", return_value=config), \
             mock.patch.object(broker, "discover_codex", return_value=None) as discover:
            result = broker.consult_codex(None, "bounded prompt", model_name="gpt-6-astra")
        discover.assert_called_once_with(config, required_model="gpt-6-astra")
        self.assertIn("Codex CLI was not found", result.response)

    def test_marker_wins_over_mocked_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            codex_dir = tmp / "home" / ".codex"
            codex_dir.mkdir(parents=True)
            marker_target = tmp / "marker-codex.exe"
            marker_target.write_text("stub", encoding="utf-8")
            (codex_dir / "config.toml").write_text(
                f'CODEX_CLI_PATH = "{marker_target}"', encoding="utf-8"
            )

            with mock.patch.object(Path, "home", return_value=tmp / "home"), \
                 mock.patch.object(broker.shutil, "which", return_value=str(tmp / "path-codex.exe")), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CODEX_PATH", None)
                result = broker.discover_codex({})
            self.assertEqual(result, str(marker_target))

    def test_falls_back_to_mocked_path_when_no_configured_or_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            home = tmp / "home"
            home.mkdir()
            path_codex = tmp / "path-codex.exe"
            path_codex.write_text("stub", encoding="utf-8")

            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(broker.shutil, "which", return_value=str(path_codex)), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CODEX_PATH", None)
                result = broker.discover_codex({})
            self.assertEqual(result, str(path_codex))

    def test_requested_model_selects_capable_path_cli_over_older_configured_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            home = tmp / "home"
            home.mkdir()
            configured = tmp / "old-codex.exe"
            path_codex = tmp / "new-codex.exe"
            configured.write_text("stub", encoding="utf-8")
            path_codex.write_text("stub", encoding="utf-8")

            def models(command, **_kwargs):
                slug = "gpt-5.6-sol" if command[0] == str(configured) else "gpt-6-astra"
                return {"models": [{"slug": slug}]}

            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(broker.shutil, "which", return_value=str(path_codex)), \
                 mock.patch.object(broker, "run_json_command", side_effect=models) as debug_models, \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp / "local")}, clear=False):
                os.environ.pop("CODEX_PATH", None)
                result = broker.discover_codex(
                    {"codex_path": str(configured)}, required_model="gpt-6-astra"
                )
            self.assertEqual(result, str(path_codex))
            self.assertEqual(
                [call.args[0][0] for call in debug_models.call_args_list],
                [str(configured), str(path_codex)],
            )

    def test_requested_model_falls_back_deterministically_when_none_advertise_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            home = tmp / "home"
            home.mkdir()
            configured = tmp / "configured-codex.exe"
            path_codex = tmp / "path-codex.exe"
            configured.write_text("stub", encoding="utf-8")
            path_codex.write_text("stub", encoding="utf-8")
            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(broker.shutil, "which", return_value=str(path_codex)), \
                 mock.patch.object(
                     broker,
                     "run_json_command",
                     return_value={"models": [{"slug": "gpt-5.6-sol"}]},
                 ), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp / "local")}, clear=False):
                os.environ.pop("CODEX_PATH", None)
                result = broker.discover_codex(
                    {"codex_path": str(configured)}, required_model="gpt-6-astra"
                )
            self.assertEqual(result, str(configured))


class ResolveCodexPathTests(unittest.TestCase):
    def test_marker_wins_over_mocked_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            marker_target = tmp / "marker-codex.exe"
            marker_target.write_text("stub", encoding="utf-8")
            toml_path = tmp / "config.toml"
            toml_path.write_text(f'CODEX_CLI_PATH = "{marker_target}"', encoding="utf-8")

            with mock.patch.object(broker_setup, "CODEX_TOML", toml_path), \
                 mock.patch.object(broker_setup.shutil, "which", return_value=str(tmp / "path-codex.exe")):
                result = broker_setup.resolve_codex_path()
            self.assertEqual(result, str(marker_target))

    def test_stale_marker_falls_back_to_mocked_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stale_target = tmp / "does-not-exist-codex.exe"
            toml_path = tmp / "config.toml"
            toml_path.write_text(f'CODEX_CLI_PATH = "{stale_target}"', encoding="utf-8")
            path_codex = tmp / "path-codex.exe"
            path_codex.write_text("stub", encoding="utf-8")

            with mock.patch.object(broker_setup, "CODEX_TOML", toml_path), \
                 mock.patch.object(broker_setup.shutil, "which", return_value=str(path_codex)):
                result = broker_setup.resolve_codex_path()
            self.assertEqual(result, str(path_codex))

    def test_no_marker_file_falls_back_to_mocked_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            toml_path = tmp / ".codex-missing" / "config.toml"
            path_codex = tmp / "path-codex.exe"
            path_codex.write_text("stub", encoding="utf-8")

            with mock.patch.object(broker_setup, "CODEX_TOML", toml_path), \
                 mock.patch.object(broker_setup.shutil, "which", return_value=str(path_codex)):
                result = broker_setup.resolve_codex_path()
            self.assertEqual(result, str(path_codex))


class RoutingContractStringsTests(unittest.TestCase):
    def test_implementation_plan_contract_has_portable_lane_fields(self):
        contract = broker.TASK_CONTRACTS["implementation_plan"]
        matches = [line for line in contract if "Lane |" in line]
        self.assertTrue(matches, "expected a portable Lane | ... work-package line")
        route_line = matches[0]
        for field in ("Lane", "mechanism", "model/effort", "deliverable", "verification", "escalation"):
            self.assertIn(field, route_line)

    def test_ascii_override_marker_present_and_ascii_only(self):
        implementation_contract = broker.TASK_CONTRACTS["implementation"]
        matches = [line for line in implementation_contract if "override: brain" in line]
        self.assertTrue(matches, "expected an ASCII override marker line in the implementation contract")
        for line in matches:
            line.encode("ascii")

        cost_aware_matches = [line for line in broker.COST_AWARE_ROUTING_RULES if "override: brain" in line]
        self.assertTrue(cost_aware_matches)
        for line in cost_aware_matches:
            line.encode("ascii")

    def test_mixed_native_and_broker_receipt_audit_required(self):
        # The old COST_AWARE_ROUTING_RULES prose rule combining these three markers
        # is gone: the model is no longer asked to recite an audit by default, so
        # there is nothing to teach it to write in the common case. The guarantee
        # survives where enforcement actually lives now -- the require-mode block
        # message in routing_gate.stop(), which still names all three valid receipt
        # forms for anyone using AGENT_BROKER_AUDIT_MODE=require.
        source = inspect.getsource(routing_gate.stop)
        self.assertIn("native:<agent-id>", source)
        self.assertIn("broker:<uuid>", source)
        self.assertIn("override: brain - <WP-ID>", source)

    def test_plan_contract_defines_reader_located_decision_premise(self):
        text = " ".join(broker.TASK_CONTRACTS["implementation_plan"]).lower()
        self.assertIn("decision premise", text)
        self.assertIn("reader to locate minimal primary evidence", text)
        self.assertIn("adjudication for the brain", text)

    def test_implementation_contract_caps_brain_context_ingress(self):
        text = " ".join(broker.TASK_CONTRACTS["implementation"]).lower()
        self.assertIn("field projection and output cap", text)
        self.assertIn("8,000 characters", text)
        self.assertIn("raw evidence external", text)

    def test_global_rules_cover_premises_and_unplanned_direct_labour(self):
        # The model-recited "planned and unplanned packages ... direct-brain-labour:"
        # audit rule is gone by design (audit_mode() defaults to on-demand). Unplanned
        # direct labour is still counted and reported -- now by the ledger rather than
        # recited by the model, since PreToolUse/PostToolUse see every call regardless
        # of whether it was declared. Assert the replacement guarantee instead.
        text = " ".join(broker.COST_AWARE_ROUTING_RULES).lower()
        self.assertIn("brain-context ingress", text)
        self.assertIn("decision premise", text)
        self.assertIn("do not write a routing audit", text)
        self.assertIn("every lane automatically", text)
        self.assertIn("routing-report --table", text)
        self.assertIn("agent_broker_audit_mode=require", text)

    def test_global_rules_never_promote_flash_to_peer_brain(self):
        text = " ".join(broker.COST_AWARE_ROUTING_RULES).lower()
        self.assertIn("capability tier outranks model version", text)
        self.assertIn("newer gemini flash remains a non-authoritative labour workhorse", text)
        self.assertIn("never becomes an astra/fable decision consultant", text)
        self.assertIn("consult_decision", text)
        self.assertIn("codex uses native astra and claude uses native fable", text)
        self.assertIn("architecture/critical work adds only the opposite-vendor adviser", text)
        self.assertIn("must never replace this with a nested same-vendor cli call", text)
        self.assertIn("never substitute flash for flagship judgment", text)

    def test_global_rules_define_proactive_external_flash_workhorse_lane(self):
        text = " ".join(broker.COST_AWARE_ROUTING_RULES).lower()
        self.assertIn(
            "default workhorse = the newest live antigravity gemini flash high", text
        )
        self.assertIn("never a native child agent", text)
        self.assertIn("standing request to delegate eligible labour", text)
        self.assertIn("pre-authorized work", text)
        for task in ("search", "reading", "extraction", "summaries", "drafting"):
            self.assertIn(task, text)
        self.assertIn("light implementation and tests from an approved plan", text)
        self.assertIn("flash is the default with objective exceptions", text)
        self.assertIn("flash_skip", text)
        self.assertIn("every flash call is exactly one bounded work package", text)
        self.assertIn("at most five allowed files", text)
        self.assertIn("--output-format json with --json-schema", text)
        self.assertIn("must call mcp route_agent_task", text)
        self.assertIn("must not shell out to agy", text)
        self.assertIn("only the switchboard backend may start agy", text)
        self.assertIn("never the brain or router", text)
        self.assertIn("entire autonomous plan", text)
        self.assertIn("flash never receives production ssh", text)
        self.assertIn("independently inspects cited lines, the actual diff, and check output", text)
        for failure in ("missing", "quota-limited", "times out", "mismatches", "blocked", "rejected/failed structured output"):
            self.assertIn(failure, text)
        self.assertIn("codex explorer/worker", text)
        self.assertIn("claude explore/economy-worker", text)
        self.assertIn("record the concrete package-specific flash_skip reason", text)
        self.assertIn("concurrently only on independent stages/packages", text)
        self.assertIn("writes run serially unless", text)
        self.assertIn("brain reviews evidence and actual diffs", text)
        self.assertNotIn(
            "agent switchboard is only for opposite-vendor consultation",
            text,
        )

    def test_runtime_rules_exclude_gemini_from_automatic_downward_routing(self):
        text = " ".join(broker.COST_AWARE_ROUTING_RULES).lower()
        self.assertIn("for codex and claude brains, default workhorse", text)
        self.assertIn("gemini, antigravity, and unknown hosts receive no automatic downward cost routing", text)
        self.assertIn("gemini upward flagship consultation", text)

    def test_contracts_require_bounded_pretooluse_relief_and_return_cap(self):
        implementation = " ".join(broker.TASK_CONTRACTS["implementation"]).lower()
        global_rules = " ".join(broker.COST_AWARE_ROUTING_RULES).lower()
        for text in (implementation, global_rules):
            self.assertIn("pretooluse", text)
            self.assertIn("next bounded block", text)
            self.assertIn("routing-override", text)
        # The canonical allowance now lives in COST_AWARE_ROUTING_RULES as "default
        # four" (routing_gate.DIRECT_LABOUR_LIMIT_DEFAULT), not the old "ten direct".
        self.assertIn("default four", global_rules)
        self.assertIn("registered overrides must appear in the final audit", global_rules)


class NativeLabourPolicyTests(unittest.TestCase):
    def test_search_task_kind_selects_reader_without_prompt_guessing(self):
        self.assertEqual(
            broker.native_semantic_lane({"task_kind": "search", "prompt": "implement everything"}),
            "reader",
        )

    def test_routing_guide_is_structurally_flash_first_for_codex_claude_only(self):
        codex_roles = {
            "frontier": {"id": "gpt-live-astra"},
            "workhorse": {"id": "gpt-live-terra"},
            "reader": {"id": "gpt-live-luna"},
        }
        catalog = {
            "catalogs": {
                "codex": {"roles": codex_roles},
                "antigravity": {"roles": {"workhorse": {"id": "gemini-live-flash-high"}}},
            },
            "defaults": [],
        }
        with mock.patch.object(broker, "list_agent_models", return_value=catalog):
            guide = broker.get_model_routing_guide()
        policy = guide["automatic_downward_cost_routing"]
        self.assertEqual(policy["eligible_host_families"], ["codex", "claude"])
        self.assertEqual(policy["excluded_host_families"], ["gemini", "antigravity", "unknown"])
        self.assertIn("Flash-first", policy["default"])
        self.assertEqual(guide["native_semantic_lanes"]["reader"]["codex"]["model"], "gpt-live-luna")
        self.assertEqual(guide["native_semantic_lanes"]["workhorse"]["codex"]["model"], "gpt-live-terra")
        fallback = guide["defaults"]["antigravity_cli"]["failure_fallback"]
        self.assertIsNone(fallback["gemini"])
        self.assertFalse(fallback["auto_launch_native"])

    def test_explicit_semantic_aliases_use_live_native_roles_without_prompt_guessing(self):
        with mock.patch.object(broker, "current_codex_role_model", side_effect=lambda role: f"live-{role}"):
            self.assertEqual(
                broker.apply_codex_model_policy({"semantic_lane": "reader"}, "implement everything", "implementation", "", None),
                ("live-reader", "low", "cheap_read"),
            )
            self.assertEqual(
                broker.apply_codex_model_policy({"model_policy": "worker"}, "read one file", "quick_check", "", None),
                ("live-workhorse", "medium", "balanced"),
            )
        with mock.patch.object(
            broker.model_roles, "select_claude_roles", return_value={"frontier": ["best"], "reader": "live-haiku", "workhorse": "live-sonnet"}
        ):
            self.assertEqual(
                broker.apply_claude_model_policy({"native_lane": "Explore"}, "", "max"),
                ("live-haiku", None, "cheap_read"),
            )
            self.assertEqual(
                broker.apply_claude_model_policy({"semantic_lane": "economy-worker"}, "", None),
                ("live-sonnet", "medium", "balanced"),
            )

    def test_noncreditable_flash_outcomes_return_broker_backed_native_handoffs(self):
        cases = {
            "unavailable_pre_mutation": "flash-unavailable:broker:receipt-1",
            "rejected": "flash-failed:broker:receipt-1",
            "blocked": "flash-failed:broker:receipt-1",
            "failed_pre_mutation": "flash-failed:broker:receipt-1",
        }
        for family, client, role, model in (
            ("codex", "codex-vscode", "explorer", "live-reader"),
            ("claude", "claude-code", "Explore", "live-haiku"),
        ):
            for outcome, reason in cases.items():
                with self.subTest(family=family, outcome=outcome), \
                     mock.patch.object(broker, "_MCP_CLIENT_NAME", client), \
                     mock.patch.object(broker, "current_codex_role_model", return_value="live-reader"), \
                     mock.patch.object(broker.model_roles, "select_claude_roles", return_value={"reader": "live-haiku", "workhorse": "live-sonnet"}):
                    handoff = broker.native_handoff_for_flash_outcome(
                        {"work_package_id": "WP-READ", "task_kind": "research"}, outcome, "broker:receipt-1"
                    )
                self.assertEqual(handoff["work_package_id"], "WP-READ")
                self.assertEqual(handoff["semantic_lane"], "reader")
                self.assertEqual(handoff["native"]["family"], family)
                self.assertEqual(handoff["native"]["role"], role)
                self.assertEqual(handoff["native"]["model"], model)
                self.assertEqual(handoff["flash_skip_reason"], reason)
                self.assertFalse(handoff["auto_launch"])
                self.assertIn(reason, handoff["record_requirement"])
                if outcome == "unavailable_pre_mutation":
                    self.assertNotIn("brain_review", handoff)
                else:
                    self.assertTrue(handoff["brain_review"]["required"])

    def test_gemini_antigravity_and_unknown_callers_get_no_native_handoff(self):
        for client in ("antigravity-ide", "gemini-cli", "third-party-client", ""):
            with self.subTest(client=client), mock.patch.object(broker, "_MCP_CLIENT_NAME", client), mock.patch.dict(
                os.environ, {"AGENT_BROKER_CALLER": ""}, clear=False
            ):
                self.assertIsNone(
                    broker.native_handoff_for_flash_outcome(
                        {"work_package_id": "WP-1", "task_kind": "implementation"}, "rejected", "broker:r"
                    )
                )


class AsyncDecisionReconciliationTests(unittest.TestCase):
    """A detached flagship result must close its immutable parent event exactly once."""

    def setUp(self):
        # SQLite WAL handles can remain briefly open on Windows after a context
        # manager returns.  TemporaryDirectory's supported cleanup tolerance
        # keeps this hermetic test from turning that platform quirk into a false
        # product failure.
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmpdir.name)
        self.paths = {
            "DB_PATH": root / "broker.db",
            "BROKER_DIR": root / "broker",
            "LOG_PATH": root / "broker" / "broker.log",
        }
        self.stack = contextlib.ExitStack()
        for name, value in self.paths.items():
            self.stack.enter_context(mock.patch.object(broker, name, value))
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        broker.init_db()

    def tearDown(self):
        broker._FLAGSHIP_AVAILABILITY_LATCHES.clear()
        self.stack.close()
        self.tmpdir.cleanup()

    @staticmethod
    def _brief():
        return {
            "decision": "Choose the compatibility-safe option.",
            "constraints": ["Preserve the public API."],
            "options": [{"id": "A", "summary": "Incremental linked event."}],
            "questions": ["Is the event link sufficient?"],
            "evidence": [{"ref": "src/router.py:1", "claim": "The parent is append-only."}],
        }

    def _pending_parent(self, request_id="async-claude"):
        args = {
            "project": "project-a",
            "topic": "async-decision",
            "session_id": "session-async-1",
            "work_package_id": "WP-ASYNC",
            "host": {"vendor": "codex", "model": "gpt-6-astra"},
            "complexity": "architecture",
            "brief": self._brief(),
        }
        with mock.patch.object(
            broker, "consult", return_value={"status": "pending", "request_id": request_id, "async": True}
        ):
            result = broker.consult_decision(args)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["decision_session_key"], "session-async-1")
        self.assertIsNotNone(result["ledger_ref"])
        return result

    def _insert_claude_result(self, request_id, status, response=None, error=None):
        with broker.db_connect() as conn:
            conn.execute(
                """
                INSERT INTO claude_requests (
                    id, project, root_path, topic, prompt, status, response, error,
                    created_by, created_at, completed_at, responder, responder_model, target_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id, "project-a", str(Path.cwd()), "async-decision", "decision brief",
                    status, response, error, "agent-switchboard", broker.utc_now(), broker.utc_now(),
                    "claude-cli-worker" if response else None,
                    "claude:fable" if response else None, "fable",
                ),
            )

    def _terminal_events(self):
        with broker.db_connect() as conn:
            return conn.execute(
                "SELECT details FROM agent_events WHERE event_type = 'flagship_consultation_terminal'"
            ).fetchall()

    def test_pending_403_is_latched_and_reconciled_once(self):
        parent = self._pending_parent()
        self._insert_claude_result(
            "async-claude", "error", error="HTTP 403: organization subscription access disabled"
        )
        first = broker.request_result("async-claude")
        update = first["decision_update"]
        self.assertEqual(update["parent_ledger_ref"], parent["ledger_ref"])
        self.assertEqual(update["updated_target_status"], "skipped_unavailable")
        self.assertEqual(update["terminal_overall_status"], "unavailable")
        self.assertTrue(update["handoff_notices"])
        self.assertIn(("session-async-1", "claude"), broker._FLAGSHIP_AVAILABILITY_LATCHES)
        again = broker.request_result("async-claude")
        self.assertTrue(again["decision_update"]["already_recorded"])
        self.assertEqual(len(self._terminal_events()), 1)

    def test_pending_completed_reconciles_to_complete_with_parent_linkage(self):
        parent = self._pending_parent("async-completed")
        self._insert_claude_result("async-completed", "completed", response="Use the linked event.")
        result = broker.request_result("async-completed")
        update = result["decision_update"]
        self.assertEqual(update["parent_ledger_ref"], parent["ledger_ref"])
        self.assertEqual(update["updated_target_status"], "completed")
        self.assertEqual(update["terminal_overall_status"], "complete")
        details = json.loads(self._terminal_events()[0][0])
        self.assertEqual(details["decision_session_key"], "session-async-1")
        self.assertEqual(details["request_id"], "async-completed")

    def test_two_async_targets_roll_prior_terminal_state_into_final_partial(self):
        args = {
            "project": "project-a", "topic": "async-decision", "session_id": "session-async-2",
            "work_package_id": "WP-GEMINI-ASYNC",
            "host": {"vendor": "gemini", "model": "gemini-3.8-flash-high"},
            "complexity": "architecture", "brief": self._brief(),
        }
        with mock.patch.object(
            broker,
            "consult",
            side_effect=[
                {"status": "pending", "request_id": "async-codex", "async": True},
                {"status": "pending", "request_id": "async-fable", "async": True},
            ],
        ):
            parent = broker.consult_decision(args)
        self.assertEqual(parent["status"], "pending")

        self._insert_claude_result("async-codex", "completed", response="Codex advice.")
        first = broker.request_result("async-codex")["decision_update"]
        self.assertEqual(first["terminal_overall_status"], "pending")

        self._insert_claude_result("async-fable", "error", error="HTTP 403: subscription disabled")
        second = broker.request_result("async-fable")["decision_update"]
        self.assertEqual(second["terminal_overall_status"], "partial")
        self.assertEqual(second["parent_ledger_ref"], parent["ledger_ref"])
        self.assertEqual(len(self._terminal_events()), 2)


class EntryVersionTests(unittest.TestCase):
    def test_all_version_aliases_print_shared_release_version(self):
        for alias in ("--version", "version", "-v"):
            with self.subTest(alias=alias), mock.patch.object(
                sys, "argv", ["agent-switchboard.exe", alias]
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = agent_broker_entry.run()
                self.assertEqual(result, 0)
                self.assertEqual(stdout.getvalue().strip(), f"Agent Switchboard {BROKER_VERSION}")

    def test_routing_override_entry_forwards_arguments(self):
        argv = [
            "agent-switchboard.exe",
            "routing-override",
            "--session",
            "session-1",
            "--package",
            "WP2",
            "--reason",
            "architecture boundary requires brain review",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            routing_gate, "routing_override_cli", return_value=0
        ) as override:
            self.assertEqual(agent_broker_entry.run(), 0)
        override.assert_called_once_with(argv[2:])

    def test_routing_override_cli_rejects_invalid_package_and_short_reason(self):
        cases = (
            ["--session", "session-1", "--package", "bad-package", "--reason", "long enough reason"],
            ["--session", "session-1", "--package", "WP2", "--reason", "short"],
        )
        for argv in cases:
            with self.subTest(argv=argv), mock.patch.object(
                routing_gate, "register_brain_override", return_value=False
            ), mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(routing_gate.routing_override_cli(argv), 2)


class NativeFirstBrokerGuardTests(unittest.TestCase):
    def test_direct_same_vendor_codex_queue_is_rejected_before_enqueue(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), mock.patch.object(
            broker, "queue_codex_request"
        ) as enqueue:
            with self.assertRaisesRegex(ValueError, "native subagents first"):
                broker.handle_tool("queue_codex_request", {"prompt": "routine implementation"})
        enqueue.assert_not_called()

    def test_direct_same_vendor_claude_queue_allows_concrete_native_failure(self):
        args = {
            "prompt": "routine implementation",
            "native_unavailable_reason": "economy-worker failed to start twice",
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "claude-code"), mock.patch.object(
            broker, "queue_claude_request", return_value={"queued": True}
        ) as enqueue:
            broker.handle_tool("queue_claude_request", args)
        enqueue.assert_called_once()

    def test_cross_vendor_queue_does_not_require_native_failure(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "claude-code"), mock.patch.object(
            broker, "queue_codex_request", return_value={"queued": True}
        ) as enqueue:
            broker.handle_tool("queue_codex_request", {"prompt": "frontier consult"})
        enqueue.assert_called_once()

    def test_route_agent_task_cannot_bypass_same_vendor_guard(self):
        resolved = {
            "status": "resolved",
            "target_agent": "codex_cli",
            "target_model": "gpt-5.6-terra",
            "effort": "medium",
            "source": "explicit_request",
        }
        args = {
            "prompt": "implement the approved mechanical package",
            "target_agent": "codex",
            "target_model": "gpt-5.6-terra",
            "model_policy": "balanced",
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), mock.patch.object(
            broker, "resolve_model_request", return_value=resolved
        ):
            with self.assertRaisesRegex(ValueError, "native subagents first"):
                broker.route_agent_task(args)


class AntigravitySelectionFailClosedTests(unittest.TestCase):
    """The in-app Antigravity surface drives the IDE model chooser over CDP.

    cdp_select_antigravity_model is best-effort and never raises: it reports
    ok=False on every failure, including "target model click did not verify as
    selected". The caller used to queue the prompt regardless, so a failed
    selection silently ran whatever model the panel had selected -- observed in
    the field as Gemini Flash quietly becoming Claude Sonnet at ~10x the cost,
    with nothing logged and nothing attested. Route must fail closed instead.
    """

    def _route(self, selection):
        resolved = {
            "status": "resolved",
            "target_agent": "antigravity",
            "target_model": "Gemini 3.7 Flash (High)",
            "effort": "high",
            "source": "explicit_request",
        }
        args = {
            "prompt": "bounded read package",
            "target_agent": "antigravity",
            "surface": "extension",
            "target_model": "Gemini 3.7 Flash (High)",
            "task_kind": "quick_check",
        }
        queue = mock.MagicMock(return_value={"queued": True, "request_id": "x"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "claude-code"),              mock.patch.object(broker, "resolve_model_request", return_value=resolved),              mock.patch.object(broker, "load_config", return_value={"antigravity_cdp_autoselect": True}),              mock.patch.object(broker, "cdp_select_antigravity_model", return_value=selection),              mock.patch.object(broker, "launch_ide_host", return_value={"ok": True}),              mock.patch.object(broker, "queue_antigravity_request", queue):
            result = broker.route_agent_task(args)
        return result, queue

    def test_unverified_selection_returns_instead_of_queueing(self):
        result, queue = self._route(
            {"ok": False, "reason": "target model click did not verify as selected",
             "current": "Claude Sonnet 4.6", "model": "Gemini 3.7 Flash (High)"}
        )
        self.assertEqual(result.get("status"), "needs_model_selection")
        self.assertIn("did not verify", str(result.get("reason", "")))
        queue.assert_not_called()

    def test_ok_without_verified_still_fails_closed(self):
        result, queue = self._route({"ok": True, "model": "Gemini 3.7 Flash (High)"})
        self.assertEqual(result.get("status"), "needs_model_selection")
        queue.assert_not_called()

    def test_verified_selection_still_dispatches(self):
        result, queue = self._route(
            {"ok": True, "verified": True, "current": "Gemini 3.7 Flash (High)"}
        )
        self.assertNotEqual(result.get("status"), "needs_model_selection")
        queue.assert_called_once()


class AntigravityCdpListModelsOptInTests(unittest.TestCase):
    """Model discovery must not touch the running IDE unless explicitly asked.

    cdp_list_models.mjs enumerates the Antigravity model picker by CLICKING it
    open over CDP (Input.dispatchMouseEvent). discover_antigravity_models runs
    from resolve_model_request, which executes BEFORE the surface branch -- so a
    surface="cli" dispatch, which never uses the IDE, was still popping the model
    chooser into the user's face on every cache miss. It is now opt-in.
    """

    def _discover(self, config):
        # The production gate is four conditions: config flag AND node AND
        # helper.exists() AND local_port_open(port). Besides the config flag
        # and node (mocked below), the gate also depends on two real
        # environment facts -- a helper .mjs file on disk and a live TCP
        # check against the IDE debug port. Both are made deterministic here
        # (a real file under a throwaway BROKER_DIR, and a patched port
        # check) so this test does not depend on whether the IDE happens to
        # be running with that port open.
        with tempfile.TemporaryDirectory() as tmp_home:
            tmp_broker_dir = Path(tmp_home)
            helper_dir = tmp_broker_dir / "extensions" / "antigravity-agent-broker-bridge"
            helper_dir.mkdir(parents=True, exist_ok=True)
            (helper_dir / "cdp_list_models.mjs").write_text("// test stub\n", encoding="utf-8")

            patches = [
                mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE", None),
                mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE_AT", 0.0),
                mock.patch.object(broker, "load_config", return_value=config),
                mock.patch.object(broker, "discover_antigravity_cli", return_value=None),
                mock.patch.object(broker, "local_port_open", return_value=True),
                mock.patch.object(broker, "BROKER_DIR", tmp_broker_dir),
            ]
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                shutil_mod = stack.enter_context(mock.patch.object(broker, "shutil"))
                shutil_mod.which.return_value = "/usr/bin/node"
                runner = stack.enter_context(
                    mock.patch.object(broker, "run_json_command",
                                      return_value={"models": ["Ghost Model"]})
                )
                broker.discover_antigravity_models()
        cdp_calls = [
            c for c in runner.call_args_list
            if any("cdp_list_models" in str(a) for a in (c.args[0] if c.args else []))
        ]
        return cdp_calls

    def test_cdp_listing_is_off_by_default(self):
        self.assertEqual(self._discover({}), [], "IDE must not be contacted by default")

    def test_cdp_listing_stays_off_when_explicitly_false(self):
        self.assertEqual(self._discover({"antigravity_cdp_list_models": False}), [])

    def test_cdp_listing_runs_when_opted_in(self):
        self.assertTrue(self._discover({"antigravity_cdp_list_models": True}))


if __name__ == "__main__":
    unittest.main()
