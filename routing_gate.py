"""Routing gate for native-first labour and opposite-vendor consultations.

Dependency-free (stdlib only). Invoked by the host (Codex/Claude Code) as a
command hook: JSON on stdin, JSON on stdout. Both hosts use the same
`hooks.<Event>[].hooks[]` structural shape and the same Stop decision shape
(`{"decision": "block", "reason": "..."}` to block once, `{}` to allow).
PreToolUse atomically records direct labour and denies work beyond a bounded
threshold until a managed native cheap-role agent starts or the brain registers
a package override. PostToolUse quarantines oversized MCP responses before they
enter brain context.
Codex replaces the result with block feedback; Claude uses its host-specific
``updatedToolOutput`` response.

Design constraints (hard, repeat-checked):
- Fail-open: any doubt (unreachable ledger, re-entrant stop, missing session id)
  allows rather than blocks.
- Stop is loop-bounded: a session is blocked at most once; the second Stop call
  for the same unresolved session allows. PreToolUse can deny repeatedly, but
  native Agent/Task creation remains unblocked so it always has an exit path.
- Never parses arbitrary transcript files -- only scans the `last_assistant_message`
  string the host hook payload already provides, via narrow regexes.
- Native receipts are host-attested from SubagentStart/SubagentStop events. Broker
  receipts remain ledger-attested; neither trusts an agent's prose identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:
    from switchboard_version import BROKER_VERSION
except Exception:  # noqa: BLE001 - the gate must load even from a partial install
    BROKER_VERSION = "unknown"

import atomic_io

BROKER_HOME = Path(os.environ.get("AGENT_BROKER_HOME", Path.home() / ".agent-broker"))
STATE_DIR = BROKER_HOME / "routing-gate"
DB_PATH = BROKER_HOME / "state.sqlite"
EVIDENCE_DIR = BROKER_HOME / "context-evidence"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


STATE_TTL_SECONDS = 24 * 60 * 60
# The allowance covers non-mutating micro-work only: reading your own handoff and
# adjudicating a premise or two before the first delegation. It was ten, which in
# practice exempted almost every real turn -- a brain finished the whole task inside
# the allowance and the gate never engaged. Four is enough to orient and no more.
DIRECT_LABOUR_LIMIT_DEFAULT = 4
DIRECT_LABOUR_LIMIT = max(
    1, _env_int("AGENT_BROKER_DIRECT_LABOUR_LIMIT", DIRECT_LABOUR_LIMIT_DEFAULT)
)
GATE_MODE_ENV = "AGENT_BROKER_GATE_MODE"
# How long a hook waits for the per-session state lock before failing open. See
# _update_state for why this is a trade rather than a bug.
STATE_LOCK_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("AGENT_BROKER_STATE_LOCK_TIMEOUT") or 4.0)
)


def gate_mode() -> str:
    """`enforce` (deny past the allowance) or `warn` (log the denial, allow the call).

    `warn` exists for one shakedown session after a policy change: a gate that starts
    denying on day one of a new classifier gets disabled by the user, and a disabled
    gate enforces nothing at all."""
    mode = str(os.environ.get(GATE_MODE_ENV, "enforce")).strip().lower()
    return "warn" if mode in {"warn", "warning", "shadow", "observe"} else "enforce"


def effective_direct_labour_limit() -> int:
    """The allowance actually applied.

    The configured value is honoured rather than silently clamped: this env var is
    owner-facing configuration, and an owner who sets 10 should get 10. The risk it
    creates -- `AGENT_BROKER_DIRECT_LABOUR_LIMIT=999` quietly turning the policy off
    while every report still claims the gate is active -- is handled by making it
    LOUD instead of impossible; see `policy_is_relaxed()`, which surfaces in the
    per-turn status line and in routing-report."""
    return DIRECT_LABOUR_LIMIT


def policy_is_relaxed() -> bool:
    """True when the running configuration is weaker than the shipped default."""
    return gate_mode() == "warn" or DIRECT_LABOUR_LIMIT > DIRECT_LABOUR_LIMIT_DEFAULT


CONTEXT_INGRESS_MAX_CHARS = max(
    2_000, _env_int("AGENT_BROKER_CONTEXT_INGRESS_MAX_CHARS", 8_000)
)
CONTEXT_EVIDENCE_MAX_CHARS = max(
    CONTEXT_INGRESS_MAX_CHARS,
    _env_int("AGENT_BROKER_CONTEXT_EVIDENCE_MAX_CHARS", 5_000_000),
)

MUTATING_TOOL_NAMES = {
    "edit", "write", "multiedit", "notebookedit", "apply_patch", "applypatch", "patch",
}
SHELL_TOOL_NAMES = {"bash", "shell", "exec", "run_command", "localshell", "terminal", "powershell"}
READ_TOOL_NAMES = {"read", "readfile", "read_file"}
SEARCH_TOOL_NAMES = {"grep", "glob", "find", "search", "search_files"}
WEB_RESEARCH_TOOL_NAMES = {"webfetch", "websearch"}
DELEGATION_TOOL_NAMES = {"agent", "task", "spawn_agent"}
SWITCHBOARD_CONTROL_SUFFIXES = {
    "consult_decision", "consult_codex", "consult_claude", "consult_gemini", "consult_antigravity",
    "queue_codex_request", "queue_claude_request", "request_status",
    "request_result", "route_agent_task",
}
CLAUDE_UNAVAILABLE_REASONS = {
    "quota",
    "subscription_disabled",
    "organization_disabled",
    "cli_missing",
    "family_access_unavailable",
    "provider_unavailable",
}
CLAUDE_CONSULT_TOOL_NAMES = {
    "mcp__agent_switchboard__consult_claude",
    "mcp__agent_switchboard__consult-claude",
    "mcp__agent_switchboard__queue_claude_request",
    "mcp__agent_switchboard__queue-claude-request",
}
ROUTE_AGENT_TASK_TOOL_NAMES = {
    "mcp__agent_switchboard__route_agent_task",
    "mcp__agent_switchboard__route-agent-task",
}
CLAUDE_AGENT_NAMES = {
    "claude",
    "claude_code",
    "claude-code",
    "claude_cli",
    "claude-cli",
    "claude_ext",
    "claude-ext",
    "claude_app",
    "claude-app",
}
CLAUDE_MODEL_ALIASES = {"fable", "opus", "sonnet", "haiku"}
CLAUDE_CIRCUIT_CONTEXT = (
    "Claude consultation is unavailable for this session. Do not retry Claude in this "
    "session. If a second opinion is still useful, route one bounded package to the newest "
    "live Gemini Flash High through Switchboard and treat it as a degraded, non-authoritative "
    "fallback; the brain retains judgment and independently verifies its evidence."
)
ROUTING_OVERRIDE_COMMAND_RE = re.compile(
    r"^\s*(?:&\s*)?(?:"
    r"(?:\"[^\"\r\n]*agent-switchboard(?:\.exe)?\"|\S*agent-switchboard(?:\.exe)?)"
    r"|(?:\"?\S*python(?:\.exe)?\"?\s+\"?[^\r\n]*agent_broker_entry\.py\"?)"
    r")\s+routing-override\s+--session\s+\S+\s+--package\s+WP[A-Za-z0-9_.-]+"
    r"\s+--reason\s+.+$",
    re.IGNORECASE,
)
# Match ``agy`` only where a shell would treat it as the command being
# executed: at the start of a command or after a command separator. This is
# deliberately not a plain word search, so prose, arguments, and path checks
# such as ``Write-Output 'agy --help'`` or ``Test-Path C:\\tools\\agy.exe`` do
# not trip the gate.
DIRECT_AGY_COMMAND_RE = re.compile(
    r"(?:^|(?:&&|\|\||[;&|\r\n])\s*)"
    r"\s*(?:&\s*)?"
    r"(?:(?:command|exec|sudo)(?:\s+--?[A-Za-z0-9_-]+)*\s+)*"
    r"(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+\s+)*)?"
    r"(?:"
    r"\"(?:[^\"\r\n]*[\\/])?agy(?:\.exe)?\""
    r"|'(?:[^'\r\n]*[\\/])?agy(?:\.exe)?'"
    r"|(?:[^\s\"';&|]+[\\/])*agy(?:\.exe)?"
    r")(?=$|\s|[;&|])",
    re.IGNORECASE,
)
START_PROCESS_AGY_RE = re.compile(
    r"(?:^|(?:&&|\|\||[;&|\r\n])\s*)\s*"
    r"start-process\s+(?:-filepath\s+)?"
    r"(?:"
    r"\"(?:[^\"\r\n]*[\\/])?agy(?:\.exe)?\""
    r"|'(?:[^'\r\n]*[\\/])?agy(?:\.exe)?'"
    r"|(?:[^\s\"';&|]+[\\/])*agy(?:\.exe)?"
    r")(?=$|\s|[;&|])",
    re.IGNORECASE,
)
TEST_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:python\s+-m\s+(?:unittest|pytest)|pytest|npm\s+test|"
    r"pnpm\s+test|yarn\s+test|cargo\s+test|go\s+test|dotnet\s+test)\b",
    re.IGNORECASE,
)
DOC_PATH_RE = re.compile(
    r"(?:^|[\\/])(?:docs?|documentation)(?:[\\/]|$)|\.(?:md|mdx|rst|txt)$",
    re.IGNORECASE,
)

# Conservative, explicit, testable in isolation -- deliberately narrow rather than
# trying to catch every possible mutating command. Each pattern targets a verb
# that mutates local/remote/user/service/file state; obvious read-only commands
# (status/list/get/show/ps/cat/ls/...) are deliberately not matched.
MUTATING_COMMAND_PATTERNS = [
    # version control writes
    re.compile(r"\bgit\s+(commit|push|merge|rebase|reset\s+--hard)\b"),
    # package installs
    re.compile(r"\b(npm|pnpm|yarn)\s+(install|ci|publish|add|remove|uninstall|run\s+deploy)\b"),
    re.compile(r"\bpip3?\s+(install|uninstall)\b"),
    re.compile(r"\b(apt|apt-get|yum|dnf|brew|choco|winget)\s+(install|remove|uninstall|upgrade)\b"),
    # remote / deployment
    re.compile(r"\bscp\b"),
    re.compile(
        r"\bssh\b.*\b(apt|apt-get|yum|dnf|systemctl|docker|podman|kubectl|terraform|"
        r"rm|mv|cp|mkdir|touch|chmod|chown|tee|useradd|usermod|userdel)\b"
    ),
    re.compile(r"\brsync\b"),
    re.compile(r"\b(deploy|terraform\s+apply|terraform\s+destroy|kubectl\s+apply)\b"),
    # containers / orchestration
    re.compile(r"\b(docker|podman)\s+(run|build|push|rm|rmi|exec|stop|kill|compose\s+up|compose\s+down)\b"),
    re.compile(r"\bkubectl\s+(apply|delete|create|patch|scale|rollout)\b"),
    # service / user management
    re.compile(r"\bsystemctl\s+(start|stop|restart|reload|enable|disable)\b"),
    re.compile(r"\b(useradd|userdel|usermod|adduser|deluser|passwd)\b"),
    # local filesystem mutation (bash/posix)
    re.compile(r"\b(rm|mv|cp|chmod|chown|mkdir|rmdir|touch)\b"),
    # PowerShell mutating cmdlets
    re.compile(r"\b(Set|New|Remove|Move|Copy)-[A-Za-z]+\b", re.IGNORECASE),
    re.compile(r"\bOut-File\b", re.IGNORECASE),
    # shell output redirection (file write), either shell family
    re.compile(r">>?\s*\S"),
]

ROUTING_AUDIT_SECTION_RE = re.compile(
    r"^#{1,6}\s+routing audit\b[^\n]*\n(.*?)(?=^#{1,6}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
BROKER_RECEIPT_RE = re.compile(
    rf"\bbroker\s*:\s*({UUID_RE.pattern})\b", re.IGNORECASE
)
NATIVE_RECEIPT_RE = re.compile(
    r"\bnative\s*:\s*([A-Za-z0-9][A-Za-z0-9_.:/-]{2,199})", re.IGNORECASE
)
STRUCTURED_OVERRIDE_RE = re.compile(
    r"\boverride:\s*brain\s*-\s*([A-Za-z0-9_.-]+)\s*:\s*(\S.{11,})",
    re.IGNORECASE,
)
AUDIT_PACKAGE_COUNT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?packages\s*:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE
)
AUDIT_ROW_RE = re.compile(
    r"^\s*(?:[-*]\s*|\|\s*)?(WP[A-Za-z0-9_.-]+|Consultation)\s*(?:\||:)(.*)$",
    re.IGNORECASE,
)
NATIVE_UNAVAILABLE_RE = re.compile(
    r"\bnative-unavailable\s*:\s*\S.{11,}", re.IGNORECASE
)
DIRECT_BRAIN_LABOUR_RE = re.compile(
    r"^\s*(?:[-*]\s*)?direct-brain-labour\s*:\s*"
    r"reads=(\d+)\s*\|\s*searches=(\d+)\s*\|\s*evidence=(\d+)\s*\|\s*"
    r"tests=(\d+)\s*\|\s*docs=(\d+)\s*\|\s*other=(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
DIRECT_BRAIN_LABOUR_CATEGORIES = (
    "reads", "searches", "evidence", "tests", "docs", "other"
)
VALID_RESPONDER_PREFIXES = ("codex:", "claude:", "antigravity:")
NATIVE_CHEAP_AGENT_TYPES = {"explore", "explorer", "worker", "economy-worker"}


def normalize_tool_name(name: str) -> str:
    """Canonicalize a tool name for classification.

    Lowercases the whole name. For an MCP-style ``mcp__<server>__<tool>``
    name, hyphens are folded to underscores ONLY in the ``<server>`` segment
    so that ``mcp__agent-switchboard__route_agent_task`` (Claude's live
    hyphenated namespace) and ``mcp__agent_switchboard__route_agent_task``
    (Codex's underscore namespace) classify identically. The split uses only
    the first two ``__`` separators, so a tool segment that itself contains
    ``__`` (or hyphens) is left untouched -- never a global hyphen/underscore
    replacement, which would collapse genuinely distinct tool names. Total:
    never raises, regardless of how many (or few) ``__`` separators appear.
    """
    text = str(name or "").strip().lower()
    if not text.startswith("mcp__"):
        return text
    parts = text.split("__", 2)
    if len(parts) < 3:
        return text
    prefix, server, tool = parts
    return f"{prefix}__{server.replace('-', '_')}__{tool}"


def _session_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]
    return STATE_DIR / f"{safe}.json"


def _session_lock_path(session_id: str) -> Path:
    return _session_path(session_id).with_suffix(".lock")


def _session_log_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]
    return STATE_DIR / f"{safe}.log.jsonl"


def log_gate_decision(
    session_id: str,
    event: str,
    tool: str,
    category: str | None,
    decision: str,
    extra: dict | None = None,
) -> None:
    """Append one JSON decision record per line to the session's local log.

    Records classification/decision metadata only: never tool arguments,
    prompts, command strings, file contents, or tool output. Must never
    raise -- a logging failure is not allowed to affect gate behaviour.
    """
    try:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        record = {
            "ts": time.time(),
            "event": str(event or ""),
            "tool": str(tool or ""),
            "category": None if category is None else str(category),
            "decision": str(decision or ""),
        }
        if extra:
            for key, value in extra.items():
                key = str(key)
                if key not in record:
                    record[key] = value
        path = _session_log_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - logging must never affect gate behaviour
        pass


def _read_state(session_id: str) -> dict:
    path = _session_path(session_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_state(session_id: str, state: dict) -> None:
    atomic_io.atomic_write_text(_session_path(session_id), json.dumps(state))


def _update_state(session_id: str, update) -> dict | None:
    """Serialize hook-process read/modify/write cycles.

    Parallel native subagents can start and stop at nearly the same time. A
    short lock prevents one receipt from overwriting another; timeout is
    fail-open because hooks must never wedge the host session.

    That fail-open is a deliberate trade, and it has a real cost: a call that
    cannot take the lock in time is allowed AND not counted, so under heavy
    concurrency the allowance can be exceeded by the number of timed-out
    reservations. Wedging the user's session is still the worse failure, so the
    fix is to make the window small rather than to hold the lock indefinitely.
    One second was tight enough to lose races in a 12-way parallel test; a few
    seconds is invisible to a human but ample for a hook that only rewrites a
    small JSON file. Every fail-open is recorded with fail_open=True so the
    routing report can show when enforcement was actually skipped.
    """
    if not session_id:
        return None
    try:
        with atomic_io.FileLock(
            _session_lock_path(session_id), timeout=STATE_LOCK_TIMEOUT_SECONDS, stale_seconds=30.0
        ):
            state = _read_state(session_id)
            update(state)
            state["updated_at"] = time.time()
            _write_state(session_id, state)
            return state
    except Exception:  # noqa: BLE001
        return None


def reset_turn_state(session_id: str) -> None:
    """Called on UserPromptSubmit: clear any mutation/blocked flags left over
    from a prior turn so they never leak into the new turn's Stop decision."""
    if not session_id:
        return
    def update(state: dict) -> None:
        # Provider availability is session-scoped, not turn-scoped. Preserve only the
        # validated enum marker while clearing all ordinary turn state. A different host
        # session has a different state file and therefore starts with a closed circuit.
        claude_unavailable = state.get("claude_unavailable")
        state.clear()
        if claude_unavailable in CLAUDE_UNAVAILABLE_REASONS:
            state["claude_unavailable"] = claude_unavailable

    _update_state(session_id, update)


def mark_mutated(session_id: str) -> dict | None:
    def update(state: dict) -> None:
        state["mutated"] = True
        state["mutation_count"] = int(state.get("mutation_count") or 0) + 1

    return _update_state(session_id, update)


def reserve_direct_labour(
    session_id: str,
    category: str,
    tool_use_id: str,
    host: str,
    cheap_subagent_call: bool,
) -> tuple[bool, dict] | None:
    """Atomically reserve one direct labour call before the host executes it.

    Reservation in PreToolUse closes the parallel-batch race and counts failed
    calls too. Claude identifies cheap subagent calls explicitly; Codex does not
    document that field, so calls are conservatively exempt while a cheap role
    is active to prevent the worker from deadlocking on its parent's state.
    """
    decision = {"allowed": True}

    def update(state: dict) -> None:
        if cheap_subagent_call:
            return
        if host != "claude" and _cheap_native_agent_active(state):
            return
        reservations = state.setdefault("direct_labour_reservations", {})
        if tool_use_id and tool_use_id in reservations:
            return
        since_relief = int(state.get("direct_labour_since_relief") or 0)
        if since_relief >= effective_direct_labour_limit():
            decision["allowed"] = False
            state["labour_gate_denials"] = int(state.get("labour_gate_denials") or 0) + 1
            return
        counts = state.setdefault("direct_labour_counts", {})
        counts[category] = int(counts.get(category) or 0) + 1
        state["direct_labour_count"] = int(state.get("direct_labour_count") or 0) + 1
        state["direct_labour_since_relief"] = since_relief + 1
        if tool_use_id:
            reservations[tool_use_id] = category

    state = _update_state(session_id, update)
    if state is None:
        return None
    return bool(decision["allowed"]), state


def has_mutation(session_id: str) -> bool:
    return bool(_read_state(session_id).get("mutated"))


def already_blocked(session_id: str) -> bool:
    return bool(_read_state(session_id).get("blocked_once"))


def mark_blocked(session_id: str) -> None:
    _update_state(session_id, lambda state: state.__setitem__("blocked_once", True))


def _normalized_agent_type(value: object) -> str:
    return str(value or "").strip().lower()


def _cheap_native_agent_active(state: dict) -> bool:
    agents = state.get("native_agents") or {}
    return any(
        isinstance(item, dict)
        and _normalized_agent_type(item.get("agent_type")) in NATIVE_CHEAP_AGENT_TYPES
        and item.get("status") == "started"
        for item in agents.values()
    )


def _payload_is_cheap_native_call(payload: dict) -> bool:
    return bool(
        str(payload.get("agent_id") or "").strip()
        and _normalized_agent_type(payload.get("agent_type"))
        in NATIVE_CHEAP_AGENT_TYPES
    )


def subagent_start(payload: dict) -> dict:
    """Record a host-issued native agent id/type without trusting prose output."""
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    agent_type = str(payload.get("agent_type") or "").strip()
    if not session_id or not agent_id or not agent_type:
        return {}

    def update(state: dict) -> None:
        agents = state.setdefault("native_agents", {})
        agents[agent_id] = {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "status": "started",
            "completed": False,
            "turn_id": str(payload.get("turn_id") or ""),
            "model": str(payload.get("model") or ""),
        }
        if _normalized_agent_type(agent_type) in NATIVE_CHEAP_AGENT_TYPES:
            state["direct_labour_since_relief"] = 0
            state["labour_relief_sequence"] = int(
                state.get("labour_relief_sequence") or 0
            ) + 1

    _update_state(session_id, update)
    log_gate_decision(
        session_id, "SubagentStart", "-", None, "native-start",
        extra={"agent_id": agent_id, "agent_type": agent_type,
               "model": str(payload.get("model") or "")},
    )
    return {}


def subagent_stop(payload: dict) -> dict:
    """Mark a native receipt complete from the host lifecycle event."""
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    agent_type = str(payload.get("agent_type") or "").strip()
    if not session_id or not agent_id or not agent_type:
        return {}

    def update(state: dict) -> None:
        agents = state.setdefault("native_agents", {})
        previous = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else {}
        stop_turn_id = str(payload.get("turn_id") or "")
        if previous.get("status") != "started":
            return
        if str(previous.get("turn_id") or "") != stop_turn_id:
            return
        if _normalized_agent_type(previous.get("agent_type")) != _normalized_agent_type(agent_type):
            return
        agents[agent_id] = {
            **previous,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "status": "completed",
            "completed": True,
            "turn_id": stop_turn_id,
            "model": str(payload.get("model") or previous.get("model") or ""),
            "has_result": bool(str(payload.get("last_assistant_message") or "").strip()),
        }
        # A worker can mutate without the parent directly calling an edit tool.
        if _normalized_agent_type(agent_type) in {"worker", "economy-worker"}:
            state["mutated"] = True

    _update_state(session_id, update)
    return {}


def sweep_stale(max_age_seconds: float = STATE_TTL_SECONDS) -> None:
    cutoff = time.time() - max_age_seconds
    for directory in (STATE_DIR, EVIDENCE_DIR):
        if not directory.exists():
            continue
        try:
            entries = list(directory.glob("*.json"))
        except OSError:
            continue
        for path in entries:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def _is_mutating(tool_name: str, tool_input: dict) -> bool:
    name = normalize_tool_name(tool_name)
    if name in MUTATING_TOOL_NAMES:
        return True
    if name in SHELL_TOOL_NAMES or "shell" in name:
        command = str(
            (tool_input or {}).get("command")
            or (tool_input or {}).get("cmd")
            or (tool_input or {}).get("script")
            or ""
        )
        return any(pattern.search(command) for pattern in MUTATING_COMMAND_PATTERNS)
    return False


def _tool_input_text(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    values = []
    for key in ("command", "cmd", "script", "path", "file_path", "filePath"):
        value = tool_input.get(key)
        if value is not None:
            values.append(str(value))
    return "\n".join(values)


def _shell_command_text(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    return "\n".join(
        str(tool_input[key])
        for key in ("command", "cmd", "script")
        if tool_input.get(key) is not None
    )


def _neutralize_quoted_separators(command: str) -> str:
    """Blank out command separators that sit INSIDE a quoted string.

    The invocation matcher keys off shell separators (`|`, `&`, `;`) to find where a
    command starts, but a regex has no idea what is quoted. So a perfectly innocent
    `Select-String -Pattern "foo|agy models"` reads as `... | agy models` — a piped
    invocation — and gets denied. That happened twice to read-only diagnostics in one
    session, and a security control that cries wolf is one that gets switched off.

    Only the separator CHARACTERS inside quotes are replaced (with spaces), never the
    quotes or any other content, so a genuinely quoted executable path such as
    `"C:\\tools\\agy.exe" --print x` still matches exactly as before."""
    out = []
    quote: str | None = None
    for ch in command:
        if quote:
            if ch == quote:
                quote = None
                out.append(ch)
            elif ch in "|&;":
                out.append(" ")
            else:
                out.append(ch)
        else:
            if ch in "\"'":
                quote = ch
            out.append(ch)
    return "".join(out)


def _is_direct_agy_shell_invocation(tool_name: object, tool_input: object) -> bool:
    name = normalize_tool_name(tool_name)
    if name not in SHELL_TOOL_NAMES and "shell" not in name:
        return False
    command = _neutralize_quoted_separators(_shell_command_text(tool_input))
    return bool(
        DIRECT_AGY_COMMAND_RE.search(command)
        or START_PROCESS_AGY_RE.search(command)
    )


def _is_claude_consult_route(tool_name: object, tool_input: object) -> bool:
    """Identify only structured Switchboard routes that actually target Claude.

    Prompt prose is intentionally ignored. A user may mention Claude while routing a
    Flash package, and allowing that text to steer a hard circuit breaker would create
    an easy false positive. An explicit non-Claude target agent also wins over a model
    field because Antigravity can itself host Claude-branded models.
    """
    name = normalize_tool_name(tool_name)
    if name in CLAUDE_CONSULT_TOOL_NAMES:
        return True
    if name not in ROUTE_AGENT_TASK_TOOL_NAMES or not isinstance(tool_input, dict):
        return False
    agent = str(tool_input.get("target_agent") or tool_input.get("agent") or "").strip().lower()
    if agent:
        return agent in CLAUDE_AGENT_NAMES
    model = str(tool_input.get("target_model") or tool_input.get("model") or "").strip().lower()
    if not model:
        return False
    normalized_model = re.sub(r"[^a-z0-9]+", " ", model).strip()
    return "claude" in normalized_model.split() or any(
        alias in normalized_model.split() for alias in CLAUDE_MODEL_ALIASES
    )


def _claude_unavailable_reason(session_id: str) -> str | None:
    reason = _read_state(session_id).get("claude_unavailable") if session_id else None
    return reason if reason in CLAUDE_UNAVAILABLE_REASONS else None


def _direct_labour_category(tool_name: object, tool_input: object) -> str | None:
    name = normalize_tool_name(tool_name)
    if not name or name in DELEGATION_TOOL_NAMES:
        return None
    # Opposite-vendor consultation is brain work, not same-vendor labour. The
    # broker independently rejects same-vendor labour unless native-unavailable
    # is documented, so consultation controls must remain usable at the gate.
    # Both the underscore (Codex) and hyphenated (Claude's live tool names)
    # Switchboard namespace spellings are exempt -- normalize_tool_name folds
    # them to the same canonical `mcp__agent_switchboard__` form.
    if name.startswith("mcp__agent_switchboard__") and any(
        name.endswith(suffix) for suffix in SWITCHBOARD_CONTROL_SUFFIXES
    ):
        return None
    if name in READ_TOOL_NAMES:
        return "reads"
    if name in SEARCH_TOOL_NAMES:
        return "searches"
    if name in WEB_RESEARCH_TOOL_NAMES or name.startswith("mcp__"):
        return "evidence"
    text = _tool_input_text(tool_input)
    if name in SHELL_TOOL_NAMES or "shell" in name:
        if ROUTING_OVERRIDE_COMMAND_RE.search(text):
            return None
        return "tests" if TEST_COMMAND_RE.search(text) else "other"
    if name in MUTATING_TOOL_NAMES:
        return "docs" if DOC_PATH_RE.search(text) else "other"
    return None


def user_prompt_submit(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    reset_turn_state(session_id)
    context = _standing_policy_context(session_id)
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def _standing_policy_context(session_id: str) -> str:
    """A fixed, field-validated status line injected each turn.

    Why this exists: a harness-level system instruction on some hosts says "do not call
    the AgentTool unless the user requested it", which outranks a policy file and left
    the brain doing everything itself. This states the owner's standing request as
    user-owned configuration, so the harness condition is satisfied rather than fought.

    Everything interpolated below is a validated int/enum/semver produced by this
    module. No free-form ids, error text, tool names, or user-derived strings — an
    injected status line must never become a channel for prompt content."""
    try:
        limit = int(effective_direct_labour_limit())
        mode = "warn" if gate_mode() == "warn" else "enforce"
        used = 0
        if session_id:
            session_state = _read_state(session_id)
            used = int(session_state.get("direct_labour_since_relief") or 0)
            claude_circuit_open = (
                session_state.get("claude_unavailable") in CLAUDE_UNAVAILABLE_REASONS
            )
        else:
            claude_circuit_open = False
        remaining = max(0, limit - used)
        version = str(BROKER_VERSION)
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            version = "unknown"
        relaxed = " | POLICY RELAXED (weaker than shipped default)" if policy_is_relaxed() else ""
        # Carried per-turn rather than left to the instruction file, because a session
        # loads its instructions ONCE at start: a policy change reaches new sessions
        # only, while every conversation already in flight keeps obeying the old rule
        # (and keeps imitating its own prior turns). This line reaches all of them on
        # the very next turn.
        audit_line = (
            "Do NOT write a routing audit into your reply -- the broker records every lane "
            "automatically. Only produce one if the user asks, by running "
            "`agent-switchboard.exe routing-report --table` and returning its output. "
            if audit_mode() == "on-demand"
            else "A routing audit IS required in your final message this turn. "
        )
        return (
            f"[routing] delegation-policy: active | gate: {mode} | "
            f"direct-labour budget: {remaining}/{limit} remaining | switchboard {version}{relaxed}. "
            f"{audit_line}"
            "The owner has a STANDING REQUEST to delegate eligible labour: dispatching one "
            "bounded package to the Flash workhorse (route_agent_task, target_agent='antigravity', "
            "surface='cli', target_model='gemini flash', effort='high') or to a managed native "
            "subagent is pre-authorized and does not need fresh permission this turn. Flash is the "
            "default lane for bounded reading, search, extraction, summarising and drafting/writing; "
            "use a native role when the package needs host-only tools or session state, and say why. "
            "For factual investigation, route task_kind='research' with 1-3 exact research_questions; "
            "every request must demand full-scope coverage beyond the first match, primary evidence, "
            "a competing-explanation check, and NOT FOUND with searched boundaries instead of inference."
            + (f" {CLAUDE_CIRCUIT_CONTEXT}" if claude_circuit_open else "")
        )
    except Exception:  # noqa: BLE001
        return ""


def register_brain_override(session_id: str, package_id: str, reason: str) -> bool:
    session_id = str(session_id or "").strip()
    package_id = str(package_id or "").strip()
    reason = " ".join(str(reason or "").split())
    if (
        not session_id
        or not re.fullmatch(r"WP[A-Za-z0-9_.-]+", package_id, re.IGNORECASE)
        or len(reason) < 12
    ):
        return False

    def update(state: dict) -> None:
        overrides = state.setdefault("brain_overrides", {})
        overrides[package_id.lower()] = {
            "package_id": package_id,
            "reason": reason,
            "registered_at": time.time(),
        }
        state["direct_labour_since_relief"] = 0
        state["labour_relief_sequence"] = int(
            state.get("labour_relief_sequence") or 0
        ) + 1

    ok = _update_state(session_id, update) is not None
    if ok:
        # Also append to the durable log. Session STATE is reset each turn, so
        # anything recorded only there vanishes and the rendered audit silently
        # loses packages; the log is append-only and survives.
        log_gate_decision(
            session_id, "Override", "-", None, "override",
            extra={"work_package_id": package_id, "reason": reason},
        )
    return ok


def routing_override_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-switchboard routing-override")
    parser.add_argument("--session", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--reason", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    ok = register_brain_override(args.session, args.package, args.reason)
    sys.stdout.write(json.dumps({"registered": ok, "package": args.package}))
    return 0 if ok else 2


def _report_state_files_by_mtime(limit: int) -> list[Path]:
    try:
        files = list(STATE_DIR.glob("*.json"))
    except OSError:
        return []
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return files[: max(0, limit)]


def _read_session_log(log_path: Path) -> list[dict]:
    records: list[dict] = []
    if not log_path.exists():
        return records
    try:
        text = log_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _format_routing_report(label: str, state: dict, log_records: list[dict]) -> str:
    lines = [f"Routing report: {label}"]
    counts = state.get("direct_labour_counts") or {}
    nonzero = [
        f"{name}={int(counts.get(name) or 0)}"
        for name in DIRECT_BRAIN_LABOUR_CATEGORIES
        if int(counts.get(name) or 0)
    ]
    lines.append("Direct labour by category: " + (", ".join(nonzero) if nonzero else "(none)"))
    lines.append(f"Total direct labour: {int(state.get('direct_labour_count') or 0)}")
    lines.append(
        f"Direct labour since last relief: {int(state.get('direct_labour_since_relief') or 0)}"
    )
    lines.append(f"Relief sequence: {int(state.get('labour_relief_sequence') or 0)}")
    lines.append(f"Denials: {int(state.get('labour_gate_denials') or 0)}")

    agents = state.get("native_agents") or {}
    started = [a for a in agents.values() if isinstance(a, dict)]
    completed = [a for a in started if a.get("completed")]
    lines.append(f"Native agents: started={len(started)} completed={len(completed)}")
    for agent_id, agent in agents.items():
        if isinstance(agent, dict):
            lines.append(
                f"  {agent_id}: type={agent.get('agent_type')} status={agent.get('status')}"
            )

    overrides = state.get("brain_overrides") or {}
    lines.append(f"Brain overrides: {len(overrides)}")
    for package_id, override in overrides.items():
        if isinstance(override, dict):
            lines.append(f"  {package_id}: {override.get('reason')}")

    denial_log_count = sum(
        1 for r in log_records if str(r.get("decision") or "") == "deny"
    )
    credit_log_count = sum(
        1 for r in log_records if str(r.get("decision") or "") == "credit"
    )
    dispatch_outcomes: dict[str, int] = {}
    for record in log_records:
        tool = str(record.get("tool") or "")
        if "route_agent_task" in tool:
            outcome = str(record.get("outcome") or record.get("decision") or "unknown")
            dispatch_outcomes[outcome] = dispatch_outcomes.get(outcome, 0) + 1
    if dispatch_outcomes:
        lines.append(
            "Switchboard dispatches: "
            + ", ".join(f"{k}={v}" for k, v in sorted(dispatch_outcomes.items()))
        )
    lines.append(f"Log denials: {denial_log_count}")
    lines.append(f"Log credits: {credit_log_count}")
    return "\n".join(lines)


_AGENT_LANES = {
    "explore": "reader",
    "explorer": "reader",
    "economy-worker": "workhorse",
    "worker": "workhorse",
    "plan": "workhorse",
    "general-purpose": "brain-priced",
}


def _format_routing_audit_table(label: str, log_records: list[dict]) -> str:
    """Render the routing audit FROM THE LEDGER, as a markdown table.

    Built from the append-only decision log rather than session state, because
    state is reset every turn: anything read from state loses packages from
    earlier in the session and silently under-reports. This is the whole point of
    having the backend render it -- one source of truth, one format, no drift
    between turns, and no cost to the model at all."""
    rows: list[tuple[str, str, str, str, str]] = []
    counts = {name: 0 for name in DIRECT_BRAIN_LABOUR_CATEGORIES}

    for record in log_records:
        decision = str(record.get("decision") or "")
        event = str(record.get("event") or "")
        if event == "PreToolUse" and decision in {"reserve", "allow", "warn", "deny"}:
            category = record.get("category")
            if category in counts and decision != "deny":
                counts[category] += 1
        elif decision == "override":
            package = str(record.get("work_package_id") or "WP-unknown")
            reason = str(record.get("reason") or "")
            rows.append((package, "brain", "direct", "-",
                         f"override: brain - {package}: {reason}"))
        elif decision == "native-start":
            agent_id = str(record.get("agent_id") or "")
            agent_type = str(record.get("agent_type") or "")
            lane = _AGENT_LANES.get(agent_type.lower(), "workhorse")
            model = str(record.get("model") or "") or "configured (runtime unverified)"
            rows.append((f"native:{agent_type}", lane, f"native {agent_type}", model,
                         f"native:{agent_id}"))
        elif decision == "credit":
            package = str(record.get("work_package_id") or "WP-unknown")
            receipt = str(record.get("receipt") or "")
            rows.append((package, "workhorse", "switchboard", "gemini flash (resolved)", receipt))

    lines = [f"## Routing audit — {label}", "", f"packages: {len(rows)}", ""]
    if rows:
        lines.append("| Package | Lane | Mechanism | Model | Receipt |")
        lines.append("|---|---|---|---|---|")
        for row in rows:
            lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
    else:
        lines.append("_No packages recorded for this session._")
    lines.append("")
    lines.append(
        "direct-brain-labour: "
        + " | ".join(f"{name}={counts[name]}" for name in DIRECT_BRAIN_LABOUR_CATEGORIES)
    )
    return "\n".join(lines)


def routing_report_cli(argv: list[str]) -> int:
    """Print a compact human summary of session gate state + decision log.

    A report never fails a session: any lookup/parse problem falls back to
    an empty/"no sessions found" report rather than raising or exiting
    non-zero.
    """
    parser = argparse.ArgumentParser(prog="agent-switchboard routing-report")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--session")
    group.add_argument("--last", type=int, default=1)
    parser.add_argument(
        "--table", action="store_true",
        help="render the routing audit as a markdown table from the ledger",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    try:
        if args.session:
            state_path = _session_path(args.session)
            sessions = [(args.session, state_path)]
        else:
            limit = args.last if args.last and args.last > 0 else 1
            sessions = [(p.stem, p) for p in _report_state_files_by_mtime(limit)]
        if not sessions:
            sys.stdout.write("Routing report: no sessions found\n")
            return 0
        reports = []
        for label, state_path in sessions:
            try:
                state = (
                    json.loads(state_path.read_text(encoding="utf-8"))
                    if state_path.exists()
                    else {}
                )
            except Exception:  # noqa: BLE001
                state = {}
            log_path = state_path.with_name(state_path.stem + ".log.jsonl")
            records = _read_session_log(log_path)
            reports.append(
                _format_routing_audit_table(label, records)
                if args.table
                else _format_routing_report(label, state, records)
            )
        sys.stdout.write("\n\n".join(reports) + "\n")
    except Exception:  # noqa: BLE001 - a report must never fail a session
        sys.stdout.write("Routing report: unavailable\n")
    return 0


def _routing_override_command(session_id: str) -> str:
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "routing-override"]
    else:
        argv = [
            sys.executable,
            str(Path(__file__).with_name("agent_broker_entry.py")),
            "routing-override",
        ]
    prefix = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    return (
        f'{prefix} --session {shlex.quote(session_id)} --package WP-ID '
        '--reason "specific reason at least 12 characters"'
    )


def pre_tool_use(payload: dict) -> dict:
    """Deny the next direct labour call after the threshold until delegation.

    Agent/Task creation is never labour-classified, so the model always retains
    an escape path. Claude identifies cheap subagent calls; Codex is exempt
    while a cheap role is active because it does not document per-tool agent ids.
    """
    sweep_stale()
    host = str(payload.get("_switchboard_host") or "").strip().lower()
    session_id = str(payload.get("session_id") or "").strip()
    normalized_tool = normalize_tool_name(payload.get("tool_name"))
    claude_unavailable = _claude_unavailable_reason(session_id)
    if claude_unavailable and _is_claude_consult_route(
        payload.get("tool_name"), payload.get("tool_input") or {}
    ):
        # Availability failures are session-scoped. This is a hard circuit breaker,
        # deliberately independent of warn/shadow mode, so an unavailable expensive
        # provider is not retried every turn. Only the enum is surfaced or logged.
        log_gate_decision(
            session_id,
            "PreToolUse",
            normalized_tool,
            None,
            "deny",
            extra={"reason": "claude_session_unavailable", "kind": claude_unavailable},
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": CLAUDE_CIRCUIT_CONTEXT,
            }
        }
    # Host-independent on purpose. This used to require host in {codex, claude}, which
    # meant a hook invoked without its host argument silently stopped enforcing a
    # security boundary. Nothing legitimate needs the SENDER to run agy itself: the
    # Switchboard backend runs it out of process, well outside this hook.
    if _is_direct_agy_shell_invocation(
        payload.get("tool_name"), payload.get("tool_input") or {}
    ):
        log_gate_decision(
            session_id, "PreToolUse", normalized_tool, None, "deny",
            extra={"reason": "direct_agy_shell"},
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Direct Antigravity CLI invocation is blocked. Call Agent "
                    "Switchboard's MCP route_agent_task with "
                    'target_agent="antigravity" and surface="cli". The Switchboard '
                    "backend may invoke agy after it validates the work-package and "
                    "JSON-schema contract; the sending Codex/Claude agent may not run "
                    "agy directly."
                ),
            }
        }
    category = _direct_labour_category(
        payload.get("tool_name"), payload.get("tool_input") or {}
    )
    if not session_id or not category:
        log_gate_decision(session_id, "PreToolUse", normalized_tool, category, "allow")
        return {}
    reserved = reserve_direct_labour(
        session_id,
        category,
        str(payload.get("tool_use_id") or ""),
        host,
        _payload_is_cheap_native_call(payload),
    )
    if reserved is None:
        log_gate_decision(
            session_id, "PreToolUse", normalized_tool, category, "allow",
            extra={"fail_open": True},
        )
        return {}
    allowed, state = reserved
    if allowed:
        log_gate_decision(session_id, "PreToolUse", normalized_tool, category, "reserve")
        return {}
    counts = state.get("direct_labour_counts") or {}
    observed = ", ".join(
        f"{name}={int(counts.get(name) or 0)}"
        for name in DIRECT_BRAIN_LABOUR_CATEGORIES
        if int(counts.get(name) or 0) > 0
    ) or f"total={effective_direct_labour_limit()}"
    # Lead with the cheapest lane and give it as a copy-pasteable call. The old message
    # named only the native roles and the override syntax, so a blocked brain reached
    # for a native subagent (or an override) and never discovered the workhorse that is
    # a tenth of the price -- the gate was quietly teaching the expensive habit.
    reason = (
        f"Routing gate: {int(state.get('direct_labour_since_relief') or 0)} direct brain "
        f"labour calls since the last delegation or override ({observed}); the allowance is "
        f"{effective_direct_labour_limit()}. The next {category} call is blocked. Pick a lane:\n"
        "1. DEFAULT — dispatch the bounded package to the Flash workhorse (~1/10 the cost of "
        "the native workhorse, several times faster):\n"
        "   route_agent_task {target_agent:'antigravity', surface:'cli', "
        "target_model:'gemini flash', effort:'high', mode:'plan', task_kind:'quick_check', "
        "work_package_id:'WP-<id>', prompt:'<one bounded package>'}\n"
        "   (add mode:'accept-edits' + allowed_files + acceptance_criteria to implement)\n"
        "2. Native cheap role (Agent/Task) when the package needs host-only tools or session "
        "state Flash cannot see — then state the flash_skip reason in the audit.\n"
        "3. Brain-retained package — register it exactly:\n"
        f"   {_routing_override_command(session_id)}\n"
        "Delegation tools are never blocked. High-risk judgment and final approval stay with "
        "the brain; the deterministic evidence, test, documentation, or mechanical remainder "
        "does not. Any registered override must appear verbatim in the completion audit."
    )
    if gate_mode() == "warn":
        # Shakedown mode: record exactly what WOULD have been denied, then allow it.
        log_gate_decision(
            session_id, "PreToolUse", normalized_tool, category, "warn",
            extra={"would_deny": True},
        )
        return {}
    log_gate_decision(session_id, "PreToolUse", normalized_tool, category, "deny")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _serialized_tool_response(response: object) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))


