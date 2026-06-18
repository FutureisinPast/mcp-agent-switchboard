# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the self-contained, dual-mode agent-broker.exe.

Build:  pyinstaller --noconfirm agent-broker.spec   (or run build-release.ps1)

One onefile binary that both installs the broker (setup.py) and runs the MCP server
(agent_broker_mcp.py) via `agent-broker.exe serve`. The bridge VSIX and helper scripts
are embedded as data, so a GitHub user needs nothing but this exe — no Python, no
separate VSIX download.
"""

import os
import re
import glob

ROOT = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else SPECPATH)


def _vsix_version_key(path):
    """Match setup.py's _vsix_version_key: pick by (major, minor, patch), not lexically
    (lexical sort makes 0.9.0 > 0.10.0 and 0.4.9 > 0.4.15)."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", os.path.basename(path))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _datas():
    items = []
    # Newest bridge VSIX -> extensions/ so setup.latest_vsix() finds it in the bundle.
    vsixes = glob.glob(os.path.join(ROOT, "extensions", "**", "*.vsix"), recursive=True)
    if vsixes:
        newest = max(vsixes, key=_vsix_version_key)
        items.append((newest, os.path.join("extensions", "antigravity-agent-broker-bridge")))
    # Helper scripts + example config -> bundle root (find_asset checks BUNDLE_DIR).
    for name in (
        "enable-antigravity-debug-shortcuts.ps1",
        "start-antigravity-debug.ps1",
        "enable-vscode-debug-shortcuts.ps1",
        "install-vscode-automation.ps1",
        "detect-agent-hosts.ps1",
        "uninstall-agent-broker.ps1",
        "config.example.json",
        "README.md",
        "LICENSE",
    ):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            items.append((p, "."))
    return items


a = Analysis(
    ["agent_broker_entry.py"],
    pathex=[ROOT],
    binaries=[],
    datas=_datas(),
    hiddenimports=["agent_broker_mcp", "setup", "tiktoken", "tiktoken_ext", "tiktoken_ext.openai_public"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agent-broker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
