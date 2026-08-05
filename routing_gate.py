"""Routing gate for native-first labour and opposite-vendor consultations.

Dependency-free (stdlib only). Invoked by the host (Codex/Claude Code) as a
command hook: JSON on stdin, JSON on stdout. Both hosts use the same
`hooks.<Event>[].hooks[]` structural shape and the same Stop decision shape
(`{"decision": "block", "reason": "..."}` to block once, `{}` to allow).
PostToolUse also quarantines oversized MCP responses before they enter brain
context. Codex replaces the result with block feedback; Claude uses its
host-specific ``updatedToolOutput`` response.

Design constraints (hard, repeat-checked):
- Fail-open: any doubt (unreachable ledger, re-entrant stop, missing session id)
  allows rather than blocks.
- Loop-bounded: a session is blocked at most once; the second Stop call for the
  same unresolved session always allows.
- Never parses arbitrary transcript files -- only scans the `last_assistant_message`
  string the host hook payload already provides, via narrow regexes.
- Native receipts are host-attested from SubagentStart/SubagentStop events. Broker
  receipts remain ledger-attested; neither trusts an agent's prose identity.
"""

from __future__ import annotations

import hashlib
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
EVIDENCE_DIR = BROKER_HOME / "context-evidence"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


STATE_TTL_SECONDS = 24 * 60 * 60
NATIVE_CHECKPOINT_MUTATIONS = 10
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


def _session_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]
    return STATE_DIR / f"{safe}.json"


def _session_lock_path(session_id: str) -> Path:
    return _session_path(session_id).with_suffix(".lock")


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
    """
    if not session_id:
        return None
    try:
        with atomic_io.FileLock(
            _session_lock_path(session_id), timeout=1.0, stale_seconds=30.0
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
    _update_state(session_id, lambda state: state.clear())


def mark_mutated(session_id: str) -> dict | None:
    def update(state: dict) -> None:
        state["mutated"] = True
        state["mutation_count"] = int(state.get("mutation_count") or 0) + 1

    return _update_state(session_id, update)


def has_mutation(session_id: str) -> bool:
    return bool(_read_state(session_id).get("mutated"))


def already_blocked(session_id: str) -> bool:
    return bool(_read_state(session_id).get("blocked_once"))


def mark_blocked(session_id: str) -> None:
    _update_state(session_id, lambda state: state.__setitem__("blocked_once", True))


def _normalized_agent_type(value: object) -> str:
    return str(value or "").strip().lower()


def _cheap_native_agent_active_or_completed(state: dict) -> bool:
    agents = state.get("native_agents") or {}
    return any(
        isinstance(item, dict)
        and _normalized_agent_type(item.get("agent_type")) in NATIVE_CHEAP_AGENT_TYPES
        and item.get("status") in {"started", "completed"}
        for item in agents.values()
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

    _update_state(session_id, update)
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


def _serialized_tool_response(response: object) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))


def _is_mcp_tool(tool_name: object) -> bool:
    return str(tool_name or "").strip().lower().startswith("mcp__")


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
    ingress_feedback = _context_ingress_feedback(payload)
    if ingress_feedback is not None:
        return ingress_feedback
    session_id = str(payload.get("session_id") or "").strip()
    if session_id and _is_mutating(payload.get("tool_name"), payload.get("tool_input") or {}):
        state = mark_mutated(session_id)
        if (
            state
            and not payload.get("agent_id")
            and int(state.get("mutation_count") or 0) == NATIVE_CHECKPOINT_MUTATIONS
            and not _cheap_native_agent_active_or_completed(state)
        ):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        "Native-first checkpoint: this turn reached 10 mutating operations "
                        "without a completed managed native explorer/worker. Before the next "
                        "work package, delegate bounded same-vendor labour natively or record "
                        "a package-specific brain override with the exact collision or risk."
                    ),
                }
            }
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
    return True


def stop(payload: dict) -> dict:
    sweep_stale()
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id or not has_mutation(session_id):
        return {}
    if payload.get("stop_hook_active"):
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
            "each nonzero category on a package row as `direct=reads,searches,...`. "
            "Bare/global overrides are invalid; then stop again."
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
        if len(argv) > 2 and str(argv[2]).strip().lower() in {"codex", "claude"}:
            payload["_switchboard_host"] = str(argv[2]).strip().lower()
        if event == "UserPromptSubmit":
            result = user_prompt_submit(payload)
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