def _is_mcp_tool(tool_name: object) -> bool:
    return str(tool_name or "").strip().lower().startswith("mcp__")


def _json_container(value: object) -> object | None:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _unwrap_switchboard_response(value: object, depth: int = 0) -> dict | None:
    """Return a structured result through common MCP response wrappers."""
    if depth > 5:
        return None
    parsed = _json_container(value)
    if parsed is None:
        return None
    if isinstance(parsed, list):
        for item in parsed:
            candidate = _unwrap_switchboard_response(item, depth + 1)
            if candidate is not None:
                return candidate
        return None
    if any(key in parsed for key in ("status", "outcome", "error")):
        return parsed
    for key in ("result", "data", "output", "content", "text"):
        if key in parsed:
            candidate = _unwrap_switchboard_response(parsed.get(key), depth + 1)
            if candidate is not None:
                return candidate
    if any(key in parsed for key in ("isError", "ok", "success")):
        return parsed
    return None


def _failure_text(result: dict) -> str:
    """Extract provider failure fields for classification, with a strict size cap."""
    pieces: list[str] = []

    def collect(value: object, depth: int = 0) -> None:
        if depth > 4 or sum(len(item) for item in pieces) >= 32_000:
            return
        if isinstance(value, str):
            pieces.append(value[:8_000])
        elif isinstance(value, list):
            for item in value[:20]:
                collect(item, depth + 1)
        elif isinstance(value, dict):
            for key in (
                "error", "message", "reason", "response", "detail", "details",
                "stderr", "note", "content", "text",
            ):
                if key in value:
                    collect(value.get(key), depth + 1)

    collect(result)
    normalized = " ".join(" ".join(pieces).lower().split())
    return re.sub(r"[_-]+", " ", normalized)[:32_000]


