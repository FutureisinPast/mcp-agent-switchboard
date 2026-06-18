#!/usr/bin/env python3
"""Agent Broker — one-run installer / uninstaller.

Detects which agents you have (Codex, Claude, Antigravity, VS Code), registers the
broker's MCP server with each, installs the bridge extension, and (optionally) sets
up Antigravity's debug port. Run with no arguments for an interactive menu, or use
the subcommands for automation/testing:

    python setup.py status                 # show what was detected + current state
    python setup.py install   [--dry-run] [--debug-port]
    python setup.py uninstall [--dry-run] [--remove-data]

Two supported install paths, both with a built-in uninstall/rollback:
  - Python 3.10+: `python setup.py install` (agents run `python agent_broker_mcp.py`).
  - Self-contained `agent-broker.exe` (PyInstaller, no Python needed): the same exe is
    dual-mode — run it for the install/uninstall menu, and agents run `agent-broker.exe
    serve` as the MCP server. The bridge VSIX is embedded and installed automatically.

Everything is idempotent and every edited file is backed up first under
%USERPROFILE%/.agent-broker/setup-backups/<timestamp>/. Uninstall reverses MCP
registration in all four hosts and removes the bridge extension.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- locations -------------------------------------------------------------
HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
BROKER_HOME = Path(os.environ.get("AGENT_BROKER_HOME", HOME / ".agent-broker"))
FROZEN = bool(getattr(sys, "frozen", False))
# Source installer mode. The public install path is the PowerShell wrapper
# calling this Python setup script, OR the self-contained agent-broker.exe.
SETUP_DIR = Path(__file__).resolve().parent
# When frozen by PyInstaller, bundled data files (the VSIX, helper scripts,
# config.example.json) are extracted to sys._MEIPASS at runtime.
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SETUP_DIR))
SERVER_NAME = "agent_broker_mcp.py"


def find_asset(rel: str) -> Path | None:
    """Locate a bundled/source asset by relative path, preferring the frozen
    bundle, then the source tree, then the installed broker home."""
    for base in (BUNDLE_DIR, SETUP_DIR, BROKER_HOME):
        candidate = base / rel
        if candidate.exists():
            return candidate
    return None

CODEX_TOML = HOME / ".codex" / "config.toml"
CLAUDE_JSON = HOME / ".claude.json"
ANTIGRAVITY_USER_DIRS = [
    APPDATA / "Antigravity IDE" / "User",
    APPDATA / "Antigravity" / "User",
]
ANTIGRAVITY_MCP_CANDIDATES = [p / "mcp_config.json" for p in ANTIGRAVITY_USER_DIRS]
VSCODE_MCP = APPDATA / "Code" / "User" / "mcp.json"

# MCP server key names per host (Codex uses an underscore; the rest use a hyphen).
CODEX_KEY = "agent_broker"
MCP_KEY = "agent-broker"

_backup_root: Path | None = None


# --- small utilities -------------------------------------------------------
def info(msg: str) -> None:
    print(f"  {msg}")


def head(msg: str) -> None:
    print(f"\n=== {msg} ===")


def backup_dir() -> Path:
    global _backup_root
    if _backup_root is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        _backup_root = BROKER_HOME / "setup-backups" / stamp
        _backup_root.mkdir(parents=True, exist_ok=True)
    return _backup_root


def backup_file(path: Path) -> None:
    if path.exists():
        dest = backup_dir() / path.name
        i = 1
        while dest.exists():
            dest = backup_dir() / f"{path.stem}.{i}{path.suffix}"
            i += 1
        shutil.copy2(path, dest)
        info(f"backed up {path} -> {dest}")


def python_command() -> str:
    """A python launcher that will exist at runtime (not the bundled exe)."""
    return shutil.which("python") or shutil.which("py") or (
        sys.executable if sys.executable.lower().endswith("python.exe") else "python"
    )


def server_path() -> Path:
    """Where the MCP server .py lives — alongside this file, else in BROKER_HOME."""
    found = find_asset(SERVER_NAME)
    return found if found else BROKER_HOME / SERVER_NAME


def frozen_broker_exe() -> Path:
    """Stable home for the self-contained exe, so the registered command keeps working
    even if the user deletes the downloaded file."""
    return BROKER_HOME / "agent-broker.exe"


def install_self_if_frozen(dry: bool) -> str | None:
    """Copy the running self-contained exe into BROKER_HOME so agents launch a durable
    path. No-op outside frozen mode."""
    if not FROZEN:
        return None
    dest = frozen_broker_exe()
    if dry:
        return f"would copy exe -> {dest}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = Path(sys.executable).resolve()
        if src == dest.resolve():
            return f"already installed at {dest}"
        # Stage then atomically replace. os.replace can overwrite a target held open by
        # readers where copy-over-open can fail, and is atomic. If it's locked by a
        # running `agent-broker.exe serve`, report a hard error so do_install aborts
        # rather than silently leaving the stale exe registered.
        tmp = dest.with_name(dest.stem + ".new" + dest.suffix)
        shutil.copy2(src, tmp)
        try:
            os.replace(tmp, dest)
        except OSError:
            return (
                f"ERROR: {dest} is in use (a running 'agent-broker.exe serve'?). "
                f"Close any Codex/Claude sessions using the broker and re-run. "
                f"Staged the new exe at {tmp}."
            )
        return f"installed exe -> {dest}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR copying exe: {exc}"


def broker_command() -> tuple[str, list[str]]:
    """The (command, args) the agents should run to start the broker.

    - Frozen (self-contained agent-broker.exe): the same exe is dual-mode, so agents
      run `<exe> serve` to start the MCP server — no Python required at runtime. Prefer
      the durable copy in BROKER_HOME once installed.
    - Source/Python mode: agents run `python agent_broker_mcp.py`.
    """
    if FROZEN:
        exe = frozen_broker_exe()
        return (str(exe) if exe.exists() else sys.executable), ["serve"]
    return python_command(), [str(server_path())]


def which(name: str) -> str | None:
    return shutil.which(name) or shutil.which(name + ".cmd") or shutil.which(name + ".exe")


def running_ide_windows() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return []
    script = r"""
