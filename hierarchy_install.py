"""Installer-managed cross-vendor brain/worker hierarchy.

This module owns only marked instruction blocks, dedicated role files, and
owned hook entries. It never writes a main-thread model or effort setting.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
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

# sha256 checksums of `routing_rules_body()` output that this installer has
# shipped in an earlier release, recorded so an in-place upgrade recognizes
# its own prior block as legacy-replaceable rather than "user edited". The
# marker's self-consistency check (see `_block_checksum_valid`) already lets
# any installer-written block -- old policy text or new -- be replaced,
# because the installer always writes a checksum that matches the body
# beneath it regardless of what that body says. This set is the defensive
# fallback for the case where an older installer release computed the marker
# checksum a different way (e.g. a canonicalization change) so
# self-consistency no longer holds for a block it wrote. A block whose
# checksum matches neither check is a genuine user edit and stays refused.
# Never hand-edit a checksum line to add yourself to this set; only record a
# checksum actually produced by a prior `routing_rules_body()` release.
KNOWN_HIERARCHY_BODY_CHECKSUMS = frozenset(
    {
        # v1.0.33 routing_rules_body(), rendered against the installer's test
        # role fixture (CODEX_ROLES/CLAUDE_ROLES in
        # tests/test_hierarchy_install.py), captured 2026-08-15 by calling
        # the pre-WP8 routing_rules_body() before its rewrite.
        "d3612ab90cd701c91ab254b0ea52841ba04e314a435992c829ed19207b737184",
    }
)


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
    def gemini_md(self) -> Path:
        return self.home / ".gemini" / "GEMINI.md"

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


def update_instruction_block(
    path: Path,
    body: str,
    backup: BackupFn,
    dry: bool = False,
    replace_legacy: Callable[[str], bool] | None = None,
    reject_legacy_mismatch: Callable[[str], bool] | None = None,
) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    rendered = _render_block(body)
    try:
        parts = _block_parts(existing)
    except ValueError as exc:
        return f"ERROR: {exc}; left untouched"
    if parts:
        start, end, _old_body = parts
        marker_checksum = start.group(1)
        if (
            not _block_checksum_valid(existing)
            and marker_checksum not in KNOWN_HIERARCHY_BODY_CHECKSUMS
        ):
            return "ERROR: managed routing block was edited; left untouched"
        updated = existing[: start.start()] + rendered + existing[end + len(BLOCK_END) :]
    elif replace_legacy and replace_legacy(existing):
        updated = rendered + "\n"
    elif reject_legacy_mismatch and reject_legacy_mismatch(existing):
        return "ERROR: possible legacy instruction file did not match the known migration; left untouched"
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
- The owner has issued a STANDING REQUEST to delegate eligible labour: dispatching a bounded package to the Flash workhorse or to a managed native subagent is pre-authorized work, not an optional extra that needs fresh permission each turn.
- DEFAULT WORKHORSE = the newest live Antigravity Gemini Flash High through Agent Switchboard. For a bounded package -- reading, search, extraction, summaries, drafting, independent parallel read-only packages, and (once containment is enabled) light implementation and tests from an approved plan -- the default lane is `route_agent_task` with `target_agent="antigravity"`, `surface="cli"`, `target_model="gemini flash"`, `effort="high"`, a `work_package_id`, the correct `task_kind`, and `mode="plan"` or `mode="accept-edits"` plus the implementation envelope. It is roughly a tenth the cost of the same-vendor native workhorse and several times faster.
- Codex, Claude, and Gemini brains use the newest live Antigravity Gemini Flash High through Agent Switchboard as the default fast, cheap external workhorse for bounded search, reading, extraction, summaries, drafting, independent parallel read-only packages, and (once containment is enabled) low-risk implementation/tests from an approved plan.
- Flash is the default with OBJECTIVE EXCEPTIONS, not an unconditional rule. A Flash-eligible package must end in exactly one of: a Switchboard dispatch, the small direct allowance for non-mutating micro-work, or a native/brain lane carrying a stated `flash_skip` reason with evidence -- `host-tools:<tool-id>` (the package needs host-only MCP tools, skills, or an IDE session), `unshared-state:<evidence-id>` (it depends on session state Flash cannot see), `flash-failed:<broker-receipt>`, `flash-unavailable:<health-id>`, or `atomic-oversize:<plan-id>` (an indivisible package over five files or past the input preflight). "It felt easier to do myself" is not one of them.
- Native cheap roles: a Codex brain uses the managed `explorer` (`{reader}`/low) and `worker` (`{workhorse}`/medium); a Claude brain uses managed `Explore` (`{claude_roles.get('reader') or 'haiku'}`) and `economy-worker` (`{claude_roles.get('workhorse') or 'sonnet'}`/medium). They remain the correct lane only for the stated exceptions above and for anything needing the host's own tools -- the fallback, not the first choice; record the fallback when one is used. The external Antigravity Flash lane is the default workhorse in the table below, not a native child agent, and never a frontier consultant: a higher Gemini version number does not promote it above Sol/Fable.
- For non-trivial planning or a hard issue, the brain must obtain one opposite-vendor maximum-effort consultation: a Codex brain uses Claude `{claude_chain}` with runtime attestation; a Claude brain uses the live Codex frontier `{frontier}` at the highest available single-agent effort. On explicit availability/entitlement failure, use the next advertised frontier candidate and report the fallback.
- Capability tier outranks model version. Gemini Flash High is a useful, non-authoritative workhorse-level adviser; a higher version does not promote it above Sol/Fable or make its advice automatically authoritative. When Claude's `{claude_chain}` frontier chain is unavailable because of quota, reachability, entitlement, or another availability failure, a Codex brain should request a second opinion from the newest live Antigravity Flash High, label it degraded advisory fallback, and retain final judgment.
- Cross-vendor routing must enter through Agent Switchboard's MCP tools whenever Switchboard is registered. For Flash labour, the sender brain MUST call MCP `route_agent_task`; a request to use Flash "through CLI" means `surface="cli"` on that MCP call. The brain MUST NOT invoke `agy` in a shell or call `consult_antigravity` directly. Only the Switchboard backend may start `agy`; sender-side direct `agy` is prohibited.
- Every Flash call is exactly one bounded work package. Never hand Flash an entire autonomous plan or let it select/continue to another package. For implementation, the sender must provide `work_package_id`, 1-5 exact `allowed_files`, explicit `acceptance_criteria`, and any package-specific `forbidden_actions`; Switchboard rejects an incomplete envelope.
- A Switchboard-launched Gemini Flash session is the non-authoritative worker for exactly its assigned envelope, never the brain or router. It must not dispatch agents, reinterpret the whole plan, or continue to another package.
- Switchboard must invoke its internal `agy` backend with the mandatory `--output-format json --json-schema` contract. Missing/malformed fields, scope violations, contradictory completion, ambiguity, failed checks, or unsupported intentional/by-design claims are failures to escalate, not answers to accept.
- Flash never receives `danger-full-access`, production SSH, live credentials, destructive operations, migrations, or live deployment. It may prepare bounded local changes and checks; the brain owns live deployment and approval.
- If `agy` or Flash is missing, quota-limited, times out, mismatches the requested model, or otherwise fails, fall back to the host's native cheap roles (Codex `explorer`/`worker`; Claude `Explore`/`economy-worker`) and record the fallback.
- Flash and native workers may run concurrently only on independent stages/packages. Read-only packages may run in parallel; writes run serially unless their files and state transitions are demonstrably isolated. The brain reviews evidence and actual diffs. A Flash completion is never acceptance: before dispatching another package, the brain independently inspects cited primary lines, the actual diff, and check output. Unsupported claims that a defect is intentional/by design keep the investigation open.
- Delegate when handoff is cheaper than direct work and verification is cheap: bulk reading/search/extraction/formatting and routine writing/light implementation/tests/scripts/reversible deployment from an approved plan go to Flash first, with the native reader/workhorse as the stated `flash_skip` fallback.
- Plans are portable across vendors. Every package states `Lane | mechanism | exact resolved model/effort | deliverable | verification | escalation`, where Lane is semantic (`brain`, `reader`, or `workhorse`). At execution start, resolve the semantic lane to Flash or to the executing brain's current same-vendor native role and record the exact model/effort. Never follow an imported foreign-vendor labour model literally; re-resolve it for the current executor.
- Keep ambiguous architecture, security/auth/payment/data-loss/migration work, irreversible actions, and approval with the brain. Workers stop on ambiguity, plan deviation, high-risk scope, or a failed fix; the brain diagnoses before redelegating a deterministic remainder.
- A dirty worktree, same-session ownership, or deployment authority is not a blanket reason to keep reading, test execution, evidence gathering, documentation, or isolated mechanical edits on the brain. Retain only the specific overlapping write or high-risk state transition.
- Brain overrides are package-specific and use exactly `override: brain - <WP-ID>: <specific reason>`. Bare/global overrides are invalid. The allowance is four direct labour calls per bounded block for non-mutating micro-work (no writes, no broad shell inspection, no delegation-sized evidence); after that, the installed `PreToolUse` gate denies the next eligible read/search/evidence/test/documentation/mechanical call until relief arrives. Relief comes from a verified Switchboard dispatch receipt, a managed native reader/workhorse package start, or a registered brain override using the gate-provided local `routing-override` command -- a failed, blocked, rejected, or unavailable dispatch earns no relief. Each relief opens only the next bounded block; completed planning delegation never disables implementation enforcement. Registered overrides must appear with the same reason in the final audit.
- Brain-context ingress is capped by default at roughly 1-2k tokens (8,000 characters). Before a verification response enters brain context, request an explicit field projection and output cap. Oversized MCP evidence is quarantined outside context with its query and location; do not pull the whole artifact back into context.
- A claim is a decision premise when it being false would change the patch, risk classification, or release decision. The reader locates it; the brain adjudicates only the minimum primary evidence. Every brain-retained premise read states `premise | what changes if false | bounded primary evidence` before inspection. "Needs judgment" never justifies broad rereading.
- Readers return file:line evidence and distinguish observed facts from interpretation. The brain reviews actual diffs and verification output. Reads may run in parallel; writes are serial unless files are demonstrably independent.
- Background shell lifecycle is part of package completion: before claiming completion or returning, reconcile every Claude-managed background Bash/PowerShell/Monitor job started in that package by obtaining its terminal result or stopping it. Launching or detaching a job is never verification.
- Do not claim implementation complete without a `Routing audit` mapping every planned and unplanned package to its lane, mechanism, resolved model/effort, verification, and one receipt: `native:<agent-id>` for a host-attested completed managed subagent, `broker:<uuid>` for an Agent Switchboard call, or the structured per-package brain override. The audit must include `direct-brain-labour: reads=N | searches=N | evidence=N | tests=N | docs=N | other=N`; every nonzero category must appear in a package row as `direct=reads,searches,...`. Native lifecycle attests agent id/type/completion; its checksum-protected role file attests configured model/effort unless the runtime exposes stronger attestation. Never treat prose self-identification as proof; label unavailable runtime model attestation unverified.

### Lane table

| Work | Default lane | Mechanism | Fallback |
|---|---|---|---|
| Architecture, ambiguity, security/auth/payment/migration, irreversible ops, approval, hard diagnosis, opposite-vendor consult | brain | direct / `consult_codex`/`consult_claude` (Sol/Fable max) | -- |
| Bounded read/search/extract/summarize; drafting; independent parallel read-only packages | workhorse = Flash High | `route_agent_task target_agent=antigravity surface=cli target_model="gemini flash" effort=high mode=plan work_package_id=...` | native reader (Explore/Haiku . explorer/Luna) with `flash_skip` |
| Bounded implementation + tests from an approved plan (<=5 files) -- after containment | workhorse = Flash High | same + `mode=accept-edits package_root allowed_files acceptance_criteria [forbidden_actions verify_commands]` | native workhorse (economy-worker/Sonnet . worker/Terra) with `flash_skip` |
| Measurement/evidence (hashes, encoding, git state, process state) | broker probes | `run_evidence_probe` | Flash plan-mode -> economy-worker read-only envelope |
| Host-only tools (session MCP tools, skills, IDE), session-bound state, atomic >5 files, oversize input | native | Agent/spawn_agent cheap role + `flash_skip` evidence | brain override |
| Tiny non-mutating lookups (<= allowance per block) | brain-direct | Read/Grep/Glob | -- |
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
You are the same-vendor native reader. Never route this package through Agent Switchboard. Read and search only the assigned scope. Return no more than 8,000 characters of concise findings with exact file:line evidence and separate observed fact from interpretation; never dump raw logs or whole files. For a decision premise, locate the minimal primary evidence and state uncertainty; never adjudicate it. Do not make architecture, risk, or approval decisions. Stop on ambiguity, high-risk scope, or a broader handoff.
"""
''' if reader else "",
        "codex_worker": f'''name = "worker"