def _claude_failure_reason(payload: dict) -> str | None:
    """Classify explicit, session-terminal Claude availability failures.

    The returned value is a fixed enum. Provider text is examined transiently but is
    never copied into state, logs, hook feedback, or standing context.
    """
    if not _is_claude_consult_route(
        payload.get("tool_name"), payload.get("tool_input") or {}
    ):
        return None
    result = _unwrap_switchboard_response(payload.get("tool_response"))
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or result.get("outcome") or "").strip().lower()
    if status in {"ok", "success", "succeeded", "completed", "queued", "pending"}:
        return None
    explicit_failure = (
        status in {"error", "failed", "failure", "unavailable", "not_available", "disabled"}
        or result.get("isError") is True
        or result.get("ok") is False
        or result.get("success") is False
        or bool(result.get("error"))
    )
    if not explicit_failure:
        return None
    text = _failure_text(result)
    if not text:
        return None

    if re.search(
        r"\bclaude(?:\s+code)?\s+cli\s+(?:was\s+|is\s+)?not\s+found\b|"
        r"\bclaude\s+(?:cli\s+)?executable\s+(?:was\s+|is\s+)?not\s+found\b|"
        r"\bcannot\s+find\b.{0,40}\bclaude(?:\s+code)?\s+cli\b",
        text,
    ):
        return "cli_missing"
    if re.search(
        r"\b(?:quota|usage\s+limit)\b.{0,50}\b(?:exceeded|exhausted|depleted|reached|hit|used\s+up)\b|"
        r"\b(?:exceeded|exhausted|depleted|reached|hit)\b.{0,35}\b(?:quota|usage\s+limit)\b|"
        r"\byou(?:'ve|\s+have)\s+hit\s+your\s+(?:usage\s+)?limit\b|"
        r"\b(?:daily|weekly|monthly)\s+(?:usage\s+)?limit\s+(?:was\s+|is\s+)?(?:reached|exceeded)\b",
        text,
    ):
        return "quota"
    if re.search(
        r"\bsubscription\b.{0,50}\b(?:disabled|inactive|suspended|deactivated|cancelled|"
        r"not\s+active|not\s+enabled|required)\b|"
        r"\b(?:disabled|inactive|suspended|deactivated|cancelled)\b.{0,35}\bsubscription\b",
        text,
    ):
        return "subscription_disabled"
    if re.search(
        r"\b(?:organization|organisation|org)\b.{0,50}\b(?:disabled|suspended|deactivated|"
        r"not\s+active|not\s+enabled)\b|"
        r"\b(?:disabled|suspended|deactivated)\b.{0,35}\b(?:organization|organisation|org)\b",
        text,
    ):
        return "organization_disabled"
    if re.search(
        r"\b(?:claude|provider|service)\b.{0,50}\b(?:unavailable|not\s+available|unreachable)\b|"
        r"\b(?:unavailable|not\s+available|unreachable)\b.{0,50}\b(?:claude|provider|service)\b",
        text,
    ):
        return "provider_unavailable"

    explicit_family_access = bool(
        re.search(
            r"\bno\s+(?:available\s+)?claude\s+models?\b|"
            r"\ball\s+claude\s+models?\b.{0,40}\b(?:unavailable|not\s+available|inaccessible)\b|"
            r"\b(?:account|organization|organisation|org|workspace|user|team|plan)\b.{0,80}"
            r"\b(?:not\s+entitled|does\s+not\s+have|has\s+no|lacks)\b.{0,80}\b(?:claude|models?)\b|"
            r"\b(?:not\s+entitled|no\s+access|access\s+(?:is\s+)?(?:denied|disabled))\b"
            r".{0,80}\bclaude\b|"
            r"\bclaude\b.{0,80}\b(?:not\s+available|unavailable|disabled|not\s+enabled)\b"
            r".{0,50}\b(?:account|organization|organisation|org|workspace|subscription|plan)\b",
            text,
        )
    )
    attempted = result.get("attempted_models")
    attempted_family = isinstance(attempted, list) and len(
        {str(item).strip().lower() for item in attempted if str(item).strip()}
    ) >= 2
    entitlement_failure = bool(
        re.search(
            r"\b(?:not\s+entitled|no\s+access|access\s+(?:denied|disabled)|"
            r"not\s+available\s+for\s+(?:your|this)\s+(?:account|organization|organisation|org|plan|subscription))\b",
            text,
        )
    )
    if explicit_family_access or (attempted_family and entitlement_failure):
        return "family_access_unavailable"
    return None