$names = @("Antigravity IDE", "Antigravity", "Code")
Get-Process -ErrorAction SilentlyContinue |
  Where-Object { ($names -contains $_.ProcessName) -and ($_.MainWindowHandle -ne 0) } |
  ForEach-Object { "{0}|{1}|{2}" -f $_.ProcessName, $_.Id, $_.MainWindowTitle }
""".strip()
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return []
    windows = []
    for line in proc.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            label = "VS Code" if parts[0] == "Code" else "Antigravity"
            windows.append({"app": label, "process": parts[0], "pid": parts[1], "title": parts[2]})
    return windows


def require_ide_windows_closed(action: str, dry: bool) -> bool:
    if dry:
        return True
    windows = running_ide_windows()
    if not windows:
        return True
    head("Close apps first")
    print(f"  Agent Broker cannot {action} while Antigravity or VS Code windows are open.")
    print("  Close these windows, then run setup again:")
    for window in windows:
        title = f" - {window['title']}" if window.get("title") else ""
        print(f"  - {window['app']} pid={window['pid']}{title}")
    print("\n  Nothing was changed.")
    return False


def existing_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        p = Path(path)
        return str(p) if p.exists() else None
    except OSError:
        return None


def broker_config() -> dict:
    cfg_path = BROKER_HOME / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def antigravity_cli() -> str | None:
    # Antigravity renamed its install folder/CLI to "Antigravity IDE". Prefer
    # the new product path over stale PATH/config entries when both exist.
    new_install = [
        LOCALAPPDATA / "Programs" / "Antigravity IDE" / "bin" / "antigravity-ide.cmd",
        LOCALAPPDATA / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe",
    ]
    for candidate in new_install:
        resolved = existing_file(candidate)
        if resolved:
            return resolved

    cfg = broker_config()
    for raw in (os.environ.get("ANTIGRAVITY_PATH"), cfg.get("antigravity_path")):
        resolved = existing_file(raw)
        if resolved:
            return resolved

    old_install = [
        LOCALAPPDATA / "Programs" / "Antigravity" / "bin" / "antigravity.cmd",
        LOCALAPPDATA / "Programs" / "Antigravity" / "Antigravity.exe",
    ]
    for candidate in old_install:
        resolved = existing_file(candidate)
        if resolved:
            return resolved

    return (
        which("antigravity-ide")
        or which("antigravity-ide.cmd")
        or which("antigravity")
        or which("antigravity.cmd")
    )


def vscode_cli() -> str | None:
    cfg = broker_config()
    for raw in (os.environ.get("VSCODE_PATH"), cfg.get("vscode_path")):
        resolved = existing_file(raw)
        if resolved:
            return resolved
    for candidate in (
        LOCALAPPDATA / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
        LOCALAPPDATA / "Programs" / "Microsoft VS Code" / "Code.exe",
    ):
        resolved = existing_file(candidate)
        if resolved:
            return resolved
    return which("code")


def antigravity_user_dir() -> Path:
    for path in ANTIGRAVITY_USER_DIRS:
        if path.exists():
            return path
    return ANTIGRAVITY_USER_DIRS[0]


def antigravity_mcp_path() -> Path:
    return antigravity_user_dir() / "mcp_config.json"


def antigravity_schema() -> str | None:
    for base in ("Antigravity IDE", "Antigravity"):
        schema = (
            LOCALAPPDATA
            / "Programs"
            / base
            / "resources"
            / "app"
            / "extensions"
            / "antigravity"
            / "schemas"
            / "mcp_config.schema.json"
        )
        if schema.exists():
            return str(schema)
    return None


# --- detection -------------------------------------------------------------
def detect() -> dict[str, dict]:
    hosts = {
        "codex": {
            "label": "Codex",
            "cli": which("codex"),
            "config": CODEX_TOML if CODEX_TOML.exists() else None,
        },
        "claude": {
            "label": "Claude Code",
            "cli": which("claude"),
            "config": CLAUDE_JSON if CLAUDE_JSON.exists() else None,
        },
        "antigravity": {
            "label": "Antigravity IDE",
            "cli": antigravity_cli(),
            "config": antigravity_mcp_path() if antigravity_user_dir().exists() else None,
        },
        "vscode": {
            "label": "VS Code",
            "cli": vscode_cli(),
            "config": VSCODE_MCP if (APPDATA / "Code" / "User").exists() else None,
        },
    }
    for h in hosts.values():
        h["present"] = bool(h["cli"] or h["config"])
    return hosts


# --- MCP registration writers (idempotent, backed up) ----------------------
def _mcp_block(caller: str, command: str, args: list[str]) -> dict:
    return {"type": "stdio", "command": command, "args": list(args), "env": {"AGENT_BROKER_CALLER": caller}}


def register_codex(command: str, args: list[str], dry: bool) -> str:
    if not (CODEX_TOML.exists() or which("codex")):
        return "skipped (not installed)"
    # json.dumps yields a valid TOML basic string (backslashes escaped). Plain
    # f-string interpolation of a Windows path produces invalid TOML escapes (\U, \P).
    args_toml = ", ".join(json.dumps(a) for a in args)
    block = (
        f"\n[mcp_servers.{CODEX_KEY}]\n"
        f"command = {json.dumps(command)}\n"
        f"args = [{args_toml}]\n\n"
        f"[mcp_servers.{CODEX_KEY}.env]\n"
        f"AGENT_BROKER_CALLER = \"codex\"\n"
    )
    if dry:
        return "would write [mcp_servers.agent_broker] to ~/.codex/config.toml"
    CODEX_TOML.parent.mkdir(parents=True, exist_ok=True)
    text = CODEX_TOML.read_text(encoding="utf-8") if CODEX_TOML.exists() else ""
    backup_file(CODEX_TOML)
    text = _remove_toml_sections(text, [f"mcp_servers.{CODEX_KEY}", f"mcp_servers.{CODEX_KEY}.env"])
    text = text.rstrip() + "\n" + block
    CODEX_TOML.write_text(text, encoding="utf-8")
    return "registered"


def _remove_toml_sections(text: str, sections: list[str]) -> str:
    """Drop the named [section] tables (header line through the line before the next
    table header). Tolerant of array values that contain '[' mid-line."""
    targets = {f"[{s}]" for s in sections}
    out, skipping = [], False
    for line in text.splitlines():
        stripped = line.strip()
        is_header = stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped
        if is_header:
            skipping = stripped in targets
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _register_json(path: Path, caller: str, command: str, args: list[str], dry: bool, ensure_schema=None) -> str:
    if dry:
        return f"would set mcpServers['{MCP_KEY}'] in {path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {path.name} is not valid JSON ({exc}); left untouched"
    backup_file(path)
    if ensure_schema and "$schema" not in data:
        data["$schema"] = ensure_schema
    servers = data.setdefault("mcpServers", {})
    servers[MCP_KEY] = _mcp_block(caller, command, args)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "registered"


def register_claude(command: str, args: list[str], dry: bool) -> str:
    if not (CLAUDE_JSON.exists() or which("claude")):
        return "skipped (not installed)"
    if not CLAUDE_JSON.exists() and not dry:
        # don't fabricate a fresh ~/.claude.json; let Claude create it on first run
        return "skipped (~/.claude.json not present yet — run Claude once, then re-install)"
    return _register_json(CLAUDE_JSON, "claude", command, args, dry)


def register_antigravity(command: str, args: list[str], dry: bool) -> str:
    if not antigravity_user_dir().exists() and not antigravity_cli():
        return "skipped (not installed)"
    schema = antigravity_schema()
    return _register_json(
        antigravity_mcp_path(),
        "antigravity",
        command,
        args,
        dry,
        ensure_schema=schema,
    )


def register_vscode(command: str, args: list[str], dry: bool) -> str:
    if not (APPDATA / "Code" / "User").exists() and not vscode_cli():
        return "skipped (not installed)"
    if dry:
        return f"would set servers['{MCP_KEY}'] in {VSCODE_MCP.name}"
    VSCODE_MCP.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if VSCODE_MCP.exists():
        try:
            data = json.loads(VSCODE_MCP.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {VSCODE_MCP.name} invalid JSON ({exc}); left untouched"
    backup_file(VSCODE_MCP)
    servers = data.setdefault("servers", {})
    servers[MCP_KEY] = {"type": "stdio", "command": command, "args": list(args),
                        "env": {"AGENT_BROKER_CALLER": "vscode"}}
    VSCODE_MCP.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "registered"


# --- unregister ------------------------------------------------------------
def unregister_codex(dry: bool) -> str:
    if not CODEX_TOML.exists():
        return "nothing to remove"
    if dry:
        return "would remove [mcp_servers.agent_broker] from config.toml"
    backup_file(CODEX_TOML)
    text = _remove_toml_sections(CODEX_TOML.read_text(encoding="utf-8"),
                                 [f"mcp_servers.{CODEX_KEY}", f"mcp_servers.{CODEX_KEY}.env"])
    CODEX_TOML.write_text(text.rstrip() + "\n", encoding="utf-8")
    return "removed"


def _unregister_json(path: Path, key_parent: str, dry: bool) -> str:
    if not path.exists():
        return "nothing to remove"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "skipped (invalid JSON)"
    if MCP_KEY not in (data.get(key_parent) or {}):
        return "nothing to remove"
    if dry:
        return f"would remove {key_parent}['{MCP_KEY}'] from {path.name}"
    backup_file(path)
    data[key_parent].pop(MCP_KEY, None)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "removed"


def unregister_antigravity(dry: bool) -> str:
    paths = [p for p in ANTIGRAVITY_MCP_CANDIDATES if p.exists()]
    if not paths:
        return "nothing to remove"
    results = []
    for path in paths:
        label = path.parent.parent.name
        results.append(f"{label}: {_unregister_json(path, 'mcpServers', dry)}")
    return "; ".join(results)


# --- config.json -----------------------------------------------------------
def write_config(dry: bool) -> str:
    cfg_path = BROKER_HOME / "config.json"
    desired = {
        "codex_path": which("codex") or "",
        "antigravity_path": antigravity_cli() or "",
        "vscode_path": vscode_cli() or "",
        "claude_path": which("claude") or "",
        "claude_model": "sonnet",
        "gemini_model": "gemini-2.5-pro",
        "app_autopaste": False,
        "app_autosubmit": False,
        "antigravity_cdp_port": 9000,
        "vscode_cdp_port": 9010,
        "antigravity_cdp_autoselect": True,
    }
    if dry:
        return f"would write {cfg_path}"
    BROKER_HOME.mkdir(parents=True, exist_ok=True)
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = {}
        backup_file(cfg_path)
    # Only fill in MISSING keys — never overwrite a value the user/GPT already set.
    for k, v in desired.items():
        existing.setdefault(k, v)
    # Fill the executable paths only if currently empty/missing (don't clobber a working path).
    if not existing.get("codex_path"):
        existing["codex_path"] = which("codex") or ""
    resolved_antigravity = antigravity_cli() or ""
    current_antigravity = str(existing.get("antigravity_path") or "")
    if (
        resolved_antigravity
        and (
            not current_antigravity
            or not Path(current_antigravity).exists()
            or ("Antigravity IDE" in resolved_antigravity and "Antigravity IDE" not in current_antigravity)
        )
    ):
        existing["antigravity_path"] = resolved_antigravity
    resolved_vscode = vscode_cli() or ""
    current_vscode = str(existing.get("vscode_path") or "")
    if resolved_vscode and (not current_vscode or not Path(current_vscode).exists()):
        existing["vscode_path"] = resolved_vscode
    if not existing.get("claude_path"):
        existing["claude_path"] = which("claude") or ""
    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return "written"


# --- bridge extension ------------------------------------------------------
def _vsix_version_key(p: Path) -> tuple:
    """Sort key from a trailing semver in the filename (e.g. ...-0.5.0.vsix),
    falling back to mtime so a build without a version still resolves."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", p.stem)
    version = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (version, mtime)


