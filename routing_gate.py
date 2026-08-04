"""Routing gate: UserPromptSubmit reset + PostToolUse mutation tracking + Stop hook gate.

Dependency-free (stdlib only). Invoked by the host (Codex/Claude Code) as a
command hook: JSON on stdin, JSON on stdout. Both hosts use the same
`hooks.<Event>[].hooks[]` structural shape and the same Stop decision shape
(`{"decision": "block", "reason": "..."}` to block once, `{}` to allow).
PostToolUse always emits `{}` (it cannot block, only observe).

Design constraints (hard, repeat-checked):
- Fail-open: any doubt (unreachable ledger, re-entrant stop, missing session id)
  allows rather than blocks.
- Loop-bounded: a session is blocked at most once; the second Stop call for the
  same unresolved session always allows.
- Never parses arbitrary transcript files -- only scans the `last_assistant_message`
  string the host hook payload already provides, via a narrow regex.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import atomic_io

BROKER_HOME = Path(os.environ.get("AGENT_BROKER_HOME", Path.home() / ".agent-broker"))
STATE_DIR = BROKER_HOME / "routing-gate"
DB_PATH = BROKER_HOME / "state.sqlite"

STATE_TTL_SECONDS = 24 * 60 * 60

MUTATING_TOOL_NAMES = {
    "edit", "write", "multiedit", "notebookedit", "apply_patch", "applypatch", "patch",
}
SHELL_TOOL_NAMES = {"bash", "shell", "exec", "run_command", "localshell", "terminal", "powershell"}

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

OVERRIDE_LINE_RE = re.compile(r"^override:\s*brain\s*-\s*\S.*$", re.MULTILINE)
ROUTING_AUDIT_SECTION_RE = re.compile(
    r"^#{1,6}\s+routing audit\b[^\n]*\n(.*?)(?=^#{1,6}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
VALID_RESPONDER_PREFIXES = ("codex:", "claude:", "antigravity:")


def _session_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]
    return STATE_DIR / f"{safe}.json"


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


def reset_turn_state(session_id: str) -> None:
    """Called on UserPromptSubmit: clear any mutation/blocked flags left over
    from a prior turn so they never leak into the new turn's Stop decision."""
    if not session_id:
        return
    _write_state(session_id, {})


def mark_mutated(session_id: str) -> None:
    state = _read_state(session_id)
    state["mutated"] = True
    state["updated_at"] = time.time()
    _write_state(session_id, state)


def has_mutation(session_id: str) -> bool:
    return bool(_read_state(session_id).get("mutated"))


def already_blocked(session_id: str) -> bool:
    return bool(_read_state(session_id).get("blocked_once"))


def mark_blocked(session_id: str) -> None:
    state = _read_state(session_id)
    state["blocked_once"] = True
    state["updated_at"] = time.time()
    _write_state(session_id, state)


def sweep_stale(max_age_seconds: float = STATE_TTL_SECONDS) -> None:
    if not STATE_DIR.exists():
        return
    cutoff = time.time() - max_age_seconds
    try:
        entries = list(STATE_DIR.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _is_mutating(tool_name: str, tool_input: dict) -> bool:
    name = str(tool_name or "").strip().lower()
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


def user_prompt_submit(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    reset_turn_state(session_id)
    return {}


def post_tool_use(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    if session_id and _is_mutating(payload.get("tool_name"), payload.get("tool_input") or {}):
        mark_mutated(session_id)
    return {}


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


def _receipt_valid(request_id: str) -> bool | None:
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
    responder_model = str(result.get("responder_model") or "").strip().lower()
    if not responder_model.startswith(VALID_RESPONDER_PREFIXES):
        return False
    if "unverified" in responder_model:
        return False
    return True


def _lookup_routing_audit_valid(last_message: str) -> bool | None:
    """Find every UUID listed under a `Routing audit` heading in the
    transcript-provided last message (regex only -- never opens transcript
    files), then validate each one against the broker's own ledger via the
    existing request_status() surface (not the transcript text). All UUIDs
    found under the heading must resolve to an answered, verified receipt."""
    section = ROUTING_AUDIT_SECTION_RE.search(last_message or "")
    if not section:
        return False
    request_ids = UUID_RE.findall(section.group(1))
    if not request_ids:
        return False
    results = [_receipt_valid(rid) for rid in request_ids]
    if any(result is None for result in results):
        return None
    return all(results)


def stop(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id or not has_mutation(session_id):
        return {}
    if payload.get("stop_hook_active"):
        return {}
    last_message = str(payload.get("last_assistant_message") or "")
    if OVERRIDE_LINE_RE.search(last_message):
        return {}
    if not _ledger_reachable():
        return {}
    audit_valid = _lookup_routing_audit_valid(last_message)
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
            "Include a 'Routing audit' section with the broker request UUID that answered the "
            "required consult (or an 'override: brain - <reason>' line), then stop again."
        ),
    }


# --- hook merge (shared by setup.py install/uninstall) --------------------
def is_owned_hook_entry(entry: dict) -> bool:
    command = str((entry or {}).get("command") or "")
    return "agent-switchboard" in command and "routing-hook" in command


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
        if event == "UserPromptSubmit":
            result = user_prompt_submit(payload)
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
