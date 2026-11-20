"""Unit tests for subprocess stdin guard.

Verifies that subprocess calls in broker MCP and evidence probe do not inherit
the server stdin pipe on Windows, preventing startup freezes in child processes.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent

SUBPROCESS_TARGET_FUNCTIONS = {"run", "Popen", "check_output", "check_call", "call"}


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            return func.attr in SUBPROCESS_TARGET_FUNCTIONS
    elif isinstance(func, ast.Name):
        return func.id in SUBPROCESS_TARGET_FUNCTIONS
    return False


def _has_stdin_guard(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg in ("stdin", "input"):
            return True
        if kw.arg is None:  # **kwargs expansion
            return True
    return False


def find_subprocess_stdin_offenders(
    source_or_node: str | Path | ast.AST,
    filename: str = "<string>",
) -> list[str]:
    """Inspect AST and return a list of 'file:line' identifiers for subprocess calls missing stdin guards."""
    if isinstance(source_or_node, Path):
        display_name = filename if filename != "<string>" else source_or_node.name
        tree = ast.parse(source_or_node.read_text(encoding="utf-8"), filename=display_name)
    elif isinstance(source_or_node, str):
        display_name = filename
        tree = ast.parse(source_or_node, filename=display_name)
    else:
        display_name = filename
        tree = source_or_node

    raw_offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_subprocess_call(node):
            if not _has_stdin_guard(node):
                raw_offenders.append((display_name, getattr(node, "lineno", 0)))

    raw_offenders.sort(key=lambda item: (item[0], item[1]))
    return [f"{fn}:{ln}" for fn, ln in raw_offenders]


class TestSubprocessStdinGuard(unittest.TestCase):
    def test_case_a_broker_and_probe_guarded(self) -> None:
        """(a) every call to subprocess.run/Popen/check_output/check_call/call in agent_broker_mcp.py

        and evidence_probe.py has a stdin or input keyword or a **kwargs expansion,
        and a failure message lists file:line for each offender.
        """
        all_offenders: list[str] = []
        for rel_name in ("agent_broker_mcp.py", "evidence_probe.py"):
            target_path = REPO_ROOT / rel_name
            self.assertTrue(target_path.is_file(), f"Expected file {rel_name} to exist at {target_path}")
            offenders = find_subprocess_stdin_offenders(target_path, filename=rel_name)
            all_offenders.extend(offenders)

        self.assertEqual(
            all_offenders,
            [],
            "Offenders missing stdin/input/**kwargs:\n" + "\n".join(all_offenders),
        )

    def test_case_b_setup_discover_hierarchy_roles_guarded(self) -> None:
        """(b) in setup.py, every such call inside the function _discover_hierarchy_roles has a stdin keyword."""
        setup_path = REPO_ROOT / "setup.py"
        self.assertTrue(setup_path.is_file(), f"Expected setup.py to exist at {setup_path}")
        tree = ast.parse(setup_path.read_text(encoding="utf-8"), filename="setup.py")

        func_node: ast.FunctionDef | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_discover_hierarchy_roles":
                func_node = node
                break

        self.assertIsNotNone(func_node, "_discover_hierarchy_roles function not found in setup.py")
        offenders = find_subprocess_stdin_offenders(func_node, filename="setup.py")
        self.assertEqual(
            offenders,
            [],
            "Offenders in setup.py:_discover_hierarchy_roles:\n" + "\n".join(offenders),
        )

    def test_case_c_negative_check_finds_offender_and_allows_devnull(self) -> None:
        """(c) a negative check: the same helper reports an offender when given a small source string

        containing subprocess.run(["x"]) with no stdin, and reports none for
        subprocess.run(["x"], stdin=subprocess.DEVNULL).
        """
        bad_source = 'subprocess.run(["x"])'
        bad_offenders = find_subprocess_stdin_offenders(bad_source, filename="inline.py")
        self.assertEqual(bad_offenders, ["inline.py:1"])

        good_source = 'subprocess.run(["x"], stdin=subprocess.DEVNULL)'
        good_offenders = find_subprocess_stdin_offenders(good_source, filename="inline.py")
        self.assertEqual(good_offenders, [])

        # Also verify **kwargs and input keyword variations
        kwargs_source = 'subprocess.run(["x"], **kwargs)'
        self.assertEqual(find_subprocess_stdin_offenders(kwargs_source, filename="inline.py"), [])

        input_source = 'subprocess.run(["x"], input=b"test")'
        self.assertEqual(find_subprocess_stdin_offenders(input_source, filename="inline.py"), [])


if __name__ == "__main__":
    unittest.main()