def _record_claude_unavailable(payload: dict) -> str | None:
    session_id = str(payload.get("session_id") or "").strip()
    reason = _claude_failure_reason(payload)
    if not session_id or reason not in CLAUDE_UNAVAILABLE_REASONS:
        return None

    def update(state: dict) -> None:
        state["claude_unavailable"] = reason

    if _update_state(session_id, update) is None:
        return None
    log_gate_decision(
        session_id,
        "PostToolUse",
        normalize_tool_name(payload.get("tool_name")),
        None,
        "claude-circuit-open",
        extra={"kind": reason},
    )
    return reason


def _merge_post_tool_context(result: dict | None, context: str | None) -> dict:
    """Add sanitized guidance without replacing ingress/credit feedback."""
    if not context:
        return result or {}
    if result is None:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }
    output = result.get("hookSpecificOutput")
    if isinstance(output, dict):
        existing = str(output.get("additionalContext") or "").strip()
        output["additionalContext"] = f"{existing} {context}".strip()
    elif result.get("decision") == "block":
        existing = str(result.get("reason") or "").strip()
        result["reason"] = f"{existing} {context}".strip()
    else:
        result["hookSpecificOutput"] = {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    return result


def _store_context_evidence(payload: dict, serialized_response: str) -> Path | None:
    """Persist oversized evidence outside model context with its query/provenance.

    The local file is intentionally short-lived and user-readable only where the
    platform supports POSIX modes. On any write failure the hook fails open so it
    never destroys the only copy of a tool result.
    """
    now = time.time()
    identity = "|".join(
        str(payload.get(key) or "")
        for key in ("session_id", "turn_id", "tool_use_id", "tool_name")
    )
    digest = hashlib.sha256(f"{identity}|{time.time_ns()}".encode("utf-8")).hexdigest()[:20]
    safe_tool = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("tool_name") or "mcp"))[:80]
    path = EVIDENCE_DIR / f"{int(now)}-{safe_tool}-{digest}.json"
    truncated = len(serialized_response) > CONTEXT_EVIDENCE_MAX_CHARS
    record = {
        "schema": "agent-switchboard.context-evidence.v1",
        "created_at_epoch": now,
        "host": str(payload.get("_switchboard_host") or "unknown"),
        "session_id": str(payload.get("session_id") or ""),
        "turn_id": str(payload.get("turn_id") or ""),
        "tool_name": str(payload.get("tool_name") or ""),
        "tool_use_id": str(payload.get("tool_use_id") or ""),
        "tool_input": payload.get("tool_input"),
        "response_chars": len(serialized_response),
        "response_truncated_on_disk": truncated,
        "tool_response_serialized": serialized_response[:CONTEXT_EVIDENCE_MAX_CHARS],
    }
    try:
        atomic_io.atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
    except Exception:  # noqa: BLE001 - preserve the original tool result on failure
        return None


