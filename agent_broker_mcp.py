#!/usr/bin/env python3
"""
Local MCP broker for cross-agent consultation.

This server is intentionally dependency-free. It speaks the small MCP surface
needed by Codex, Claude Code, and Antigravity over stdio JSON-RPC.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BROKER_DIR = Path(os.environ.get("AGENT_BROKER_HOME", Path.home() / ".agent-broker"))
DB_PATH = BROKER_DIR / "state.sqlite"
LOG_PATH = BROKER_DIR / "agent-broker.log"
CONFIG_PATH = BROKER_DIR / "config.json"

# Tracks broker releases (surfaced via MCP serverInfo); may differ from the bridge
# package.json version when a change is broker-only (e.g. the request ledger / return path).
BROKER_VERSION = "0.5.0"

# How long to wait for the SQLite write lock before raising "database is locked".
DB_TIMEOUT_SECONDS = 30


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to default instead of crashing on import."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


DEFAULT_TIMEOUT_SECONDS = _env_int("AGENT_BROKER_TIMEOUT_SECONDS", 600)
DEFAULT_CONTEXT_BUDGET = _env_int("AGENT_BROKER_CONTEXT_BUDGET", 8000)
SHARED_CONTEXT_THRESHOLD_CHARS = _env_int("AGENT_BROKER_CONTEXT_THRESHOLD_CHARS", 1200)
SHARED_CONTEXT_INLINE_CHARS = _env_int("AGENT_BROKER_CONTEXT_INLINE_CHARS", 700)
# A routed handoff prompt over this many tokens trips a token-economy nudge: the broker
# stashes the full prompt as a context_ref and warns the caller to send a short
# instruction + ref instead of inlining context the receiver can read itself.
PROMPT_SOFT_LIMIT_TOKENS = _env_int("AGENT_BROKER_PROMPT_SOFT_LIMIT_TOKENS", 600)

SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "credentials.json",
    ".credentials.json",
    "id_rsa",
    "id_ed25519",
}

SECRET_WORDS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
}

MODEL_ALIASES = {
    "gemini flash": "Gemini 3.5 Flash (High)",
    "gemini flash high": "Gemini 3.5 Flash (High)",
    "gemini 3.5 flash high": "Gemini 3.5 Flash (High)",
    "gemini 3.5 high": "Gemini 3.5 Flash (High)",
    "flash 3.5 high": "Gemini 3.5 Flash (High)",
    "opus": "Claude Opus 4.6 (Thinking)",
    "opus 4.6": "Claude Opus 4.6 (Thinking)",
    "claude opus": "Claude Opus 4.6 (Thinking)",
    "claude opus 4.6": "Claude Opus 4.6 (Thinking)",
    "sonnet": "Claude Sonnet 4.6 (Thinking)",
    "sonnet 4.6": "Claude Sonnet 4.6 (Thinking)",
    "claude sonnet": "Claude Sonnet 4.6 (Thinking)",
    "claude sonnet 4.6": "Claude Sonnet 4.6 (Thinking)",
    "gpt 5.5": "gpt-5.5",
    "gpt-5.5": "gpt-5.5",
}

GENERIC_MODEL_REQUESTS = {
    "",
    "default",
    "current",
    "current selected model",
    "codex",
    "gpt",
    "openai",
    "claude",
    "anthropic",
    "gemini",
    "antigravity",
}

STATIC_ANTIGRAVITY_MODELS = [
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
]

STATIC_CLAUDE_MODELS = [
    {
        "id": "opus",
        "display": "Claude alias: opus (runs whatever Opus the installed Claude CLI maps 'opus' to)",
        "aliases": [
            "claude opus", "opus",
            "opus 4.8", "opus4.8", "claude opus 4.8",
            "opus 4.6", "opus 4.5", "opus 4.1",
        ],
    },
    {
        "id": "sonnet",
        "display": "Claude alias: sonnet (latest available Sonnet)",
        "aliases": [
            "claude sonnet", "sonnet",
            "sonnet 4.6", "sonnet 4.5", "claude sonnet 4.6", "claude sonnet 4.8",
        ],
    },
    {
        "id": "haiku",
        "display": "Claude alias: haiku (latest available Haiku)",
        "aliases": ["claude haiku", "haiku", "haiku 4.5"],
    },
]

STATIC_CODEX_MODELS = [
    {
        "id": "gpt-5.5",
        "display": "GPT-5.5",
        "aliases": ["gpt-5.5", "GPT-5.5", "gpt 5.5"],
    },
    {
        "id": "gpt-5.4",
        "display": "GPT-5.4",
        "aliases": ["gpt-5.4", "GPT-5.4", "gpt 5.4"],
    },
    {
        "id": "gpt-5.4-mini",
        "display": "GPT-5.4-Mini",
        "aliases": ["gpt-5.4-mini", "GPT-5.4-Mini", "gpt 5.4 mini"],
    },
    {
        "id": "codex-auto-review",
        "display": "Codex Auto Review",
        "aliases": ["codex-auto-review", "Codex Auto Review"],
    },
]

WINDOWS_APP_NAME_PATTERNS = {
    "codex": ("Codex", "OpenAI", "ChatGPT"),
    "claude": ("Claude",),
    "antigravity": ("Antigravity IDE", "Antigravity"),
    "vscode": ("Visual Studio Code", "VS Code"),
}

WINDOWS_APP_ID_HINTS = {
    "codex": ("OpenAI.Codex", "Codex"),
    "claude": ("Claude_", "Claude"),
    "antigravity": ("Google.Antigravity", "Antigravity IDE", "Antigravity"),
    "vscode": ("Microsoft.VisualStudioCode", "Code"),
}

# Exact Get-Process names (case-insensitive) used to detect a running instance
# before launching, so the broker focuses an open app instead of spawning a duplicate.
WINDOWS_APP_PROCESS_NAMES = {
    "codex": ("Codex", "codex"),
    "claude": ("claude", "Claude"),
    "antigravity": ("Antigravity IDE", "Antigravity"),
    "vscode": ("Code",),
}

IDE_HOSTS = {"antigravity", "vscode", "vs_code", "code"}

TASK_BUDGETS = {
    "quick_check": 1200,
    "implementation_plan": 5000,
    "co_audit": 3500,
    "debate": 4500,
    "argue": 3500,
    "implementation": 6500,
    "review": 3000,
    "bug_hunt": 3500,
    "sanity_check": 1800,
    "consult": 2500,
}

TASK_CONTRACTS = {
    "quick_check": [
        "Return the answer in at most 8 bullets.",
        "Do not read broad files or restate the context pack unless needed.",
        "Flag uncertainty instead of expanding scope.",
    ],
    "implementation_plan": [
        "Produce an exact implementation plan, not code edits.",
        "Use numbered steps with target files, functions, required checks, and rollback/risks.",
        "Do not invent architecture beyond the request.",
        "If handing to a weaker/cheaper model, make the plan deterministic: include acceptance criteria and forbidden changes.",
        "Do not continue if critical context is missing; ask one concise blocking question.",
    ],
    "implementation": [
        "Implement only the requested change.",
        "Follow the approved plan and acceptance criteria as binding constraints.",
        "Do not redesign, reorder, expand scope, or substitute architecture.",
        "Do not refactor unrelated code or change behavior outside scope.",
        "If any plan step is impossible or ambiguous, stop and report the blocker instead of improvising.",
        "Report files changed, checks run, and remaining risks.",
    ],
    "co_audit": [
        "Audit for bugs, missed edge cases, bad assumptions, and missing tests.",
        "Findings first, ordered by severity, with evidence.",
        "Do not rewrite the solution unless asked.",
        "Keep the audit bounded to the provided topic/context.",
    ],
    "debate": [
        "Argue the strongest technical case for and against the proposal.",
        "Separate facts, assumptions, and opinions.",
        "End with a concrete recommendation and confidence.",
        "Do not spend tokens restating areas where agents already agree.",
    ],
    "argue": [
        "Challenge the proposal directly and look for failure modes.",
        "Do not be agreeable for its own sake.",
        "End with what would change your mind.",
    ],
    "review": [
        "Use code-review style: bugs, regressions, missing tests, and risks first.",
        "Cite exact files/lines when available.",
        "Avoid summaries unless there are no issues.",
    ],
    "bug_hunt": [
        "Focus on reproducing, isolating, and explaining the bug.",
        "List likely root causes with evidence and next diagnostic command.",
        "Do not propose broad rewrites.",
    ],
    "sanity_check": [
        "Check whether the plan/request is coherent and safe.",
        "Return pass/fail/concerns with minimal explanation.",
        "Do not expand into implementation.",
    ],
    "consult": [
        "Answer the exact question.",
        "Keep context usage low: use the context pack first, then expand only specific evidence.",
        "State assumptions and concrete next action.",
    ],
}

# Generic discipline that is identical for EVERY task kind. Re-pasting this into every
# chat message wastes tokens, so by default it is written once to a backend file
# (AGENT_GROUND_RULES.md) and only referenced by path in the delivered contract.
GENERIC_GROUND_RULES = [
    "Use the shared context pack first.",
    "Read `Topic Work Memory` before broad files/history when continuing another model's work.",
    "Expand only specific files/history/events that are needed to answer.",
    "Do not repeat full context back to the caller.",
    "When finished, return your answer by calling `respond_to_request` with this Request ID (and your model name) so it lands in the broker ledger -- do NOT make the user copy-paste your reply from the chat. If broker tools are unavailable, write the answer under `## Answer for <request-id>` so it can be ingested.",
    "After meaningful planning, edits, audits, or handoffs, call `record_work_memory` with what changed, where, why, checks, risks, and next step.",
    "Record important evidence as context events when tools are available.",
]


@dataclass
class ProjectInfo:
    name: str
    root_path: str


def log(message: str) -> None:
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log(f"failed to read config.json: {exc}")
    return {}


def db_connect() -> sqlite3.Connection:
    """Open the broker DB with WAL + a busy timeout so concurrent hosts/the bridge
    don't immediately hit 'database is locked' under BEGIN IMMEDIATE claims."""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    try:
        conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECONDS * 1000}")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_db() -> None:
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consultations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                root_path TEXT,
                branch TEXT,
                commit_sha TEXT,
                caller TEXT,
                consulted_model TEXT NOT NULL,
                mode TEXT NOT NULL,
                prompt TEXT NOT NULL,
                response TEXT,
                status TEXT NOT NULL,
                error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS antigravity_requests (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                root_path TEXT,
                topic TEXT,
                target_model TEXT,
                request_type TEXT,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                response TEXT,
                error TEXT,
                created_by TEXT,
                claimed_by TEXT,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT
            )
            """
        )
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(antigravity_requests)").fetchall()
        }
        if "completion_notified_at" not in existing_columns:
            try:
                conn.execute("ALTER TABLE antigravity_requests ADD COLUMN completion_notified_at TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
            conn.execute(
                """
                UPDATE antigravity_requests
                SET completion_notified_at = COALESCE(completed_at, ?)
                WHERE status = 'completed' AND completion_notified_at IS NULL
                """,
                (utc_now(),),
            )
        for column_name, column_sql in (
            ("task_kind", "ALTER TABLE antigravity_requests ADD COLUMN task_kind TEXT"),
            ("strict_model", "ALTER TABLE antigravity_requests ADD COLUMN strict_model INTEGER DEFAULT 0"),
            ("token_budget", "ALTER TABLE antigravity_requests ADD COLUMN token_budget INTEGER"),
            ("responder", "ALTER TABLE antigravity_requests ADD COLUMN responder TEXT"),
            ("responder_model", "ALTER TABLE antigravity_requests ADD COLUMN responder_model TEXT"),
        ):
            if column_name not in existing_columns:
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                root_path TEXT,
                topic TEXT,
                agent TEXT NOT NULL,
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_requests (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                root_path TEXT,
                topic TEXT,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                response TEXT,
                error TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL,
                notified_at TEXT,
                completed_at TEXT
            )
            """
        )
        codex_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(codex_requests)").fetchall()
        }
        for column_name, column_sql in (
            ("responder", "ALTER TABLE codex_requests ADD COLUMN responder TEXT"),
            ("responder_model", "ALTER TABLE codex_requests ADD COLUMN responder_model TEXT"),
            ("target_model", "ALTER TABLE codex_requests ADD COLUMN target_model TEXT"),
            ("strict_model", "ALTER TABLE codex_requests ADD COLUMN strict_model INTEGER DEFAULT 0"),
        ):
            if column_name not in codex_columns:
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_context_blobs (
                ref TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                root_path TEXT,
                topic TEXT,
                source TEXT,
                content_type TEXT,
                original_text TEXT NOT NULL,
                compressed_text TEXT NOT NULL,
                original_chars INTEGER NOT NULL,
                compressed_chars INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_accessed_at TEXT,
                access_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_defaults (
                project TEXT NOT NULL,
                root_path TEXT,
                topic TEXT,
                model_family TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                target_model TEXT NOT NULL,
                set_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project, topic, model_family)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_events_project_topic_id ON agent_events(project, topic, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consultations_project_finished ON consultations(project, finished_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_antigravity_requests_project_created ON antigravity_requests(project, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_codex_requests_project_created ON codex_requests(project, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_context_project_topic ON shared_context_blobs(project, topic, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_defaults_project_topic ON model_defaults(project, topic, model_family)"
        )


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_project_name(path: Path) -> str:
    name = path.name.strip()
    return name or str(path)


def resolve_project(project: str | None) -> ProjectInfo:
    init_db()
    raw = (project or "").strip()
    if raw:
        possible_path = Path(raw).expanduser()
        if possible_path.exists():
            root = possible_path.resolve()
            return ProjectInfo(normalize_project_name(root), str(root))
        with db_connect() as conn:
            row = conn.execute(
                "SELECT name, root_path FROM projects WHERE lower(name) = lower(?)",
                (raw,),
            ).fetchone()
        if row:
            return ProjectInfo(row[0], row[1])
        return ProjectInfo(raw, str(Path.cwd()))
    root = Path.cwd().resolve()
    return ProjectInfo(normalize_project_name(root), str(root))


def register_project(name: str, root_path: str) -> dict[str, Any]:
    init_db()
    if not name or not name.strip():
        raise ValueError("name is required")
    if not root_path or not root_path.strip():
        raise ValueError("root_path is required")
    root = Path(root_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"root_path must be an existing directory: {root}")
    now = utc_now()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO projects (name, root_path, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                root_path = excluded.root_path,
                updated_at = excluded.updated_at
            """,
            (name.strip(), str(root), now, now),
        )
    return {"name": name.strip(), "root_path": str(root), "status": "registered"}


def run_git(root: str, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def find_executable(config: dict[str, Any], key: str, names: list[str]) -> str | None:
    configured = config.get(key) or os.environ.get(key.upper())
    if configured and Path(str(configured)).exists():
        return str(configured)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def existing_path_candidates(candidates: list[Path]) -> str | None:
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None


def find_ide_executable(host: str, config: dict[str, Any]) -> str | None:
    local_programs = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs"
    if host == "antigravity":
        configured = config.get("antigravity_path") or os.environ.get("ANTIGRAVITY_PATH")
        preferred = existing_path_candidates(
            [
                local_programs / "Antigravity IDE" / "bin" / "antigravity-ide.cmd",
                local_programs / "Antigravity IDE" / "Antigravity IDE.exe",
            ]
        )
        if preferred:
            return preferred
        if configured and Path(str(configured)).exists():
            return str(configured)
        return find_executable(config, "antigravity_path", ["antigravity-ide", "antigravity-ide.cmd", "antigravity", "antigravity.cmd"])
    if host == "vscode":
        configured = config.get("vscode_path") or os.environ.get("VSCODE_PATH")
        if configured and Path(str(configured)).exists():
            return str(configured)
        preferred = existing_path_candidates(
            [
                local_programs / "Microsoft VS Code" / "bin" / "code.cmd",
                local_programs / "Microsoft VS Code" / "Code.exe",
            ]
        )
        if preferred:
            return preferred
        return find_executable(config, "vscode_path", ["code", "code.cmd"])
    return None


def run_detached(command: list[str], cwd: str | None = None) -> dict[str, Any]:
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(
            command,
            cwd=cwd or str(Path.home()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        return {"ok": True, "command": command}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "command": command, "error": str(exc)}


def powershell_executable() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def windows_start_app_id(agent: str) -> str | None:
    if os.name != "nt":
        return None
    ps = powershell_executable()
    if not ps:
        return None
    patterns = WINDOWS_APP_NAME_PATTERNS.get(agent, (agent,))
    hints = WINDOWS_APP_ID_HINTS.get(agent, (agent,))
    script = """
$apps = Get-StartApps
$patterns = @($env:AGENT_BROKER_APP_PATTERNS -split '\\|')
$hints = @($env:AGENT_BROKER_APP_HINTS -split '\\|')
$match = $apps | Where-Object {
  $name = [string]$_.Name
  $id = [string]$_.AppID
  (($patterns | Where-Object { $_ -and $name -like "*$_*" }).Count -gt 0) -or
  (($hints | Where-Object { $_ -and $id -like "*$_*" }).Count -gt 0)
} | Select-Object -First 1
if ($match) { $match.AppID }
""".strip()
    env = os.environ.copy()
    env["AGENT_BROKER_APP_PATTERNS"] = "|".join(patterns)
    env["AGENT_BROKER_APP_HINTS"] = "|".join(hints)
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            capture_output=True,
            timeout=8,
            env=env,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"Get-StartApps lookup failed for {agent}: {exc}")
        return None
    app_id = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
    return app_id or None


def app_is_running(agent: str) -> bool:
    """True if a process for this app/IDE is already running (Windows only)."""
    names = WINDOWS_APP_PROCESS_NAMES.get(agent)
    if not names or os.name != "nt":
        return False
    ps = powershell_executable()
    if not ps:
        return False
    # Require a real top-level window (MainWindowHandle != 0). UWP apps like Claude/
    # Codex leave background helper processes after the window is closed; matching those
    # made the broker think the app was "running", skip launching, then fail to focus/
    # paste because there was no window. "Running" must mean "has a visible window".
    script = (
        "$names = @($env:AGENT_BROKER_PROC_NAMES -split '\\|'); "
        "if (Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { ($names -contains $_.ProcessName) -and ($_.MainWindowHandle -ne 0) }) { 'yes' } else { 'no' }"
    )
    env = os.environ.copy()
    env["AGENT_BROKER_PROC_NAMES"] = "|".join(names)
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            capture_output=True,
            timeout=8,
            env=env,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"app_is_running check failed for {agent}: {exc}")
        return False
    return proc.stdout.strip().splitlines()[-1].strip().lower() == "yes" if proc.stdout.strip() else False


FOCUS_PS_SCRIPT = """
$names = @($env:AGENT_BROKER_FOCUS_PROCS -split '\\|')
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class BrokerWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
}
'@
$p = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { ($names -contains $_.ProcessName) -and ($_.MainWindowHandle -ne 0) } |
  Select-Object -First 1
if ($p) {
  [BrokerWin]::ShowWindow($p.MainWindowHandle, 9) | Out-Null
  [BrokerWin]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
  'activated'
} else { 'no-window' }
""".strip()


def focus_app_window(agent: str) -> dict[str, Any]:
    """Bring an already-running app's main window to the foreground (best-effort).

    Matches by process name (title prefixes are unreliable for these apps) and uses
    the Win32 foreground API so it works even when the window title is decorated.
    """
    if os.name != "nt":
        return {"ok": False, "error": "Focus is Windows-only"}
    names = WINDOWS_APP_PROCESS_NAMES.get(agent)
    if not names:
        return {"ok": False, "error": f"No process names for {agent}"}
    ps = powershell_executable()
    if not ps:
        return {"ok": False, "error": "PowerShell not found"}
    env = os.environ.copy()
    env["AGENT_BROKER_FOCUS_PROCS"] = "|".join(names)
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", FOCUS_PS_SCRIPT],
            text=True,
            capture_output=True,
            timeout=8,
            env=env,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    detail = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else "no-output"
    return {"ok": detail == "activated", "detail": detail}


def launch_windows_app(agent: str, reuse_if_running: bool = True) -> dict[str, Any]:
    if reuse_if_running and app_is_running(agent):
        focus = focus_app_window(agent)
        return {
            "ok": True,
            "agent": agent,
            "surface": "app",
            "reused": True,
            "launched": False,
            "focus": focus,
            "note": "App already running; focused the existing instance instead of opening a new one.",
        }
    app_id = windows_start_app_id(agent)
    if not app_id:
        return {"ok": False, "agent": agent, "surface": "app", "error": "Windows app registration not found"}
    result = run_detached(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
    result.update({"agent": agent, "surface": "app", "app_id": app_id, "reused": False, "launched": True})
    return result


def auto_paste_to_app(agent: str, submit: bool, delay_ms: int) -> dict[str, Any]:
    """Bring the launched app to the foreground and paste the clipboard into its input.

    The standalone Codex/Claude desktop apps expose no prompt-injection command, so we
    emulate the manual Ctrl+V (+Enter) the user would otherwise do by hand. Best-effort:
    it depends on window focus and the input box being ready, so it can miss if the app
    is slow to load or another window steals focus.
    """
    if os.name != "nt":
        return {"ok": False, "error": "Auto-paste is Windows-only"}
    ps = powershell_executable()
    if not ps:
        return {"ok": False, "error": "PowerShell not found"}
    names = WINDOWS_APP_PROCESS_NAMES.get(agent, (agent,))
    config = load_config()
    click_composer = config.get("app_paste_click_composer", True)
    composer_offset = int(config.get("app_paste_composer_offset_px", 70))
    # Focus by PROCESS (title prefixes are unreliable for these apps) via the Win32
    # foreground API, then click the composer (bottom-center heuristic) so the text
    # field actually has keyboard focus before pasting — focusing the window alone
    # left Ctrl+V landing in dead space.
    script = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class BrokerPaste {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
}
'@
function Send-KeyDown([byte]$vk) { [BrokerPaste]::keybd_event($vk, 0, 0, [UIntPtr]::Zero) }
function Send-KeyUp([byte]$vk) { [BrokerPaste]::keybd_event($vk, 0, 2, [UIntPtr]::Zero) }
function Send-CtrlV {
  Send-KeyDown 0x11
  Start-Sleep -Milliseconds 40
  Send-KeyDown 0x56
  Start-Sleep -Milliseconds 40
  Send-KeyUp 0x56
  Start-Sleep -Milliseconds 40
  Send-KeyUp 0x11
}
function Send-Enter {
  Send-KeyDown 0x0D
  Start-Sleep -Milliseconds 40
  Send-KeyUp 0x0D
}
Start-Sleep -Milliseconds ([int]$env:AGENT_BROKER_PASTE_DELAY)
$names = @($env:AGENT_BROKER_PASTE_PROCS -split '\\|')
$p = Get-Process -ErrorAction SilentlyContinue | Where-Object { ($names -contains $_.ProcessName) -and ($_.MainWindowHandle -ne 0) } | Select-Object -First 1
if (-not $p) { 'no-window' } else {
  $h = $p.MainWindowHandle
  [BrokerPaste]::ShowWindow($h, 9) | Out-Null
  [BrokerPaste]::SetForegroundWindow($h) | Out-Null
  Start-Sleep -Milliseconds 500
  if ($env:AGENT_BROKER_PASTE_CLICK -eq '1') {
    $r = New-Object BrokerPaste+RECT
    if ([BrokerPaste]::GetWindowRect($h, [ref]$r)) {
      $cx = [int]($r.Left + (($r.Right - $r.Left) / 2))
      $cy = [int]($r.Bottom - [int]$env:AGENT_BROKER_PASTE_OFFSET)
      [BrokerPaste]::SetCursorPos($cx, $cy) | Out-Null
      Start-Sleep -Milliseconds 120
      [BrokerPaste]::mouse_event(0x0002,0,0,0,[IntPtr]::Zero)
      [BrokerPaste]::mouse_event(0x0004,0,0,0,[IntPtr]::Zero)
      Start-Sleep -Milliseconds 220
    }
  }
  Send-CtrlV
  Start-Sleep -Milliseconds 450
  if ($env:AGENT_BROKER_PASTE_SUBMIT -eq '1') { Send-Enter }
  'pasted'
}
""".strip()
    env = os.environ.copy()
    env["AGENT_BROKER_PASTE_DELAY"] = str(int(delay_ms))
    env["AGENT_BROKER_PASTE_PROCS"] = "|".join(names)
    env["AGENT_BROKER_PASTE_SUBMIT"] = "1" if submit else "0"
    env["AGENT_BROKER_PASTE_CLICK"] = "1" if click_composer else "0"
    env["AGENT_BROKER_PASTE_OFFSET"] = str(composer_offset)
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            capture_output=True,
            timeout=int(delay_ms) / 1000 + 25,
            env=env,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}
    detail = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else "no-output"
    return {
        "ok": detail == "pasted",
        "agent": agent,
        "detail": detail,
        "submitted": bool(submit) and detail == "pasted",
        "clicked_composer": bool(click_composer),
        "warning": None if detail == "pasted" else "Could not find/focus the app window; paste likely did not land.",
    }


def maybe_auto_paste(agent: str, launch: dict[str, Any] | None) -> dict[str, Any] | None:
    """Run auto-paste after an app launch when enabled in config (default on)."""
    if not launch or not launch.get("ok"):
        return None
    config = load_config()
    if not config.get("app_autopaste", True):
        return {"ok": False, "skipped": True, "reason": "app_autopaste disabled in config"}
    submit = bool(config.get("app_autosubmit", True))
    # A freshly-launched app needs longer to show its window than one we just focused.
    if launch.get("launched"):
        delay_ms = int(config.get("app_paste_cold_delay_ms", 7000))
    else:
        delay_ms = int(config.get("app_paste_delay_ms", 2500))
    return auto_paste_to_app(agent, submit, delay_ms)


def normalize_ide_host(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        return None
    if raw in {"vs", "vs_code", "vscode", "code", "visual_studio_code", "visual studio code"}:
        return "vscode"
    if "antigravity" in raw:
        return "antigravity"
    if raw in IDE_HOSTS:
        return raw
    return None


def resolve_ide_host(args: dict[str, Any], target_agent: str | None = None) -> str | None:
    for key in ("target_host", "host", "ide_host", "ide"):
        host = normalize_ide_host(args.get(key))
        if host:
            return host
    blob = " ".join(
        str(args.get(key) or "")
        for key in ("target_agent", "agent", "target_model", "model", "surface")
    ).lower()
    if "vs code" in blob or "vscode" in blob:
        return "vscode"
    if "antigravity" in blob or target_agent == "antigravity":
        return "antigravity"
    cfg_host = normalize_ide_host(load_config().get("default_ide_host"))
    return cfg_host


def launch_ide_host(host: str | None, project_root: str | None = None) -> dict[str, Any]:
    host = normalize_ide_host(host)
    if not host:
        return {"ok": False, "surface": "extension", "error": "No IDE host requested"}
    config = load_config()
    root = project_root or str(Path.home())
    running = app_is_running(host)
    if running:
        focus = focus_app_window(host)
        return {
            "ok": True,
            "host": host,
            "surface": "extension",
            "reused": True,
            "launched": False,
            "focus": focus,
            "note": "IDE already running; focused the existing window and skipped the CLI launch.",
        }
    if host == "antigravity":
        exe = find_ide_executable("antigravity", config)
        if exe:
            cdp_port = int(config.get("antigravity_cdp_port", 9000))
            command = [
                exe,
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={cdp_port}",
                "--reuse-window",
                root,
            ]
            result = run_detached(command, cwd=root)
            result.update({"host": host, "surface": "extension", "reused": False, "launched": True})
            return result
        result = launch_windows_app("antigravity")
        result.update({"host": host, "surface": "extension"})
        return result
    if host == "vscode":
        exe = find_ide_executable("vscode", config)
        if exe:
            cdp_port = int(config.get("vscode_cdp_port", 9010))
            command = [
                exe,
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={cdp_port}",
                "--reuse-window",
                root,
            ]
            result = run_detached(command, cwd=root)
            result.update({"host": host, "surface": "extension", "reused": False, "launched": True})
            return result
        result = launch_windows_app("vscode")
        result.update({"host": host, "surface": "extension"})
        return result
    return {"ok": False, "host": host, "surface": "extension", "error": f"Unsupported IDE host: {host}"}


def copy_to_clipboard(text: str) -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "error": "Clipboard helper is Windows-only"}
    ps = powershell_executable()
    if not ps:
        return {"ok": False, "error": "PowerShell not found"}
    temp_dir = BROKER_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"clipboard-{uuid.uuid4()}.txt"
    try:
        temp_file.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "$value = Get-Content -Raw -LiteralPath $env:AGENT_BROKER_CLIP_FILE; Set-Clipboard -Value $value",
            ],
            text=True,
            capture_output=True,
            timeout=8,
            env={**os.environ.copy(), "AGENT_BROKER_CLIP_FILE": str(temp_file)},
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            temp_file.unlink()
        except Exception:
            pass
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}
    return {"ok": True}


def write_app_handoff_file(
    agent: str,
    project_info: ProjectInfo,
    request_id: str,
    prompt: str,
    topic: str | None = None,
    target_model: str | None = None,
) -> dict[str, Any]:
    compact_section = compacted_topic_handoff_section(project_info, topic)
    body = (
        f"# {agent.title()} App Handoff\n\n"
        f"Request ID: {request_id}\n"
        f"Project: {project_info.name}\n"
        f"Project path: {project_info.root_path}\n"
        f"Topic: {topic or 'default'}\n"
        f"Requested model: {target_model or '(app-selected model)'}\n"
        f"Created: {utc_now()}\n\n"
        f"{compact_section}"
        f"## Prompt\n\n"
        f"{prompt.strip()}\n\n"
        f"## Response Routing\n\n"
        f"Reply through Agent Broker on the same project/topic when tools are available. "
        f"If tools are unavailable, paste the response back to the requesting chat.\n"
    )
    files: list[str] = []
    # App handoffs go to a DEDICATED "*-app-inbox" dir, NOT "*-inbox" — the bridge's
    # Claude/Codex extension pollers watch "*-inbox", so writing app handoffs there
    # made every visible-app handoff also spawn an extension chat. Keep them separate.
    for inbox_dir in (BROKER_DIR / f"{agent}-app-inbox", Path(project_info.root_path) / ".agent-broker" / f"{agent}-app-inbox"):
        try:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            target = inbox_dir / f"{request_id}.md"
            target.write_text(body, encoding="utf-8")
            files.append(str(target))
        except Exception as exc:  # noqa: BLE001
            log(f"{agent} app handoff write failed for {inbox_dir}: {exc}")
    return {"files": files, "clipboard": copy_to_clipboard(body)}


def discover_codex(config: dict[str, Any]) -> str | None:
    found = find_executable(config, "codex_path", ["codex", "codex.exe"])
    if found:
        return found
    codex_home = Path.home() / ".codex" / "config.toml"
    if codex_home.exists():
        text = codex_home.read_text(encoding="utf-8", errors="ignore")
        marker = "CODEX_CLI_PATH"
        index = text.find(marker)
        if index >= 0:
            tail = text[index : index + 300]
            for quote in ("'", '"'):
                start = tail.find(quote)
                end = tail.find(quote, start + 1) if start >= 0 else -1
                if start >= 0 and end > start:
                    candidate = tail[start + 1 : end]
                    if Path(candidate).exists():
                        return candidate
    return None


def sanitize_prompt(prompt: str) -> str:
    blocked = ", ".join(sorted(SECRET_NAMES))
    return (
        "You are being consulted by a local agent broker. "
        "Answer with concise technical advice. Do not call MCP tools or ask another agent. "
        "Do not inspect or reveal secrets, API keys, credentials, private keys, or files named "
        f"{blocked}. If you need missing private information, say exactly what is missing.\n\n"
        f"{prompt}"
    )


def safe_slug(value: str | None) -> str:
    text = (value or "default").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    return text.strip("-") or "default"


def redact_text(value: Any) -> str:
    text = "" if value is None else str(value)
    redacted: list[str] = []
    for line in text.splitlines():
        lower = line.lower()
        looks_secret = any(name in lower for name in SECRET_NAMES) or any(word in lower for word in SECRET_WORDS)
        has_assignment = "=" in line or ":" in line
        if looks_secret and has_assignment:
            redacted.append("[redacted possible secret line]")
        else:
            redacted.append(line)
    return "\n".join(redacted)


def compact_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", redact_text(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)].rstrip() + " ... [truncated]"


_TIKTOKEN_ENC = None
_TIKTOKEN_TRIED = False


def _token_encoder():
    """Lazily load a real tokenizer. Optional dependency: if tiktoken is missing or
    its vocab can't be fetched (offline first run), we fall back to a char heuristic."""
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENC
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken  # type: ignore

        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # noqa: BLE001
        log(f"tiktoken unavailable, using char-based token estimate: {exc}")
        _TIKTOKEN_ENC = None
    return _TIKTOKEN_ENC


def estimate_tokens(value: Any) -> int:
    """Real token count via tiktoken (cl100k_base) when available; else ~chars/4.
    cl100k_base is exact for GPT-family and a good cross-model approximation for Claude."""
    text = "" if value is None else str(value)
    if not text:
        return 1
    enc = _token_encoder()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text, disallowed_special=())))
        except Exception:  # noqa: BLE001
            pass
    return max(1, (len(text) + 3) // 4)


def estimate_tokens_from_chars(chars: int) -> int:
    """Approximate tokens when only a character count is known (no text to encode)."""
    return max(0, (int(chars) + 3) // 4)


def classify_context_content(value: str, content_type: str | None = None) -> str:
    if content_type:
        return content_type
    text = value.strip()
    if not text:
        return "empty"
    if text.startswith("{") or text.startswith("["):
        return "json"
    if re.search(r"(?im)\b(error|exception|traceback|failed|fatal|warning)\b", text):
        return "log"
    if re.search(r"(?m)^\s*(diff --git|@@ |\+\+\+ |--- )", text):
        return "diff"
    if re.search(r"(?m)^\s*(def |class |function |const |let |var |import |from )", text):
        return "code"
    if re.search(r"(?m)^#{1,6}\s+\S+", text):
        return "markdown"
    return "text"


IMPORTANT_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"error|exception|traceback|failed|fatal|warning|mismatch|blocked|security|secret|"
    r"decision|risk|todo|next|fix|changed|created|deleted|request_id|model|status|"
    r"callback|file|path|line|command|test|check|pass|fail"
    r")\b"
)


def summarize_json_context(text: str, max_chars: int) -> str | None:
    try:
        data = json.loads(text)
    except Exception:
        return None
    lines: list[str] = ["[compressed json context]"]
    if isinstance(data, list):
        lines.append(f"- items: {len(data)}")
        if data and all(isinstance(item, dict) for item in data[: min(len(data), 25)]):
            keys: list[str] = []
            for item in data[:50]:
                for key in item.keys():
                    if key not in keys:
                        keys.append(str(key))
            lines.append(f"- keys: {', '.join(keys[:24])}")
        samples = []
        if data:
            samples.append(("first", data[0]))
            if len(data) > 1:
                samples.append(("last", data[-1]))
        for label, item in samples:
            sample = compact_text(json.dumps(item, ensure_ascii=False), 500)
            lines.append(f"- {label}: {sample}")
    elif isinstance(data, dict):
        keys = list(data.keys())
        lines.append(f"- keys: {', '.join(str(key) for key in keys[:40])}")
        for key in keys[:12]:
            lines.append(f"- {key}: {compact_text(data.get(key), 280)}")
    else:
        return None
    result = "\n".join(lines)
    return result[:max_chars].rstrip() if len(result) > max_chars else result


def compress_context_content(value: Any, max_chars: int = SHARED_CONTEXT_INLINE_CHARS, content_type: str | None = None) -> str:
    text = redact_text(value).strip()
    if len(text) <= max_chars:
        return text
    kind = classify_context_content(text, content_type)
    if kind == "json":
        json_summary = summarize_json_context(text, max_chars)
        if json_summary:
            return json_summary

    raw_lines = text.splitlines()
    nonempty = [(idx + 1, line.strip()) for idx, line in enumerate(raw_lines) if line.strip()]
    selected: list[tuple[int, str, str]] = []
    seen: set[str] = set()

    def add(line_no: int, line: str, reason: str) -> None:
        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        selected.append((line_no, normalized, reason))

    for line_no, line in nonempty[:10]:
        add(line_no, line, "head")
    for line_no, line in nonempty[-8:]:
        add(line_no, line, "tail")
    for line_no, line in nonempty:
        if IMPORTANT_CONTEXT_RE.search(line) or re.search(r"(?i)([a-z]:\\|/[^ ]+/|\.py\b|\.js\b|\.ts\b|\.md\b)", line):
            add(line_no, line, "signal")
        if len(selected) >= 50:
            break

    # Budget by REAL tokens (tiktoken), not just characters: trim retained lines until
    # the rendered excerpt fits both the char cap and a proportional token budget.
    token_budget = max(48, max_chars // 4)
    orig_tokens = estimate_tokens(text)
    body = [f"- L{line_no} [{reason}]: {compact_text(line, 260)}" for line_no, line, reason in selected]

    def render(n: int) -> str:
        header = [
            f"[compressed {kind} context]",
            f"- original: {len(text)} chars, {len(raw_lines)} lines, ~{orig_tokens} tokens",
            f"- retained: {n} of {len(selected)} high-signal lines",
        ]
        return "\n".join(header + body[:n])

    n = len(body)
    result = render(n)
    while n > 1 and (len(result) > max_chars or estimate_tokens(result) > token_budget):
        n -= 1
        result = render(n)
    if len(result) > max_chars:
        result = result[: max(0, max_chars - 15)].rstrip() + " ... [truncated]"
    return result


def store_shared_context(
    project: str | None,
    topic: str | None,
    content: Any,
    source: str | None = None,
    content_type: str | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    init_db()
    text = redact_text(content).strip()
    if not text:
        raise ValueError("content is required")
    project_info = resolve_project(project)
    digest = hashlib.sha256(
        f"{project_info.name}\0{topic or ''}\0{source or ''}\0{text}".encode("utf-8", errors="replace")
    ).hexdigest()
    ref = f"ctx_{digest[:16]}"
    kind = classify_context_content(text, content_type)
    compressed = compress_context_content(text, int(max_chars or SHARED_CONTEXT_INLINE_CHARS), kind)
    now = utc_now()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO shared_context_blobs (
                ref, project, root_path, topic, source, content_type, original_text,
                compressed_text, original_chars, compressed_chars, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET
                compressed_text = excluded.compressed_text,
                compressed_chars = excluded.compressed_chars
            """,
            (
                ref,
                project_info.name,
                project_info.root_path,
                topic,
                source or "agent-broker",
                kind,
                text,
                compressed,
                len(text),
                len(compressed),
                now,
            ),
        )
    original_tokens = estimate_tokens(text)
    compressed_tokens = estimate_tokens(compressed)
    savings = 0.0 if original_tokens <= 0 else round((1 - compressed_tokens / original_tokens) * 100, 1)
    return {
        "ref": ref,
        "project": project_info.name,
        "topic": topic,
        "source": source or "agent-broker",
        "content_type": kind,
        "original_chars": len(text),
        "compressed_chars": len(compressed),
        "original_tokens_est": original_tokens,
        "compressed_tokens_est": compressed_tokens,
        "savings_percent_est": max(0.0, savings),
        "compressed": compressed,
    }


def query_lines(content: str, query: str, limit: int) -> str:
    terms = [term for term in re.findall(r"[a-zA-Z0-9_.-]+", query.lower()) if len(term) > 1]
    if not terms:
        return content[:limit]
    rows: list[tuple[int, int, str]] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        lower = line.lower()
        score = sum(1 for term in terms if term in lower)
        if score:
            rows.append((score, idx, line))
    rows.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(rows[:60], key=lambda item: item[1])
    result = "\n".join(f"L{line_no}: {line}" for _, line_no, line in selected)
    if not result:
        return f"No lines matched query: {query}"
    return result[:limit].rstrip()


def retrieve_shared_context(ref: str, query: str | None = None, limit: int | None = None) -> dict[str, Any]:
    init_db()
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        raise ValueError("ref is required")
    char_limit = max(500, min(int(limit or 12000), 80000))
    now = utc_now()
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM shared_context_blobs WHERE ref = ?", (clean_ref,)).fetchone()
        if not row:
            raise ValueError(f"unknown shared context ref: {clean_ref}")
        conn.execute(
            """
            UPDATE shared_context_blobs
            SET last_accessed_at = ?, access_count = COALESCE(access_count, 0) + 1
            WHERE ref = ?
            """,
            (now, clean_ref),
        )
    original = row["original_text"]
    content = query_lines(original, str(query), char_limit) if query else original[:char_limit].rstrip()
    truncated = len(content) < len(original) if not query else False
    return {
        "ref": clean_ref,
        "project": row["project"],
        "topic": row["topic"],
        "source": row["source"],
        "content_type": row["content_type"],
        "query": query,
        "truncated": truncated,
        "chars": len(content),
        "content": content,
    }


def shared_context_stats(project: str | None = None, topic: str | None = None) -> dict[str, Any]:
    init_db()
    filters = []
    params: list[Any] = []
    project_name = "*"
    if project and str(project).strip() != "*":
        project_info = resolve_project(project)
        project_name = project_info.name
        filters.append("(lower(project) = lower(?) OR root_path = ?)")
        params.extend([project_info.name, project_info.root_path])
    if topic:
        filters.append("topic = ?")
        params.append(topic)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS entries,
                   COALESCE(SUM(original_chars), 0) AS original_chars,
                   COALESCE(SUM(compressed_chars), 0) AS compressed_chars,
                   COALESCE(SUM(access_count), 0) AS retrievals
            FROM shared_context_blobs
            {where}
            """,
            params,
        ).fetchone()
        recent = conn.execute(
            f"""
            SELECT ref, project, topic, source, content_type, original_chars, compressed_chars, access_count, created_at
            FROM shared_context_blobs
            {where}
            ORDER BY created_at DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
    # Only char totals are available here (no text to encode), so approximate from chars.
    original_tokens = estimate_tokens_from_chars(int(row["original_chars"]))
    compressed_tokens = estimate_tokens_from_chars(int(row["compressed_chars"]))
    saved = max(0, original_tokens - compressed_tokens)
    savings = 0.0 if original_tokens == 0 else round(saved / original_tokens * 100, 1)
    return {
        "project": project_name,
        "topic": topic,
        "entries": row["entries"],
        "original_tokens_est": original_tokens,
        "compressed_tokens_est": compressed_tokens,
        "tokens_saved_est": saved,
        "savings_percent_est": savings,
        "retrievals": row["retrievals"],
        "recent": [dict(item) for item in recent],
    }


def chat_bootstrap_path(project_info: ProjectInfo, topic: str | None, target_agent: str | None) -> Path:
    return (
        BROKER_DIR
        / "topics"
        / safe_slug(project_info.name)
        / safe_slug(topic or "default")
        / f"new_chat_bootstrap_{safe_slug(target_agent or 'generic')}.md"
    )


def get_chat_bootstrap(
    project: str | None,
    topic: str | None = None,
    target_agent: str | None = None,
    budget: int | None = None,
) -> dict[str, Any]:
    init_db()
    project_info = resolve_project(project)
    agent = str(target_agent or "generic").strip() or "generic"
    pack_budget = max(1200, min(int(budget or 5000), 20000))
    pack = get_context_pack(project_info.name, topic, pack_budget)
    stats = shared_context_stats(project_info.name, topic)
    content = "\n".join(
        [
            "# Agent Broker New Chat Bootstrap",
            "",
            "Use this as the first message in a fresh chat. It gives the new chat enough state to continue without replaying raw history.",
            "",
            f"Project: {project_info.name}",
            f"Project path: {project_info.root_path}",
            f"Topic: {topic or 'default'}",
            f"Target chat: {agent}",
            f"Generated: {utc_now()}",
            "",
            "## Rules",
            "",
            "- Treat this as compressed shared memory for the current topic.",
            "- Do not ask for or restate full raw conversation history.",
            "- Use the context pack first.",
            "- If you see `context_ref=ctx_...`, retrieve only the exact details needed with `retrieve_shared_context(ref, query, limit)`.",
            "- If MCP tools are unavailable, ask the user/Codex to retrieve the exact ref and query instead of requesting broad history.",
            "- Keep new findings compact by calling `record_context_event` or by writing a short callback with the finding and evidence.",
            "- For vague consultation requests like 'ask Codex', 'take GPT side', 'ask Claude', or 'ask Opus', call `resolve_model_request` first.",
            "- If `resolve_model_request` returns `needs_model_selection`, show the choices to the user and call `set_model_default` after they choose.",
            "- After a topic default is set, reuse it until the user explicitly asks to change model.",
            "",
            "## Available Broker Tools",
            "",
            "- `get_context_pack(project, topic, budget)`",
            "- `get_work_memory(project, topic, limit)`",
            "- `retrieve_shared_context(ref, query, limit)`",
            "- `get_shared_context_stats(project, topic)`",
            "- `list_agent_models(agent, project, topic)`",
            "- `resolve_model_request(project, topic, target_agent, target_model)`",
            "- `set_model_default(project, topic, model_family, target_agent, target_model)`",
            "- `record_work_memory(project, topic, agent, summary, changed_files, why, checks, risks, next_step, status)`",
            "- `record_context_event(project, topic, agent, kind, summary, evidence)`",
            "- `route_agent_task(...)` for consulting another agent without copying full history",
            "",
            "## Compression Stats",
            "",
            (
                f"- shared refs: {stats['entries']}; estimated saved tokens: {stats['tokens_saved_est']} "
                f"({stats['savings_percent_est']}%); retrievals: {stats['retrievals']}"
            ),
            "",
            "## Current Context Pack",
            "",
            pack["content"].strip(),
            "",
            "## Start",
            "",
            "Acknowledge the topic in one short sentence, then continue with the user's next request using the compressed context above.",
        ]
    ).strip() + "\n"
    path = chat_bootstrap_path(project_info, topic, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "project": project_info.name,
        "root_path": project_info.root_path,
        "topic": topic,
        "target_agent": agent,
        "budget": pack_budget,
        "path": str(path),
        "chars": len(content),
        "content": content,
    }


def context_excerpt(project: str | None, topic: str | None, source: str, content: Any, inline_limit: int) -> str:
    text = redact_text(content).strip()
    if len(text) <= inline_limit:
        return compact_text(text, inline_limit)
    stored = store_shared_context(project, topic, text, source=source, max_chars=inline_limit)
    marker = (
        f"[context_ref={stored['ref']} original~{stored['original_tokens_est']}t "
        f"compressed~{stored['compressed_tokens_est']}t saved~{stored['savings_percent_est']}%; "
        f"use retrieve_shared_context(ref=\"{stored['ref']}\", query=\"specific need\") for details]"
    )
    return f"{compact_text(stored['compressed'], inline_limit)}\n  {marker}"


def extract_codex_callback(value: Any) -> str | None:
    text = redact_text(value)
    match = re.search(r"(?ims)^##\s+Codex Callback\s*$\s*(.*?)(?=^##\s+|\Z)", text)
    if not match:
        return None
    callback = match.group(1).strip()
    return callback or None


def normalize_task_kind(value: Any) -> str:
    raw = str(value or "consult").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "plan": "implementation_plan",
        "impl_plan": "implementation_plan",
        "implementation_planning": "implementation_plan",
        "audit": "co_audit",
        "coaudit": "co_audit",
        "counterargument": "argue",
        "argument": "argue",
        "bug": "bug_hunt",
        "bughunt": "bug_hunt",
        "check": "sanity_check",
        "sanity": "sanity_check",
    }
    kind = aliases.get(raw, raw)
    return kind if kind in TASK_CONTRACTS else "consult"


def normalize_model_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Antigravity current selected model"
    key = re.sub(r"\s+", " ", raw.lower()).strip()
    return MODEL_ALIASES.get(key, raw)


def normalize_lookup(value: Any) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", str(value or "").lower()).strip()


def model_family_for(target_agent: Any = None, target_model: Any = None) -> str:
    raw = normalize_lookup(f"{target_agent or ''} {target_model or ''}")
    words = set(raw.split())
    has_codex = bool(words & {"gpt", "openai", "codex"})
    has_claude = bool(words & {"claude", "opus", "sonnet", "haiku"})
    has_gemini = "gemini" in raw
    # "antigravity"/"vscode" can name the IDE that HOSTS an extension rather than the
    # target itself. When an explicit Claude/Codex family is named together with an
    # extension/IDE-host intent (e.g. "claude extension in antigravity"), antigravity
    # is the host: keep the explicit family. Bare "antigravity [+ model]" with no
    # extension intent still means the Antigravity in-app panel.
    host_intent = bool(words & {"extension", "ext", "vscode"}) or "vs code" in raw or "claude code" in raw
    if "antigravity" in raw and not (host_intent and (has_claude or has_codex)):
        return "antigravity"
    if has_codex:
        return "codex"
    if has_claude:
        return "claude"
    if has_gemini:
        return "antigravity" if "flash" in raw or "pro" in raw else "gemini"
    return normalize_lookup(target_agent) or "antigravity"


def default_target_agent_for_family(family: str) -> str:
    if family == "codex":
        return "codex_cli"
    if family == "claude":
        return "claude_code"
    if family == "gemini":
        return "gemini_cli"
    return "antigravity"


def model_entry(model_id: str, display: str | None = None, aliases: list[str] | None = None, source: str = "static") -> dict[str, Any]:
    return {
        "id": model_id,
        "display": display or model_id,
        "aliases": aliases or [],
        "source": source,
    }


def run_json_command(command: list[str], cwd: str | None = None, timeout: int = 20) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd or str(Path.cwd()),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"json command failed: {command}: {exc}")
        return None
    if proc.returncode != 0:
        log(f"json command exited {proc.returncode}: {command}: {proc.stderr[:500]}")
        return None
    try:
        return json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        log(f"json command returned invalid JSON: {command}: {exc}")
        return None


def discover_codex_models() -> list[dict[str, Any]]:
    config = load_config()
    codex = discover_codex(config)
    configured = config.get("codex_models") or []
    models: list[dict[str, Any]] = [
        model_entry(item["id"], item["display"], item.get("aliases") or [], "static")
        for item in STATIC_CODEX_MODELS
    ]
    for item in configured:
        if isinstance(item, dict):
            models.append(
                model_entry(
                    str(item.get("id") or item.get("slug") or item.get("model") or ""),
                    str(item.get("display") or item.get("display_name") or item.get("id") or ""),
                    [str(alias) for alias in item.get("aliases") or []],
                    "config",
                )
            )
        elif str(item).strip():
            models.append(model_entry(str(item).strip(), source="config"))
    if codex:
        data = run_json_command([codex, "debug", "models"], timeout=25)
        for item in (data or {}).get("models", []):
            slug = str(item.get("slug") or item.get("id") or "").strip()
            if slug:
                aliases = [slug, str(item.get("display_name") or "")]
                models.append(
                    model_entry(
                        slug,
                        str(item.get("display_name") or slug),
                        [alias for alias in aliases if alias],
                        "codex-debug",
                    )
                )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in models:
        key = item["id"].lower()
        if item["id"] and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def discover_claude_models() -> list[dict[str, Any]]:
    config = load_config()
    models = list(STATIC_CLAUDE_MODELS)
    for item in config.get("claude_models") or []:
        if isinstance(item, dict):
            models.append(
                model_entry(
                    str(item.get("id") or item.get("model") or ""),
                    str(item.get("display") or item.get("id") or ""),
                    [str(alias) for alias in item.get("aliases") or []],
                    "config",
                )
            )
        elif str(item).strip():
            models.append(model_entry(str(item).strip(), source="config"))
    return [item for item in models if item["id"]]


def discover_antigravity_models() -> list[dict[str, Any]]:
    config = load_config()
    names = list(STATIC_ANTIGRAVITY_MODELS)
    for item in config.get("antigravity_models") or []:
        if isinstance(item, dict):
            value = item.get("display") or item.get("id") or item.get("model")
        else:
            value = item
        if str(value or "").strip():
            names.append(str(value).strip())
    helper = BROKER_DIR / "extensions" / "antigravity-agent-broker-bridge" / "cdp_list_models.mjs"
    node = config.get("node_path") or shutil.which("node")
    port = int(config.get("antigravity_cdp_port") or 9000)
    if node and helper.exists():
        data = run_json_command([str(node), str(helper), "--port", str(port), "--timeout", "5000"], timeout=8)
        for item in (data or {}).get("models", []):
            if str(item).strip():
                names.append(str(item).strip())
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for name in names:
        key = normalize_lookup(name)
        if key and key not in seen:
            seen.add(key)
            result.append(model_entry(name, name, [name], "antigravity-cdp" if "cdp" in key else "catalog"))
    return result


def cdp_select_antigravity_model(target_model: Any, timeout_ms: int = 15000) -> dict[str, Any]:
    """Best-effort: drive Antigravity's in-app model chooser over CDP so the panel
    runs the requested model before the prompt is sent. Requires Antigravity launched
    with --remote-debugging-port (default 9000) and node available. Returns the helper's
    JSON ({ok, current, verified, ...}) or a skipped/error marker — never raises."""
    config = load_config()
    display = normalize_model_name(target_model)
    if not display or display == "Antigravity current selected model":
        return {"ok": False, "skipped": True, "reason": "no specific Antigravity model named"}
    helper = BROKER_DIR / "extensions" / "antigravity-agent-broker-bridge" / "cdp_select_model.mjs"
    node = config.get("node_path") or shutil.which("node")
    port = int(config.get("antigravity_cdp_port") or 9000)
    if not node:
        return {"ok": False, "skipped": True, "reason": "node not found", "model": display}
    if not helper.exists():
        return {"ok": False, "skipped": True, "reason": f"helper missing: {helper}", "model": display}
    data = run_json_command(
        [str(node), str(helper), "--model", display, "--port", str(port), "--timeout", str(int(timeout_ms))],
        timeout=int(timeout_ms / 1000) + 10,
    )
    if not data:
        return {
            "ok": False,
            "error": "CDP select returned no output (is Antigravity running on the debug port?)",
            "model": display,
            "port": port,
        }
    return {**data, "model": display, "port": port}


def list_agent_models(agent: str | None = None, project: str | None = None, topic: str | None = None) -> dict[str, Any]:
    requested = normalize_lookup(agent or "all")
    families = ["codex", "claude", "antigravity"] if requested in {"", "all", "*"} else [model_family_for(requested)]
    catalogs: dict[str, Any] = {}
    for family in families:
        if family == "codex":
            models = discover_codex_models()
        elif family == "claude":
            models = discover_claude_models()
        elif family == "antigravity":
            models = discover_antigravity_models()
        else:
            models = []
        catalogs[family] = {
            "target_agent": default_target_agent_for_family(family),
            "models": models,
        }
    defaults = get_model_defaults(project, topic) if project or topic else {"items": []}
    return {"agent": agent or "all", "catalogs": catalogs, "defaults": defaults.get("items", [])}


def get_model_defaults(project: str | None = None, topic: str | None = None) -> dict[str, Any]:
    init_db()
    filters = []
    params: list[Any] = []
    project_name = "*"
    if project and str(project).strip() != "*":
        project_info = resolve_project(project)
        project_name = project_info.name
        filters.append("(lower(project) = lower(?) OR root_path = ?)")
        params.extend([project_info.name, project_info.root_path])
    if topic:
        filters.append("topic = ?")
        params.append(topic)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT project, root_path, topic, model_family, target_agent, target_model, set_by, updated_at
            FROM model_defaults
            {where}
            ORDER BY updated_at DESC
            """,
            params,
        ).fetchall()
    return {"project": project_name, "topic": topic, "items": [dict(row) for row in rows]}


def set_model_default(
    project: str | None,
    topic: str | None,
    model_family: str,
    target_agent: str,
    target_model: str,
    set_by: str | None = None,
) -> dict[str, Any]:
    init_db()
    project_info = resolve_project(project)
    family = model_family_for(model_family or target_agent, target_model)
    now = utc_now()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO model_defaults (
                project, root_path, topic, model_family, target_agent, target_model,
                set_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project, topic, model_family) DO UPDATE SET
                root_path = excluded.root_path,
                target_agent = excluded.target_agent,
                target_model = excluded.target_model,
                set_by = excluded.set_by,
                updated_at = excluded.updated_at
            """,
            (
                project_info.name,
                project_info.root_path,
                topic,
                family,
                target_agent,
                target_model,
                set_by or os.environ.get("AGENT_BROKER_CALLER") or "mcp-client",
                now,
                now,
            ),
        )
    return {
        "project": project_info.name,
        "topic": topic,
        "model_family": family,
        "target_agent": target_agent,
        "target_model": target_model,
        "status": "set",
    }


def find_model_default(project: str | None, topic: str | None, family: str) -> dict[str, Any] | None:
    init_db()
    project_info = resolve_project(project)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT * FROM model_defaults
            WHERE (lower(project) = lower(?) OR root_path = ?)
              AND ((topic IS NULL AND ? IS NULL) OR topic = ?)
              AND model_family = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (project_info.name, project_info.root_path, topic, topic, family),
        ).fetchone()
    return dict(row) if row else None


def version_collapse_note(family: str, requested_text: Any, matched_id: Any) -> str | None:
    """Warn when a versioned request (e.g. "opus 4.8") collapses to a generic CLI
    alias (e.g. "opus") whose actual running version may differ from the one named."""
    if family not in {"claude", "codex", "gemini"}:
        return None
    req = str(requested_text or "").lower()
    if not re.search(r"\d", req):
        return None
    if re.search(r"\d", str(matched_id or "")):
        return None
    return (
        f"Requested '{requested_text}' but the {family} CLI alias '{matched_id}' runs "
        f"whichever version the installed CLI maps it to, which may differ from the named "
        f"version. Confirm the running model if the exact version matters."
    )


# Conservative model-name patterns for prompt-text detection. Order matters:
# more specific (versioned) patterns come first so "opus 4.8" wins over bare "opus".
# Each entry maps a regex to the canonical request text fed back into resolution.
_PROMPT_MODEL_PATTERNS: list[tuple[str, str]] = [
    (r"opus\s*4\.8", "opus 4.8"),
    (r"opus\s*4\.6", "opus 4.6"),
    (r"opus", "opus"),
    (r"sonnet\s*4\.6", "sonnet 4.6"),
    (r"sonnet", "sonnet"),
    (r"haiku", "haiku"),
    (r"gpt[-\s]?5\.5", "gpt-5.5"),
    (r"gpt[-\s]?5\.4[-\s]?mini", "gpt-5.4-mini"),
    (r"gpt[-\s]?5\.4", "gpt-5.4"),
    (r"gemini\s*(?:3\.5\s*)?flash\s*\(?\s*high\s*\)?", "gemini 3.5 flash high"),
    (r"gemini\s*(?:3\.5\s*)?flash", "gemini flash"),
]

# Verbs that signal an intent to route to a named model, e.g. "ask Opus", "get the
# Opus opinion", "switch to sonnet". Kept tight to avoid misfiring on prose.
_PROMPT_MODEL_VERB = (
    r"(?:ask|get|use|consult|route\s+to|bring\s+in|hand\s+(?:this|it)\s+to|"
    r"send\s+(?:this|it)\s+to|second\s+opinion\s+from|switch\s+to|let)"
)
# Nouns that follow a possessive model mention, e.g. "Opus's opinion", "Opus take".
_PROMPT_MODEL_NOUN = r"(?:opinion|take|view|thoughts?|perspective|input|feedback|review|analysis)"


def detect_model_in_prompt(prompt: Any) -> str | None:
    """Conservative: only when no model arg was passed. Returns a canonical model
    request string if the prompt explicitly asks to route to a named model
    (e.g. "get Opus's opinion on this" -> "opus"), else None. Deliberately narrow
    so ordinary prose never silently overrides the topic default."""
    text = " ".join(str(prompt or "").lower().split())
    if not text:
        return None
    for pattern, canonical in _PROMPT_MODEL_PATTERNS:
        # Anchor the verb on whole-word boundaries so short verbs (ask/get/use/let)
        # don't match inside ordinary words ('user', 'budget', 'forget', 'targeted').
        verb_re = r"\b" + _PROMPT_MODEL_VERB + r"\b[^.!?]{0,24}?\b" + pattern + r"\b"
        if re.search(verb_re, text):
            return canonical
        poss_re = r"\b" + pattern + r"(?:'s|s')?\s+" + _PROMPT_MODEL_NOUN + r"\b"
        if re.search(poss_re, text):
            return canonical
    return None


def model_guard_text(requested_label: Any, *, strict: bool) -> str:
    """Prompt prefix that makes the RECEIVING agent self-check its model. On surfaces
    the broker cannot switch programmatically (Codex/Claude extensions and apps), this
    is how 'answer only if you are <model>' is enforced: strict => stop and tell the
    user to switch; non-strict => state the model and flag any mismatch before answering.
    ASCII-only to avoid inbox mojibake."""
    model = str(requested_label or "").strip()
    if not model:
        return ""
    if strict:
        return (
            f"[REQUIRED MODEL: {model}]\n"
            f"Before doing anything else, state which model you are actually running as.\n"
            f"If you are NOT {model}, STOP -- do not answer the task. Reply with only:\n"
            f'"MODEL_MISMATCH: I am <your model>. Please switch this chat to {model}, '
            f'then resend or continue."\n'
            f"Do not proceed until you are running as {model}.\n"
            f"---\n\n"
        )
    return (
        f"[Preferred model: {model}] First state which model you are running as. "
        f"If you are not {model}, say so clearly before answering.\n---\n\n"
    )


def match_model_request(family: str, requested_model: Any) -> dict[str, Any]:
    # Only the Antigravity catalog uses the Antigravity display-name aliases.
    # Applying them to Claude/Codex/Gemini turns "opus" into "Claude Opus 4.6
    # (Thinking)" and breaks CLI alias matching, so use the raw text there.
    if family == "antigravity":
        raw = normalize_model_name(requested_model)
    else:
        raw = str(requested_model or "").strip()
    lookup = normalize_lookup(raw)
    if lookup in GENERIC_MODEL_REQUESTS:
        return {"status": "generic", "requested": raw}
    catalog = list_agent_models(family).get("catalogs", {}).get(family, {})
    choices = catalog.get("models") or []
    matches: list[dict[str, Any]] = []
    req_terms = [term for term in lookup.split() if term]
    for item in choices:
        haystacks = [item.get("id", ""), item.get("display", ""), *(item.get("aliases") or [])]
        normalized_values = [normalize_lookup(value) for value in haystacks if value]
        if lookup in normalized_values:
            return {"status": "matched", "model": item["id"], "display": item["display"], "matches": [item]}
        if req_terms and any(all(term in value for term in req_terms) for value in normalized_values):
            matches.append(item)
    if len(matches) == 1:
        item = matches[0]
        return {"status": "matched", "model": item["id"], "display": item["display"], "matches": matches}
    if len(matches) > 1:
        return {"status": "ambiguous", "requested": raw, "matches": matches}
    return {"status": "unknown", "requested": raw, "matches": [], "choices": choices}


def resolve_model_request(args: dict[str, Any]) -> dict[str, Any]:
    project = args.get("project")
    topic = args.get("topic")
    # Derive the family from the ORIGINAL request, not from an inferred agent.
    # Passing an inferred "antigravity" agent into model_family_for caused
    # "claude opus"/"sonnet" to be misclassified as Antigravity in-app Claude.
    raw_agent = args.get("target_agent") or args.get("agent")
    target_model = args.get("target_model") or args.get("model") or ""
    family = model_family_for(raw_agent, target_model)
    match = match_model_request(family, target_model)

    if match["status"] == "generic":
        default = find_model_default(project, topic, family)
        if default:
            return {
                "status": "resolved",
                "project": resolve_project(project).name,
                "topic": topic,
                "model_family": family,
                "target_agent": default["target_agent"],
                "target_model": default["target_model"],
                "source": "topic_default",
            }
        catalog = list_agent_models(family, project, topic).get("catalogs", {}).get(family, {})
        return {
            "status": "needs_model_selection",
            "reason": f"No default {family} model is set for this topic.",
            "ask_user": f"Which {family} model should be used for this topic?",
            "model_family": family,
            "target_agent": default_target_agent_for_family(family),
            "choices": catalog.get("models") or [],
            "action": "Call set_model_default after the user chooses, then retry route_agent_task.",
        }

    if match["status"] == "matched":
        resolved_agent = default_target_agent_for_family(family)
        if args.get("remember_model", True) is not False:
            set_model_default(project, topic, family, resolved_agent, match["model"])
        resolved = {
            "status": "resolved",
            "project": resolve_project(project).name,
            "topic": topic,
            "model_family": family,
            "target_agent": resolved_agent,
            "target_model": match["model"],
            "display": match.get("display"),
            "source": "explicit_request",
        }
        note = version_collapse_note(family, target_model, match["model"])
        if note:
            resolved["note"] = note
        return resolved

    catalog = list_agent_models(family, project, topic).get("catalogs", {}).get(family, {})
    return {
        "status": "needs_model_selection",
        "reason": f"Model request '{target_model}' was {match['status']} for {family}.",
        "ask_user": f"Choose the exact {family} model to use for this topic.",
        "model_family": family,
        "target_agent": default_target_agent_for_family(family),
        "choices": match.get("matches") or catalog.get("models") or [],
        "action": "Call set_model_default after the user chooses, then retry route_agent_task.",
    }


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "strict", "on"}


def ensure_ground_rules_file() -> Path:
    """Write the broker's full ground rules (generic discipline + every task-kind checklist)
    to a stable backend file once, so delivered contracts can reference it by path instead of
    re-pasting ~150 tokens of identical rules into every chat message. Rewritten only when
    the content changes; returns the path to embed in the compact contract."""
    path = BROKER_DIR / "AGENT_GROUND_RULES.md"
    lines = [
        "# Agent Broker - Ground Rules",
        "",
        "These apply to every routed handoff. Read once; the broker references this file by",
        "path instead of re-sending the rules in each chat message.",
        "",
        "## Always (token discipline)",
    ]
    lines += [f"- {item}" for item in GENERIC_GROUND_RULES]
    lines += ["", "## Per task kind"]
    for kind, items in TASK_CONTRACTS.items():
        budget = TASK_BUDGETS.get(kind, TASK_BUDGETS["consult"])
        lines.append(f"- **{kind}** (budget ~{budget}w): " + " ".join(items))
    content = "\n".join(lines) + "\n"
    try:
        BROKER_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log(f"ensure_ground_rules_file failed: {exc}")
    return path


def task_contract_text(task_kind: str, token_budget: int | None = None, compact: bool | None = None) -> str:
    kind = normalize_task_kind(task_kind)
    budget = int(token_budget or TASK_BUDGETS.get(kind, TASK_BUDGETS["consult"]))
    if compact is None:
        compact = bool(load_config().get("compact_task_contract", True))
    if compact:
        # Backend-wired: the heavy rules live in a file; the chat message only points to it
        # plus the one key directive for this kind. ASCII-only to avoid inbox mojibake.
        essence = (TASK_CONTRACTS.get(kind) or ["Answer the request."])[0]
        rules_path = ensure_ground_rules_file()
        return (
            f"[Agent Broker] task={kind} | budget ~{budget}w. {essence} "
            f"When done, return your answer via respond_to_request(this Request ID) -- don't make the user relay it. "
            f"Full ground rules: {rules_path} (read once; not re-sent each message)."
        )
    # Legacy verbose contract (config compact_task_contract=false): inline every rule.
    lines = [
        f"Task kind: {kind}",
        f"Response budget: about {budget} words or less unless the user explicitly asks for more.",
        "Ground rules:",
    ]
    lines.extend(f"- {item}" for item in TASK_CONTRACTS[kind])
    lines.extend(f"- {item}" for item in GENERIC_GROUND_RULES)
    return "\n".join(lines)


def wrap_task_prompt(prompt: str, task_kind: str, token_budget: int | None = None) -> str:
    return f"{task_contract_text(task_kind, token_budget)}\n\nRequest:\n\n{prompt.strip()}"


def infer_target_agent(target_agent: Any, target_model: Any = None) -> str:
    raw = f"{target_agent or ''} {target_model or ''}".lower()
    # Explicit CLI/app surfaces win over family defaults.
    if "claude code" in raw or "claude_cli" in raw or "claude cli" in raw:
        return "claude_code"
    if "codex_cli" in raw or "codex cli" in raw:
        return "codex_cli"
    if "gemini_cli" in raw or "gemini api" in raw:
        return "gemini_cli"
    # Otherwise fall back to the model family. This is what stops bare
    # "opus"/"sonnet" requests from being mistaken for Antigravity in-app Claude.
    return default_target_agent_for_family(model_family_for(target_agent, target_model))


def append_budgeted(lines: list[str], line: str, budget: int) -> bool:
    projected = sum(len(item) + 1 for item in lines) + len(line) + 1
    if projected <= budget:
        lines.append(line)
        return True
    remaining = budget - sum(len(item) + 1 for item in lines) - 1
    if remaining > 80:
        lines.append(line[: remaining - 15].rstrip() + " ... [truncated]")
    return False


def run_process(
    command: list[str],
    cwd: str,
    stdin_text: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["AGENT_BROKER_CHILD"] = "1"
    proc = subprocess.run(
        command,
        cwd=cwd,
        input=stdin_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def consult_codex(project: str | None, prompt: str, mode: str = "read-only", model_name: str | None = None) -> str:
    config = load_config()
    codex = discover_codex(config)
    if not codex:
        return (
            "Codex CLI was not found. Install Codex CLI or set codex_path in "
            f"{CONFIG_PATH}."
        )
    project_info = resolve_project(project)
    sandbox = "read-only" if mode not in {"workspace-write", "danger-full-access"} else mode
    command = [
        codex,
        "exec",
        "--cd",
        project_info.root_path,
        "--sandbox",
        sandbox,
        "--skip-git-repo-check",
        "--ephemeral",
        "-",
    ]
    if model_name:
        command[2:2] = ["--model", str(model_name)]
    code, stdout, stderr = run_process(command, project_info.root_path, sanitize_prompt(prompt))
    if code != 0:
        return f"Codex exited with code {code}.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}".strip()
    return stdout or stderr or "Codex returned no output."


def consult_claude(
    project: str | None,
    prompt: str,
    mode: str = "plan",
    model_name: str | None = None,
    workspace: str | None = None,
) -> str:
    config = load_config()
    claude = find_executable(config, "claude_path", ["claude", "claude.cmd", "claude.ps1"])
    if not claude:
        return (
            "Claude Code CLI was not found. Install Claude Code or set claude_path in "
            f"{CONFIG_PATH}."
        )
    project_info = resolve_project(project)
    permission_mode = "plan" if mode not in {"default", "acceptEdits", "bypassPermissions"} else mode
    command = [
        claude,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        permission_mode,
    ]
    claude_model = model_name or config.get("claude_model") or os.environ.get("CLAUDE_MODEL")
    if claude_model:
        command.extend(["--model", str(claude_model)])
    # Run in the per-topic workspace when given so the session buckets into its own
    # ~/.claude/projects folder; otherwise use the project root. When bucketing in a
    # workspace, still grant read access to the project via --add-dir so codebase
    # consults work. Prompt goes on stdin (claude -p) to dodge the Windows cmd limit.
    run_cwd = workspace or project_info.root_path
    if workspace and os.path.abspath(workspace) != os.path.abspath(project_info.root_path):
        command.extend(["--add-dir", project_info.root_path])
    code, stdout, stderr = run_process(command, run_cwd, sanitize_prompt(prompt))
    if code != 0:
        return f"Claude exited with code {code}.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}".strip()
    try:
        payload = json.loads(stdout)
        if isinstance(payload, dict):
            return str(payload.get("result") or payload.get("response") or stdout)
    except Exception:
        pass
    return stdout or stderr or "Claude returned no output."


def consult_gemini(project: str | None, prompt: str, mode: str = "read-only", model_name: str | None = None) -> str:
    config = load_config()
    project_info = resolve_project(project)
    gemini = find_executable(config, "gemini_path", ["gemini", "gemini.cmd", "gemini.ps1"])
    if gemini:
        gem_model = model_name or config.get("gemini_model") or os.environ.get("GEMINI_MODEL")
        command = [gemini]
        if gem_model:
            command += ["-m", str(gem_model)]
        command += ["-p", sanitize_prompt(prompt)]
        code, stdout, stderr = run_process(command, project_info.root_path)
        if code != 0:
            return f"Gemini CLI exited with code {code}.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}".strip()
        return stdout or stderr or "Gemini returned no output."

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = model_name or config.get("gemini_model") or os.environ.get("GEMINI_MODEL") or "gemini-2.5-pro"
    if not api_key:
        return (
            "Gemini is not configured. Install a gemini CLI or set GEMINI_API_KEY "
            "or GOOGLE_API_KEY for API-backed consultation."
        )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = json.dumps(
        {"contents": [{"parts": [{"text": sanitize_prompt(prompt)}]}]},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return f"Gemini API returned HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return f"Gemini API call failed: {exc}"
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "\n".join(part.get("text", "") for part in parts).strip() or json.dumps(payload)
    except Exception:
        return json.dumps(payload, ensure_ascii=False, indent=2)


def store_consultation(
    project_info: ProjectInfo,
    consulted_model: str,
    mode: str,
    prompt: str,
    response: str,
    status: str,
    error: str | None,
    started_at: str,
) -> None:
    init_db()
    branch = run_git(project_info.root_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit_sha = run_git(project_info.root_path, ["rev-parse", "HEAD"])
    caller = os.environ.get("AGENT_BROKER_CALLER") or "mcp-client"
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO consultations (
                project, root_path, branch, commit_sha, caller, consulted_model,
                mode, prompt, response, status, error, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_info.name,
                project_info.root_path,
                branch,
                commit_sha,
                caller,
                consulted_model,
                mode,
                prompt,
                response,
                status,
                error,
                started_at,
                utc_now(),
            ),
        )


def consult(model: str, args: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGENT_BROKER_CHILD") == "1":
        raise RuntimeError("Nested broker consultations are disabled to avoid recursive agent loops.")
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    project_arg = args.get("project")
    topic_arg = str(args.get("topic") or "").strip() or None
    task_kind = normalize_task_kind(args.get("task_kind"))
    token_budget = int(args.get("token_budget") or TASK_BUDGETS.get(task_kind, TASK_BUDGETS["consult"]))
    mode = str(args.get("mode") or ("plan" if model == "claude" else "read-only"))
    requested_model = args.get("target_model") or args.get("model_name") or args.get("model")
    resolved_model = str(requested_model).strip() if requested_model else None
    project_info = resolve_project(str(project_arg) if project_arg is not None else None)
    if task_kind != "consult" or args.get("include_task_contract", True) is not False:
        prompt = wrap_task_prompt(prompt, task_kind, token_budget)
    if topic_arg and args.get("include_context_pack", True) is not False:
        pack = get_context_pack(project_info.name, topic_arg, DEFAULT_CONTEXT_BUDGET)["content"]
        prompt = f"Shared context pack for this topic:\n\n{pack}\n\nCurrent request:\n\n{prompt}"
    started_at = utc_now()
    try:
        if model == "codex":
            response = consult_codex(project_info.root_path, prompt, mode, resolved_model)
        elif model == "claude":
            claude_workspace = None
            if topic_arg and load_config().get("topic_workspaces", True):
                claude_workspace = str(topic_workspace_dir(project_info, topic_arg))
            response = consult_claude(project_info.root_path, prompt, mode, resolved_model, claude_workspace)
        elif model == "gemini":
            response = consult_gemini(project_info.root_path, prompt, mode, resolved_model)
        else:
            raise ValueError(f"unknown model: {model}")
        failure_prefixes = (
            "Codex CLI was not found.",
            "Codex exited with code",
            "Claude Code CLI was not found.",
            "Claude exited with code",
            "Gemini is not configured.",
            "Gemini CLI exited with code",
            "Gemini API returned HTTP",
            "Gemini API call failed",
        )
        status = "error" if response.startswith(failure_prefixes) else "ok"
        error = response if status == "error" else None
        consulted_name = f"{model}:{resolved_model}" if resolved_model else model
        store_consultation(project_info, consulted_name, mode, prompt, response, status, error, started_at)
        return {
            "project": project_info.name,
            "root_path": project_info.root_path,
            "model": consulted_name,
            "mode": mode,
            "status": status,
            "response": response,
        }
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        store_consultation(project_info, model, mode, prompt, "", "error", error, started_at)
        raise


def get_history(project: str | None, limit: int = 20) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 20), 100))
    project_info = resolve_project(project)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, project, root_path, branch, commit_sha, caller, consulted_model,
                   mode, prompt, response, status, error, started_at, finished_at
            FROM consultations
            WHERE lower(project) = lower(?) OR root_path = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (project_info.name, project_info.root_path, limit),
        ).fetchall()
    return {"project": project_info.name, "items": [dict(row) for row in rows]}


def context_pack_path(project_info: ProjectInfo, topic: str | None) -> Path:
    return BROKER_DIR / "topics" / safe_slug(project_info.name) / safe_slug(topic or "all") / "context_pack.md"


def work_memory_path(project_info: ProjectInfo, topic: str | None) -> Path:
    return BROKER_DIR / "topics" / safe_slug(project_info.name) / safe_slug(topic or "all") / "work_memory.md"


def coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [line.strip(" -\t") for line in value.splitlines()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        items = [str(value).strip()]
    return [compact_text(item, 260) for item in items if item]


def topic_work_memory_section(
    project_info: ProjectInfo,
    topic: str | None,
    limit: int = 10,
    budget_chars: int = 5000,
) -> str:
    limit = max(1, min(int(limit or 10), 50))
    params: list[Any] = [project_info.name, project_info.root_path]
    topic_filter = ""
    if topic:
        topic_filter = "AND topic = ?"
        params.append(topic)
    params.append(limit)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, agent, event_type, summary, details, created_at
            FROM agent_events
            WHERE (lower(project) = lower(?) OR root_path = ?)
            {topic_filter}
            AND event_type NOT IN (
                'claimed_antigravity_request',
                'queued_antigravity_request',
                'queued_codex_request',
                'queued_claude_request'
            )
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    lines: list[str] = [
        "## Topic Work Memory",
        "",
        "Fast continuation log. Read this before broad files/history to see what another model changed, where, why, checks, risks, and next step.",
        "Update it after meaningful planning, edits, audits, or handoffs with `record_work_memory`.",
        "",
    ]
    if not rows:
        lines.append("- No work memory entries yet.")
        return "\n".join(lines).strip() + "\n"

    for row in rows:
        if not append_budgeted(
            lines,
            f"- {row['created_at']} | {row['agent']} | {row['event_type']}: {compact_text(row['summary'], 260)}",
            budget_chars,
        ):
            break
        details_obj: Any = None
        if row["details"]:
            try:
                details_obj = json.loads(row["details"])
            except Exception:
                details_obj = None
        if isinstance(details_obj, dict):
            fields = [
                ("files", details_obj.get("changed_files") or details_obj.get("files")),
                ("why", details_obj.get("why")),
                ("checks", details_obj.get("checks")),
                ("risks", details_obj.get("risks")),
                ("next", details_obj.get("next_step") or details_obj.get("next")),
                ("status", details_obj.get("status")),
            ]
            for label, value in fields:
                items = coerce_string_list(value)
                if items and not append_budgeted(lines, f"  {label}: {'; '.join(items)}", budget_chars):
                    break
        elif row["details"]:
            if not append_budgeted(lines, f"  details: {compact_text(row['details'], 420)}", budget_chars):
                break
    return "\n".join(lines).strip() + "\n"


def get_work_memory(project: str | None, topic: str | None = None, limit: int = 10) -> dict[str, Any]:
    init_db()
    project_info = resolve_project(project)
    content = topic_work_memory_section(project_info, topic, limit, 8000)
    path = work_memory_path(project_info, topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "project": project_info.name,
        "root_path": project_info.root_path,
        "topic": topic,
        "limit": max(1, min(int(limit or 10), 50)),
        "path": str(path),
        "content": content,
    }


def get_context_pack(project: str | None, topic: str | None = None, budget: int | None = None) -> dict[str, Any]:
    init_db()
    context_budget = max(1200, min(int(budget or DEFAULT_CONTEXT_BUDGET), 40000))
    project_info = resolve_project(project)
    topic_filter = ""
    params: list[Any] = [project_info.name, project_info.root_path]
    if topic:
        topic_filter = "AND topic = ?"
        params.append(topic)

    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            f"""
            SELECT id, agent, event_type, summary, details, created_at
            FROM agent_events
            WHERE (lower(project) = lower(?) OR root_path = ?)
            {topic_filter}
            ORDER BY id DESC
            LIMIT 60
            """,
            params,
        ).fetchall()
        consultations = conn.execute(
            """
            SELECT consulted_model, mode, prompt, response, status, error, finished_at
            FROM consultations
            WHERE lower(project) = lower(?) OR root_path = ?
            ORDER BY id DESC
            LIMIT 12
            """,
            (project_info.name, project_info.root_path),
        ).fetchall()
        antigravity_requests = conn.execute(
            f"""
            SELECT id, topic, target_model, request_type, status, created_by, claimed_by, created_at, completed_at
            FROM antigravity_requests
            WHERE (lower(project) = lower(?) OR root_path = ?)
            {topic_filter}
            ORDER BY created_at DESC
            LIMIT 20
            """,
            params,
        ).fetchall()
        codex_requests = conn.execute(
            f"""
            SELECT id, topic, status, created_by, created_at, notified_at, completed_at
            FROM codex_requests
            WHERE (lower(project) = lower(?) OR root_path = ?)
            {topic_filter}
            ORDER BY created_at DESC
            LIMIT 20
            """,
            params,
        ).fetchall()

    branch = run_git(project_info.root_path, ["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    commit_sha = run_git(project_info.root_path, ["rev-parse", "--short", "HEAD"]) or "unknown"
    lines: list[str] = [
        "# Agent Broker Context Pack",
        "",
        f"Project: {project_info.name}",
        f"Project path: {project_info.root_path}",
        f"Topic: {topic or 'all recent topics'}",
        f"Git: {branch} @ {commit_sha}",
        f"Generated: {utc_now()}",
        "",
        "Use this pack first. Expand full files, full history, or raw responses only when the pack points to a specific need.",
        "Large evidence is compressed with context_ref markers. Use retrieve_shared_context(ref, query) only when exact details are needed.",
        "",
    ]

    work_memory = topic_work_memory_section(project_info, topic, 10, max(1600, min(5000, context_budget // 3)))
    for line in work_memory.strip().splitlines():
        if not append_budgeted(lines, line, context_budget):
            break

    append_budgeted(lines, "", context_budget)
    append_budgeted(lines, "## Recent Topic Timeline", context_budget)

    if events:
        for row in events:
            if not append_budgeted(
                lines,
                f"- {row['created_at']} | {row['agent']} | {row['event_type']}: {compact_text(row['summary'], 260)}",
                context_budget,
            ):
                break
            if row["details"]:
                evidence = context_excerpt(
                    project_info.name,
                    topic,
                    f"agent_event:{row['id']}:{row['event_type']}",
                    row["details"],
                    520,
                )
                if not append_budgeted(lines, f"  evidence: {evidence}", context_budget):
                    break
    else:
        lines.append("- No recorded topic events yet.")

    append_budgeted(lines, "", context_budget)
    append_budgeted(lines, "## Recent Consultations", context_budget)
    if consultations:
        for idx, row in enumerate(consultations, start=1):
            prompt = compact_text(row["prompt"], 220)
            response = context_excerpt(
                project_info.name,
                topic,
                f"consultation:{idx}:{row['consulted_model']}:{row['finished_at']}",
                row["response"] or row["error"],
                520,
            )
            if not append_budgeted(
                lines,
                f"- {row['finished_at']} | {row['consulted_model']} | {row['mode']} | {row['status']}: {prompt} -> {response}",
                context_budget,
            ):
                break
    else:
        append_budgeted(lines, "- No stored consultations yet.", context_budget)

    append_budgeted(lines, "", context_budget)
    append_budgeted(lines, "## Bridge Requests", context_budget)
    for row in antigravity_requests:
        if not append_budgeted(
            lines,
            (
                f"- Antigravity {row['id']} | topic={row['topic'] or 'default'} | "
                f"target={row['target_model']} | status={row['status']} | by={row['created_by'] or 'unknown'}"
            ),
            context_budget,
        ):
            break
    for row in codex_requests:
        if not append_budgeted(
            lines,
            (
                f"- Codex {row['id']} | topic={row['topic'] or 'default'} | "
                f"status={row['status']} | by={row['created_by'] or 'unknown'}"
            ),
            context_budget,
        ):
            break
    if not antigravity_requests and not codex_requests:
        append_budgeted(lines, "- No queued bridge requests yet.", context_budget)

    append_budgeted(lines, "", context_budget)
    append_budgeted(lines, "## Expansion Tools", context_budget)
    append_budgeted(lines, "- get_topic_timeline(project, topic, limit) for fuller event history.", context_budget)
    append_budgeted(lines, "- get_consultation_history(project, limit) for raw consultation records.", context_budget)
    append_budgeted(lines, "- retrieve_shared_context(ref, query, limit) for context_ref originals or filtered matching lines.", context_budget)
    append_budgeted(lines, "- get_shared_context_stats(project, topic) to inspect compression savings.", context_budget)
    append_budgeted(lines, "- Read specific files only after this pack identifies a concrete file or question.", context_budget)

    content = "\n".join(lines).strip() + "\n"
    path = context_pack_path(project_info, topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "project": project_info.name,
        "root_path": project_info.root_path,
        "topic": topic,
        "budget": context_budget,
        "chars": len(content),
        "path": str(path),
        "content": content,
    }


def record_context_event(
    project: str | None,
    topic: str | None,
    agent: str,
    kind: str,
    summary: str,
    evidence: Any = None,
) -> dict[str, Any]:
    details = evidence if isinstance(evidence, str) else json.dumps(evidence, ensure_ascii=False, indent=2)
    return record_agent_event(project, topic, agent, kind, summary, details)


def record_work_memory(
    project: str | None,
    topic: str | None,
    agent: str,
    summary: str,
    changed_files: Any = None,
    why: Any = None,
    checks: Any = None,
    risks: Any = None,
    next_step: Any = None,
    status: Any = None,
) -> dict[str, Any]:
    details = {
        "changed_files": coerce_string_list(changed_files),
        "why": coerce_string_list(why),
        "checks": coerce_string_list(checks),
        "risks": coerce_string_list(risks),
        "next_step": coerce_string_list(next_step),
        "status": coerce_string_list(status),
    }
    details = {key: value for key, value in details.items() if value}
    result = record_agent_event(
        project,
        topic,
        agent,
        "work_memory",
        summary,
        json.dumps(details, ensure_ascii=False, indent=2) if details else None,
    )
    try:
        memory = get_work_memory(project, topic, 10)
        result["memory_file"] = memory["path"]
    except Exception as exc:  # noqa: BLE001
        result["memory_warning"] = str(exc)
    return result


def queue_antigravity_request(
    project: str | None,
    prompt: str,
    topic: str | None = None,
    target_model: str | None = None,
    request_type: str | None = None,
    task_kind: str | None = None,
    strict_model: Any = None,
    token_budget: int | None = None,
) -> dict[str, Any]:
    init_db()
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    project_info = resolve_project(project)
    request_id = str(uuid.uuid4())
    now = utc_now()
    created_by = os.environ.get("AGENT_BROKER_CALLER") or "mcp-client"
    normalized_model = normalize_model_name(target_model)
    normalized_task = normalize_task_kind(task_kind or request_type)
    budget = int(token_budget or TASK_BUDGETS.get(normalized_task, TASK_BUDGETS["consult"]))
    strict = (
        truthy(strict_model)
        if strict_model is not None
        else normalized_model != "Antigravity current selected model"
    )
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO antigravity_requests (
                id, project, root_path, topic, target_model, request_type,
                prompt, status, created_by, created_at, task_kind, strict_model, token_budget
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                project_info.name,
                project_info.root_path,
                topic,
                normalized_model,
                request_type or "consult",
                prompt.strip(),
                created_by,
                now,
                normalized_task,
                1 if strict else 0,
                budget,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_events (
                project, root_path, topic, agent, event_type, summary, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_info.name,
                project_info.root_path,
                topic,
                created_by,
                "queued_antigravity_request",
                f"Queued Antigravity request {request_id}",
                prompt.strip(),
                now,
            ),
        )
    try:
        render_request_ledger(project_info.name, topic)
    except Exception as exc:  # noqa: BLE001
        log(f"ledger refresh after queue-antigravity failed: {exc}")
    return {
        "id": request_id,
        "project": project_info.name,
        "root_path": project_info.root_path,
        "topic": topic,
        "target_model": normalized_model,
        "task_kind": normalized_task,
        "strict_model": strict,
        "token_budget": budget,
        "status": "queued",
    }


def claim_antigravity_request(consumer: str = "antigravity-bridge") -> dict[str, Any]:
    init_db()
    now = utc_now()
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM antigravity_requests
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return {"status": "empty"}
        conn.execute(
            """
            UPDATE antigravity_requests
            SET status = 'in_progress', claimed_by = ?, claimed_at = ?
            WHERE id = ?
            """,
            (consumer, now, row["id"]),
        )
        updated = conn.execute(
            "SELECT * FROM antigravity_requests WHERE id = ?",
            (row["id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO agent_events (
                project, root_path, topic, agent, event_type, summary, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["project"],
                row["root_path"],
                row["topic"],
                consumer,
                "claimed_antigravity_request",
                f"Claimed Antigravity request {row['id']}",
                row["prompt"],
                now,
            ),
        )
        conn.commit()
    request = dict(updated)
    try:
        request["context_pack"] = get_context_pack(request["project"], request.get("topic"), DEFAULT_CONTEXT_BUDGET)[
            "content"
        ]
    except Exception as exc:  # noqa: BLE001
        log(f"failed to attach context pack to request {request.get('id')}: {exc}")
    return {"status": "claimed", "request": request}


def complete_antigravity_request(
    request_id: str,
    response: str,
    status: str = "ok",
    model: str | None = None,
) -> dict[str, Any]:
    init_db()
    if not request_id or not request_id.strip():
        raise ValueError("request_id is required")
    if response is None:
        raise ValueError("response is required")
    final_status = "ok" if status not in {"error", "cancelled"} else status
    now = utc_now()
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM antigravity_requests WHERE id = ?",
            (request_id.strip(),),
        ).fetchone()
        if not row:
            raise ValueError(f"unknown Antigravity request: {request_id}")
        # Guard against re-completion: a stale fallback file or a manual retry must
        # not re-store the consultation, re-emit the event, or re-queue the callback.
        if row["status"] in {"completed", "error", "cancelled"}:
            return {
                "id": request_id.strip(),
                "status": row["status"],
                "already_completed": True,
                "note": "Request was already terminal; no side effects re-run.",
            }
        cur = conn.execute(
            """
            UPDATE antigravity_requests
            SET status = ?, response = ?, error = ?, completed_at = ?
            WHERE id = ? AND status NOT IN ('completed', 'error', 'cancelled')
            """,
            (
                "completed" if final_status == "ok" else final_status,
                response,
                response if final_status == "error" else None,
                now,
                request_id.strip(),
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            # Lost the race to a concurrent completer; report its ACTUAL terminal state
            # (could be error/cancelled, not necessarily completed) and skip side effects.
            raced = conn.execute(
                "SELECT status FROM antigravity_requests WHERE id = ?",
                (request_id.strip(),),
            ).fetchone()
            return {
                "id": request_id.strip(),
                "status": (raced["status"] if raced else "completed"),
                "already_completed": True,
                "note": "Request was already completed concurrently; no side effects re-run.",
            }
    project_info = ProjectInfo(row["project"], row["root_path"] or "")
    store_consultation(
        project_info,
        model or row["target_model"] or "antigravity",
        row["request_type"] or "consult",
        row["prompt"],
        response,
        "ok" if final_status == "ok" else "error",
        response if final_status == "error" else None,
        row["created_at"],
    )
    record_agent_event(
        row["project"],
        row["topic"],
        model or row["target_model"] or "antigravity",
        "completed_antigravity_request",
        f"Completed Antigravity request {request_id}",
        response,
    )
    callback_result = None
    callback = extract_codex_callback(response) if final_status == "ok" else None
    if callback:
        callback_result = queue_codex_request(row["project"], callback, row["topic"])
        record_agent_event(
            row["project"],
            row["topic"],
            "agent-broker",
            "queued_codex_callback",
            f"Queued Codex callback {callback_result['id']} from Antigravity request {request_id}",
            callback,
        )
    try:
        render_request_ledger(row["project"], row["topic"])
    except Exception as exc:  # noqa: BLE001
        log(f"ledger refresh after complete failed: {exc}")
    result = {"id": request_id.strip(), "status": "completed" if final_status == "ok" else final_status}
    if callback_result:
        result["codex_callback"] = callback_result
    return result


def get_antigravity_requests(project: str | None, limit: int = 20) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 20), 100))
    if not project or str(project).strip() == "*":
        with db_connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM antigravity_requests
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"project": "*", "items": [dict(row) for row in rows]}
    project_info = resolve_project(project)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM antigravity_requests
            WHERE lower(project) = lower(?) OR root_path = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (project_info.name, project_info.root_path, limit),
        ).fetchall()
    return {"project": project_info.name, "items": [dict(row) for row in rows]}


def get_unnotified_antigravity_completions(limit: int = 20) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 20), 100))
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM antigravity_requests
            WHERE status = 'completed' AND completion_notified_at IS NULL
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


def requeue_antigravity_request(request_id: str) -> dict[str, Any]:
    init_db()
    if not request_id or not request_id.strip():
        raise ValueError("request_id is required")
    with db_connect() as conn:
        cursor = conn.execute(
            """
            UPDATE antigravity_requests
            SET status = 'queued', claimed_by = NULL, claimed_at = NULL
            WHERE id = ? AND status = 'in_progress'
            """,
            (request_id.strip(),),
        )
    return {"id": request_id.strip(), "status": "queued", "updated": cursor.rowcount}


def await_antigravity_model_selection(request_id: str) -> dict[str, Any]:
    init_db()
    if not request_id or not request_id.strip():
        raise ValueError("request_id is required")
    with db_connect() as conn:
        cursor = conn.execute(
            """
            UPDATE antigravity_requests
            SET status = 'awaiting_model_selection'
            WHERE id = ? AND status = 'in_progress'
            """,
            (request_id.strip(),),
        )
    return {"id": request_id.strip(), "status": "awaiting_model_selection", "updated": cursor.rowcount}


def resume_antigravity_model_selection(request_id: str | None = None) -> dict[str, Any]:
    init_db()
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        if request_id and request_id.strip():
            target = conn.execute(
                "SELECT id FROM antigravity_requests WHERE id = ? AND status = 'awaiting_model_selection'",
                (request_id.strip(),),
            ).fetchone()
        else:
            target = conn.execute(
                """
                SELECT id FROM antigravity_requests
                WHERE status = 'awaiting_model_selection'
                ORDER BY claimed_at DESC, created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if not target:
            return {"status": "empty", "updated": 0}
        cursor = conn.execute(
            """
            UPDATE antigravity_requests
            SET status = 'queued', claimed_by = NULL, claimed_at = NULL
            WHERE id = ?
            """,
            (target["id"],),
        )
    return {"id": target["id"], "status": "queued", "updated": cursor.rowcount}


def get_awaiting_model_requests(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 20), 100))
    if not project or str(project).strip() == "*":
        with db_connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM antigravity_requests
                WHERE status = 'awaiting_model_selection'
                ORDER BY claimed_at DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"project": "*", "items": [dict(row) for row in rows]}
    project_info = resolve_project(project)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM antigravity_requests
            WHERE status = 'awaiting_model_selection'
              AND (lower(project) = lower(?) OR root_path = ?)
            ORDER BY claimed_at DESC, created_at DESC
            LIMIT ?
            """,
            (project_info.name, project_info.root_path, limit),
        ).fetchall()
    return {"project": project_info.name, "items": [dict(row) for row in rows]}


def mark_antigravity_completion_notified(request_id: str) -> dict[str, Any]:
    init_db()
    if not request_id or not request_id.strip():
        raise ValueError("request_id is required")
    now = utc_now()
    with db_connect() as conn:
        cursor = conn.execute(
            """
            UPDATE antigravity_requests
            SET completion_notified_at = ?
            WHERE id = ? AND completion_notified_at IS NULL
            """,
            (now, request_id.strip()),
        )
    return {"id": request_id.strip(), "status": "completion_notified", "updated": cursor.rowcount}


def record_agent_event(
    project: str | None,
    topic: str | None,
    agent: str,
    event_type: str,
    summary: str,
    details: str | None = None,
) -> dict[str, Any]:
    init_db()
    project_info = resolve_project(project)
    if not agent or not agent.strip():
        raise ValueError("agent is required")
    if not event_type or not event_type.strip():
        raise ValueError("event_type is required")
    if not summary or not summary.strip():
        raise ValueError("summary is required")
    now = utc_now()
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_events (
                project, root_path, topic, agent, event_type, summary, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_info.name,
                project_info.root_path,
                topic,
                agent.strip(),
                event_type.strip(),
                summary.strip(),
                details,
                now,
            ),
        )
        event_id = cursor.lastrowid
    return {"id": event_id, "project": project_info.name, "topic": topic, "status": "recorded"}


def get_topic_timeline(project: str | None, topic: str | None = None, limit: int = 50) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 50), 200))
    project_info = resolve_project(project)
    params: list[Any] = [project_info.name, project_info.root_path]
    topic_filter = ""
    if topic:
        topic_filter = "AND topic = ?"
        params.append(topic)
    params.append(limit)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, project, root_path, topic, agent, event_type, summary, details, created_at
            FROM agent_events
            WHERE (lower(project) = lower(?) OR root_path = ?)
            {topic_filter}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return {"project": project_info.name, "topic": topic, "items": [dict(row) for row in rows]}


def queue_codex_request(
    project: str | None,
    prompt: str,
    topic: str | None = None,
    target_model: str | None = None,
    strict_model: Any = None,
) -> dict[str, Any]:
    init_db()
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    project_info = resolve_project(project)
    request_id = str(uuid.uuid4())
    now = utc_now()
    created_by = os.environ.get("AGENT_BROKER_CALLER") or "mcp-client"
    clean_prompt = prompt.strip()
    model_label = (str(target_model).strip() or None) if target_model else None
    strict_flag = 1 if truthy(strict_model) else 0
    # Direct callers that name a model get the self-check guard too; route_agent_task
    # already prepends it, so skip if it's present to avoid double-injection.
    if model_label and "[REQUIRED MODEL:" not in clean_prompt and "[Preferred model:" not in clean_prompt:
        clean_prompt = model_guard_text(model_label, strict=bool(strict_flag)) + clean_prompt
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            """
            SELECT id, project, root_path, topic, status, created_by, created_at, notified_at,
                   completed_at, target_model, strict_model
            FROM codex_requests
            WHERE (lower(project) = lower(?) OR root_path = ?)
              AND ((topic IS NULL AND ? IS NULL) OR topic = ?)
              AND prompt = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_info.name, project_info.root_path, topic, topic, clean_prompt),
        ).fetchone()
        if existing:
            result = dict(existing)
            result["deduped"] = True
            return result
        conn.execute(
            """
            INSERT INTO codex_requests (
                id, project, root_path, topic, prompt, status, created_by, created_at,
                target_model, strict_model
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                request_id,
                project_info.name,
                project_info.root_path,
                topic,
                clean_prompt,
                created_by,
                now,
                model_label,
                strict_flag,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_events (
                project, root_path, topic, agent, event_type, summary, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_info.name,
                project_info.root_path,
                topic,
                created_by,
                "queued_codex_request",
                f"Queued Codex request {request_id}",
                clean_prompt,
                now,
            ),
        )
    try:
        render_request_ledger(project_info.name, topic)
    except Exception as exc:  # noqa: BLE001
        log(f"ledger refresh after queue-codex failed: {exc}")
    return {
        "id": request_id,
        "project": project_info.name,
        "root_path": project_info.root_path,
        "topic": topic,
        "target_model": model_label,
        "strict_model": bool(strict_flag),
        "status": "queued",
    }


def get_codex_requests(project: str | None, limit: int = 20) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 20), 100))
    if not project or str(project).strip() == "*":
        with db_connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM codex_requests
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"project": "*", "items": [dict(row) for row in rows]}
    project_info = resolve_project(project)
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM codex_requests
            WHERE lower(project) = lower(?) OR root_path = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (project_info.name, project_info.root_path, limit),
        ).fetchall()
    return {"project": project_info.name, "items": [dict(row) for row in rows]}


def ledger_path(project_info: ProjectInfo, topic: str | None) -> Path:
    return BROKER_DIR / "topics" / safe_slug(project_info.name) / safe_slug(topic or "all") / "ledger.md"


def _ledger_oneline(value: Any, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    text = text.replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _iso_epoch(value: Any) -> int | None:
    try:
        return int(time.mktime(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:  # noqa: BLE001
        return None


def _latency(created: Any, completed: Any) -> str:
    a, b = _iso_epoch(created), _iso_epoch(completed)
    if a is None or b is None or b < a:
        return "-"
    secs = b - a
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def render_request_ledger(project: str | None, topic: str | None) -> dict[str, Any]:
    """Render a per-topic, human-readable request->answer->timing ledger from SQLite (the
    broker is the SINGLE writer, so there is no concurrent-edit corruption). SQLite stays the
    source of truth; ledger.md is a generated view that any IDE/agent can open to track a topic."""
    init_db()
    project_info = resolve_project(project)
    entries: list[tuple[str, dict[str, Any]]] = []
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        for table, kind in (("codex_requests", "codex"), ("antigravity_requests", "antigravity")):
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE lower(project) = lower(?) "
                    "AND COALESCE(topic,'') = COALESCE(?, '') ORDER BY created_at",
                    (project_info.name, topic),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            entries.extend((kind, dict(row)) for row in rows)
    entries.sort(key=lambda e: e[1].get("created_at") or "")

    header = [
        f"# Request Ledger - {project_info.name} / {topic or 'all'}",
        "",
        "*Generated by the Agent Broker (single writer). Source of truth is SQLite; this is a view.*",
        "",
        "| # | Created (UTC) | To | Model | Status | Latency | Request | Answer |",
        "|---|---|---|---|---|---|---|---|",
    ]
    table_rows: list[str] = []
    answers: list[str] = []
    for i, (kind, row) in enumerate(entries, 1):
        model = row.get("responder_model") or row.get("target_model") or "-"
        answer_cell = "(awaiting)" if not row.get("response") else _ledger_oneline(row.get("response"), 60)
        table_rows.append(
            f"| {i} | {row.get('created_at') or '-'} | {kind} | {_ledger_oneline(model, 28)} | "
            f"{row.get('status') or '-'} | {_latency(row.get('created_at'), row.get('completed_at'))} | "
            f"{_ledger_oneline(row.get('prompt'), 60)} | {answer_cell} |"
        )
        if row.get("response"):
            responder = row.get("responder") or kind
            answers.append(
                f"### {i}. {kind} - {responder}"
                + (f" ({row.get('responder_model')})" if row.get("responder_model") else "")
                + f" - {row.get('completed_at') or ''}\n\n"
                + (str(row.get("response"))[:1500] + (" …[truncated]" if len(str(row.get("response"))) > 1500 else ""))
            )
    if not entries:
        table_rows.append("| - | - | - | - | - | - | (no requests yet) | - |")
    content = "\n".join(header + table_rows)
    if answers:
        content += "\n\n## Answers\n\n" + "\n\n".join(answers)
    content += "\n"
    path = ledger_path(project_info, topic)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log(f"failed to write ledger {path}: {exc}")
    return {"project": project_info.name, "topic": topic, "path": str(path), "count": len(entries), "content": content}


def respond_to_request(
    project: str | None,
    topic: str | None,
    request_id: str,
    response: str,
    agent: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Return path for ANY surface. A receiving agent (Codex/Claude/Antigravity) calls this with
    its answer; the broker records it on the queued request (response + timing + responder) and
    refreshes the ledger. This is the symmetric reply the audit flagged as missing for Codex."""
    init_db()
    rid = str(request_id or "").strip()
    if not rid:
        raise ValueError("request_id is required")
    if response is None:
        raise ValueError("response is required")
    now = utc_now()
    found: tuple[str, dict[str, Any]] | None = None
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        for table in ("codex_requests", "antigravity_requests"):
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
            if not row:
                continue
            found = (table, dict(row))
            conn.execute(
                f"""
                UPDATE {table}
                SET response = ?, responder = ?, responder_model = ?,
                    completed_at = COALESCE(completed_at, ?),
                    status = CASE WHEN status IN ('completed','error','cancelled') THEN status ELSE 'completed' END
                WHERE id = ?
                """,
                (response, agent or "agent", model, now, rid),
            )
            conn.commit()
            break
    if not found:
        raise ValueError(f"unknown request: {request_id}")
    table, row = found
    proj = project or row.get("project")
    top = topic if topic is not None else row.get("topic")
    record_agent_event(proj, top, agent or "agent", "response_recorded",
                       f"Response recorded for {table.split('_')[0]} request {rid}", response)
    ledger = None
    try:
        ledger = render_request_ledger(proj, top)
    except Exception as exc:  # noqa: BLE001
        log(f"ledger render after respond failed: {exc}")
    return {
        "id": rid,
        "status": "recorded",
        "request_kind": table.split("_")[0],
        "recorded_at": now,
        "responder": agent or "agent",
        "responder_model": model,
        "ledger_path": (ledger or {}).get("path"),
    }


def get_request_ledger(project: str | None, topic: str | None) -> dict[str, Any]:
    """Render + return the per-topic request ledger (and write topics/<project>/<topic>/ledger.md)."""
    return render_request_ledger(project, topic)


def mark_codex_request_notified(request_id: str) -> dict[str, Any]:
    init_db()
    if not request_id or not request_id.strip():
        raise ValueError("request_id is required")
    now = utc_now()
    with db_connect() as conn:
        cursor = conn.execute(
            """
            UPDATE codex_requests
            SET status = 'notified', notified_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, request_id.strip()),
        )
    return {"id": request_id.strip(), "status": "notified", "updated": cursor.rowcount}


CODEX_EXTENSION_HINTS = ("openai.chatgpt", "openai.codex", "chatgpt", "codex")
CLAUDE_EXTENSION_HINTS = ("anthropic.claude", "claude-code", "claude-vscode", "claude.code", "claude")


def _extension_scan_dirs() -> list[Path]:
    home = Path.home()
    # Antigravity, VS Code, Insiders, and Cursor are separate VS Code forks,
    # each with its own extensions folder. The same agent extension may live in
    # any of them, so scan all known locations to avoid false negatives.
    dirs = [
        home / ".vscode" / "extensions",
        home / ".vscode-insiders" / "extensions",
        home / ".antigravity" / "extensions",
        home / ".cursor" / "extensions",
    ]
    extra = load_config().get("extension_dirs") or []
    for item in extra:
        try:
            dirs.append(Path(str(item)))
        except Exception:  # noqa: BLE001
            continue
    return dirs


def detect_agent_surfaces() -> dict[str, dict[str, Any]]:
    """Best-effort scan of VS Code/host extension folders.

    `extension=None` means we could not scan (no folder), so callers should not
    downgrade to the app on that basis. `True`/`False` are confident results.
    """
    matches = {"codex": False, "claude": False}
    scanned = False
    for directory in _extension_scan_dirs():
        try:
            if not directory.exists():
                continue
            scanned = True
            for child in directory.iterdir():
                name = child.name.lower()
                if any(hint in name for hint in CODEX_EXTENSION_HINTS):
                    matches["codex"] = True
                if any(hint in name for hint in CLAUDE_EXTENSION_HINTS):
                    matches["claude"] = True
        except Exception:  # noqa: BLE001
            continue
    return {
        "scanned": scanned,
        "codex": {"extension": matches["codex"] if scanned else None},
        "claude": {"extension": matches["claude"] if scanned else None},
    }


def surface_available(family: str, surface: str) -> bool:
    """Whether the requested delivery surface exists for a model family.

    Visible app and CLI/headless surfaces report their own availability.
    Extension availability is config-overridable, then falls back to detection;
    an unscanned result is treated as available so we do not silently route to a
    different surface.
    """
    if surface != "extension":
        return True
    cfg = (load_config().get("surfaces") or {}).get(family) or {}
    if isinstance(cfg.get("extension"), bool):
        return cfg["extension"]
    detected = detect_agent_surfaces().get(family, {}).get("extension")
    return True if detected is None else bool(detected)


def resolve_surface(args: dict[str, Any]) -> str:
    """Decide extension vs visible app vs CLI. Default is extension."""
    explicit = str(args.get("surface") or "").strip().lower()
    if explicit in {"extension", "ext", "panel", "ide"}:
        return "extension"
    if explicit in {"app", "desktop", "gui", "standalone", "standalone_app"}:
        return "app"
    if explicit in {"cli", "headless", "terminal"}:
        return "cli"
    blob = " ".join(
        str(args.get(key) or "")
        for key in ("target_agent", "agent", "target_model", "model", "surface")
    ).lower()
    if any(token in blob for token in ("_cli", " cli", "headless", "terminal")):
        return "cli"
    if any(token in blob for token in ("desktop app", "standalone app", "visible app", "gui app")):
        return "app"
    return "extension"


def queue_claude_request(
    project: str | None,
    prompt: str,
    topic: str | None = None,
    target_model: str | None = None,
    task_kind: str | None = None,
    token_budget: int | None = None,
    new_chat: Any = None,
) -> dict[str, Any]:
    """Default 'Claude Code extension' delivery: write an inbox markdown file.

    The bridge extension polls this inbox and attempts to open/submit it in
    Claude Code. The inbox file remains the durable fallback; the CLI route
    (claude_code) is the headless app fallback.
    """
    init_db()
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    project_info = resolve_project(project)
    request_id = str(uuid.uuid4())
    now = utc_now()
    created_by = os.environ.get("AGENT_BROKER_CALLER") or "mcp-client"
    model_label = target_model or "Claude (extension-selected model)"
    force_new_chat = truthy(new_chat)
    thread_policy = "new Claude session requested" if force_new_chat else "same project/topic session by default"
    compact_section = compacted_topic_handoff_section(project_info, topic)
    body = (
        f"# Claude Inbox Request - Requested model: {model_label}\n\n"
        f"Request ID: {request_id}\n"
        f"Project: {project_info.name}\n"
        f"Topic: {topic or '(none)'}\n"
        f"Requested model: {model_label}\n"
        f"New chat: {'yes' if force_new_chat else 'no'}\n"
        f"Thread policy: {thread_policy}\n"
        f"Created by: {created_by}\n"
        f"Created at: {now}\n\n"
        f"> Open this in the Claude Code extension panel. To reply through the broker, "
        f"call queue_codex_request / complete_antigravity_request, or write a response file "
        f"under .agent-broker/claude-responses/.\n\n"
        f"---\n\n"
        f"Requested Claude model: {model_label}\n"
        f"Broker topic: {topic or '(none)'}\n"
        f"Thread policy: {thread_policy}\n"
        f"Reply routing: respond through the broker to the requesting agent on this same topic unless the user asks for a new chat.\n\n"
        f"{compact_section}"
        f"{prompt.strip()}\n"
    )
    written: list[str] = []
    for inbox_dir in (BROKER_DIR / "claude-inbox", Path(project_info.root_path) / ".agent-broker" / "claude-inbox"):
        try:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            target = inbox_dir / f"{request_id}.md"
            target.write_text(body, encoding="utf-8")
            written.append(str(target))
        except Exception as exc:  # noqa: BLE001
            log(f"claude inbox write failed for {inbox_dir}: {exc}")
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_events (
                project, root_path, topic, agent, event_type, summary, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_info.name,
                project_info.root_path,
                topic,
                created_by,
                "queued_claude_request",
                f"Queued Claude inbox request {request_id} ({model_label})",
                prompt.strip(),
                now,
            ),
        )
    return {
        "id": request_id,
        "project": project_info.name,
        "root_path": project_info.root_path,
        "topic": topic,
        "target_model": model_label,
        "new_chat": force_new_chat,
        "task_kind": normalize_task_kind(task_kind),
        "token_budget": int(token_budget or 0) or None,
        "inbox_files": written,
        "status": "queued",
        "note": "Queued for the bridge to open/submit in Claude Code. The inbox file is the durable fallback; CLI is the headless fallback.",
    }


def prompt_budget_notice(
    project: str | None, topic: str | None, prompt: str, source: str = "handoff_prompt"
) -> dict[str, Any] | None:
    """Token-economy guard for cross-agent handoffs. If the raw prompt is large, stash
    the full text as a retrievable context_ref and return a notice nudging the caller to
    send a short instruction + ref next time. Never mutates the delivered prompt, so a
    long instruction still works — the caller just learns to be lean. Returns None when
    the prompt is within budget or on any error (the guard must never block delivery)."""
    try:
        tokens = estimate_tokens(prompt or "")
    except Exception:  # noqa: BLE001
        return None
    if tokens <= PROMPT_SOFT_LIMIT_TOKENS:
        return None
    ref = None
    try:
        ref = store_shared_context(project, topic, prompt, source, "handoff_prompt").get("ref")
    except Exception as exc:  # noqa: BLE001
        log(f"prompt_budget_notice: could not stash oversized prompt: {exc}")
    message = (
        f"This handoff prompt is ~{tokens} tokens (soft limit {PROMPT_SOFT_LIMIT_TOKENS}). "
        "The broker is built for token economy: send a SHORT instruction and reference large "
        "context with a context_ref instead of inlining it — a long prompt usually duplicates "
        "files/state the receiver can read itself (see AGENT_COOP_RULES 'Token Rules')."
    )
    if ref:
        message += f" The full prompt was stashed as {ref}; use retrieve_shared_context(ref, query) if needed."
    return {
        "tokens": tokens,
        "soft_limit_tokens": PROMPT_SOFT_LIMIT_TOKENS,
        "context_ref": ref,
        "message": message,
    }


def route_agent_task(args: dict[str, Any]) -> dict[str, Any]:
    """Public router entry. Runs the token-economy guard on the raw prompt, then delegates
    to the routing impl and attaches a `prompt_notice` to actual deliveries."""
    result = _route_agent_task_impl(args)
    try:
        if isinstance(result, dict) and result.get("status") not in (None, "needs_model_selection"):
            notice = prompt_budget_notice(args.get("project"), args.get("topic"), str(args.get("prompt") or ""))
            if notice:
                result["prompt_notice"] = notice
    except Exception as exc:  # noqa: BLE001
        log(f"route_agent_task prompt guard failed: {exc}")
    return result


def _route_agent_task_impl(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    project = args.get("project")
    topic = args.get("topic")
    # Pass the RAW model into resolution. normalize_model_name applies the
    # Antigravity display-name aliases (opus -> "Claude Opus 4.6 (Thinking)"),
    # which would corrupt Claude/Codex CLI resolution if applied this early.
    target_model = str(args.get("target_model") or args.get("model") or "")
    new_chat = truthy(args.get("new_chat") or args.get("force_new_chat"))
    # Conservative fallback: if no model was named in the structured args, look for an
    # explicit "ask/get Opus" style mention in the prompt so the topic default (e.g.
    # Sonnet) doesn't silently win. A detected mention is a one-off (remember_model
    # stays False below) and never rewrites the stored topic default.
    detected_model = None
    if not target_model.strip():
        detected_model = detect_model_in_prompt(prompt)
        if detected_model:
            target_model = detected_model
    requested_label = target_model.strip()
    # Detect the family over the WHOLE routing intent (all hint fields), so a host
    # word like "antigravity"/"vscode" sitting next to an explicit family + an
    # "extension" intent routes to that family's extension rather than the
    # Antigravity in-app panel. This is what fixes "claude extension in antigravity".
    routing_blob = normalize_lookup(
        " ".join(
            str(args.get(k) or "")
            for k in ("target_agent", "agent", "target_model", "model", "surface", "target_host", "host", "ide_host", "ide")
        )
    )
    intent_family = model_family_for(routing_blob)
    target_agent = infer_target_agent(args.get("target_agent") or args.get("agent"), target_model)
    if intent_family in ("claude", "codex", "gemini") and model_family_for(target_agent, target_model) == "antigravity":
        # The narrow per-field inference picked Antigravity-as-host; the full intent
        # names a real family hosted in that IDE. Correct it before model resolution.
        target_agent = default_target_agent_for_family(intent_family)
    model_resolution = resolve_model_request(
        {
            "project": project,
            "topic": topic,
            "target_agent": target_agent,
            "target_model": target_model,
            # A model only mentioned in the prompt is a one-off; don't overwrite the
            # stored topic default with it.
            "remember_model": False if detected_model else args.get("remember_model", True),
        }
    )
    if model_resolution.get("status") == "needs_model_selection":
        return model_resolution
    if model_resolution.get("status") == "resolved":
        target_agent = model_resolution["target_agent"]
        target_model = model_resolution["target_model"]
    # Mark a prompt-detected model BEFORE computing requested_explicitly so the source
    # is set when it's read.
    if detected_model:
        model_resolution["source"] = "prompt_detected"
        model_resolution["detected_from_prompt"] = detected_model
    # A concrete model was actively requested (explicit arg or detected in the prompt),
    # as opposed to falling back to the topic default / current selected model.
    requested_explicitly = (
        model_resolution.get("status") == "resolved"
        and model_resolution.get("source") in ("explicit_request", "prompt_detected")
    )

    # Surface selection: default to the extension. "app" means a visible
    # desktop app handoff; "cli" is the headless backend.
    surface = resolve_surface(args)
    family = model_family_for(target_agent, target_model)
    # Prefer the full-intent family when the resolved agent collapsed back to the
    # Antigravity host but a real family was named with extension/host intent.
    if family == "antigravity" and intent_family in ("claude", "codex", "gemini"):
        family = intent_family
    surface_note: str | None = None
    ide_host = resolve_ide_host(args, target_agent)
    if family == "claude":
        if surface == "cli":
            target_agent = "claude_code"
        elif surface == "app":
            target_agent = "claude_app"
        elif surface_available("claude", "extension"):
            target_agent = "claude_ext"
        else:
            target_agent = "claude_app"
            surface_note = "Claude extension not detected; fell back to visible Claude app handoff."
    elif family == "codex":
        if surface == "cli":
            target_agent = "codex_cli"
        elif surface == "app":
            target_agent = "codex_app"
        elif surface_available("codex", "extension"):
            target_agent = "codex"
        else:
            target_agent = "codex_app"
            surface_note = "Codex extension not detected; fell back to visible Codex app handoff."

    # Antigravity hosts a separate, subscription-backed Claude/Gemini. Never
    # silently use whatever is selected: require an explicit model choice.
    if target_agent == "antigravity" and normalize_model_name(target_model) == "Antigravity current selected model":
        catalog = list_agent_models("antigravity", project, topic).get("catalogs", {}).get("antigravity", {})
        return {
            "status": "needs_model_selection",
            "reason": "Antigravity is the target but no specific Antigravity model was named.",
            "ask_user": "Which Antigravity model should run this? (e.g. Gemini 3.5 Flash (High), Claude Opus 4.6 (Thinking))",
            "model_family": "antigravity",
            "target_agent": "antigravity",
            "choices": catalog.get("models") or [],
            "action": "Call route_agent_task again with target_model set to the chosen Antigravity model.",
        }

    task_kind = normalize_task_kind(args.get("task_kind") or args.get("request_type"))
    token_budget = int(args.get("token_budget") or TASK_BUDGETS.get(task_kind, TASK_BUDGETS["consult"]))
    # Surfaces the broker cannot switch programmatically. For these, a concrete model
    # request is enforced by making the receiving agent self-check (model_guard_text)
    # and, when strict, stop + ask the user to switch instead of letting a lesser/default
    # model answer.
    GUARD_SURFACES = {"codex", "codex_app", "claude_ext", "claude_app"}
    strict_model = args.get("strict_model")
    if strict_model is None:
        strict_model = (
            (target_agent == "antigravity" and target_model != "Antigravity current selected model")
            or (requested_explicitly and target_agent in GUARD_SURFACES)
        )

    if target_agent == "antigravity":
        # Make sure the visible Antigravity panel is up (focuses an existing
        # instance), then drive its model chooser over CDP so the requested model
        # is actually selected before the prompt is sent — this is the broker-side
        # equivalent of picking the model by hand in the app.
        launch = launch_ide_host("antigravity", resolve_project(project).root_path)
        model_selection = None
        if load_config().get("antigravity_cdp_autoselect", False):
            model_selection = cdp_select_antigravity_model(target_model)
        queued = queue_antigravity_request(
            project,
            prompt,
            topic,
            target_model,
            args.get("request_type") or "consult",
            task_kind,
            strict_model,
            token_budget,
        )
        queued["route"] = "antigravity"
        queued["surface"] = "extension"
        queued["launch"] = launch
        queued["model_selection"] = model_selection
        queued["model_resolution"] = model_resolution
        return queued

    wrapped_prompt = wrap_task_prompt(prompt, task_kind, token_budget)
    # On surfaces the broker can't switch, prepend the self-check guard so the requested
    # model answers or the agent stops and asks the user to switch.
    guard_label = requested_label or model_resolution.get("display") or target_model
    if requested_explicitly and target_agent in GUARD_SURFACES:
        guard = model_guard_text(guard_label, strict=bool(strict_model))
        if guard:
            wrapped_prompt = guard + wrapped_prompt
    if target_agent == "codex":
        queued = queue_codex_request(project, wrapped_prompt, topic, guard_label, strict_model)
        queued["route"] = "codex_inbox"
        queued["surface"] = surface
        queued["task_kind"] = task_kind
        queued["token_budget"] = token_budget
        if ide_host:
            queued["launch"] = launch_ide_host(ide_host, resolve_project(project).root_path)
            queued["target_host"] = ide_host
        if surface_note:
            queued["surface_note"] = surface_note
        queued["model_resolution"] = model_resolution
        return queued
    if target_agent == "codex_app":
        queued = queue_codex_request(project, wrapped_prompt, topic, guard_label, strict_model)
        project_info = resolve_project(project)
        handoff = write_app_handoff_file("codex", project_info, queued["id"], wrapped_prompt, topic, target_model)
        queued["route"] = "codex_app_handoff"
        queued["surface"] = "app"
        queued["handoff"] = handoff
        queued["launch"] = launch_windows_app("codex")
        queued["autopaste"] = maybe_auto_paste("codex", queued["launch"])
        queued["note"] = (
            "Opened the visible Codex app when available. Standalone Codex prompt injection is not public; "
            "the broker copied the handoff to the clipboard and best-effort auto-pasted it into the app "
            "(see 'autopaste'); the file remains as a manual-paste fallback."
        )
        if surface_note:
            queued["surface_note"] = surface_note
        queued["model_resolution"] = model_resolution
        return queued
    if target_agent == "claude_ext":
        queued = queue_claude_request(project, wrapped_prompt, topic, target_model, task_kind, token_budget, new_chat)
        queued["route"] = "claude_inbox"
        queued["surface"] = surface
        if ide_host:
            queued["launch"] = launch_ide_host(ide_host, resolve_project(project).root_path)
            queued["target_host"] = ide_host
        if surface_note:
            queued["surface_note"] = surface_note
        queued["model_resolution"] = model_resolution
        return queued
    if target_agent == "claude_app":
        project_info = resolve_project(project)
        request_id = str(uuid.uuid4())
        handoff = write_app_handoff_file("claude", project_info, request_id, wrapped_prompt, topic, target_model)
        record_agent_event(
            project,
            topic,
            os.environ.get("AGENT_BROKER_CALLER") or "mcp-client",
            "queued_claude_app_handoff",
            f"Queued Claude app handoff {request_id}",
            wrapped_prompt,
        )
        queued = {
            "id": request_id,
            "project": project_info.name,
            "root_path": project_info.root_path,
            "topic": topic,
            "target_model": target_model,
            "status": "queued",
            "route": "claude_app_handoff",
            "surface": "app",
            "handoff": handoff,
            "launch": launch_windows_app("claude"),
            "note": (
                "Opened the visible Claude app when available. Standalone Claude prompt injection is not public; "
                "the broker copied the handoff to the clipboard and best-effort auto-pasted it into the app "
                "(see 'autopaste'); the file remains as a manual-paste fallback."
            ),
        }
        queued["autopaste"] = maybe_auto_paste("claude", queued["launch"])
        if surface_note:
            queued["surface_note"] = surface_note
        queued["model_resolution"] = model_resolution
        return queued
    if target_agent == "codex_cli":
        result = consult(
            "codex",
            {
                "project": project,
                "topic": topic,
                "prompt": prompt,
                "mode": args.get("mode") or "read-only",
                "task_kind": task_kind,
                "token_budget": token_budget,
                "target_model": target_model,
            },
        )
        result["route"] = "codex_cli"
        result["surface"] = surface
        if surface_note:
            result["surface_note"] = surface_note
        result["model_resolution"] = model_resolution
        return result
    if target_agent == "claude_code":
        result = consult(
            "claude",
            {
                "project": project,
                "topic": topic,
                "prompt": prompt,
                "mode": args.get("mode") or "plan",
                "task_kind": task_kind,
                "token_budget": token_budget,
                "target_model": target_model,
            },
        )
        result["route"] = "claude_code"
        result["surface"] = surface
        if surface_note:
            result["surface_note"] = surface_note
        result["model_resolution"] = model_resolution
        return result
    if target_agent == "gemini_cli":
        result = consult(
            "gemini",
            {
                "project": project,
                "topic": topic,
                "prompt": prompt,
                "mode": args.get("mode") or "default",
                "task_kind": task_kind,
                "token_budget": token_budget,
                "target_model": target_model,
            },
        )
        result["route"] = "gemini_cli"
        result["model_resolution"] = model_resolution
        return result
    raise ValueError(f"unknown target_agent: {target_agent}")


TOOLS = [
    {
        "name": "register_project",
        "description": "Register a project name and root path for later cross-agent consultations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "root_path": {"type": "string"},
            },
            "required": ["name", "root_path"],
        },
    },
    {
        "name": "consult_codex",
        "description": "Ask Codex for read-only consultation on a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"]},
                "include_context_pack": {"type": "boolean"},
                "task_kind": {"type": "string"},
                "token_budget": {"type": "integer", "minimum": 500, "maximum": 20000},
                "include_task_contract": {"type": "boolean"},
                "target_model": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "consult_claude",
        "description": "Ask Claude Code for consultation on a project. Defaults to plan permission mode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string", "enum": ["plan", "default", "acceptEdits", "bypassPermissions"]},
                "include_context_pack": {"type": "boolean"},
                "task_kind": {"type": "string"},
                "token_budget": {"type": "integer", "minimum": 500, "maximum": 20000},
                "include_task_contract": {"type": "boolean"},
                "target_model": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "consult_gemini",
        "description": "Ask Gemini for consultation through Gemini CLI or Gemini API.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string"},
                "include_context_pack": {"type": "boolean"},
                "task_kind": {"type": "string"},
                "token_budget": {"type": "integer", "minimum": 500, "maximum": 20000},
                "include_task_contract": {"type": "boolean"},
                "target_model": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "get_consultation_history",
        "description": "Get recent consultations stored by the local agent broker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "queue_antigravity_request",
        "description": "Queue a prompt for the Antigravity bridge extension to send into Antigravity's in-app agent panel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "target_model": {"type": "string"},
                "request_type": {"type": "string"},
                "task_kind": {"type": "string"},
                "strict_model": {"type": "boolean"},
                "token_budget": {"type": "integer", "minimum": 500, "maximum": 20000},
                "prompt": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "route_agent_task",
        "description": "Route a task to Antigravity, Codex, Claude, or Gemini. Defaults to the IDE extension surface; 'app' opens a visible desktop app handoff; 'cli' runs the headless backend. Sending to Antigravity requires naming a specific Antigravity model. KEEP `prompt` SHORT: write a brief instruction and let the receiver read the files/work-memory itself; do NOT inline large context — stash it with store_shared_context and pass the context_ref. Oversized prompts trip a token-economy notice (`prompt_notice`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "target_agent": {"type": "string"},
                "target_model": {"type": "string"},
                "target_host": {
                    "type": "string",
                    "description": "IDE host to open for extension delivery, such as 'antigravity' or 'vscode'.",
                },
                "new_chat": {
                    "type": "boolean",
                    "description": "Open a fresh target-agent chat/session instead of reusing the project/topic thread when the bridge supports it.",
                },
                "surface": {
                    "type": "string",
                    "enum": ["extension", "ide", "app", "desktop", "cli", "headless", "auto"],
                    "description": "Delivery surface. 'extension'/'ide' opens the requested IDE host and uses the bridge extension; 'app'/'desktop' opens the visible desktop app and writes a handoff; 'cli'/'headless' uses the backend without a GUI.",
                },
                "task_kind": {
                    "type": "string",
                    "enum": [
                        "quick_check",
                        "implementation_plan",
                        "implementation",
                        "co_audit",
                        "debate",
                        "argue",
                        "review",
                        "bug_hunt",
                        "sanity_check",
                        "consult",
                    ],
                },
                "strict_model": {"type": "boolean"},
                "token_budget": {"type": "integer", "minimum": 500, "maximum": 20000},
                "mode": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "list_agent_models",
        "description": "List available/detected models for Codex, Claude Code, and Antigravity, including topic defaults.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "topic": {"type": "string"},
            },
        },
    },
    {
        "name": "resolve_model_request",
        "description": "Resolve a vague model request such as 'codex', 'gpt side', 'claude', or 'opus' into a concrete model, or return choices to ask the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "target_agent": {"type": "string"},
                "target_model": {"type": "string"},
                "remember_model": {"type": "boolean"},
            },
        },
    },
    {
        "name": "set_model_default",
        "description": "Set the default model for a project/topic/family after the user chooses once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "model_family": {"type": "string"},
                "target_agent": {"type": "string"},
                "target_model": {"type": "string"},
                "set_by": {"type": "string"},
            },
            "required": ["model_family", "target_agent", "target_model"],
        },
    },
    {
        "name": "get_model_defaults",
        "description": "Get remembered model defaults for a project/topic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
            },
        },
    },
    {
        "name": "claim_antigravity_request",
        "description": "Claim the next queued Antigravity request. Intended for the Antigravity bridge extension.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "consumer": {"type": "string"},
            },
        },
    },
    {
        "name": "complete_antigravity_request",
        "description": "Complete an Antigravity bridge request with the in-app model response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "response": {"type": "string"},
                "status": {"type": "string", "enum": ["ok", "error", "cancelled"]},
                "model": {"type": "string"},
            },
            "required": ["request_id", "response"],
        },
    },
    {
        "name": "get_antigravity_requests",
        "description": "List queued, active, and completed Antigravity bridge requests for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "record_agent_event",
        "description": "Record what an agent did, found, decided, or handed off on a topic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "agent": {"type": "string"},
                "event_type": {"type": "string"},
                "summary": {"type": "string"},
                "details": {"type": "string"},
            },
            "required": ["agent", "event_type", "summary"],
        },
    },
    {
        "name": "get_topic_timeline",
        "description": "Get the shared topic timeline of agent events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "get_work_memory",
        "description": "Get the short per-topic continuation log: what changed, where, why, checks, risks, and next step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "record_work_memory",
        "description": "Record a compact work-memory update so the next model can continue without rereading broad history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "agent": {"type": "string"},
                "summary": {"type": "string"},
                "changed_files": {},
                "why": {},
                "checks": {},
                "risks": {},
                "next_step": {},
                "status": {},
            },
            "required": ["agent", "summary"],
        },
    },
    {
        "name": "get_topic_status",
        "description": "Get per-topic tracking status (counts of routes/sessions/events, last model, last activity, current state) so any IDE/agent can see where a topic is at. Also writes tracker.json under the topic folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
            },
        },
    },
    {
        "name": "compact_topic",
        "description": "Compact a topic's accumulated context to a real token budget (tiktoken) and stash the full version for retrieval. Returns tokens before/after and a context_ref. Use before a big handoff so it carries a small brief instead of replaying history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "budget_tokens": {"type": "integer", "minimum": 200, "maximum": 8000},
            },
        },
    },
    {
        "name": "get_context_pack",
        "description": "Get a compact shared context pack for a project/topic before reading raw history or files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "budget": {"type": "integer", "minimum": 1200, "maximum": 40000},
            },
        },
    },
    {
        "name": "store_shared_context",
        "description": "Store large handoff/evidence content as compressed shared context with a retrievable local original.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "source": {"type": "string"},
                "content_type": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 300, "maximum": 8000},
                "content": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "retrieve_shared_context",
        "description": "Retrieve a full or query-filtered original for a context_ref from the shared local context store.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 500, "maximum": 80000},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "get_shared_context_stats",
        "description": "Show estimated token savings and retrieval counts for shared compressed context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
            },
        },
    },
    {
        "name": "get_chat_bootstrap",
        "description": "Create a compact first-message bootstrap for a fresh Codex, Claude, Antigravity, or generic chat on a project/topic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "target_agent": {"type": "string"},
                "budget": {"type": "integer", "minimum": 1200, "maximum": 20000},
            },
        },
    },
    {
        "name": "record_context_event",
        "description": "Record a compact context event with optional evidence for later context packs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "agent": {"type": "string"},
                "kind": {"type": "string"},
                "summary": {"type": "string"},
                "evidence": {},
            },
            "required": ["agent", "kind", "summary"],
        },
    },
    {
        "name": "queue_codex_request",
        "description": "Queue a request or handoff for Codex. The Antigravity bridge will notify the user and write an inbox file. Pass target_model to require a specific Codex model; with strict_model the prompt tells Codex to STOP and ask the user to switch if it isn't that model (the broker can't switch the Codex extension's model itself).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "prompt": {"type": "string"},
                "target_model": {"type": "string"},
                "strict_model": {"type": "boolean"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "get_codex_requests",
        "description": "List requests queued for Codex.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "respond_to_request",
        "description": "Return your finished answer to the broker for a queued request (Codex/Claude/Antigravity). Use the Request ID from the inbox/handoff. The broker records the response, timing, and responder on the request and refreshes the per-topic ledger.md. This is how a non-Antigravity surface (e.g. Codex) sends its reply back instead of the user copy-pasting it from the chat panel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "request_id": {"type": "string"},
                "response": {"type": "string"},
                "agent": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["request_id", "response"],
        },
    },
    {
        "name": "get_request_ledger",
        "description": "Render and return the per-topic request ledger (request -> answer -> timing) and write topics/<project>/<topic>/ledger.md. A human-readable view of all cross-agent traffic for a topic; SQLite stays the source of truth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "topic": {"type": "string"},
            },
        },
    },
]


def text_content(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def topic_workspace_dir(project_info: "ProjectInfo", topic: str | None) -> Path:
    """Deterministic per-topic working folder. Used as the Claude CLI cwd so each
    topic's sessions bucket into their own ~/.claude/projects/<bucket> folder."""
    d = BROKER_DIR / "topics" / safe_slug(project_info.name) / safe_slug(topic or "all") / "workspace"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log(f"workspace dir create failed: {exc}")
    return d


def claude_bucket_name(path: str) -> str:
    """Encode a working directory into Claude Code's ~/.claude/projects bucket name.
    Rule (verified against existing buckets): every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_bucket_path(workspace: Path) -> Path:
    return Path.home() / ".claude" / "projects" / claude_bucket_name(str(workspace))


def count_claude_sessions(workspace: Path) -> int:
    bucket = claude_bucket_path(workspace)
    if not bucket.exists():
        return 0
    try:
        return sum(1 for _ in bucket.glob("*.jsonl"))
    except Exception:  # noqa: BLE001
        return 0


def get_topic_status(project: str | None, topic: str | None) -> dict[str, Any]:
    """Aggregate per-topic counts/state so any agent/IDE can see where a topic is at.
    Reads counts from SQLite + a live Claude session count, and writes tracker.json."""
    init_db()
    project_info = resolve_project(project)
    pname = project_info.name
    workspace = topic_workspace_dir(project_info, topic)
    open_states = {"queued", "in_progress", "claimed", "awaiting_model", "notified", "running"}
    counts: dict[str, Any] = {}
    last_activity = None
    last_model = None
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row

        def scalar(query: str) -> Any:
            row = conn.execute(query, (pname, topic)).fetchone()
            return (row[0] if row and row[0] is not None else None)

        counts["events"] = scalar("select count(*) from agent_events where project=? and topic=?") or 0
        counts["codex_requests"] = scalar("select count(*) from codex_requests where project=? and topic=?") or 0
        counts["antigravity_requests"] = scalar("select count(*) from antigravity_requests where project=? and topic=?") or 0
        counts["context_blobs"] = scalar("select count(*) from shared_context_blobs where project=? and topic=?") or 0
        times = [
            scalar("select max(created_at) from agent_events where project=? and topic=?"),
            scalar("select max(created_at) from codex_requests where project=? and topic=?"),
            scalar("select max(created_at) from antigravity_requests where project=? and topic=?"),
        ]
        times = [t for t in times if t]
        last_activity = max(times) if times else None
        md = conn.execute(
            "select target_model from model_defaults where project=? and topic=? order by updated_at desc limit 1",
            (pname, topic),
        ).fetchone()
        if md:
            last_model = md["target_model"]
        ag_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                "select status, count(*) c from antigravity_requests where project=? and topic=? group by status",
                (pname, topic),
            ).fetchall()
        }
        cx_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                "select status, count(*) c from codex_requests where project=? and topic=? group by status",
                (pname, topic),
            ).fetchall()
        }
    counts["claude_sessions"] = count_claude_sessions(workspace)
    counts["routes"] = counts["codex_requests"] + counts["antigravity_requests"] + counts["claude_sessions"]
    pending = any(s in open_states for s in list(ag_status) + list(cx_status))
    status = "in_progress" if pending else ("active" if counts["routes"] else "empty")
    result = {
        "project": pname,
        "topic": topic or "all",
        "workspace": str(workspace),
        "claude_bucket": str(claude_bucket_path(workspace)),
        "counts": counts,
        "last_model": last_model,
        "last_activity": last_activity,
        "status": status,
        "request_status": {"antigravity": ag_status, "codex": cx_status},
        "generated_at": utc_now(),
    }
    try:
        tracker_path = BROKER_DIR / "topics" / safe_slug(pname) / safe_slug(topic or "all") / "tracker.json"
        tracker_path.parent.mkdir(parents=True, exist_ok=True)
        tracker_path.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
        result["tracker_file"] = str(tracker_path)
    except Exception as exc:  # noqa: BLE001
        log(f"tracker write failed: {exc}")
    return result


def compact_topic(project: str | None, topic: str | None, budget_tokens: int = 2000) -> dict[str, Any]:
    """Opt-in 'compact this topic': build the topic's context pack, reduce it to a real
    token budget (tiktoken), stash the full version for retrieval, and write compacted.md.
    Future handoffs can carry the small compacted artifact instead of replaying history."""
    init_db()
    project_info = resolve_project(project)
    budget_tokens = max(200, min(int(budget_tokens or 2000), 8000))
    full = (get_context_pack(project_info.name, topic, 20000) or {}).get("content") or ""
    before = estimate_tokens(full)
    compacted = compress_context_content(full, max_chars=budget_tokens * 4, content_type="markdown")
    after = estimate_tokens(compacted)
    blob = store_shared_context(project_info.name, topic, full, "compact_topic", "markdown") if full else {}
    ref = blob.get("ref")
    path = BROKER_DIR / "topics" / safe_slug(project_info.name) / safe_slug(topic or "all") / "compacted.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Compacted context - {project_info.name} / {topic or 'all'}\n\n"
            f"Budget: {budget_tokens} tokens. Retrieve the full version with "
            f"retrieve_shared_context(ref=\"{ref}\").\n\n{compacted}\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        log(f"compacted.md write failed: {exc}")
    return {
        "project": project_info.name,
        "topic": topic or "all",
        "tokenizer": "tiktoken/cl100k_base" if _token_encoder() is not None else "chars/4 (tiktoken unavailable)",
        "tokens_before": before,
        "tokens_after": after,
        "saved_tokens": max(0, before - after),
        "saved_pct": 0.0 if before == 0 else round((before - after) / before * 100, 1),
        "context_ref": ref,
        "compacted_file": str(path),
        "compacted": compacted,
    }


def compacted_topic_handoff_section(project_info: ProjectInfo, topic: str | None) -> str:
    """Small, automatic context section for handoffs that do not have a live bridge
    claim step. Antigravity claims already attach context_pack; app/Claude-extension
    handoffs need the compact brief in the pasted/opened payload itself."""
    if not topic:
        return ""
    config = load_config()
    if config.get("handoff_auto_compact", True) is False:
        return ""
    budget = int(config.get("handoff_compact_budget_tokens", 1800))
    try:
        result = compact_topic(project_info.name, topic, budget)
        compacted = str(result.get("compacted") or "").strip()
        if not compacted:
            return ""
        ref = result.get("context_ref") or ""
        return (
            "## Compacted Topic Context\n\n"
            f"Tokenizer: {result.get('tokenizer')}\n"
            f"Tokens: {result.get('tokens_before')} -> {result.get('tokens_after')} "
            f"saved {result.get('saved_pct')}%\n"
            f"Full context ref: {ref}\n"
            f"Compacted file: {result.get('compacted_file')}\n\n"
            "Use this compacted brief first. Retrieve or ask the caller to retrieve "
            "the full context only for a specific missing detail.\n\n"
            f"{compacted}\n\n"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"handoff auto-compact failed for {project_info.name}/{topic}: {exc}")
        try:
            pack = get_context_pack(project_info.name, topic, DEFAULT_CONTEXT_BUDGET).get("content") or ""
        except Exception:
            pack = ""
        if not pack:
            return ""
        return f"## Shared Context Pack\n\n{pack}\n\n"


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "register_project":
        return text_content(register_project(str(args.get("name") or ""), str(args.get("root_path") or "")))
    if name == "consult_codex":
        return text_content(consult("codex", args))
    if name == "consult_claude":
        return text_content(consult("claude", args))
    if name == "consult_gemini":
        return text_content(consult("gemini", args))
    if name == "get_consultation_history":
        return text_content(get_history(args.get("project"), int(args.get("limit") or 20)))
    if name == "queue_antigravity_request":
        return text_content(
            queue_antigravity_request(
                args.get("project"),
                str(args.get("prompt") or ""),
                args.get("topic"),
                args.get("target_model"),
                args.get("request_type"),
                args.get("task_kind"),
                args.get("strict_model"),
                int(args.get("token_budget") or 0) or None,
            )
        )
    if name == "route_agent_task":
        return text_content(route_agent_task(args))
    if name == "list_agent_models":
        return text_content(list_agent_models(args.get("agent"), args.get("project"), args.get("topic")))
    if name == "resolve_model_request":
        return text_content(resolve_model_request(args))
    if name == "set_model_default":
        return text_content(
            set_model_default(
                args.get("project"),
                args.get("topic"),
                str(args.get("model_family") or ""),
                str(args.get("target_agent") or ""),
                str(args.get("target_model") or ""),
                args.get("set_by"),
            )
        )
    if name == "get_model_defaults":
        return text_content(get_model_defaults(args.get("project"), args.get("topic")))
    if name == "claim_antigravity_request":
        return text_content(claim_antigravity_request(str(args.get("consumer") or "antigravity-bridge")))
    if name == "complete_antigravity_request":
        return text_content(
            complete_antigravity_request(
                str(args.get("request_id") or ""),
                str(args.get("response") or ""),
                str(args.get("status") or "ok"),
                args.get("model"),
            )
        )
    if name == "get_antigravity_requests":
        return text_content(get_antigravity_requests(args.get("project"), int(args.get("limit") or 20)))
    if name == "record_agent_event":
        return text_content(
            record_agent_event(
                args.get("project"),
                args.get("topic"),
                str(args.get("agent") or ""),
                str(args.get("event_type") or ""),
                str(args.get("summary") or ""),
                args.get("details"),
            )
        )
    if name == "get_topic_timeline":
        return text_content(get_topic_timeline(args.get("project"), args.get("topic"), int(args.get("limit") or 50)))
    if name == "get_work_memory":
        return text_content(get_work_memory(args.get("project"), args.get("topic"), int(args.get("limit") or 10)))
    if name == "record_work_memory":
        return text_content(
            record_work_memory(
                args.get("project"),
                args.get("topic"),
                str(args.get("agent") or ""),
                str(args.get("summary") or ""),
                args.get("changed_files"),
                args.get("why"),
                args.get("checks"),
                args.get("risks"),
                args.get("next_step"),
                args.get("status"),
            )
        )
    if name == "get_topic_status":
        return text_content(get_topic_status(args.get("project"), args.get("topic")))
    if name == "compact_topic":
        return text_content(compact_topic(args.get("project"), args.get("topic"), int(args.get("budget_tokens") or 2000)))
    if name == "get_context_pack":
        return text_content(get_context_pack(args.get("project"), args.get("topic"), int(args.get("budget") or 0) or None))
    if name == "store_shared_context":
        return text_content(
            store_shared_context(
                args.get("project"),
                args.get("topic"),
                args.get("content"),
                args.get("source"),
                args.get("content_type"),
                int(args.get("max_chars") or 0) or None,
            )
        )
    if name == "retrieve_shared_context":
        return text_content(
            retrieve_shared_context(
                str(args.get("ref") or ""),
                args.get("query"),
                int(args.get("limit") or 0) or None,
            )
        )
    if name == "get_shared_context_stats":
        return text_content(shared_context_stats(args.get("project"), args.get("topic")))
    if name == "get_chat_bootstrap":
        return text_content(
            get_chat_bootstrap(
                args.get("project"),
                args.get("topic"),
                args.get("target_agent"),
                int(args.get("budget") or 0) or None,
            )
        )
    if name == "record_context_event":
        return text_content(
            record_context_event(
                args.get("project"),
                args.get("topic"),
                str(args.get("agent") or ""),
                str(args.get("kind") or ""),
                str(args.get("summary") or ""),
                args.get("evidence"),
            )
        )
    if name == "queue_codex_request":
        return text_content(queue_codex_request(
            args.get("project"),
            str(args.get("prompt") or ""),
            args.get("topic"),
            args.get("target_model"),
            args.get("strict_model"),
        ))
    if name == "get_codex_requests":
        return text_content(get_codex_requests(args.get("project"), int(args.get("limit") or 20)))
    if name == "respond_to_request":
        return text_content(respond_to_request(
            args.get("project"), args.get("topic"), str(args.get("request_id") or ""),
            str(args.get("response") or ""), args.get("agent"), args.get("model")))
    if name == "get_request_ledger":
        return text_content(get_request_ledger(args.get("project"), args.get("topic")))
    raise ValueError(f"unknown tool: {name}")


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return success_response(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-broker", "version": BROKER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return success_response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            return success_response(request_id, handle_tool(name, args))
        except Exception as exc:  # noqa: BLE001
            log(f"tool {name} failed: {traceback.format_exc()}")
            return success_response(
                request_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                },
            )
    if request_id is not None:
        return error_response(request_id, -32601, f"method not found: {method}")
    return None


def handle_bridge_cli(argv: list[str]) -> int:
    if not argv or argv[0] in {"help", "-h", "--help"}:
        print(
            "Usage: agent_broker_mcp.py bridge "
            "(claim [consumer] | requests [project] [limit] | queue <project> <topic> <target_model> <prompt> | "
            "route <project> <topic> <target_agent> <target_model> <task_kind> <prompt> | "
            "requeue <request_id> | await-model <request_id> | resume-model [request_id] | awaiting-model [project] [limit] | "
            "complete <request_id> <model> <response> | complete-file <request_id> <model> <path> | "
            "codex-inbox [project] [limit] | queue-codex <project> <topic> <prompt> | "
            "respond <project> <topic> <request_id> <response> [agent] [model] | ledger [project] [topic] | "
            "codex-notified <request_id> | completed-unnotified [limit] | completion-notified <request_id> | "
            "context-pack [project] [topic] [budget] | context-retrieve <ref> [query] [limit] | "
            "context-stats [project] [topic] | chat-bootstrap [project] [topic] [target_agent] [budget] | "
            "work-memory [project] [topic] [limit] | "
            "models [agent] [project] [topic] | resolve-model <project> <topic> <target_agent> <target_model> | "
            "set-model-default <project> <topic> <model_family> <target_agent> <target_model> | "
            "model-defaults [project] [topic] | topic-status [project] [topic] | "
            "compact-topic [project] [topic] [budget_tokens])"
        )
        return 0
    command = argv[0]
    if command == "claim":
        result = claim_antigravity_request(argv[1] if len(argv) > 1 else "antigravity-bridge")
    elif command == "requests":
        project = argv[1] if len(argv) > 1 else None
        limit = int(argv[2]) if len(argv) > 2 else 20
        result = get_antigravity_requests(project, limit)
    elif command == "queue":
        if len(argv) < 5:
            raise ValueError("queue requires <project> <topic> <target_model> <prompt>")
        result = queue_antigravity_request(argv[1], argv[4], argv[2], argv[3], "consult")
    elif command == "route":
        if len(argv) < 6:
            raise ValueError("route requires <project> <topic> <target_agent> [target_model] <task_kind> <prompt>")
        if len(argv) == 6:
            target_model = ""
            task_kind = argv[4]
            prompt = argv[5]
        else:
            target_model = argv[4]
            task_kind = argv[5]
            prompt = argv[6]
        result = route_agent_task(
            {
                "project": argv[1],
                "topic": argv[2],
                "target_agent": argv[3],
                "target_model": target_model,
                "task_kind": task_kind,
                "prompt": prompt,
            }
        )
    elif command == "requeue":
        if len(argv) < 2:
            raise ValueError("requeue requires <request_id>")
        result = requeue_antigravity_request(argv[1])
    elif command == "await-model":
        if len(argv) < 2:
            raise ValueError("await-model requires <request_id>")
        result = await_antigravity_model_selection(argv[1])
    elif command == "resume-model":
        result = resume_antigravity_model_selection(argv[1] if len(argv) > 1 else None)
    elif command == "awaiting-model":
        project = argv[1] if len(argv) > 1 else "*"
        limit = int(argv[2]) if len(argv) > 2 else 20
        result = get_awaiting_model_requests(project, limit)
    elif command == "complete":
        if len(argv) < 4:
            raise ValueError("complete requires <request_id> <model> <response>")
        result = complete_antigravity_request(argv[1], argv[3], "ok", argv[2])
    elif command == "complete-file":
        if len(argv) < 4:
            raise ValueError("complete-file requires <request_id> <model> <path>")
        response_path = Path(argv[3]).expanduser()
        result = complete_antigravity_request(
            argv[1],
            response_path.read_text(encoding="utf-8", errors="replace"),
            "ok",
            argv[2],
        )
    elif command == "codex-inbox":
        project = argv[1] if len(argv) > 1 else "*"
        limit = int(argv[2]) if len(argv) > 2 else 20
        result = get_codex_requests(project, limit)
    elif command == "queue-codex":
        if len(argv) < 4:
            raise ValueError("queue-codex requires <project> <topic> <prompt>")
        result = queue_codex_request(argv[1], argv[3], argv[2])
    elif command == "respond":
        if len(argv) < 5:
            raise ValueError("respond requires <project> <topic> <request_id> <response> [agent] [model]")
        result = respond_to_request(
            argv[1], argv[2] if argv[2] != "*" else None, argv[3], argv[4],
            argv[5] if len(argv) > 5 else None, argv[6] if len(argv) > 6 else None,
        )
    elif command == "ledger":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        result = get_request_ledger(project, topic)
    elif command == "codex-notified":
        if len(argv) < 2:
            raise ValueError("codex-notified requires <request_id>")
        result = mark_codex_request_notified(argv[1])
    elif command == "completed-unnotified":
        limit = int(argv[1]) if len(argv) > 1 else 20
        result = get_unnotified_antigravity_completions(limit)
    elif command == "completion-notified":
        if len(argv) < 2:
            raise ValueError("completion-notified requires <request_id>")
        result = mark_antigravity_completion_notified(argv[1])
    elif command == "context-pack":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        budget = int(argv[3]) if len(argv) > 3 else DEFAULT_CONTEXT_BUDGET
        result = get_context_pack(project, topic, budget)
    elif command == "context-retrieve":
        if len(argv) < 2:
            raise ValueError("context-retrieve requires <ref> [query] [limit]")
        query = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        limit = int(argv[3]) if len(argv) > 3 else 12000
        result = retrieve_shared_context(argv[1], query, limit)
    elif command == "context-stats":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        result = shared_context_stats(project, topic)
    elif command == "chat-bootstrap":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        target_agent = argv[3] if len(argv) > 3 else "generic"
        budget = int(argv[4]) if len(argv) > 4 else 5000
        result = get_chat_bootstrap(project, topic, target_agent, budget)
    elif command == "work-memory":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        limit = int(argv[3]) if len(argv) > 3 else 10
        result = get_work_memory(project, topic, limit)
    elif command == "models":
        agent = argv[1] if len(argv) > 1 else "all"
        project = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        topic = argv[3] if len(argv) > 3 and argv[3] != "*" else None
        result = list_agent_models(agent, project, topic)
    elif command == "resolve-model":
        if len(argv) < 5:
            raise ValueError("resolve-model requires <project> <topic> <target_agent> <target_model>")
        result = resolve_model_request(
            {
                "project": argv[1],
                "topic": None if argv[2] == "*" else argv[2],
                "target_agent": argv[3],
                "target_model": argv[4],
            }
        )
    elif command == "set-model-default":
        if len(argv) < 6:
            raise ValueError("set-model-default requires <project> <topic> <model_family> <target_agent> <target_model>")
        result = set_model_default(
            argv[1],
            None if argv[2] == "*" else argv[2],
            argv[3],
            argv[4],
            argv[5],
            "bridge-cli",
        )
    elif command == "model-defaults":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        result = get_model_defaults(project, topic)
    elif command == "topic-status":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        result = get_topic_status(project, topic)
    elif command == "compact-topic":
        project = argv[1] if len(argv) > 1 else None
        topic = argv[2] if len(argv) > 2 and argv[2] != "*" else None
        budget = int(argv[3]) if len(argv) > 3 else 2000
        result = compact_topic(project, topic, budget)
    else:
        raise ValueError(f"unknown bridge command: {command}")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        if sys.argv[1] != "bridge":
            raise SystemExit(f"unknown command: {sys.argv[1]}")
        return handle_bridge_cli(sys.argv[2:])
    init_db()
    log("agent-broker MCP server started")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = handle_message(message)
        except Exception as exc:  # noqa: BLE001
            log(f"message handling failed: {traceback.format_exc()}")
            response = error_response(None, -32603, f"{type(exc).__name__}: {exc}")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    log("agent-broker MCP server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
