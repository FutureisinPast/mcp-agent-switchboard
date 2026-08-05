"""Focused standard-library tests for cost-aware routing (Package D).

Covers: Claude stream parsing/model attestation, Codex stdout parsing across two
observed CLI versions, turn_context model/effort extraction, discover_codex /
resolve_codex_path resolution order, and the required routing contract strings.

Uses only unittest/tempfile/unittest.mock. No real home/config/DB is touched:
every filesystem lookup that would otherwise hit Path.home() or the real broker
home is redirected to a TemporaryDirectory for the duration of each test.
"""
from __future__ import annotations

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
        matches = [
            line
            for line in broker.COST_AWARE_ROUTING_RULES
            if "native:<agent-id>" in line
            and "broker:<uuid>" in line
            and "structured per-package brain override" in line
        ]
        self.assertTrue(matches, "expected the mixed native/broker routing audit rule")

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
        text = " ".join(broker.COST_AWARE_ROUTING_RULES).lower()
        self.assertIn("brain-context ingress", text)
        self.assertIn("decision premise", text)
        self.assertIn("planned and unplanned packages", text)
        self.assertIn("direct-brain-labour:", text)


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


if __name__ == "__main__":
    unittest.main()