def _context_ingress_feedback(payload: dict) -> dict | None:
    if not _is_mcp_tool(payload.get("tool_name")) or "tool_response" not in payload:
        return None
    try:
        serialized = _serialized_tool_response(payload.get("tool_response"))
    except Exception:  # noqa: BLE001
        return None
    if len(serialized) <= CONTEXT_INGRESS_MAX_CHARS:
        return None
    evidence_path = _store_context_evidence(payload, serialized)
    if evidence_path is None:
        return None
    feedback = (
        f"Brain-context ingress gate replaced an oversized MCP response "
        f"({len(serialized)} characters; limit {CONTEXT_INGRESS_MAX_CHARS}). "
        f"Raw evidence plus the original query is quarantined at {evidence_path}. "
        "Do not read the whole file into the brain context. Delegate extraction to the "
        "native reader or re-run/filter with an explicit field projection and output cap. "
        "If a claim could change the patch, risk classification, or release decision, first "
        "state: decision premise | what changes if false | bounded primary evidence; then "
        "inspect only that minimal evidence range."
    )
    if str(payload.get("_switchboard_host") or "").strip().lower() == "claude":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": feedback,
            }
        }
    return {"decision": "block", "reason": feedback}


def post_tool_use(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    tool_input = payload.get("tool_input") or {}
    normalized_tool = normalize_tool_name(payload.get("tool_name"))
    mutated = _is_mutating(payload.get("tool_name"), tool_input)
    if session_id and mutated and not _payload_is_cheap_native_call(payload):
        mark_mutated(session_id)
    claude_failure = _record_claude_unavailable(payload)
    claude_context = CLAUDE_CIRCUIT_CONTEXT if claude_failure else None
    ingress_feedback = _context_ingress_feedback(payload)
    log_gate_decision(
        session_id,
        "PostToolUse",
        normalized_tool,
        None,
        "deny" if ingress_feedback is not None else "allow",
        extra={"mutated": bool(mutated)},
    )
    if ingress_feedback is not None:
        return _merge_post_tool_context(ingress_feedback, claude_context)
    credit = _credit_switchboard_dispatch(session_id, normalized_tool, payload)
    if credit is not None:
        return _merge_post_tool_context(credit, claude_context)
    if session_id:
        notify = {"value": False}

        def update(state: dict) -> None:
            sequence = int(state.get("labour_relief_sequence") or 0)
            checkpoint_sequence = state.get("labour_checkpoint_sequence")
            checkpoint_sequence = (
                -1 if checkpoint_sequence is None else int(checkpoint_sequence)
            )
            if (
                int(state.get("direct_labour_since_relief") or 0)
                == effective_direct_labour_limit()
                and checkpoint_sequence != sequence
            ):
                state["labour_checkpoint_sequence"] = sequence
                notify["value"] = True

        state = _update_state(session_id, update)
        if state and notify["value"]:
            checkpoint = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"Routing checkpoint: {effective_direct_labour_limit()} direct brain "
                        "labour calls since the last delegation or override. The next eligible "
                        "labour call will be denied. Cheapest next move: dispatch the bounded "
                        "package with route_agent_task (target_agent='antigravity', "
                        "surface='cli', target_model='gemini flash', effort='high', mode='plan')."
                    ),
                }
            }
            return _merge_post_tool_context(checkpoint, claude_context)
    return _merge_post_tool_context(None, claude_context)


