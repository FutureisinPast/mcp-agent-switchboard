#!/usr/bin/env python3
"""Single entry point for the self-contained agent-switchboard.exe (PyInstaller).

The one binary is dual-mode so a GitHub user needs no Python at all:

  agent-switchboard.exe                 -> interactive install / uninstall menu (setup.py)
  agent-switchboard.exe install ...     -> setup subcommands (install/uninstall/status)
  agent-switchboard.exe uninstall ...   -> rollback everything this tool changed
  agent-switchboard.exe serve           -> run the MCP server over stdio (what agents launch)
  agent-switchboard.exe doctor [--json] -> read-only capability report for this machine
  agent-switchboard.exe bridge <args>   -> broker CLI used by the bridge extension
  agent-switchboard.exe --version       -> print the packaged release version
  agent-switchboard.exe routing-override -> register a package-specific gate override
  agent-switchboard.exe routing-report  -> summarize a session's routing lanes
  agent-switchboard.exe canary flash    -> live read-only Flash dispatch + receipt
  agent-switchboard.exe gate-harness    -> deterministic proof the gate enforces

`broker_command()` in setup.py registers `<this-exe> serve` with every host, so the
exact same binary that installs the broker is also the broker server.
"""

from __future__ import annotations

import os
import sys

SERVE_ALIASES = {"serve", "server", "mcp", "--serve", "stdio"}
VERSION_ALIASES = {"version", "--version", "-v"}
BACKGROUND_ALIASES = SERVE_ALIASES | {"bridge", "routing-hook"}


def _hide_private_console_for_background_mode(command: str) -> bool:
    """Hide a console created only for a frozen background broker process.

    A console-subsystem PyInstaller executable is required for MCP stdio, but a GUI
    host may launch it without Windows' hidden-process flag.  Hide only when this is
    a background command and the console is private to this process; never hide a
    user's existing PowerShell/cmd window shared with the broker.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False) or command not in BACKGROUND_ALIASES:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        process_ids = (ctypes.c_ulong * 2)()
        attached = int(kernel32.GetConsoleProcessList(process_ids, len(process_ids)))
        if attached > 1:
            return False
        return bool(ctypes.windll.user32.ShowWindow(hwnd, 0))
    except Exception:  # noqa: BLE001
        return False


def run() -> int:
    first = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    _hide_private_console_for_background_mode(first)
    if first in VERSION_ALIASES:
        from switchboard_version import BROKER_VERSION
        print(f"Agent Switchboard {BROKER_VERSION}")
        return 0
    if first == "routing-override":
        import routing_gate
        return routing_gate.routing_override_cli(sys.argv[2:])
    if first == "routing-report":
        import routing_gate
        return routing_gate.routing_report_cli(sys.argv[2:])
    if first == "routing-hook":
        import routing_gate
        return routing_gate.main(sys.argv[2:])
    if first == "canary":
        import switchboard_canary
        return switchboard_canary.main(sys.argv[2:])
    if first == "gate-harness":
        import switchboard_canary
        return switchboard_canary.gate_harness(sys.argv[2:])
    if first in SERVE_ALIASES:
        import setup
        if os.environ.get("AGENT_BROKER_DIAGNOSTIC_PROBE") != "1":
            try:
                hierarchy = setup.refresh_hierarchy(silent=True)
                errors = [f"{name}: {result}" for name, result in hierarchy.items() if result.startswith("ERROR")]
                if errors:
                    print("Agent Switchboard hierarchy refresh: " + "; ".join(errors), file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"Agent Switchboard hierarchy refresh failed open: {exc}", file=sys.stderr)
        import agent_broker_mcp as broker
        # Enter the MCP stdio loop: the server keys off argv, so present it with none.
        sys.argv = [sys.argv[0]]
        return broker.main()
    if first in ("doctor", "debate"):
        import agent_broker_mcp as broker
        # Map `agent-broker doctor|debate [args]` onto the broker's `bridge` path.
        sys.argv = [sys.argv[0], "bridge", *sys.argv[1:]]
        return broker.main()
    if first == "bridge":
        import agent_broker_mcp as broker
        # broker.main() dispatches "bridge <subcommand>" from sys.argv as-is.
        return broker.main()
    import setup
    return setup.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(run())