def latest_vsix() -> Path | None:
    """Find the newest bridge VSIX. Searches the frozen bundle, the source tree, and
    BROKER_HOME RECURSIVELY (the VSIX lives in extensions/antigravity-agent-broker-bridge/),
    and picks the highest version — the old non-recursive top-level glob missed it."""
    found: dict[Path, Path] = {}
    for base in (BUNDLE_DIR, SETUP_DIR, BROKER_HOME):
        ext_dir = base / "extensions"
        if not ext_dir.exists():
            continue
        for p in ext_dir.rglob("*.vsix"):
            found[p.resolve()] = p
    if not found:
        return None
    return sorted(found.values(), key=_vsix_version_key)[-1]


BRIDGE_EXTENSION_ID = "futureisinpast.antigravity-agent-broker-bridge"


def _host_cli(host_cli: str) -> str | None:
    if host_cli == "antigravity":
        return antigravity_cli()
    if host_cli == "code":
        return vscode_cli()
    return which(host_cli)


def durable_vsix() -> Path | None:
    """Return a VSIX path that outlives this process. When frozen, the bundled VSIX
    lives in the temp _MEIPASS dir, so copy it into BROKER_HOME/extensions first so
    re-install/uninstall and later inspection still work."""
    vsix = latest_vsix()
    if not vsix:
        return None
    try:
        is_temp = str(vsix).startswith(str(BUNDLE_DIR)) and FROZEN
    except Exception:  # noqa: BLE001
        is_temp = FROZEN
    if not is_temp:
        return vsix
    dest_dir = BROKER_HOME / "extensions"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / vsix.name
    try:
        shutil.copy2(vsix, dest)
        return dest
    except Exception:  # noqa: BLE001
        return vsix


