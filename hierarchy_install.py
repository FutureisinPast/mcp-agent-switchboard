"""Installer-managed cross-vendor brain/worker hierarchy.

This module owns only marked instruction blocks, dedicated role files, and
owned hook entries. It never writes a main-thread model or effort setting.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import atomic_io
import routing_gate

BackupFn = Callable[[Path], None]

BLOCK_START_RE = re.compile(
    r"<!-- agent-switchboard:cost-routing:begin sha256=([0-9a-f]{64}) -->"
)
BLOCK_END = "<!-- agent-switchboard:cost-routing:end -->"
LEGACY_HEADING_RE = re.compile(r"(?im)^(#{1,2})\s+cost-aware model routing\s*$")
MANAGED_FILE_RE = re.compile(r"(?m)^# agent-switchboard:managed sha256=([0-9a-f]{64})\s*$")


@dataclass(frozen=True)
class HierarchyPaths:
    home: Path
    broker_home: Path

    @property
    def codex_agents_md(self) -> Path:
        return self.home / ".codex" / "AGENTS.md"

    @property
    def claude_md(self) -> Path:
        return self.home / ".claude" / "CLAUDE.md"

    @property
    def codex_explorer(self) -> Path:
        return self.home / ".codex" / "agents" / "explorer.toml"

    @property
    def codex_worker(self) -> Path:
        return self.home / ".codex" / "agents" / "worker.toml"

    @property
    def claude_explore(self) -> Path:
        return self.home / ".claude" / "agents" / "Explore.md"

    @property
    def claude_worker(self) -> Path:
        return self.home / ".claude" / "agents" / "economy-worker.md"

    @property
    def codex_hooks(self) -> Path:
        return self.home / ".codex" / "hooks.json"

    @property
    def claude_settings(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def lock(self) -> Path:
        return self.broker_home / "hierarchy-install.lock"


def _canonical(text: str) -> str:
    return text.strip() + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_block(body: str) -> str:
    canonical = _canonical(body)
    return (
        f"<!-- agent-switchboard:cost-routing:begin sha256={_sha(canonical)} -->\n"
        f"{canonical}{BLOCK_END}"
    )


def _block_parts(text: str) -> tuple[re.Match[str], int, str] | None:
    start = BLOCK_START_RE.search(text)
    if not start:
        return None
    body_start = start.end()
    if text[body_start : body_start + 1] == "\n":
        body_start += 1
    end = text.find(BLOCK_END, body_start)
    if end < 0:
        raise ValueError("managed routing block has no end marker")
    return start, end, text[body_start:end]


def _block_checksum_valid(text: str) -> bool:
    parts = _block_parts(text)
    if not parts:
        return False
    start, _end, body = parts
    return start.group(1) == _sha(body)


def _legacy_section_end(text: str, match: re.Match[str]) -> int:
    level = len(match.group(1))
    next_heading = re.compile(rf"(?m)^#{{1,{level}}}\s+").search(text, match.end())
    return next_heading.start() if next_heading else len(text)


def update_instruction_block(path: Path, body: str, backup: BackupFn, dry: bool = False) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    rendered = _render_block(body)
    try:
        parts = _block_parts(existing)
    except ValueError as exc:
        return f"ERROR: {exc}; left untouched"
    if parts:
        if not _block_checksum_valid(existing):
            return "ERROR: managed routing block was edited; left untouched"
        start, end, _old_body = parts
        updated = existing[: start.start()] + rendered + existing[end + len(BLOCK_END) :]
    else:
        legacy = LEGACY_HEADING_RE.search(existing)
        if legacy:
            section_end = _legacy_section_end(existing, legacy)
            tail = existing[section_end:]
            separator = "\n\n" if tail else "\n"
            updated = existing[: legacy.start()] + rendered + separator + tail
        else:
            separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
            updated = existing + separator + rendered + "\n"
    if updated == existing:
        return "unchanged"
    if dry:
        return f"would update {path}"
    if path.exists():
        backup(path)
    atomic_io.atomic_write_text(path, updated)
    return "updated"


def remove_instruction_block(path: Path, backup: BackupFn, dry: bool = False) -> str:
    if not path.exists():
        return "nothing to remove"
    existing = path.read_text(encoding="utf-8")
    try:
        parts = _block_parts(existing)
    except ValueError as exc:
        return f"ERROR: {exc}; left untouched"
    if not parts:
        return "nothing to remove"
    if not _block_checksum_valid(existing):
        return "ERROR: managed routing block was edited; left untouched"
    start, end, _body = parts
    head = existing[: start.start()].rstrip()
    tail = existing[end + len(BLOCK_END) :].lstrip("\n")
    updated = head + (("\n\n" + tail) if head and tail else (tail or ("\n" if head else "")))
    if dry:
        return f"would remove managed block from {path}"
    backup(path)
    atomic_io.atomic_write_text(path, updated)
    return "removed"


def _render_managed_file(body: str, markdown: bool) -> str:
    canonical = _canonical(body)
    marker = f"# agent-switchboard:managed sha256={_sha(canonical)}"
    if markdown:
        if not canonical.startswith("---\n"):
            raise ValueError("managed Claude agent must begin with YAML frontmatter")
        return "---\n" + marker + "\n" + canonical[4:]
    return marker + "\n" + canonical


def _managed_file_body(text: str) -> tuple[str, str] | None:
    marker = MANAGED_FILE_RE.search(text)
    if not marker:
        return None
    start, end = marker.span()
    if text[end : end + 1] == "\n":
        end += 1
    body = text[:start] + text[end:]
    return marker.group(1), body


def _managed_file_valid(text: str) -> bool:
    parts = _managed_file_body(text)
    return bool(parts and parts[0] == _sha(parts[1]))


def write_managed_file(
    path: Path,
    body: str,
    markdown: bool,
    legacy_owned: Callable[[str], bool],
    backup: BackupFn,
    dry: bool = False,
) -> str:
    rendered = _render_managed_file(body, markdown)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing == rendered:
        return "unchanged"
    if existing:
        if _managed_file_body(existing):
            if not _managed_file_valid(existing):
                return f"ERROR: managed role file was edited: {path}; left untouched"
        elif not legacy_owned(existing):
            return f"ERROR: existing role file is user-owned: {path}; left untouched"
    if dry:
        return f"would write {path}"
    if path.exists():
        backup(path)
    atomic_io.atomic_write_text(path, rendered)
    return "updated"


def remove_managed_file(path: Path, backup: BackupFn, dry: bool = False) -> str:
    if not path.exists():
        return "nothing to remove"
    existing = path.read_text(encoding="utf-8")
    if not _managed_file_body(existing):
        return "skipped (not managed by Agent Switchboard)"
    if not _managed_file_valid(existing):
        return "ERROR: managed role file was edited; left untouched"
    if dry:
        return f"would remove {path}"
    backup(path)
    path.unlink()
    return "removed"


def routing_rules_body(codex_roles: dict, claude_roles: dict) -> str:
    def role_id(name: str, fallback: str) -> str:
        value = codex_roles.get(name)
        return str((value or {}).get("id") or fallback)

    frontier = role_id("frontier", "current Codex frontier")
    workhorse = role_id("workhorse", "current Codex workhorse")
    reader = role_id("reader", "current Codex reader")
    claude_chain = " -> ".join(claude_roles.get("frontier") or ["fable", "opus"])
    return f"""## Cost-aware model hierarchy

