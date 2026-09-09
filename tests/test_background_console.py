"""Windows background-process lifecycle regressions."""
from __future__ import annotations

import ast
import ctypes
import inspect
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_entry as entry  # noqa: E402
import agent_broker_mcp as broker  # noqa: E402


class _Kernel32:
    def __init__(self, attached: int):
        self.attached = attached

    @staticmethod
    def GetConsoleWindow():
        return 123

    def GetConsoleProcessList(self, _buffer, _length):
        return self.attached


class _User32:
    def __init__(self):
        self.calls = []

    def ShowWindow(self, hwnd, mode):
        self.calls.append((hwnd, mode))
        return 1


class BackgroundConsoleTests(unittest.TestCase):
    def test_private_frozen_serve_console_is_hidden(self):
        user32 = _User32()
        windll = mock.Mock(kernel32=_Kernel32(1), user32=user32)
        with mock.patch.object(entry.os, "name", "nt"), \
             mock.patch.object(entry.sys, "frozen", True, create=True), \
             mock.patch.object(ctypes, "windll", windll, create=True):
            hidden = entry._hide_private_console_for_background_mode("serve")
        self.assertTrue(hidden)
        self.assertEqual(user32.calls, [(123, 0)])

    def test_shared_user_console_is_never_hidden(self):
        user32 = _User32()
        windll = mock.Mock(kernel32=_Kernel32(2), user32=user32)
        with mock.patch.object(entry.os, "name", "nt"), \
             mock.patch.object(entry.sys, "frozen", True, create=True), \
             mock.patch.object(ctypes, "windll", windll, create=True):
            hidden = entry._hide_private_console_for_background_mode("bridge")
        self.assertFalse(hidden)
        self.assertEqual(user32.calls, [])

    def test_every_production_subprocess_declares_windows_creation_flags(self):
        for name in ("agent_broker_mcp.py", "setup.py", "evidence_probe.py"):
            source = (REPO_ROOT / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            missing = []
            weak = []
            flag_assignments = {}
            for candidate in ast.walk(tree):
                if isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                    targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
                    value = candidate.value
                    for target in targets:
                        if isinstance(target, ast.Name) and value is not None:
                            flag_assignments.setdefault(target.id, []).append(ast.get_source_segment(source, value) or "")
            for node in ast.walk(tree):
                func = node.func if isinstance(node, ast.Call) else None
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                    and func.attr in {"run", "Popen"}
                ):
                    continue
                if not any(keyword.arg == "creationflags" for keyword in node.keywords):
                    missing.append(node.lineno)
                    continue
                value = next(keyword.value for keyword in node.keywords if keyword.arg == "creationflags")
                expression = ast.get_source_segment(source, value) or ""
                carries_no_window = "NO_WINDOW" in expression
                if isinstance(value, ast.Name):
                    carries_no_window = carries_no_window or any(
                        "NO_WINDOW" in assignment for assignment in flag_assignments.get(value.id, [])
                    )
                if not carries_no_window:
                    weak.append((node.lineno, expression))
            self.assertEqual(missing, [], f"{name} subprocess calls missing creationflags")
            self.assertEqual(weak, [], f"{name} subprocess calls lack a CREATE_NO_WINDOW contract")

    def test_run_detached_call_sites_are_only_persistent_ui_launchers(self):
        tree = ast.parse((REPO_ROOT / "agent_broker_mcp.py").read_text(encoding="utf-8"))
        parents = []
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_detached":
                    parents.append(function.name)
        self.assertEqual(sorted(parents), ["launch_ide_host", "launch_ide_host", "launch_windows_app"])

    def test_run_detached_returns_explicit_persistent_unmanaged_metadata(self):
        proc = mock.Mock(pid=4321)
        with mock.patch.object(broker.os, "name", "nt"), \
             mock.patch.object(broker.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(broker.subprocess, "Popen", return_value=proc) as popen:
            result = broker.run_detached(["ui.exe", "--open"], cwd="C:\\workspace")
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 4321)
        self.assertEqual(result["lifecycle"], "persistent_external_ui")
        self.assertFalse(result["managed"])
        self.assertIn("not waited or terminated", result["reconciliation"])
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        proc.wait.assert_not_called()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_run_process_timeout_kills_tree_then_communicates_to_reap(self):
        proc = mock.Mock(pid=77, returncode=None)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["worker"], 3, output="partial", stderr="warning"),
            ("after-kill", "after-error"),
        ]
        with mock.patch.object(broker.subprocess, "Popen", return_value=proc), \
             mock.patch.object(broker, "kill_process_tree") as kill_tree:
            code, stdout, stderr = broker.run_process(["worker"], ".", timeout=3)
        self.assertEqual(code, 124)
        kill_tree.assert_called_once_with(proc)
        self.assertEqual(proc.communicate.call_count, 2)
        self.assertIn("partial", stdout)
        self.assertIn("after-kill", stdout)
        self.assertIn("timed out after 3 seconds", stderr)

    def test_async_worker_launchers_keep_hidden_detached_pid_and_completion_contracts(self):
        for start_name, run_name, table in (
            ("start_codex_request_worker", "run_codex_request_worker", "codex_requests"),
            ("start_claude_request_worker", "run_claude_request_worker", "claude_requests"),
        ):
            start_source = inspect.getsource(getattr(broker, start_name))
            run_source = inspect.getsource(getattr(broker, run_name))
            for marker in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
                self.assertIn(marker, start_source, f"{start_name}: {marker}")
            self.assertIn("worker_pid = ?", start_source)
            self.assertIn("proc.pid", start_source)
            self.assertIn("status = 'error'", start_source)
            self.assertIn(table, run_source)
            self.assertIn("worker_completed_at", run_source)
            self.assertIn("completed_at", run_source)


if __name__ == "__main__":
    unittest.main()