def install_bridge(host_cli: str, dry: bool) -> str:
    cli = _host_cli(host_cli)
    vsix = durable_vsix()
    if not cli:
        return f"skipped ({host_cli} CLI not found)"
    if not vsix:
        return "skipped (no .vsix found)"
    if dry:
        return f"would run {cli} --install-extension {vsix.name}"
    try:
        subprocess.run([cli, "--install-extension", str(vsix), "--force"],
                       capture_output=True, text=True, timeout=120, check=False)
        return f"installed {vsix.name}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def uninstall_bridge(host_cli: str, dry: bool) -> str:
    cli = _host_cli(host_cli)
    if not cli:
        return f"skipped ({host_cli} CLI not found)"
    if dry:
        return f"would run {cli} --uninstall-extension {BRIDGE_EXTENSION_ID}"
    try:
        proc = subprocess.run([cli, "--uninstall-extension", BRIDGE_EXTENSION_ID],
                              capture_output=True, text=True, timeout=120, check=False)
        out = (proc.stdout + proc.stderr).lower()
        if "not installed" in out or "is not installed" in out:
            return "not installed"
        return f"uninstalled {BRIDGE_EXTENSION_ID}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


# --- high-level actions ----------------------------------------------------
def do_status() -> None:
    head("Detected agents")
    for key, h in detect().items():
        mark = "[x]" if h["present"] else "[ ]"
        cli = h["cli"] or "-"
        cfg = "config found" if h["config"] else "no config"
        print(f"  {mark} {h['label']:<12} cli={cli!s:<8} {cfg}")
    head("Broker")
    cmd, cargs = broker_command()
    info(f"command: {' '.join([cmd, *cargs])}")
    info("mode:    " + ("self-contained exe (serve)" if FROZEN else "python script"))
    info(f"home:    {BROKER_HOME}")
    vsix = latest_vsix()
    info(f"bridge:  {vsix.name if vsix else 'no .vsix found (build it: see build-release.ps1)'}")