- The model selected for the main session is the brain. Never rewrite that user choice. The brain owns requirements, architecture, planning, decomposition, hard diagnosis, high-risk decisions, and final sign-off.
- Native-first routing order is mandatory. For same-vendor bounded labour, use native subagents first: a Codex brain uses the managed `explorer` (`{reader}`/low) and `worker` (`{workhorse}`/medium); a Claude brain uses managed `Explore` (`{claude_roles.get('reader') or 'haiku'}`) and `economy-worker` (`{claude_roles.get('workhorse') or 'sonnet'}`/medium). Do not use Agent Switchboard to launch same-vendor labour unless the named native role is unavailable or fails to start, and record that fallback.
- For non-trivial planning or a hard issue, the brain must obtain one opposite-vendor maximum-effort consultation: a Codex brain uses Claude `{claude_chain}` with runtime attestation; a Claude brain uses the live Codex frontier `{frontier}` at the highest available single-agent effort. On explicit availability/entitlement failure, use the next advertised frontier candidate and report the fallback.
- Delegate when handoff is cheaper than direct work and verification is cheap: bulk reading/search/extraction/formatting to the native reader; routine writing, light implementation, tests, scripts, and reversible deployment steps from an approved plan to the native workhorse.
- Plans are portable across vendors. Every package states `Lane | mechanism | exact resolved model/effort | deliverable | verification | escalation`, where Lane is semantic (`brain`, `reader`, or `workhorse`). At execution start, resolve the semantic lane to the executing brain's current same-vendor native role and record the exact model/effort. Never follow an imported foreign-vendor labour model literally; re-resolve it for the current executor.
- Keep ambiguous architecture, security/auth/payment/data-loss/migration work, irreversible actions, and approval with the brain. Workers stop on ambiguity, plan deviation, high-risk scope, or a failed fix; the brain diagnoses before redelegating a deterministic remainder.
- A dirty worktree, same-session ownership, or deployment authority is not a blanket reason to keep reading, test execution, evidence gathering, documentation, or isolated mechanical edits on the brain. Retain only the specific overlapping write or high-risk state transition.
- Brain overrides are package-specific and use exactly `override: brain - <WP-ID>: <specific reason>`. Bare/global overrides are invalid. After ten mutating operations without a completed native cheap-role agent, stop at the next package boundary and re-evaluate delegation.
- Brain-context ingress is capped by default at roughly 1-2k tokens (8,000 characters). Before a verification response enters brain context, request an explicit field projection and output cap. Oversized MCP evidence is quarantined outside context with its query and location; do not pull the whole artifact back into context.
- A claim is a decision premise when it being false would change the patch, risk classification, or release decision. The reader locates it; the brain adjudicates only the minimum primary evidence. Every brain-retained premise read states `premise | what changes if false | bounded primary evidence` before inspection. "Needs judgment" never justifies broad rereading.
- Readers return file:line evidence and distinguish observed facts from interpretation. The brain reviews actual diffs and verification output. Reads may run in parallel; writes are serial unless files are demonstrably independent.
- Do not claim implementation complete without a `Routing audit` mapping every planned and unplanned package to its lane, mechanism, resolved model/effort, verification, and one receipt: `native:<agent-id>` for a host-attested completed managed subagent, `broker:<uuid>` for an Agent Switchboard call, or the structured per-package brain override. The audit must include `direct-brain-labour: reads=N | searches=N | evidence=N | tests=N | docs=N | other=N`; every nonzero category must appear in a package row as `direct=reads,searches,...`. Native lifecycle attests agent id/type/completion; its checksum-protected role file attests configured model/effort unless the runtime exposes stronger attestation. Never treat prose self-identification as proof; label unavailable runtime model attestation unverified.
"""


def role_file_bodies(codex_roles: dict, claude_roles: dict) -> dict[str, str]:
    reader = str((codex_roles.get("reader") or {}).get("id") or "").strip()
    workhorse = str((codex_roles.get("workhorse") or {}).get("id") or "").strip()
    return {
        "codex_explorer": f'''name = "explorer"
description = "Cost-efficient read-only exploration. Use proactively for search, bulk reading, extraction, inventories, and evidence gathering before the brain decides."
model = "{reader}"
model_reasoning_effort = "low"
sandbox_mode = "read-only"
developer_instructions = """
You are the same-vendor native reader. Never route this package through Agent Switchboard. Read and search only the assigned scope. Return concise findings with exact file:line evidence and separate observed fact from interpretation. For a decision premise, locate the minimal primary evidence and state uncertainty; never adjudicate it. Do not make architecture, risk, or approval decisions. Stop on ambiguity, high-risk scope, or a broader handoff.
"""
''' if reader else "",
        "codex_worker": f'''name = "worker"
description = "Cost-efficient worker for bounded writing, implementation, tests, scripts, and reversible deployment steps after an approved plan."
model = "{workhorse}"
model_reasoning_effort = "medium"
developer_instructions = """
You are the same-vendor native workhorse. Never route this package through Agent Switchboard. Require a work package with Lane, mechanism, exact resolved model/effort, deliverable, verification, and escalation. Implement only that package. Stop on ambiguity, plan deviation, high-risk scope, or the first failed fix and return evidence to the brain.
"""
''' if workhorse else "",
        "claude_explore": f'''---
name: Explore
description: Cost-efficient read-only exploration. Use proactively for search, bulk reading, extraction, inventories, and evidence gathering before the brain decides.
tools: Read, Grep, Glob
model: {claude_roles.get('reader') or 'haiku'}
---

You are the same-vendor native reader. Never route this package through Agent Switchboard. Read and search only the assigned scope. Return concise findings with exact file:line evidence and separate observed fact from interpretation. For a decision premise, locate the minimal primary evidence and state uncertainty; never adjudicate it. Do not make architecture, risk, or approval decisions. Stop on ambiguity, high-risk scope, or a broader handoff.
''',
        "claude_worker": f'''---
name: economy-worker
description: Use proactively for bounded writing, implementation, tests, scripts, and reversible deployment steps after the brain supplies an approved plan and acceptance criteria.
model: {claude_roles.get('workhorse') or 'sonnet'}
effort: medium
---

You are the same-vendor native workhorse. Never route this package through Agent Switchboard. Require a work package with Lane, mechanism, exact resolved model/effort, deliverable, verification, and escalation. Implement only that package. Stop on ambiguity, plan deviation, high-risk scope, or the first failed fix and return evidence to the brain.
''',
    }


def _legacy_codex_role(name: str) -> Callable[[str], bool]:
    return lambda text: f'name = "{name}"' in text and "Cost-efficient" in text


def _legacy_claude_role(name: str) -> Callable[[str], bool]:
    # v1.0.25's economy-worker description did not include the literal
    # "Cost-efficient" phrase, but it did carry this distinctive routing
    # contract. Recognize both installer-owned legacy forms without treating an
    # arbitrary same-named user agent as ours.
    return lambda text: f"name: {name}" in text and (
        "Cost-efficient" in text
        or "Require an approved work package stating Route, exact model/effort" in text
    )


def _merge_hook_event(data: dict, event: str, command: str, matcher: str | None) -> None:
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"hooks.{event} must be a JSON array")
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError(f"hooks.{event} contains a non-object entry")
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            raise ValueError(f"hooks.{event}.hooks must be a JSON array")
        filtered = routing_gate.remove_owned_hook_entries(handlers)
        if filtered or not any(routing_gate.is_owned_hook_entry(item) for item in handlers):
            new_group = copy.deepcopy(group)
            new_group["hooks"] = filtered
            kept.append(new_group)
    owned_group = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        owned_group["matcher"] = matcher
    kept.append(owned_group)
    hooks[event] = kept


def update_hooks(
    path: Path,
    command_prefix: str,
    host: str,
    backup: BackupFn,
    dry: bool = False,
) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        data = json.loads(existing) if existing else {}
        if not isinstance(data, dict):
            raise ValueError("top-level JSON must be an object")
        _merge_hook_event(data, "UserPromptSubmit", f"{command_prefix} UserPromptSubmit agent-switchboard {host}", None)
        _merge_hook_event(data, "SubagentStart", f"{command_prefix} SubagentStart agent-switchboard {host}", None)
        _merge_hook_event(data, "SubagentStop", f"{command_prefix} SubagentStop agent-switchboard {host}", None)
        _merge_hook_event(
            data,
            "PostToolUse",
            f"{command_prefix} PostToolUse agent-switchboard {host}",
            "Bash|Edit|Write|MultiEdit|NotebookEdit|apply_patch|mcp__.*",
        )
        _merge_hook_event(data, "Stop", f"{command_prefix} Stop agent-switchboard {host}", None)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {path.name} is not safely mergeable ({exc}); left untouched"
    rendered = json.dumps(data, indent=2) + "\n"
    if rendered == existing:
        return "unchanged"
    if dry:
        return f"would merge owned routing hooks into {path}"
    if path.exists():
        backup(path)
    atomic_io.atomic_write_text(path, rendered)
    return "updated"


def remove_hooks(path: Path, backup: BackupFn, dry: bool = False) -> str:
    if not path.exists():
        return "nothing to remove"
    existing = path.read_text(encoding="utf-8")
    try:
        data = json.loads(existing)
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks must be a JSON object")
        changed = False
        for event, groups in list(hooks.items()):
            if not isinstance(groups, list):
                continue
            kept = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                    kept.append(group)
                    continue
                handlers = group.get("hooks", [])
                filtered = routing_gate.remove_owned_hook_entries(handlers)
                if filtered != handlers:
                    changed = True
                # Keep non-empty user handlers, or untouched groups that never
                # contained one of ours. Drop owned-only groups completely.
                if filtered or filtered == handlers:
                    new_group = copy.deepcopy(group)
                    new_group["hooks"] = filtered
                    kept.append(new_group)
            if kept:
                hooks[event] = kept
            elif event in hooks:
                hooks.pop(event)
        if not changed:
            return "nothing to remove"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {path.name} is not safely mergeable ({exc}); left untouched"
    if dry:
        return f"would remove owned routing hooks from {path}"
    backup(path)
    atomic_io.atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return "removed"


def refresh(
    paths: HierarchyPaths,
    codex_roles: dict,
    claude_roles: dict,
    hook_command_prefix: str,
    backup: BackupFn,
    dry: bool = False,
) -> dict[str, str]:
    try:
        with atomic_io.FileLock(paths.lock):
            body = routing_rules_body(codex_roles, claude_roles)
            role_bodies = role_file_bodies(codex_roles, claude_roles)
            def write_codex_role(path: Path, body: str, role_name: str) -> str:
                if not body:
                    if not path.exists():
                        return "skipped (live role unavailable; no stale model installed)"
                    existing = path.read_text(encoding="utf-8")
                    if _managed_file_body(existing):
                        if _managed_file_valid(existing):
                            return "unchanged (live role unavailable; kept last-known managed role)"
                        return "ERROR: managed role file was edited; live role unavailable; left untouched"
                    return "skipped (live role unavailable; existing role is user-owned)"
                return write_managed_file(
                    path, body, False, _legacy_codex_role(role_name), backup, dry
                )

            return {
                "Codex global hierarchy": update_instruction_block(paths.codex_agents_md, body, backup, dry),
                "Claude global hierarchy": update_instruction_block(paths.claude_md, body, backup, dry),
                "Codex explorer role": write_codex_role(paths.codex_explorer, role_bodies["codex_explorer"], "explorer"),
                "Codex worker role": write_codex_role(paths.codex_worker, role_bodies["codex_worker"], "worker"),
                "Claude Explore role": write_managed_file(paths.claude_explore, role_bodies["claude_explore"], True, _legacy_claude_role("Explore"), backup, dry),
                "Claude worker role": write_managed_file(paths.claude_worker, role_bodies["claude_worker"], True, _legacy_claude_role("economy-worker"), backup, dry),
                "Codex routing hooks": update_hooks(paths.codex_hooks, hook_command_prefix, "codex", backup, dry),
                "Claude routing hooks": update_hooks(paths.claude_settings, hook_command_prefix, "claude", backup, dry),
            }
    except TimeoutError as exc:
        return {"Hierarchy": f"ERROR: {exc}; left untouched"}


def uninstall(paths: HierarchyPaths, backup: BackupFn, dry: bool = False) -> dict[str, str]:
    try:
        with atomic_io.FileLock(paths.lock):
            return {
                "Codex global hierarchy": remove_instruction_block(paths.codex_agents_md, backup, dry),
                "Claude global hierarchy": remove_instruction_block(paths.claude_md, backup, dry),
                "Codex explorer role": remove_managed_file(paths.codex_explorer, backup, dry),
                "Codex worker role": remove_managed_file(paths.codex_worker, backup, dry),
                "Claude Explore role": remove_managed_file(paths.claude_explore, backup, dry),
                "Claude worker role": remove_managed_file(paths.claude_worker, backup, dry),
                "Codex routing hooks": remove_hooks(paths.codex_hooks, backup, dry),
                "Claude routing hooks": remove_hooks(paths.claude_settings, backup, dry),
            }
    except TimeoutError as exc:
        return {"Hierarchy": f"ERROR: {exc}; left untouched"}