description = "Cost-efficient worker for bounded writing, implementation, tests, scripts, and reversible deployment steps after an approved plan."
model = "{workhorse}"
model_reasoning_effort = "medium"
developer_instructions = """
You are the same-vendor native workhorse. Never route this package through Agent Switchboard. Require a work package with Lane, mechanism, exact resolved model/effort, deliverable, verification, and escalation. Implement only that package. Return no more than 8,000 characters; keep large logs/artifacts outside the brain context and report only their location plus the bounded verification result. Stop on ambiguity, plan deviation, high-risk scope, or the first failed fix and return evidence to the brain.
"""
''' if workhorse else "",
        "claude_explore": f'''---
name: Explore
description: Cost-efficient read-only exploration. Use proactively for search, bulk reading, extraction, inventories, and evidence gathering before the brain decides.
tools: Read, Grep, Glob
model: {claude_roles.get('reader') or 'haiku'}
---

You are the same-vendor native reader. Never route this package through Agent Switchboard. Read and search only the assigned scope. Return no more than 8,000 characters of concise findings with exact file:line evidence and separate observed fact from interpretation; never dump raw logs or whole files. For a decision premise, locate the minimal primary evidence and state uncertainty; never adjudicate it. Do not make architecture, risk, or approval decisions. Stop on ambiguity, high-risk scope, or a broader handoff.
''',
        "claude_worker": f'''---