def do_install(dry: bool, debug_port: bool) -> bool:
    head("Install" + (" (dry-run)" if dry else ""))
    if not require_ide_windows_closed("install or repair", dry):
        return False
    # In source/Python mode the agents run the .py, so it must exist. When frozen, the
    # exe serves itself (`<exe> serve`), so there is no .py to find.
    if not FROZEN and not server_path().exists():
        info(f"ERROR: {SERVER_NAME} was not found next to setup or in {BROKER_HOME}. Aborting.")
        return False
    self_install = install_self_if_frozen(dry)
    if self_install and self_install.startswith("ERROR") and not dry:
        info(self_install)
        info("Aborting: the broker exe could not be installed. Close any running agents "
             "(Codex/Claude) using the broker, then re-run. Nothing else was changed.")
        return False
    command, cargs = broker_command()
    info("broker: " + " ".join([command, *cargs]) + (" (self-contained exe)" if FROZEN else ""))
    print()
    results = {}
    if self_install:
        results["Broker exe"] = self_install
    results.update({
        "Codex MCP": register_codex(command, cargs, dry),
        "Claude MCP": register_claude(command, cargs, dry),
        "Antigravity MCP": register_antigravity(command, cargs, dry),
        "VS Code MCP": register_vscode(command, cargs, dry),
        "Antigravity bridge": install_bridge("antigravity", dry),
        "VS Code bridge": install_bridge("code", dry),
        "config.json": write_config(dry),
    })
    if debug_port:
        results["Antigravity debug port"] = setup_debug_port(dry)
    for k, v in results.items():
        print(f"  {k:<22} : {v}")
    if not dry:
        print("\n  Done. Restart the agents you use so they pick up the broker.")
    return True