CREDITABLE_DISPATCH_TOOL = "mcp__agent_switchboard__route_agent_task"
CREDITABLE_OUTCOME = "completed_verified"


def _coerce_json_dict(value: object) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _extract_dispatch_result(payload: dict) -> dict | None:
    """Pull the router's result dict out of a PostToolUse payload.

    Hosts disagree about the shape of `tool_response` for an MCP tool: it may be the
    result object, a list of content blocks, a dict wrapping `content`, or a bare JSON
    string. This tried only two of those, so in practice it silently returned None and
    the credit path never ran -- with no log line to say so, which is how a broken
    incentive stayed invisible while looking healthy.

    Whatever is found here is only ever used to look up a server-issued receipt in the
    ledger; it is never trusted as authority in itself."""
    response = payload.get("tool_response")

    direct = _coerce_json_dict(response)
    if direct is not None and "content" not in direct:
        return direct

    # A list of content blocks, or a dict wrapping one.
    blocks: list = []
    if isinstance(response, list):
        blocks = response
    elif isinstance(direct, dict):
        raw = direct.get("content")
        if isinstance(raw, list):
            blocks = raw
        elif isinstance(raw, (str, dict)):
            blocks = [raw]

    for block in blocks:
        candidate = block
        if isinstance(block, dict):
            candidate = block.get("text", block)
        parsed = _coerce_json_dict(candidate)
        if parsed is not None:
            return parsed

    if direct is not None:
        return direct
    return None