name: economy-worker
description: Use proactively for bounded writing, implementation, tests, scripts, and reversible deployment steps after the brain supplies an approved plan and acceptance criteria.
model: {claude_roles.get('workhorse') or 'sonnet'}
effort: medium
---

You are the same-vendor native workhorse. Never route this package through Agent Switchboard. Require a work package with Lane, mechanism, exact resolved model/effort, deliverable, verification, and escalation. Implement only that package. Return no more than 8,000 characters; keep large logs/artifacts outside the brain context and report only their location plus the bounded verification result. Before returning, reconcile every background Bash/PowerShell/Monitor job started in this package by obtaining its terminal result or stopping it; launching or detaching a job is never verification. Stop on ambiguity, plan deviation, high-risk scope, or the first failed fix and return evidence to the brain.
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


def _legacy_gemini_pine_persona(text: str) -> bool:
    """Recognize only the obsolete global persona previously shipped on this host."""
    fingerprints = (
        "Role: Production-grade Quant Dev for TradingView Pine Script v6",
        "SECTION 1: SCOPE & BOUNDARIES",
        "SECTION 7: OUTPUT REQUIREMENTS",
    )
    return all(item in text for item in fingerprints)


def _near_legacy_gemini_pine_persona(text: str) -> bool:
    fingerprints = (
        "Role: Production-grade Quant Dev for TradingView Pine Script v6",
        "SECTION 1: SCOPE & BOUNDARIES",
        "SECTION 7: OUTPUT REQUIREMENTS",
    )
    return not _legacy_gemini_pine_persona(text) and sum(
        item in text for item in fingerprints
    ) >= 2