def do_uninstall(dry: bool, remove_data: bool) -> bool:
    head("Uninstall" + (" (dry-run)" if dry else ""))
    if not require_ide_windows_closed("uninstall", dry):
        return False
    results = {
        "Codex MCP": unregister_codex(dry),
        "Claude MCP": _unregister_json(CLAUDE_JSON, "mcpServers", dry),
        "Antigravity MCP": unregister_antigravity(dry),
        "VS Code MCP": _unregister_json(VSCODE_MCP, "servers", dry),
        "Antigravity bridge": uninstall_bridge("antigravity", dry),
        "VS Code bridge": uninstall_bridge("code", dry),
    }
    # Roll back the durable self-contained exe copy (best effort; can't delete the
    # currently-running exe on Windows, so note that case).
    exe = frozen_broker_exe()
    if exe.exists():
        if dry:
            results["Broker exe"] = f"would remove {exe}"
        else:
            try:
                running = Path(sys.executable).resolve() == exe.resolve()
            except Exception:  # noqa: BLE001
                running = False
            if running:
                results["Broker exe"] = f"left in place (in use): {exe}"
            else:
                try:
                    exe.unlink()
                    results["Broker exe"] = f"removed {exe}"
                except Exception as ex:  # noqa: BLE001
                    results["Broker exe"] = f"ERROR: {ex}"
    if remove_data and not dry:
        moved = BROKER_HOME.with_name(".agent-broker-removed-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.move(str(BROKER_HOME), str(moved))
            results["data"] = f"moved to {moved}"
        except Exception as exc:  # noqa: BLE001
            results["data"] = f"ERROR: {exc}"
    elif remove_data:
        results["data"] = f"would move {BROKER_HOME} aside"
    else:
        results["data"] = f"kept ({BROKER_HOME})"
    for k, v in results.items():
        print(f"  {k:<18} : {v}")
    info("Note: reopen Antigravity/VS Code so the removed bridge extension unloads.")
    info("To strip the Antigravity --remote-debugging-port flag, run uninstall-agent-broker.ps1")
    info("(it restores the patched shortcuts).")
    return True


def setup_debug_port(dry: bool) -> str:
    script = find_asset("enable-antigravity-debug-shortcuts.ps1")
    if not script:
        return "skipped (helper script missing)"
    if dry:
        return "would patch Antigravity shortcuts with --remote-debugging-port=9000"
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return "skipped (PowerShell not found)"
    try:
        subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                       capture_output=True, text=True, timeout=60, check=False)
        return "patched shortcuts (port 9000)"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


