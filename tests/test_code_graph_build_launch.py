"""Unit tests for code graph build launch, verifying subprocess isolation and timeout handling."""
from __future__ import annotations

import ast
import importlib.metadata
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

# Provide dummy graphify module stubs if not installed so gfy_adapter can import without real graphify
if "graphify" not in sys.modules:
    graphify_mod = types.ModuleType("graphify")
    serve_mod = types.ModuleType("graphify.serve")
    detect_mod = types.ModuleType("graphify.detect")
    detect_mod.CODE_EXTENSIONS = [".py"]
    detect_mod._is_noise_dir = lambda d, p: False
    graphify_mod.serve = serve_mod
    graphify_mod.detect = detect_mod
    sys.modules["graphify"] = graphify_mod
    sys.modules["graphify.serve"] = serve_mod
    sys.modules["graphify.detect"] = detect_mod

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.abspath(os.path.join(HERE, "..", "code_graph_runtime"))
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)

_orig_version = importlib.metadata.version


def _safe_version(pkg_name: str) -> str:
    try:
        return _orig_version(pkg_name)
    except Exception:
        if pkg_name == "graphifyy":
            return "0.9.77"
        raise


with mock.patch("importlib.metadata.version", side_effect=_safe_version):
    import gfy_adapter


class SimpleProject:
    """Fake project object providing .root, .exclude, .work, and .out_dir under a temporary directory."""

    def __init__(self, base_dir: pathlib.Path) -> None:
        self.root = str(base_dir / "repo")
        self.exclude = ["*.tmp", "build/*"]
        self.work = base_dir / "work"
        self.out_dir = base_dir / "out"


class TestCodeGraphBuildLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = pathlib.Path(self.temp_dir.name)
        (self.base_path / "out").mkdir(parents=True, exist_ok=True)
        self.proj = SimpleProject(self.base_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_case_a_run_build_passes_stdin_devnull(self) -> None:
        """(a) run_build passes stdin=subprocess.DEVNULL to Popen."""
        fake_proc = mock.MagicMock()
        fake_proc.communicate.return_value = ("ok", "")
        fake_proc.returncode = 0
        with mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            res = gfy_adapter.run_build(self.proj, {})
            self.assertIsNone(res)
            mock_popen.assert_called_once()
            _, kwargs = mock_popen.call_args
            self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_case_b_env_default_workers_and_caller_not_mutated(self) -> None:
        """(b) env gets GRAPHIFY_MAX_WORKERS '8' by default, and the caller's env dict is not mutated."""
        caller_env = {"EXISTING_VAR": "value"}
        fake_proc = mock.MagicMock()
        fake_proc.communicate.return_value = ("ok", "")
        fake_proc.returncode = 0
        with mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            gfy_adapter.run_build(self.proj, caller_env)
            mock_popen.assert_called_once()
            _, kwargs = mock_popen.call_args
            passed_env = kwargs.get("env")

            self.assertEqual(passed_env.get("GRAPHIFY_MAX_WORKERS"), "8")
            self.assertEqual(passed_env.get("EXISTING_VAR"), "value")
            self.assertNotIn("GRAPHIFY_MAX_WORKERS", caller_env)
            self.assertEqual(caller_env, {"EXISTING_VAR": "value"})

    def test_case_b_env_existing_workers_preserved(self) -> None:
        """(b) a value already in env is kept, and the caller's env dict is not mutated."""
        caller_env = {"GRAPHIFY_MAX_WORKERS": "16", "CUSTOM": "1"}
        caller_env_copy = dict(caller_env)
        fake_proc = mock.MagicMock()
        fake_proc.communicate.return_value = ("ok", "")
        fake_proc.returncode = 0
        with mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            gfy_adapter.run_build(self.proj, caller_env)
            mock_popen.assert_called_once()
            _, kwargs = mock_popen.call_args
            passed_env = kwargs.get("env")

            self.assertEqual(passed_env.get("GRAPHIFY_MAX_WORKERS"), "16")
            self.assertEqual(caller_env, caller_env_copy)

    def test_case_c_timeout_expired_kills_tree_and_propagates(self) -> None:
        """(c) a fake Popen whose first communicate raises TimeoutExpired: _kill_tree is called with the pid, kill is called, and TimeoutExpired propagates."""
        fake_proc = mock.MagicMock()
        fake_proc.pid = 12345
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["mock_cmd"], timeout=900),
            ("post_kill_stdout", "post_kill_stderr"),
        ]
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("gfy_adapter._kill_tree") as mock_kill_tree:
            with self.assertRaises(subprocess.TimeoutExpired):
                gfy_adapter.run_build(self.proj, {})

            mock_kill_tree.assert_called_once_with(12345)
            fake_proc.kill.assert_called_once()
            self.assertEqual(fake_proc.communicate.call_count, 2)
            self.assertEqual(fake_proc.communicate.call_args_list[0], mock.call(timeout=900))
            self.assertEqual(fake_proc.communicate.call_args_list[1], mock.call(timeout=30))

    def test_case_c_timeout_second_communicate_timeout_handled(self) -> None:
        """(c) After timeout, when second communicate also times out at 30 s, TimeoutExpired still propagates cleanly."""
        fake_proc = mock.MagicMock()
        fake_proc.pid = 54321
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["mock_cmd"], timeout=900),
            subprocess.TimeoutExpired(cmd=["mock_cmd"], timeout=30),
        ]
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("gfy_adapter._kill_tree") as mock_kill_tree:
            with self.assertRaises(subprocess.TimeoutExpired):
                gfy_adapter.run_build(self.proj, {})

            mock_kill_tree.assert_called_once_with(54321)
            fake_proc.kill.assert_called_once()
            self.assertEqual(fake_proc.communicate.call_count, 2)
            self.assertEqual(fake_proc.communicate.call_args_list[1], mock.call(timeout=30))

    def test_case_c_kill_tree_win32(self) -> None:
        """_kill_tree on win32 invokes taskkill with /T /F /PID and stdin=DEVNULL."""
        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run") as mock_run:
            gfy_adapter._kill_tree(777)
            mock_run.assert_called_once_with(
                ["taskkill", "/T", "/F", "/PID", "777"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=False,
            )

    def test_case_c_kill_tree_non_win32(self) -> None:
        """_kill_tree on non-win32 does not invoke taskkill."""
        with mock.patch("sys.platform", "linux"), \
             mock.patch("subprocess.run") as mock_run:
            gfy_adapter._kill_tree(777)
            mock_run.assert_not_called()

    def test_case_c_kill_tree_exception_suppressed(self) -> None:
        """_kill_tree catches OSError and SubprocessError silently."""
        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run", side_effect=OSError("process not found")):
            gfy_adapter._kill_tree(777)

    def test_case_d_rc_zero_returns_none(self) -> None:
        """(d) rc 0 returns None."""
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("normal stdout output", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            res = gfy_adapter.run_build(self.proj, {})
            self.assertIsNone(res)

    def test_case_d_rc_one_returns_error_string(self) -> None:
        """(d) rc 1 returns a string starting 'graphify extract rc=1'."""
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 1
        fake_proc.communicate.return_value = ("", "failure traceback details")
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            res = gfy_adapter.run_build(self.proj, {})
            self.assertIsNotNone(res)
            self.assertTrue(res.startswith("graphify extract rc=1: "))
            self.assertIn("failure traceback details", res)

    def test_case_d_rc_one_prefers_stderr_over_stdout(self) -> None:
        """(d) rc 1 uses stderr or falls back to stdout if stderr is empty."""
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 1
        fake_proc.communicate.return_value = ("fallback stdout error", "")
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            res = gfy_adapter.run_build(self.proj, {})
            self.assertIsNotNone(res)
            self.assertTrue(res.startswith("graphify extract rc=1: "))
            self.assertIn("fallback stdout error", res)

    def test_case_e_ast_walk_asserts_stdin_keyword(self) -> None:
        """(e) an ast walk of gfy_adapter.py's source asserts every call to subprocess.run or subprocess.Popen has a stdin keyword."""
        adapter_path = pathlib.Path(gfy_adapter.__file__)
        source = adapter_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(adapter_path))

        checked_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen"):
                    func_name = ast.unparse(node.func)
                elif isinstance(node.func, ast.Name) and node.func.id in ("run", "Popen"):
                    func_name = node.func.id

                if func_name and ("subprocess" in func_name or func_name in ("run", "Popen")):
                    kw_names = [kw.arg for kw in node.keywords]
                    self.assertIn(
                        "stdin",
                        kw_names,
                        f"Line {node.lineno}: call to {func_name} does not pass 'stdin' keyword",
                    )
                    checked_calls.append((node.lineno, func_name))

        self.assertGreaterEqual(
            len(checked_calls),
            4,
            f"Expected at least 4 subprocess calls in gfy_adapter.py, found {len(checked_calls)}: {checked_calls}",
        )


if __name__ == "__main__":
    unittest.main()
