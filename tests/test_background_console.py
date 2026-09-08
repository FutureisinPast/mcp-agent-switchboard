"""Windows background-process lifecycle regressions."""
from __future__ import annotations

import ast
import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_entry as entry  # noqa: E402


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
            tree = ast.parse((REPO_ROOT / name).read_text(encoding="utf-8"))
            missing = []
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
            self.assertEqual(missing, [], f"{name} subprocess calls missing creationflags")


if __name__ == "__main__":
    unittest.main()