def _receipt_resolves(receipt: str) -> bool:
    """True when the receipt names a real consultation row.

    This is the whole point of a server-issued receipt: without the ledger lookup a
    sender could mint `broker:<any-uuid>` and claim credit for a dispatch it never made.

    Fails CLOSED, unlike the rest of the gate. That is safe here rather than merely
    strict: a genuine receipt can only have been issued by a server new enough to have
    the `request_id` column, and that same server creates the column on startup. So an
    unreadable or unmigrated ledger cannot be hiding a real receipt — it can only be
    hiding a fabricated one. The cost of a false negative is one override or native
    package; the cost of a false positive is that the receipt means nothing at all."""
    request_id = str(receipt or "").split(":", 1)[-1].strip()
    if not request_id:
        return False
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT 1 FROM consultations WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone()
        finally:
            # sqlite3.Connection's context manager commits/rolls back but does not
            # close the handle.  An explicit close prevents persistent Windows file
            # locks in short-lived hook processes and their in-process tests.
            conn.close()
        return bool(row)
    except sqlite3.Error:
        return False


def _credit_switchboard_dispatch(session_id: str, normalized_tool: str, payload: dict) -> dict | None:
    """Grant one bounded block for a dispatch that actually completed.

    Only `completed_verified` earns relief. A blocked, rejected, failed, or
    unavailable dispatch earns nothing: otherwise a sender could farm credit by
    repeatedly firing a route it knows is broken, which is strictly cheaper than
    doing the work and would make the gate an incentive to spam a dead lane."""
    if not session_id or normalized_tool != CREDITABLE_DISPATCH_TOOL:
        return None
    result = _extract_dispatch_result(payload)
    if not isinstance(result, dict):
        # Never fail silently here again: an unreadable response means the whole
        # Flash-relieves-the-gate incentive quietly stops working while every other
        # signal still looks healthy.
        log_gate_decision(
            session_id, "PostToolUse", normalized_tool, None, "no-credit",
            extra={"reason": "tool_response not parseable",
                   "response_type": type(payload.get("tool_response")).__name__},
        )
        return None
    receipt = str(result.get("receipt") or "")
    outcome = str(result.get("outcome") or "")
    work_package = str(result.get("work_package_id") or "") or "unnamed-package"
    if not BROKER_RECEIPT_RE.search(receipt):
        log_gate_decision(session_id, "PostToolUse", normalized_tool, None, "no-credit",
                          extra={"reason": "missing or malformed receipt", "outcome": outcome})
        return None
    if outcome != CREDITABLE_OUTCOME:
        log_gate_decision(session_id, "PostToolUse", normalized_tool, None, "no-credit",
                          extra={"reason": f"outcome={outcome or 'unknown'}", "receipt": receipt})
        return None
    if not _receipt_resolves(receipt):
        log_gate_decision(session_id, "PostToolUse", normalized_tool, None, "no-credit",
                          extra={"reason": "receipt does not resolve in the ledger"})
        return None

    granted = {"value": False}

    def update(state: dict) -> None:
        credited = state.setdefault("credited_receipts", [])
        if receipt in credited:
            return  # one-use: a replayed receipt buys nothing
        credited.append(receipt)
        state["direct_labour_since_relief"] = 0
        state["labour_relief_sequence"] = int(state.get("labour_relief_sequence") or 0) + 1
        dispatches = state.setdefault("switchboard_dispatches", [])
        dispatches.append({"receipt": receipt, "work_package_id": work_package, "outcome": outcome})
        granted["value"] = True

    _update_state(session_id, update)
    if not granted["value"]:
        log_gate_decision(session_id, "PostToolUse", normalized_tool, None, "no-credit",
                          extra={"reason": "receipt already credited", "receipt": receipt})
        return None
    log_gate_decision(session_id, "PostToolUse", normalized_tool, None, "credit",
                      extra={"receipt": receipt, "work_package_id": work_package})
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"Routing credit: {work_package} completed on the Flash workhorse lane "
                f"({receipt}); the next bounded block is open. The result is EVIDENCE, not "
                "acceptance — check the cited lines, the actual diff, and any check output "
                "before accepting it or dispatching the next package, and cite this receipt "
                "in the routing audit."
            ),
        }
    }


