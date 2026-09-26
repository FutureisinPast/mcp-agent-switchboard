"""Tests for WP-SB2: the progressive effort ladder and bounded payload caps for
direct frontier (codex/claude flagship) route_agent_task calls, the
needs_native_consultation adviser_instructions/max_prompt_bytes/size bound, and
the routing_gate.py flagship prompt cap on the Agent/Task delegation tools.

Standard-library only. Every filesystem lookup that would otherwise hit
Path.home() (``~/.claude.json``, ``~/.codex/auth.json``) is redirected to a
TemporaryDirectory; HOME and USERPROFILE are overridden alongside Path.home so
nothing here can ever touch the real signed-in account files.
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


class _HomeRedirectedTestCase(unittest.TestCase):
    """Every test here can read/write a flagship latch, which reads (never
    writes) ~/.claude.json / $CODEX_HOME/auth.json for the account-hash
    fingerprint. HOME/USERPROFILE and Path.home are redirected to an empty
    TemporaryDirectory so the real account files are never touched."""

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


def _resolved(target_agent: str, target_model: str, effort: str | None, **extra) -> dict:
    result = {
        "status": "resolved",
        "project": "p",
        "topic": "t",
        "model_family": "codex" if "codex" in target_agent else "claude",
        "target_agent": target_agent,
        "target_model": target_model,
        "effort": effort,
        "source": "family_flagship",
    }
    result.update(extra)
    return result


def _route_args(**overrides) -> dict:
    args = {
        "prompt": "Audit this bounded change and report concrete issues.",
        "target_agent": "codex",
        "surface": "cli",
        "task_kind": "co_audit",
    }
    args.update(overrides)
    return args


class RouteAgentTaskLadderTests(_HomeRedirectedTestCase):
    def _run(self, args, resolved, consult_return=None):
        consult_mock = mock.MagicMock(return_value=consult_return or {"status": "ok"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "resolve_model_request", return_value=resolved), \
             mock.patch.object(broker, "consult", consult_mock), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None):
            result = broker.route_agent_task(args)
        return result, consult_mock

    def test_no_complexity_defaults_to_xhigh(self):
        result, consult = self._run(
            _route_args(target_agent="codex", surface="cli"),
            _resolved("codex_cli", "gpt-6-astra", "max"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "xhigh")
        self.assertEqual(result["model_resolution"]["effort"], "xhigh")
        self.assertEqual(result["model_resolution"]["effort_source"], "ladder")
        self.assertEqual(result["model_resolution"]["complexity"], "architecture")

    def test_bounded_complexity_resolves_high(self):
        result, consult = self._run(
            _route_args(target_agent="codex", surface="cli", complexity="bounded"),
            _resolved("codex_cli", "gpt-6-astra", "max"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "high")
        self.assertEqual(result["model_resolution"]["effort_source"], "ladder")

    def test_critical_complexity_resolves_max(self):
        result, consult = self._run(
            _route_args(target_agent="codex", surface="cli", complexity="critical"),
            _resolved("codex_cli", "gpt-6-astra", "max"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "max")

    def test_risk_flag_forces_max_regardless_of_complexity(self):
        result, consult = self._run(
            _route_args(
                target_agent="codex", surface="cli", complexity="bounded",
                risk_flags=["migration"],
            ),
            _resolved("codex_cli", "gpt-6-astra", "max"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "max")
        self.assertEqual(result["model_resolution"]["complexity"], "critical")

    def test_invalid_complexity_raises(self):
        with self.assertRaisesRegex(ValueError, "complexity must be"):
            broker.route_agent_task(_route_args(complexity="urgent"))

    def test_too_many_risk_flags_raises(self):
        with self.assertRaisesRegex(ValueError, "at most 8"):
            broker.route_agent_task(
                _route_args(risk_flags=[f"flag{i}" for i in range(9)])
            )

    def test_explicit_effort_wins_over_ladder(self):
        result, consult = self._run(
            _route_args(target_agent="codex", surface="cli", effort="low"),
            _resolved("codex_cli", "gpt-6-astra", "low"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "low")
        self.assertNotIn("effort_source", result["model_resolution"])

    def test_model_policy_path_is_unaffected_by_ladder(self):
        # A resolved cheap/balanced tier is never a frontier model in real use
        # (apply_*_model_policy only matches when no target_model was given),
        # but the ladder's own gate must still respect an explicit model_policy
        # even if somehow paired with a frontier resolution -- this directly
        # exercises that "not model_policy" guard.
        resolved = _resolved("codex_cli", "gpt-6-astra", "medium", model_policy="balanced")
        result, consult = self._run(
            _route_args(target_agent="codex", surface="cli", model_policy="balanced"),
            resolved,
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "medium")
        self.assertNotIn("effort_source", result["model_resolution"])

    def test_non_frontier_target_is_unaffected(self):
        result, consult = self._run(
            _route_args(target_agent="codex", surface="cli", target_model="gpt-5.6-terra"),
            _resolved("codex_cli", "gpt-5.6-terra", "medium"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "medium")
        self.assertNotIn("effort_source", result["model_resolution"])

    def test_internal_decision_token_is_unaffected(self):
        result, consult = self._run(
            _route_args(
                target_agent="codex", surface="cli",
                _decision_internal_token=broker._DECISION_INTERNAL_TOKEN,
                effort="max",
            ),
            _resolved("codex_cli", "gpt-6-astra", "max"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "max")
        self.assertNotIn("effort_source", result["model_resolution"])

    def test_claude_frontier_ladder_defaults_xhigh(self):
        result, consult = self._run(
            _route_args(
                target_agent="claude", surface="cli", target_model="fable",
                task_kind="co_audit",
            ),
            _resolved("claude_code", "fable", "max"),
        )
        self.assertEqual(consult.call_args.args[1]["effort"], "xhigh")
        self.assertEqual(result["model_resolution"]["effort_source"], "ladder")


class RouteAgentTaskFlagshipFallbackTests(_HomeRedirectedTestCase):
    def _run(self, args, resolved, consult_return=None):
        consult_mock = mock.MagicMock(return_value=consult_return or {"status": "ok"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "resolve_model_request", return_value=resolved), \
             mock.patch.object(broker, "consult", consult_mock), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None):
            result = broker.route_agent_task(args)
        return result, consult_mock

    def test_latched_fable_uses_original_model_with_notice_only(self):
        # WP-SB7: the Claude flagship chain carries no same-vendor fallback any
        # more (Opus is never offered as a fallback), so a direct
        # route_agent_task call on a latched fable proceeds on fable unchanged
        # and only adds a handoff notice -- it never swaps to Opus, and it
        # never swaps cross-vendor either (that would also change
        # target_agent/CLI, which this preflight-only helper does not do).
        broker._set_flagship_latch(
            ("session-x", "claude", "fable"), "plan", "skipped_unavailable",
            "requires usage credits",
        )
        result, consult = self._run(
            _route_args(
                target_agent="claude", surface="cli", target_model="fable",
                task_kind="co_audit", session_id="session-x", effort="xhigh",
            ),
            _resolved("claude_code", "fable", "xhigh"),
        )
        self.assertEqual(consult.call_args.args[1]["target_model"], "fable")
        self.assertEqual(result["model_resolution"]["target_model"], "fable")
        self.assertNotIn("fallback_from", result["model_resolution"])
        notices = result["model_resolution"].get("notices") or []
        self.assertTrue(
            any("all fallback models are latched" in n.lower() and "fable" in n for n in notices),
            notices,
        )
        self.assertFalse(any("opus" in n.lower() for n in notices), notices)
        # Effort is preserved.
        self.assertEqual(consult.call_args.args[1]["effort"], "xhigh")

    def test_latched_codex_frontier_uses_original_model_with_notice_only(self):
        # WP-SB7: the Codex flagship chain is the live frontier role model
        # only -- CODEX_PREVIOUS_FRONTIER_MODEL (gpt-5.6-sol) is never offered
        # as a fallback any more, so a latched Astra proceeds on Astra
        # unchanged with only a handoff notice.
        broker._set_flagship_latch(
            ("session-y", "codex", broker.normalize_lookup("gpt-6-astra")), "quota",
            "skipped_quota", "429 too many requests",
        )
        result, consult = self._run(
            _route_args(
                target_agent="codex", surface="cli", target_model="gpt-6-astra",
                session_id="session-y", effort="high",
            ),
            _resolved("codex_cli", "gpt-6-astra", "high"),
        )
        self.assertEqual(consult.call_args.args[1]["target_model"], "gpt-6-astra")
        self.assertNotIn("fallback_from", result["model_resolution"])
        notices = result["model_resolution"].get("notices") or []
        self.assertTrue(
            any("all fallback models are latched" in n.lower() and "gpt-6-astra" in n for n in notices),
            notices,
        )
        self.assertFalse(any(broker.CODEX_PREVIOUS_FRONTIER_MODEL in n for n in notices), notices)

    def test_no_latch_leaves_model_unchanged(self):
        result, consult = self._run(
            _route_args(
                target_agent="claude", surface="cli", target_model="fable",
                session_id="session-z", effort="xhigh",
            ),
            _resolved("claude_code", "fable", "xhigh"),
        )
        self.assertEqual(consult.call_args.args[1]["target_model"], "fable")
        self.assertNotIn("fallback_from", result["model_resolution"])

class FrontierPromptCapTests(_HomeRedirectedTestCase):
    def test_oversized_frontier_prompt_raises(self):
        big_prompt = "x" * (broker.DECISION_PROVIDER_PROMPT_MAX_BYTES + 500)
        resolved = _resolved("codex_cli", "gpt-6-astra", "xhigh")
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "resolve_model_request", return_value=resolved):
            with self.assertRaisesRegex(ValueError, "exceeds the provider budget"):
                broker.route_agent_task(
                    _route_args(target_agent="codex", surface="cli", prompt=big_prompt)
                )

    def test_internal_decision_token_is_exempt_from_the_cap(self):
        big_prompt = "x" * (broker.DECISION_PROVIDER_PROMPT_MAX_BYTES + 500)
        resolved = _resolved("codex_cli", "gpt-6-astra", "max")
        consult_mock = mock.MagicMock(return_value={"status": "ok"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "resolve_model_request", return_value=resolved), \
             mock.patch.object(broker, "consult", consult_mock), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None):
            broker.route_agent_task(
                _route_args(
                    target_agent="codex", surface="cli", prompt=big_prompt,
                    _decision_internal_token=broker._DECISION_INTERNAL_TOKEN, effort="max",
                )
            )
        consult_mock.assert_called_once()

    def test_non_frontier_target_is_unaffected_by_the_cap(self):
        big_prompt = "x" * (broker.DECISION_PROVIDER_PROMPT_MAX_BYTES + 500)
        resolved = _resolved("codex_cli", "gpt-5.6-terra", "medium")
        consult_mock = mock.MagicMock(return_value={"status": "ok"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "resolve_model_request", return_value=resolved), \
             mock.patch.object(broker, "consult", consult_mock), \
             mock.patch.object(broker, "prompt_budget_notice", return_value=None):
            broker.route_agent_task(
                _route_args(
                    target_agent="codex", surface="cli", target_model="gpt-5.6-terra",
                    prompt=big_prompt,
                )
            )
        consult_mock.assert_called_once()


class MCPEntryPointCapTests(_HomeRedirectedTestCase):
    """WP-SB2b(a): consult_codex/consult_claude/queue_codex_request/
    queue_claude_request are separate MCP entry points that can forward a
    caller-supplied prompt straight to a frontier model without ever going
    through route_agent_task's own cap -- each must be bounded too."""

    def _big_prompt(self) -> str:
        return "x" * (broker.DECISION_PROVIDER_PROMPT_MAX_BYTES + 500)

    def test_consult_codex_oversize_frontier_prompt_raises(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gpt-6-astra", "xhigh")):
            with self.assertRaisesRegex(ValueError, "exceeds the provider budget"):
                broker.handle_tool(
                    "consult_codex", {"prompt": self._big_prompt(), "target_model": "gpt-6-astra"}
                )

    def test_consult_codex_small_prompt_proceeds(self):
        run_mock = mock.MagicMock(return_value={"pending": False, "response": "ok"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gpt-6-astra", "xhigh")), \
             mock.patch.object(broker, "_run_codex_consult", run_mock), \
             mock.patch.object(broker, "store_consultation", return_value=None), \
             mock.patch.object(broker, "get_context_pack", return_value={"content": ""}):
            broker.handle_tool(
                "consult_codex", {"prompt": "a short bounded prompt", "target_model": "gpt-6-astra"}
            )
        run_mock.assert_called_once()

    def test_consult_codex_non_frontier_model_proceeds_despite_oversize_prompt(self):
        run_mock = mock.MagicMock(return_value={"pending": False, "response": "ok"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gpt-5.6-terra", "medium")), \
             mock.patch.object(broker, "_run_codex_consult", run_mock), \
             mock.patch.object(broker, "store_consultation", return_value=None), \
             mock.patch.object(broker, "get_context_pack", return_value={"content": ""}):
            broker.handle_tool(
                "consult_codex", {"prompt": self._big_prompt(), "target_model": "gpt-5.6-terra"}
            )
        run_mock.assert_called_once()

    def test_consult_claude_oversize_frontier_prompt_raises(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("fable", "xhigh")):
            with self.assertRaisesRegex(ValueError, "exceeds the provider budget"):
                broker.handle_tool(
                    "consult_claude", {"prompt": self._big_prompt(), "target_model": "fable"}
                )

    def test_consult_claude_small_prompt_proceeds(self):
        claude_result = broker.ClaudeConsultResult(
            response="ok", requested_model="fable", actual_model="fable",
            model_attested=True,
            initial_model="fable", attempted_models=("fable",), fallback_reason=None,
        )
        consult_claude_mock = mock.MagicMock(return_value=claude_result)
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("fable", "xhigh")), \
             mock.patch.object(broker, "consult_claude", consult_claude_mock), \
             mock.patch.object(broker, "should_queue_heavy_claude_consult", return_value=(False, None)), \
             mock.patch.object(broker, "store_consultation", return_value=None), \
             mock.patch.object(broker, "get_context_pack", return_value={"content": ""}):
            broker.handle_tool(
                "consult_claude", {"prompt": "a short bounded prompt", "target_model": "fable"}
            )
        consult_claude_mock.assert_called_once()

    def test_consult_claude_non_frontier_model_proceeds_despite_oversize_prompt(self):
        claude_result = broker.ClaudeConsultResult(
            response="ok", requested_model="sonnet", actual_model="sonnet",
            model_attested=True,
            initial_model="sonnet", attempted_models=("sonnet",), fallback_reason=None,
        )
        consult_claude_mock = mock.MagicMock(return_value=claude_result)
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("sonnet", "medium")), \
             mock.patch.object(broker, "consult_claude", consult_claude_mock), \
             mock.patch.object(broker, "should_queue_heavy_claude_consult", return_value=(False, None)), \
             mock.patch.object(broker, "store_consultation", return_value=None), \
             mock.patch.object(broker, "get_context_pack", return_value={"content": ""}):
            broker.handle_tool(
                "consult_claude", {"prompt": self._big_prompt(), "target_model": "sonnet"}
            )
        consult_claude_mock.assert_called_once()

    def test_queue_codex_request_oversize_frontier_prompt_raises(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"):
            with self.assertRaisesRegex(ValueError, "exceeds the provider budget"):
                broker.handle_tool(
                    "queue_codex_request", {"prompt": self._big_prompt(), "target_model": "gpt-6-astra"}
                )

    def test_queue_codex_request_small_prompt_proceeds(self):
        queue_mock = mock.MagicMock(return_value={"id": "r1", "status": "queued"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "queue_codex_request", queue_mock):
            broker.handle_tool(
                "queue_codex_request", {"prompt": "a short bounded prompt", "target_model": "gpt-6-astra"}
            )
        queue_mock.assert_called_once()

    def test_queue_codex_request_non_frontier_model_proceeds_despite_oversize_prompt(self):
        queue_mock = mock.MagicMock(return_value={"id": "r1", "status": "queued"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "queue_codex_request", queue_mock):
            broker.handle_tool(
                "queue_codex_request", {"prompt": self._big_prompt(), "target_model": "gpt-5.6-terra"}
            )
        queue_mock.assert_called_once()

    def test_queue_claude_request_oversize_frontier_prompt_raises(self):
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""):
            with self.assertRaisesRegex(ValueError, "exceeds the provider budget"):
                broker.handle_tool(
                    "queue_claude_request", {"prompt": self._big_prompt(), "target_model": "fable"}
                )

    def test_queue_claude_request_small_prompt_proceeds(self):
        queue_mock = mock.MagicMock(return_value={"id": "r1", "status": "queued"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "queue_claude_request", queue_mock):
            broker.handle_tool(
                "queue_claude_request", {"prompt": "a short bounded prompt", "target_model": "fable"}
            )
        queue_mock.assert_called_once()

    def test_queue_claude_request_non_frontier_model_proceeds_despite_oversize_prompt(self):
        queue_mock = mock.MagicMock(return_value={"id": "r1", "status": "queued"})
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "queue_claude_request", queue_mock):
            broker.handle_tool(
                "queue_claude_request", {"prompt": self._big_prompt(), "target_model": "sonnet"}
            )
        queue_mock.assert_called_once()


class NativeRequestAdviserFieldsTests(_HomeRedirectedTestCase):
    """Verifies needs_native_consultation.native_request carries the shared
    adviser_instructions/max_prompt_bytes contract, and that the whole result
    stays under the 6,000-char cap it must pass through an 8,000-char context
    gate. consult_decision itself is unchanged by WP-SB2 (it already returns
    needs_native_consultation for a codex/claude host with no native_consultation
    supplied); this only checks the new fields and the size bound."""

    def _brief(self) -> dict:
        return {
            "decision": "Choose a routing design.",
            "constraints": ["Preserve compatibility with the existing public tool signatures."],
            "options": [
                {"id": "A", "summary": "Add one bounded orchestrator function."},
                {"id": "B", "summary": "Extend the existing router in place."},
            ],
            "questions": ["Which option best satisfies the constraints?"],
            "evidence": [
                {"ref": "router.py:120", "claim": "The current frontier resolution is fixed."},
            ],
        }

    def test_native_request_has_adviser_instructions_and_max_prompt_bytes(self):
        args = {
            "work_package_id": "WP-SB2-TEST",
            "host": {"vendor": "codex", "model": "gpt-5.6-sol"},
            "complexity": "architecture",
            "brief": self._brief(),
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 1}):
            result = broker.consult_decision(args)
        self.assertEqual(result["status"], "needs_native_consultation")
        native_request = result["native_request"]
        self.assertEqual(native_request["adviser_instructions"], broker._DECISION_ADVISER_INSTRUCTIONS)
        self.assertEqual(native_request["max_prompt_bytes"], broker.DECISION_PROVIDER_PROMPT_MAX_BYTES)

    def test_needs_native_consultation_result_stays_under_6000_chars(self):
        args = {
            "work_package_id": "WP-SB2-SIZE-TEST",
            "host": {"vendor": "codex", "model": "gpt-5.6-sol"},
            "complexity": "critical",
            "risk_flags": ["security", "migration"],
            "brief": self._brief(),
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", ""), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-6-astra"), \
             mock.patch.object(broker, "record_agent_event", return_value={"id": 1}):
            result = broker.consult_decision(args)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertLess(len(serialized), 6000, serialized)


class FlagshipPromptCapPinTests(unittest.TestCase):
    def test_max_bytes_constants_are_pinned_equal(self):
        self.assertEqual(
            broker.DECISION_PROVIDER_PROMPT_MAX_BYTES, routing_gate.FLAGSHIP_PROMPT_MAX_BYTES
        )


class RoutingGateFlagshipCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_dir = Path(self.tmp.name) / "routing-gate"
        evidence_dir = Path(self.tmp.name) / "context-evidence"
        db_path = Path(self.tmp.name) / "state.sqlite"
        for patch in (
            mock.patch.object(routing_gate, "STATE_DIR", state_dir),
            mock.patch.object(routing_gate, "EVIDENCE_DIR", evidence_dir),
            mock.patch.object(routing_gate, "DB_PATH", db_path),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def _payload(model, prompt, tool_name="Agent"):
        return {
            "session_id": "session-1",
            "tool_use_id": "call-1",
            "tool_name": tool_name,
            "tool_input": {"model": model, "prompt": prompt},
            "_switchboard_host": "claude",
        }

    def test_oversized_fable_agent_call_is_denied(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._payload("fable", big_prompt))
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn("bounded", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_oversized_claude_opus_5_5_agent_call_is_denied(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._payload("claude-opus-5-5", big_prompt))
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_small_flagship_prompt_is_allowed(self):
        result = routing_gate.pre_tool_use(self._payload("fable", "a short bounded prompt"))
        self.assertNotIn("hookSpecificOutput", result)

    def test_oversized_prompt_with_no_model_is_allowed(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._payload(None, big_prompt))
        self.assertNotIn("hookSpecificOutput", result)

    def test_oversized_prompt_with_sonnet_is_allowed(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._payload("sonnet", big_prompt))
        self.assertNotIn("hookSpecificOutput", result)

    def test_oversized_prompt_with_haiku_is_allowed(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._payload("haiku", big_prompt))
        self.assertNotIn("hookSpecificOutput", result)

    def test_non_delegation_tool_with_flagship_model_is_unaffected(self):
        big_prompt = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(
            self._payload("fable", big_prompt, tool_name="Read")
        )
        self.assertNotIn("hookSpecificOutput", result)

    @staticmethod
    def _spawn_agent_payload(model, message, host="codex"):
        # Codex's spawn_agent does not use a "prompt" field -- it uses "message"
        # (WP-SB2b(b)).
        return {
            "session_id": "session-1",
            "tool_use_id": "call-1",
            "tool_name": "spawn_agent",
            "tool_input": {"model": model, "message": message},
            "_switchboard_host": host,
        }

    def test_codex_spawn_agent_oversized_astra_message_is_denied(self):
        big_message = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._spawn_agent_payload("gpt-6-astra", big_message))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_codex_spawn_agent_small_astra_message_is_allowed(self):
        result = routing_gate.pre_tool_use(
            self._spawn_agent_payload("gpt-6-astra", "a short bounded task")
        )
        self.assertNotIn("hookSpecificOutput", result)

    def test_codex_spawn_agent_oversized_sol_message_is_allowed(self):
        big_message = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._spawn_agent_payload("gpt-6-sol", big_message))
        self.assertNotIn("hookSpecificOutput", result)

    def test_codex_spawn_agent_oversized_luna_message_is_allowed(self):
        big_message = "x" * (routing_gate.FLAGSHIP_PROMPT_MAX_BYTES + 1)
        result = routing_gate.pre_tool_use(self._spawn_agent_payload("gpt-6-luna", big_message))
        self.assertNotIn("hookSpecificOutput", result)


class IsFlagshipAgentModelTests(unittest.TestCase):
    def test_bare_fable_and_opus_match(self):
        for value in ("fable", "Fable", " opus ", "OPUS"):
            with self.subTest(value=value):
                self.assertTrue(routing_gate._is_flagship_agent_model(value))

    def test_claude_fable_and_opus_ids_match(self):
        for value in ("claude-fable-5", "claude-opus-4-6-thinking", "Claude-Opus-5-5"):
            with self.subTest(value=value):
                self.assertTrue(routing_gate._is_flagship_agent_model(value))

    def test_astra_ids_match_case_insensitively(self):
        for value in ("gpt-6-astra", "GPT-6-ASTRA", "Astra", " astra "):
            with self.subTest(value=value):
                self.assertTrue(routing_gate._is_flagship_agent_model(value))

    def test_sonnet_haiku_and_empty_do_not_match(self):
        for value in ("sonnet", "haiku", "", None, "claude-sonnet-4-6"):
            with self.subTest(value=value):
                self.assertFalse(routing_gate._is_flagship_agent_model(value))

    def test_codex_non_frontier_tiers_do_not_match(self):
        for value in ("gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"):
            with self.subTest(value=value):
                self.assertFalse(routing_gate._is_flagship_agent_model(value))


class DelegationPromptTextTests(unittest.TestCase):
    def test_prefers_prompt_then_falls_back_in_order(self):
        self.assertEqual(routing_gate._delegation_prompt_text({"prompt": "p"}), "p")
        self.assertEqual(routing_gate._delegation_prompt_text({"message": "m"}), "m")
        self.assertEqual(routing_gate._delegation_prompt_text({"input": "i"}), "i")
        self.assertEqual(routing_gate._delegation_prompt_text({"task": "t"}), "t")
        self.assertEqual(routing_gate._delegation_prompt_text({"instructions": "n"}), "n")
        self.assertEqual(
            routing_gate._delegation_prompt_text({"prompt": "", "message": "m"}), "m"
        )
        self.assertEqual(routing_gate._delegation_prompt_text({}), "")


if __name__ == "__main__":
    unittest.main()