# --- interactive menu ------------------------------------------------------
def menu() -> int:
    while True:
        print("\n" + "=" * 44)
        print("  Agent Broker - setup")
        print("=" * 44)
        print("  1) Status (what's detected)")
        print("  2) Install / repair")
        print("  3) Install + enable Antigravity debug port")
        print("  4) Uninstall (keep my data)")
        print("  5) Uninstall + remove all data")
        print("  6) Preview install (dry-run)")
        print("  0) Quit")
        try:
            choice = input("\n  Choose: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "1":
            do_status()
        elif choice == "2":
            do_install(dry=False, debug_port=False)
        elif choice == "3":
            do_install(dry=False, debug_port=True)
        elif choice == "4":
            do_uninstall(dry=False, remove_data=False)
        elif choice == "5":
            if input("  Type REMOVE to confirm deleting broker data: ").strip() == "REMOVE":
                do_uninstall(dry=False, remove_data=True)
            else:
                info("cancelled")
        elif choice == "6":
            do_install(dry=True, debug_port=False)
        elif choice == "0":
            return 0
        else:
            info("unknown choice")


def main(argv: list[str]) -> int:
    if not argv:
        return menu()
    cmd = argv[0]
    flags = set(argv[1:])
    dry = "--dry-run" in flags
    if cmd == "status":
        do_status()
    elif cmd == "install":
        return 0 if do_install(dry=dry, debug_port="--debug-port" in flags) else 1
    elif cmd == "uninstall":
        return 0 if do_uninstall(dry=dry, remove_data="--remove-data" in flags) else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        print(
            f"Agent Broker setup requires Python 3.10+ (found {sys.version.split()[0]}). "
            "Install a newer Python and re-run."
        )
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1:]))