def _ledger_reachable(timeout: float = 1.5) -> bool:
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=timeout)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _receipt_valid(
    request_id: str,
    caller_host: str = "",
    allow_same_vendor: bool = False,
) -> bool | None:
    """Return True/False for a reachable ledger, or None when validation itself
    is unavailable. Stop hooks must fail open for the None case."""
    try:
        import agent_broker_mcp  # local import: avoid import cost/side effects when unused
        result = agent_broker_mcp.request_status(request_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(result, dict) or not result.get("found"):
        return False
    if not result.get("answered"):
        return False
    if str(result.get("state") or "").strip().lower() != "completed":
        return False
    if result.get("model_attested") is not True:
        return False
    responder_model = str(result.get("responder_model") or "").strip().lower()
    if not responder_model.startswith(VALID_RESPONDER_PREFIXES):
        return False
    if "unverified" in responder_model:
        return False
    responder_vendor = responder_model.split(":", 1)[0]
    normalized_host = str(caller_host or "").strip().lower()
    if (
        normalized_host in {"codex", "claude"}
        and responder_vendor == normalized_host
        and not allow_same_vendor
    ):
        return False
    return True


def _native_receipt_valid(session_id: str, agent_id: str) -> bool:
    state = _read_state(session_id)
    agents = state.get("native_agents") or {}
    item = agents.get(agent_id)
    return bool(
        isinstance(item, dict)
        and (item.get("completed") is True or item.get("status") == "completed")
        and _normalized_agent_type(item.get("agent_type")) in NATIVE_CHEAP_AGENT_TYPES
    )


def _lookup_routing_audit_valid(
    last_message: str, session_id: str, caller_host: str = ""
) -> bool | None:
    """Validate explicit broker/native/per-package receipts in the final audit.

    Broker UUIDs are checked against the ledger. Native ids are checked against
    host lifecycle events captured for this session. A structured brain override
    is deliberately package-scoped; a bare global override is not a receipt.
    """
    section = ROUTING_AUDIT_SECTION_RE.search(last_message or "")
    if not section:
        return False
    body = section.group(1)
    direct_labour = DIRECT_BRAIN_LABOUR_RE.search(body)
    if not direct_labour:
        return False
    session_state = _read_state(session_id)
    observed_counts = (session_state.get("direct_labour_counts") or {})
    for category, raw_count in zip(
        DIRECT_BRAIN_LABOUR_CATEGORIES, direct_labour.groups()
    ):
        if int(raw_count) < int(observed_counts.get(category) or 0):
            return False
    package_count_match = AUDIT_PACKAGE_COUNT_RE.search(body)
    if not package_count_match:
        return False
    package_count = int(package_count_match.group(1))
    rows = []
    for line in body.splitlines():
        match = AUDIT_ROW_RE.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
    if package_count < 1 or len(rows) != package_count:
        return False
    for category, raw_count in zip(
        DIRECT_BRAIN_LABOUR_CATEGORIES, direct_labour.groups()
    ):
        if int(raw_count) > 0 and not any(
            re.search(
                rf"\bdirect\s*=\s*[^|\n]*\b{re.escape(category)}\b",
                row_body,
                re.IGNORECASE,
            )
            for _, row_body in rows
        ):
            return False
    seen_ids: set[str] = set()
    seen_override_ids: set[str] = set()
    for row_id, row_body in rows:
        normalized_row_id = row_id.lower()
        if normalized_row_id in seen_ids:
            return False
        seen_ids.add(normalized_row_id)
        broker_ids = BROKER_RECEIPT_RE.findall(row_body)
        native_ids = NATIVE_RECEIPT_RE.findall(row_body)
        overrides = STRUCTURED_OVERRIDE_RE.findall(row_body)
        if len(broker_ids) + len(native_ids) + len(overrides) != 1:
            return False
        if overrides and overrides[0][0].lower() != normalized_row_id:
            return False
        if overrides:
            override_id, override_reason = overrides[0]
            override_id = override_id.lower()
            seen_override_ids.add(override_id)
            registered = (session_state.get("brain_overrides") or {}).get(override_id)
            if registered and str(registered.get("reason") or "").lower() not in str(
                override_reason
            ).lower():
                return False
        if native_ids and not _native_receipt_valid(session_id, native_ids[0]):
            return False
        if broker_ids:
            broker_valid = _receipt_valid(
                broker_ids[0],
                caller_host,
                allow_same_vendor=bool(NATIVE_UNAVAILABLE_RE.search(row_body)),
            )
            if broker_valid is None:
                return None
            if not broker_valid:
                return False
    if not set((session_state.get("brain_overrides") or {}).keys()).issubset(
        seen_override_ids
    ):
        return False
    return True


AUDIT_MODE_ENV = "AGENT_BROKER_AUDIT_MODE"


def audit_mode() -> str:
    """`on-demand` (default) or `require`.

    The Stop gate used to demand that the model retype, in prose, a summary of work
    the hooks had already recorded. That was worth doing when the hooks saw only some
    tool calls and the model's declaration was the only record. It is not worth doing
    now: PreToolUse/PostToolUse intercept every call, so the append-only decision log
    is strictly MORE reliable than the model's account of itself -- prose can
    misreport, the log cannot. Requiring it bought nothing and cost a great deal:
    ~700-900 output tokens on every mutating turn, format-only rejections that
    discarded and regenerated whole messages, and an observer effect where a session
    had to spend a labour call reading its own gate state in order to describe it.

    So the default is now silent-but-recorded. Enforcement is untouched -- the
    allowance, the credit rules and the denials all still apply. Only the demand for
    a model-written summary is gone. Ask for it with `routing-report` and the backend
    renders it from the ledger, correctly and identically every time.

    `require` restores the old attestation behaviour for anyone who wants the model
    to sign its own work."""
    mode = str(os.environ.get(AUDIT_MODE_ENV, "on-demand")).strip().lower()
    return "require" if mode in {"require", "required", "strict", "always"} else "on-demand"


def stop(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id or not has_mutation(session_id):
        return {}
    if payload.get("stop_hook_active"):
        return {}
    if audit_mode() == "on-demand":
        # Recorded, not recited. Everything the audit would have said is already in
        # the decision log and the consultation ledger.
        log_gate_decision(session_id, "Stop", "-", None, "recorded", extra={"mutated": True})
        return {}
    last_message = str(payload.get("last_assistant_message") or "")
    if not ROUTING_AUDIT_SECTION_RE.search(last_message) and not _ledger_reachable():
        return {}
    audit_valid = _lookup_routing_audit_valid(
        last_message, session_id, str(payload.get("_switchboard_host") or "")
    )
    if audit_valid is None:
        return {}
    if audit_valid:
        return {}
    if already_blocked(session_id):
        return {}
    mark_blocked(session_id)
    return {
        "decision": "block",
        "reason": (
            "This turn mutated files/ran a mutating command without a verified routing audit. "
            "Include a 'Routing audit' section using broker:<uuid> for Switchboard work, "
            "native:<agent-id> for a completed managed native worker/explorer, or "
            "'override: brain - <WP-ID>: <specific reason>' for a package retained by the "
            "brain. Also include `direct-brain-labour: reads=N | searches=N | evidence=N | "
            "tests=N | docs=N | other=N`, counting planned and unplanned direct labour; map "
            "each nonzero category on a package row as `direct=reads,searches,...`. Reported "
            "counts cannot be lower than the host-observed pre-delegation floor. "
            "Bare/global overrides are invalid; then stop again."
        ),
    }


# --- hook merge (shared by setup.py install/uninstall) --------------------
def is_owned_hook_entry(entry: dict) -> bool:
    command = str((entry or {}).get("command") or "")
    args = (entry or {}).get("args") or []
    if not isinstance(args, list):
        args = []
    invocation = " ".join([command, *(str(arg) for arg in args)])
    return "agent-switchboard" in invocation and "routing-hook" in invocation


def merge_hook_entry(existing_hooks_for_event: list, our_command: str) -> list:
    """Return a new hooks-for-event list with exactly one owned entry (ours),
    all pre-existing non-owned entries preserved verbatim and in order."""
    kept = [h for h in (existing_hooks_for_event or []) if not is_owned_hook_entry(h)]
    kept.append({"type": "command", "command": our_command})
    return kept


def remove_owned_hook_entries(existing_hooks_for_event: list) -> list:
    return [h for h in (existing_hooks_for_event or []) if not is_owned_hook_entry(h)]


# --- CLI entry point --------------------------------------------------------
def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write(json.dumps({}))
        return 0
    event = argv[0]
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        payload = {}
    try:
        if len(argv) > 2 and str(argv[2]).strip().lower() in {"codex", "claude"}:
            payload["_switchboard_host"] = str(argv[2]).strip().lower()
        if event == "UserPromptSubmit":
            result = user_prompt_submit(payload)
        elif event == "PreToolUse":
            result = pre_tool_use(payload)
        elif event == "SubagentStart":
            result = subagent_start(payload)
        elif event == "SubagentStop":
            result = subagent_stop(payload)
        elif event == "PostToolUse":
            result = post_tool_use(payload)
        elif event == "Stop":
            result = stop(payload)
        else:
            result = {}
    except Exception:  # noqa: BLE001 - fail open, always emit valid JSON
        result = {}
    sys.stdout.write(json.dumps(result))
    return 0
