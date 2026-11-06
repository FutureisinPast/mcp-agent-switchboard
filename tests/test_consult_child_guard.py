"""Tests for WP-SB6: a Switchboard-dispatched codex/claude CLI child must never
recurse -- spawn its own sub-agents, call Switchboard MCP tools, or start another
consultation.

Covers:
  - the AGENT_BROKER_CHILD=1 env flag reaching a CLI subprocess launched via
    run_process, on both the sync consult() path and the async worker path;
  - the ``[Switchboard child - depth 1]`` prompt marker prepended once (idempotently)
    to every prompt consult_codex/consult_claude actually dispatch;
  - the generated hierarchy block's child rule, first in the block;
  - the Claude child's ``--disallowedTools`` flag;
  - the Codex child's ``-c mcp_servers.agent_switchboard.enabled=false``/
    ``--disable multi_agent`` flags (WP-SB8C: replaced --ignore-user-config,
    which also skipped the project's config.toml trust_level and blocked
    read-only shell commands for the child);
  - routing_gate.py's PreToolUse child guard.

Standard-library only. Makes zero real Claude or Codex model calls -- every CLI
subprocess is mocked at run_process/subprocess.Popen.
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


def codex_stream(text: str = "ok") -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "11111111-1111-1111-1111-111111111111"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
        ]
    )


def claude_stream(model: str = "claude-fable-5", response: str = "ok") -> str:
    return "\n".join(
        [
            json.dumps({
                "type": "assistant",
                "message": {"model": model, "content": [{"type": "text", "text": response}]},
            }),
            json.dumps({"type": "result", "result": response}),
        ]
    )


# ---------------------------------------------------------------------------
# Criterion 2: the child flag reaches every codex/claude/agy CLI subprocess,
# sync or async-worker launched.
# ---------------------------------------------------------------------------
class ChildEnvFlagTests(unittest.TestCase):
    def test_child_environment_always_sets_the_flag(self):
        env = broker.child_environment()
        self.assertEqual(env.get("AGENT_BROKER_CHILD"), "1")

    def test_child_environment_sets_flag_even_when_ambient_env_lacks_it(self):
        # Simulates the async worker: its own os.environ has AGENT_BROKER_CHILD popped
        # (see start_codex_request_worker/start_claude_request_worker) before the CLI
        # child subprocess is launched from inside that worker process.
        stripped = {k: v for k, v in __import__("os").environ.items() if k != "AGENT_BROKER_CHILD"}
        with mock.patch.object(broker.os, "environ", stripped):
            env = broker.child_environment()
        self.assertEqual(env.get("AGENT_BROKER_CHILD"), "1")

    def test_run_process_passes_the_flag_to_subprocess_popen(self):
        captured = {}

        class FakeProc:
            def communicate(self, input=None, timeout=None):
                return "", ""

            returncode = 0

        def fake_popen(command, **kwargs):
            captured["env"] = kwargs.get("env")
            return FakeProc()

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker.subprocess, "Popen", side_effect=fake_popen):
            broker.run_process(["codex", "exec"], tmpdir, "hi", timeout=5)
        self.assertEqual(captured["env"].get("AGENT_BROKER_CHILD"), "1")

    def test_worker_launcher_env_drops_the_flag_for_its_own_broker_calls(self):
        # start_codex_request_worker/start_claude_request_worker intentionally pop the
        # flag from the DETACHED WORKER PROCESS's own env (so the worker process itself
        # is not treated as a child when it makes its own broker calls); this is
        # orthogonal to run_process/child_environment always re-adding it for the actual
        # CLI grandchild the worker later launches.
        import agent_broker_mcp as m
        src = m.__file__
        text = Path(src).read_text(encoding="utf-8")
        self.assertIn('env.pop("AGENT_BROKER_CHILD", None)', text)


# ---------------------------------------------------------------------------
# Criterion 3: the child prompt marker.
# ---------------------------------------------------------------------------
class ChildPromptMarkerTests(unittest.TestCase):
    def _consult_codex(self, prompt: str, runs):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_codex", return_value="codex"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "BROKER_DIR", Path(tmpdir) / "broker-home"), \
             mock.patch.object(broker, "run_process", side_effect=runs) as run:
            result = broker.consult_codex(tmpdir, prompt, "read-only", None, None, 30)
            return result, run

    def _consult_claude(self, prompt: str, runs):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value="claude"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "claude_empty_mcp_config_path", return_value=Path(tmpdir) / "empty.json"), \
             mock.patch.object(broker, "run_process", side_effect=runs) as run:
            result = broker.consult_claude(tmpdir, prompt, model_name="fable")
            return result, run

    def test_codex_dispatch_is_prefixed_with_the_marker(self):
        result, run = self._consult_codex("Review this diff.", [(0, codex_stream("ok"), "")])
        sent_prompt = run.call_args.args[2]
        self.assertTrue(sent_prompt.startswith(broker.CHILD_PROMPT_MARKER))
        self.assertIn("Review this diff.", sent_prompt)
        self.assertEqual(result.response, "ok")

    def test_claude_dispatch_is_prefixed_with_the_marker(self):
        result, run = self._consult_claude("Review this diff.", [(0, claude_stream(), "")])
        sent_prompt = run.call_args.args[2]
        self.assertTrue(sent_prompt.startswith(broker.CHILD_PROMPT_MARKER))
        self.assertIn("Review this diff.", sent_prompt)

    def test_marker_is_never_doubled(self):
        already_marked = broker.CHILD_PROMPT_MARKER + "\n\nReview this diff."
        result, run = self._consult_codex(already_marked, [(0, codex_stream("ok"), "")])
        sent_prompt = run.call_args.args[2]
        self.assertEqual(sent_prompt.count(broker.CHILD_PROMPT_MARKER), 1)

    def test_marker_exact_text(self):
        self.assertEqual(
            broker.CHILD_PROMPT_MARKER,
            "[Switchboard child · depth 1] You are answering one delegated request. "
            "Do not spawn or dispatch agents, do not start consultations, and do not call "
            "Agent Switchboard tools.",
        )

    def test_queued_worker_dispatch_is_also_marked(self):
        # run_codex_request_worker feeds a stored (unmarked) prompt straight into
        # consult_codex -- the single chokepoint -- so the marker still lands exactly once.
        # sqlite3's context manager commits but does not close the connection; tolerate
        # delayed handle release on Windows test cleanup (see test_dynamic_hierarchy.py).
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite"
            with mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "BROKER_DIR", Path(tmpdir)), \
                 mock.patch.object(broker, "start_codex_request_worker", return_value={"started": False}):
                # start_codex_request_worker is mocked out: autorun=True below would
                # otherwise spawn a REAL detached subprocess (subprocess.Popen) whose
                # log file lives under this test's own TemporaryDirectory, which is
                # deleted a few lines later while that process may still hold the
                # file open -- a genuine (pre-existing, environment-timing-dependent)
                # Windows flake, not something this test needs a live worker for since
                # it drives run_codex_request_worker directly afterward anyway.
                broker.init_db()
                queued = broker.queue_codex_request(tmpdir, "Do the thing.", None, "gpt-6-luna", False, "consult", None, "low", True)
                rid = queued["id"]
                with mock.patch.object(broker, "load_config", return_value={}), \
                     mock.patch.object(broker, "discover_codex", return_value="codex"), \
                     mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
                     mock.patch.object(broker, "run_process", return_value=(0, codex_stream("done"), "")) as run, \
                     mock.patch.object(broker, "store_consultation"), \
                     mock.patch.object(broker, "record_agent_event"), \
                     mock.patch.object(broker, "render_request_ledger"):
                    broker.run_codex_request_worker(rid)
            sent_prompt = run.call_args.args[2]
            self.assertTrue(sent_prompt.startswith(broker.CHILD_PROMPT_MARKER))
            self.assertIn("Do the thing.", sent_prompt)


# ---------------------------------------------------------------------------
# Criterion 4: the generated hierarchy block's child rule, in first position.
# ---------------------------------------------------------------------------
class HierarchyChildRuleTests(unittest.TestCase):
    CODEX_ROLES = {
        "frontier": {"id": "gpt-6-astra"},
        "workhorse": {"id": "gpt-6-sol"},
        "reader": {"id": "gpt-6-luna"},
    }
    CLAUDE_ROLES = {"frontier": ["fable", "opus"], "workhorse": "sonnet", "reader": "haiku"}

    def test_child_rule_present_and_exact(self):
        import hierarchy_install
        body = hierarchy_install.routing_rules_body(self.CODEX_ROLES, self.CLAUDE_ROLES)
        expected = (
            "Switchboard children: if your prompt begins with the [Switchboard child · "
            "depth 1] marker, or says you are a bounded flagship decision adviser or at "
            "consultation depth 1, or AGENT_BROKER_CHILD=1 is set, you are a delegated "
            "adviser/worker, not a brain: never spawn agents, never start consultations, "
            "never call Agent Switchboard tools; answer from the supplied request only."
        )
        self.assertIn(expected, body)

    def test_child_rule_is_first_bullet_in_the_block(self):
        import hierarchy_install
        body = hierarchy_install.routing_rules_body(self.CODEX_ROLES, self.CLAUDE_ROLES)
        heading_index = body.index("## Cost-aware model hierarchy")
        child_rule_index = body.index("Switchboard children:")
        model_selected_index = body.index("The model selected for the main session is the brain")
        self.assertLess(heading_index, child_rule_index)
        self.assertLess(child_rule_index, model_selected_index)
        # The child rule is the first bullet in the block: the only "\n- " between the
        # heading and it is the one immediately introducing the child rule itself.
        between = body[heading_index:child_rule_index]
        self.assertEqual(between.count("\n- "), 1)
        self.assertTrue(between.rstrip().endswith("\n-") or between.endswith("- "))


# ---------------------------------------------------------------------------
# Criterion 5: Claude children get --disallowedTools for Agent/Task.
# ---------------------------------------------------------------------------
class ClaudeChildDisallowedToolsTests(unittest.TestCase):
    def test_disallowed_tools_flag_present_in_argv(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value="claude"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "claude_empty_mcp_config_path", return_value=Path(tmpdir) / "empty.json"), \
             mock.patch.object(broker, "run_process", return_value=(0, claude_stream(), "")) as run:
            broker.consult_claude(tmpdir, "check", model_name="fable")
        argv = run.call_args.args[0]
        self.assertIn("--disallowedTools", argv)
        idx = argv.index("--disallowedTools")
        self.assertEqual(argv[idx + 1], "Agent,Task")

    def test_empty_mcp_config_has_no_switchboard_server(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "BROKER_DIR", Path(tmpdir)):
            path = broker.claude_empty_mcp_config_path()
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"mcpServers": {}})


# ---------------------------------------------------------------------------
# Criterion 6: Codex children get -c mcp_servers.agent_switchboard.enabled=false
# and --disable multi_agent, and NEVER --ignore-user-config (WP-SB8C: that flag
# also skipped the project's trust_level in config.toml, blocking even read-only
# shell commands for the child -- verified regression, see agent_broker_mcp.py).
# ---------------------------------------------------------------------------
class CodexChildFlagsTests(unittest.TestCase):
    def test_switchboard_mcp_disabled_and_multi_agent_disabled_present(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_codex", return_value="codex"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "BROKER_DIR", Path(tmpdir) / "broker-home"), \
             mock.patch.object(broker, "run_process", return_value=(0, codex_stream("ok"), "")) as run:
            broker.consult_codex(tmpdir, "check", "read-only", None, None, 30)
        command = run.call_args.args[0]
        self.assertNotIn("--ignore-user-config", command)
        self.assertIn("-c", command)
        c_values = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
        self.assertIn("mcp_servers.agent_switchboard.enabled=false", c_values)
        self.assertIn("--disable", command)
        self.assertEqual(command[command.index("--disable") + 1], "multi_agent")


# ---------------------------------------------------------------------------
# Criterion 7: routing_gate.py PreToolUse child guard.
# ---------------------------------------------------------------------------
class GateChildGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_dir = Path(self.tmp.name) / "routing-gate"
        evidence_dir = Path(self.tmp.name) / "context-evidence"
        db_path = Path(self.tmp.name) / "state.sqlite"
        for target, value in (
            ("STATE_DIR", state_dir), ("EVIDENCE_DIR", evidence_dir), ("DB_PATH", db_path),
        ):
            patcher = mock.patch.object(routing_gate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _payload(tool_name: str, tool_input=None, host="codex"):
        return {
            "session_id": "session-1",
            "tool_use_id": "call-1",
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "_switchboard_host": host,
        }

    def _with_child_env(self, fn, *args, **kwargs):
        with mock.patch.dict(routing_gate.os.environ, {"AGENT_BROKER_CHILD": "1"}):
            return fn(*args, **kwargs)

    def test_agent_denied_under_child_flag(self):
        result = self._with_child_env(
            routing_gate.pre_tool_use, self._payload("Agent", {"prompt": "do x"})
        )
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["permissionDecisionReason"], "Switchboard children may not spawn agents or start consultations")

    def test_task_denied_under_child_flag(self):
        result = self._with_child_env(routing_gate.pre_tool_use, self._payload("Task", {"prompt": "x"}))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_consult_decision_denied_under_child_flag(self):
        result = self._with_child_env(
            routing_gate.pre_tool_use,
            self._payload("mcp__agent-switchboard__consult_decision", {}),
        )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_route_agent_task_denied_under_child_flag_underscore_namespace(self):
        result = self._with_child_env(
            routing_gate.pre_tool_use,
            self._payload("mcp__agent_switchboard__route_agent_task", {}),
        )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_queue_codex_request_denied_under_child_flag(self):
        result = self._with_child_env(
            routing_gate.pre_tool_use,
            self._payload("mcp__agent-switchboard__queue_codex_request", {}),
        )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unrelated_tool_unaffected_by_child_flag(self):
        result = self._with_child_env(routing_gate.pre_tool_use, self._payload("Read", {"path": "a.py"}))
        self.assertNotIn("hookSpecificOutput", result)

    def test_behaviour_unchanged_without_the_flag(self):
        # Without AGENT_BROKER_CHILD=1 set, Agent/Task remain ungated by this guard
        # (they still pass through the ordinary delegation-tool exemption).
        with mock.patch.dict(routing_gate.os.environ, {}, clear=False):
            routing_gate.os.environ.pop("AGENT_BROKER_CHILD", None)
            result = routing_gate.pre_tool_use(self._payload("Agent", {"prompt": "do x"}))
        self.assertNotIn("hookSpecificOutput", result)

    def test_request_status_and_result_not_child_guarded(self):
        # These are read-only polling tools, deliberately excluded from the narrower
        # child-guard set (unlike the broader SWITCHBOARD_CONTROL_SUFFIXES).
        self.assertFalse(routing_gate._is_child_guarded_tool("mcp__agent_switchboard__request_status"))
        self.assertFalse(routing_gate._is_child_guarded_tool("mcp__agent_switchboard__request_result"))


# Codex execution modes and immediate parallel dispatch (1.0.64).
class CodexExecutionModeTests(unittest.TestCase):
    def test_aliases_defaults_and_invalid_modes(self):
        for mode, expected in (
            ("plan", "read-only"), ("read-only", "read-only"),
            ("accept-edits", "workspace-write"), ("acceptEdits", "workspace-write"),
            ("workspace-write", "workspace-write"), ("danger-full-access", "danger-full-access"),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(broker.normalize_codex_mode(mode, "implementation"), expected)
        self.assertEqual(broker.normalize_codex_mode(None, "implementation"), "workspace-write")
        for task in (None, "research", "implementation_plan", "quick_check"):
            self.assertEqual(broker.normalize_codex_mode(None, task), "read-only")
        for mode in ("typo", "bypassPermissions", "unrestricted", "danger full access"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Unsupported Codex mode"):
                broker.normalize_codex_mode(mode, "implementation")

    def test_leaf_uses_canonical_sandbox_and_rejects_before_discovery(self):
        stream = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(broker, "BROKER_DIR", Path(tmp) / "broker"), \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_codex", return_value="codex") as discover, \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmp)), \
             mock.patch.object(broker, "run_process", return_value=(0, stream, "")) as run:
            for mode, expected in (("plan", "read-only"), ("acceptEdits", "workspace-write"),
                                   ("workspace-write", "workspace-write"), (None, "read-only")):
                broker.consult_codex(tmp, "check", mode)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--sandbox") + 1], expected)
            discover.reset_mock()
            with self.assertRaises(ValueError):
                broker.consult_codex(tmp, "check", "typo")
            discover.assert_not_called()


class CodexExecutionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (
            ("DB_PATH", self.root / "broker.db"), ("BROKER_DIR", self.root / "broker"),
            ("resolve_project", mock.Mock(return_value=broker.ProjectInfo("p", str(self.root)))),
            ("render_request_ledger", mock.Mock()),
            ("start_codex_request_worker", mock.Mock(return_value={"started": True})),
        ):
            patcher = mock.patch.object(broker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(broker.os.environ, {"AGENT_BROKER_CHILD": "0"})
        env.start()
        self.addCleanup(env.stop)
        broker.init_db()

    def test_queue_persists_defaults_and_explicit_modes_without_promoting_read_only(self):
        for index, (mode, task, expected) in enumerate((
            (None, "implementation", "workspace-write"),
            (None, "research", "read-only"),
            ("plan", "implementation", "read-only"),
            ("read-only", "implementation", "read-only"),
            ("accept-edits", "research", "workspace-write"),
            ("acceptEdits", "implementation", "workspace-write"),
        )):
            row = broker.queue_codex_request("p", f"task {index}", mode=mode, task_kind=task, autorun=False)
            self.assertEqual(row["mode"], expected)
            with broker.db_connect() as conn:
                stored = conn.execute("SELECT mode FROM codex_requests WHERE id = ?", (row["id"],)).fetchone()
            self.assertEqual(stored[0], expected)
        with self.assertRaisesRegex(ValueError, "Unsupported Codex mode"):
            broker.queue_codex_request("p", "bad", mode="typo", autorun=True)
        broker.start_codex_request_worker.assert_not_called()

    def test_worker_forwards_stored_aliases_and_keeps_legacy_null_read_only(self):
        result = broker.CodexConsultResult("ok", "gpt-6-luna", "gpt-6-luna", "low", "low", True)
        with mock.patch.object(broker, "consult_codex", return_value=result) as consult, \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gpt-6-luna", "low")), \
             mock.patch.object(broker, "enforce_codex_effort_policy", return_value=("low", None)), \
             mock.patch.object(broker, "store_consultation"), \
             mock.patch.object(broker, "record_agent_event"):
            for index, (mode, expected) in enumerate(((None, "read-only"), ("acceptEdits", "workspace-write"),
                                                     ("plan", "read-only"), ("workspace-write", "workspace-write"))):
                row = broker.queue_codex_request("p", f"worker {index}", task_kind="implementation", autorun=False)
                with broker.db_connect() as conn:
                    conn.execute("UPDATE codex_requests SET mode = ? WHERE id = ?", (mode, row["id"]))
                broker.run_codex_request_worker(row["id"])
                self.assertEqual(consult.call_args.args[2], expected)
                with broker.db_connect() as conn:
                    stored = conn.execute("SELECT mode FROM codex_requests WHERE id = ?", (row["id"],)).fetchone()
                self.assertEqual(stored[0], expected)

    def test_router_cli_and_inbox_forward_canonical_modes_and_async_boolean(self):
        resolution = {"status": "resolved", "target_agent": "codex_cli", "target_model": "gpt-6-luna",
                      "effort": "low", "source": "explicit_request"}
        with mock.patch.object(broker, "resolve_model_request", return_value=resolution), \
             mock.patch.object(broker, "apply_codex_model_policy", return_value=("gpt-6-luna", "low", None)), \
             mock.patch.object(broker, "enforce_native_first_broker_fallback"), \
             mock.patch.object(broker, "_is_frontier_target", return_value=False), \
             mock.patch.object(broker, "resolve_ide_host", return_value=None), \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "surface_available", return_value=True), \
             mock.patch.object(broker, "consult", side_effect=lambda model, args: {"mode": args["mode"]}) as consult:
            for surface in ("cli", "extension"):
                for mode, expected in ((None, "workspace-write"), ("accept-edits", "workspace-write"),
                                       ("plan", "read-only"), ("read-only", "read-only")):
                    args = {"target_agent": "codex", "target_model": "gpt-6-luna", "surface": surface,
                            "task_kind": "implementation", "project": "p", "prompt": f"{surface} {mode}",
                            "mode": mode, "async": False}
                    routed = broker._route_agent_task_impl(args)
                    self.assertEqual(routed["mode"], expected)
                    if surface == "cli":
                        self.assertIs(consult.call_args.args[1]["async"], False)
            args.update(surface="cli", mode="workspace-write", **{"async": True})
            broker._route_agent_task_impl(args)
            self.assertIs(consult.call_args.args[1]["async"], True)
            consult.reset_mock()
            with self.assertRaises(ValueError):
                broker._route_agent_task_impl(dict(args, mode="typo"))
            consult.assert_not_called()

    def test_consult_resolves_mode_before_default_and_forwards_explicit_async(self):
        with mock.patch.object(broker, "enforce_native_first_broker_fallback"), \
             mock.patch.object(broker, "apply_codex_model_policy", return_value=("gpt-6-luna", "low", None)), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gpt-6-luna", "low")), \
             mock.patch.object(broker, "enforce_codex_effort_policy", return_value=("low", None)), \
             mock.patch.object(broker, "_enforce_frontier_prompt_cap", return_value=("standard", None)), \
             mock.patch.object(broker, "_run_codex_consult", return_value={"pending": True, "payload": {}}) as run:
            for mode, expected in ((None, "workspace-write"), ("plan", "read-only"), ("acceptEdits", "workspace-write")):
                broker.consult("codex", {"prompt": "check", "task_kind": "implementation", "mode": mode, "async": True})
                self.assertEqual(run.call_args.args[2], expected)
                self.assertIs(run.call_args.kwargs["async_requested"], True)

    def test_explicit_async_returns_distinct_requests_without_waiting_and_keeps_sync_default(self):
        with mock.patch.object(broker, "_await_codex_row", return_value=None) as wait, \
             mock.patch.object(broker, "_refresh_stale_codex_request", side_effect=lambda table, row: row):
            results = []
            for prompt in ("parallel one", "parallel two"):
                result = broker._run_codex_consult(
                    broker.ProjectInfo("p", str(self.root)), prompt, "workspace-write", "gpt-6-luna", "low",
                    30, None, "implementation", 1000, async_requested=True)
                self.assertTrue(result["pending"])
                self.assertEqual(result["payload"]["routing"], "explicit_async")
                self.assertIs(result["payload"]["async"], True)
                self.assertEqual(result["payload"]["retry_after_seconds"], 20)
                self.assertIsNone(result["payload"]["typical_wait"])
                results.append(result["request_id"])
            self.assertNotEqual(*results)
            wait.assert_not_called()
            broker._run_codex_consult(broker.ProjectInfo("p", str(self.root)), "sync", "read-only",
                                      "gpt-6-luna", "low", 30, None, "research", 1000)
            wait.assert_called_once()
            with broker.db_connect() as conn:
                conn.execute("UPDATE codex_requests SET status='completed', response='done' WHERE id=?", (results[0],))
            terminal = broker._run_codex_consult(
                broker.ProjectInfo("p", str(self.root)), "parallel one", "workspace-write", "gpt-6-luna", "low",
                30, None, "implementation", 1000, async_requested=True)
            self.assertFalse(terminal["pending"])
            self.assertEqual(terminal["response"], "done")


if __name__ == "__main__":
    unittest.main()
