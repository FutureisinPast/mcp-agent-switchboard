#!/usr/bin/env python3
"""Single entry point for the self-contained agent-broker.exe (PyInstaller).

The one binary is dual-mode so a GitHub user needs no Python at all:

  agent-broker.exe                 -> interactive install / uninstall menu (setup.py)
  agent-broker.exe install ...     -> setup subcommands (install/uninstall/status)
  agent-broker.exe uninstall ...   -> rollback everything this tool changed
  agent-broker.exe serve           -> run the MCP server over stdio (what agents launch)
  agent-broker.exe bridge <args>   -> broker CLI used by the bridge extension

`broker_command()` in setup.py registers `<this-exe> serve` with every host, so the
exact same binary that installs the broker is also the broker server.
"""

from __future__ import annotations

import sys

SERVE_ALIASES = {"serve", "server", "mcp", "--serve", "stdio"}


def run() -> int:
    first = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if first in SERVE_ALIASES:
        import agent_broker_mcp as broker
        # Enter the MCP stdio loop: the server keys off argv, so present it with none.
        sys.argv = [sys.argv[0]]
        return broker.main()
    if first == "bridge":
        import agent_broker_mcp as broker
        # broker.main() dispatches "bridge <subcommand>" from sys.argv as-is.
        return broker.main()
    import setup
    return setup.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(run())