def _merge_hook_event(data: dict, event: str, handler: dict, matcher: str | None) -> None:
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
    owned_group = {"hooks": [copy.deepcopy(handler)]}
    if matcher:
        owned_group["matcher"] = matcher
    kept.append(owned_group)
    hooks[event] = kept


def _hook_prefix_argv(command_prefix: str) -> list[str]:
    """Split the installer-generated command prefix without consuming backslashes."""
    parts = shlex.split(command_prefix, posix=False)
    argv = [
        part[1:-1]
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
        else part
        for part in parts
    ]
    if not argv:
        raise ValueError("routing hook command prefix is empty")
    return argv


def _hook_handler(command_prefix: str, event: str, host: str) -> dict:
    suffix = [event, "agent-switchboard", host]
    if host == "claude":
        # Claude Code executes a string command through its configured shell.
        # Exec-form hooks keep Windows paths out of bash parsing entirely.
        argv = _hook_prefix_argv(command_prefix)
        return {"type": "command", "command": argv[0], "args": argv[1:] + suffix}
    return {
        "type": "command",
        "command": f"{command_prefix} {' '.join(suffix)}",
    }


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
        _merge_hook_event(data, "UserPromptSubmit", _hook_handler(command_prefix, "UserPromptSubmit", host), None)
        _merge_hook_event(data, "SubagentStart", _hook_handler(command_prefix, "SubagentStart", host), None)
        _merge_hook_event(data, "SubagentStop", _hook_handler(command_prefix, "SubagentStop", host), None)
        # Intercept every tool call, not a hand-maintained name list: a static
        # matcher silently regresses whenever a host adds or renames a tool
        # (PowerShell -- this box's primary shell tool -- was missing from the
        # old list, so PowerShell calls bypassed labour counting, mutation
        # tracking, and the direct-`agy` hard deny). The classifier owns
        # per-tool categorization; the matcher's only job is to make sure it
        # sees everything.
        _merge_hook_event(
            data,
            "PreToolUse",
            _hook_handler(command_prefix, "PreToolUse", host),
            ".*",
        )
        _merge_hook_event(
            data,
            "PostToolUse",
            _hook_handler(command_prefix, "PostToolUse", host),
            ".*",
        )
        _merge_hook_event(data, "Stop", _hook_handler(command_prefix, "Stop", host), None)
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
                "Gemini global hierarchy": update_instruction_block(
                    paths.gemini_md,
                    body,
                    backup,
                    dry,
                    replace_legacy=_legacy_gemini_pine_persona,
                    reject_legacy_mismatch=_near_legacy_gemini_pine_persona,
                ),
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
                "Gemini global hierarchy": remove_instruction_block(paths.gemini_md, backup, dry),
                "Codex explorer role": remove_managed_file(paths.codex_explorer, backup, dry),
                "Codex worker role": remove_managed_file(paths.codex_worker, backup, dry),
                "Claude Explore role": remove_managed_file(paths.claude_explore, backup, dry),
                "Claude worker role": remove_managed_file(paths.claude_worker, backup, dry),
                "Codex routing hooks": remove_hooks(paths.codex_hooks, backup, dry),
                "Claude routing hooks": remove_hooks(paths.claude_settings, backup, dry),
            }
    except TimeoutError as exc:
        return {"Hierarchy": f"ERROR: {exc}; left untouched"}
