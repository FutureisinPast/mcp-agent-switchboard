"""project_notes.py

Cross-host per-project lesson ledger for Agent Switchboard.
Enables new sessions in any host to learn project identity, entry points,
owner decisions, pitfalls/failures to avoid regressions, and what worked.
Only VERIFIED lessons are ever exposed automatically.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
from typing import Any

try:
    from atomic_io import atomic_write_text
except ImportError:
    import tempfile

    def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(path))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


NOTE_KINDS = ("decision", "failed", "worked", "pitfall", "progress")
STATUSES = ("pending", "verified", "disputed", "superseded", "expired")
ACTIVITY_KINDS = ("touch", "edit", "note")

PROJECT_ID_REGEX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Regex for absolute paths: Windows drive paths, UNC paths, and POSIX home / Users / root paths
_PATH_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9])("
    r"[a-zA-Z]:[\\/][^\s\"'<>]*"
    r"|\\\\[^\s\"'<>\\]+[\\/][^\s\"'<>]*"
    r"|\\\\[^\s\"'<>]+"
    r")"
    r"|"
    r"(?<![a-zA-Z0-9_/:])("
    r"/(?:home|Users|root)(?:[\\/][^\s\"'<>]*|(?=[\s\"'<>,;.!?)]|$))"
    r")"
)


def validate_project_id(project: str) -> None:
    if not isinstance(project, str) or not PROJECT_ID_REGEX.fullmatch(project):
        raise ValueError(
            f"Invalid project id: '{project}'. Must match ^[a-z0-9][a-z0-9._-]{{0,63}}$"
        )


def _base_home() -> Path:
    home = os.environ.get("AGENT_BROKER_HOME")
    if home:
        return Path(home).expanduser()
    return Path.home() / ".agent-broker"


def _project_dir(project: str) -> Path:
    validate_project_id(project)
    return _base_home() / "projects" / project


def _format_now(now: str | datetime | None = None) -> str:
    if now is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(now, str):
        return now
    return str(now)


def _generate_note_id(now_dt: datetime | None = None) -> str:
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    ts_bytes = int(now_dt.timestamp()).to_bytes(4, "big")
    rand_bytes = secrets.token_bytes(2)
    return "n_" + (ts_bytes + rand_bytes).hex()


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    length = len(s)
    counts = Counter(s)
    return -sum((cnt / length) * math.log2(cnt / length) for cnt in counts.values())


def _count_classes(s: str) -> int:
    has_lower = any(c.islower() for c in s)
    has_upper = any(c.isupper() for c in s)
    has_digit = any(c.isdigit() for c in s)
    has_symbol = any(not c.isalnum() for c in s)
    return sum([has_lower, has_upper, has_digit, has_symbol])


def _load_redact_terms() -> list[str]:
    try:
        path = _base_home() / "code-graph" / "redact_terms.json"
        if not path.is_file():
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            terms = [str(x) for x in data if isinstance(x, str) and x.strip()]
            terms.sort(key=len, reverse=True)
            return terms
    except Exception:
        pass
    return []


def _replace_paths(text: str) -> str:
    def repl(m: re.Match) -> str:
        s = m.group(0)
        trailing = ""
        while s and s[-1] in ".,;:!?)]}\"'":
            trailing = s[-1] + trailing
            s = s[:-1]
        if not s:
            return m.group(0)
        return "<path>" + trailing

    return _PATH_PATTERN.sub(repl, text)


def _replace_redact_terms(text: str) -> str:
    terms = _load_redact_terms()
    for term in terms:
        if term:
            text = re.sub(re.escape(term), "<masked>", text, flags=re.IGNORECASE)
    return text


def _replace_secrets(text: str) -> str:
    # 1. PEM private key headers or blocks
    text = re.sub(
        r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z0-9_-]*PRIVATE KEY-----",
        "[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----",
        "[redacted]",
        text,
        flags=re.IGNORECASE,
    )

    # 2. AKIA / ASIA keys (AWS access key IDs are 20 characters)
    text = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[redacted]", text)

    # 3. GitHub tokens
    text = re.sub(r"\bgh[a-zA-Z0-9]_[A-Za-z0-9_]{10,}\b", "[redacted]", text)

    # 4. Slack tokens
    text = re.sub(r"\bxox[a-zA-Z0-9]-[A-Za-z0-9_-]{10,}\b", "[redacted]", text)

    # 5. sk- / sk_live_ keys
    text = re.sub(r"\bsk-(?:live-)?[A-Za-z0-9_-]{10,}\b", "[redacted]", text)
    text = re.sub(r"\bsk_live_[A-Za-z0-9_-]{10,}\b", "[redacted]", text)

    # 6. Google API keys
    text = re.sub(r"\bAIza[0-9A-Za-z_-]{30,}\b", "[redacted]", text)

    # 7. JWT tokens
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\b", "[redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\b", "[redacted]", text)

    # 8. Bearer tokens
    text = re.sub(r"(?i)\bBearer\s+(?!\[redacted\])[^\s,;]+", "Bearer [redacted]", text)

    # 9. password=/secret=/token=/api_key= values
    text = re.sub(
        r"(?i)\b(password|secret|token|api_key)\s*=\s*(?!\[redacted\])([\'\"]?)([^\s,;\'\"]+)\2",
        r"\1=[redacted]",
        text,
    )

    # 10. High-entropy tokens (>= 32 chars, 3+ classes, entropy >= 4.0, no '://')
    def _entropy_repl(m: re.Match) -> str:
        word = m.group(0)
        if "://" in word:
            return word
        suffix = ""
        core = word
        while core and core[-1] in ".,;:!?)]}\"'":
            suffix = core[-1] + suffix
            core = core[:-1]
        prefix = ""
        while core and core[0] in "([{\'\"<":
            prefix += core[0]
            core = core[1:]

        # Relative file paths / file names (e.g. dev_docs/SOME_HANDOFF_2026-10-04.md) are not secrets.
        path_like = bool(re.fullmatch(r"[A-Za-z0-9_.\-/\\]+", core)) and (
            "/" in core or "\\" in core or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", core))
        )
        if (
            len(core) >= 32
            and not path_like
            and "://" not in core
            and core != "[redacted]"
            and _count_classes(core) >= 3
            and _shannon_entropy(core) >= 4.0
        ):
            return prefix + "[redacted]" + suffix
        return word

    text = re.sub(r"\S+", _entropy_repl, text)
    return text


def sanitize_text(s: str | None) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    if not s:
        return ""
    s = _replace_paths(s)
    s = _replace_redact_terms(s)
    s = _replace_secrets(s)
    return s


def add_note(
    project: str,
    kind: str,
    text: str,
    *,
    applies_when: str | None = None,
    evidence: str | None = None,
    supersedes: str | None = None,
    author: str | None = None,
    status: str = "pending",
    now: str | datetime | None = None,
) -> dict:
    validate_project_id(project)
    if kind not in NOTE_KINDS:
        raise ValueError(f"Invalid kind '{kind}', must be one of {NOTE_KINDS}")
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}', must be one of {STATUSES}")

    if not isinstance(text, str) or len(text) < 1 or len(text) > 400:
        raise ValueError("text must be between 1 and 400 characters")
    if applies_when is not None and (not isinstance(applies_when, str) or len(applies_when) > 200):
        raise ValueError("applies_when must be at most 200 characters")
    if evidence is not None and (not isinstance(evidence, str) or len(evidence) > 200):
        raise ValueError("evidence must be at most 200 characters")
    if author is not None and (not isinstance(author, str) or len(author) > 60):
        raise ValueError("author must be at most 60 characters")

    clean_text = sanitize_text(text)
    clean_applies = sanitize_text(applies_when) if applies_when is not None else None
    clean_evidence = sanitize_text(evidence) if evidence is not None else None
    clean_author = sanitize_text(author) if author is not None else None

    created_at = _format_now(now)
    now_dt = now if isinstance(now, datetime) else None
    note_id = _generate_note_id(now_dt)

    note = {
        "id": note_id,
        "project": project,
        "kind": kind,
        "text": clean_text,
        "applies_when": clean_applies,
        "evidence": clean_evidence,
        "supersedes": supersedes,
        "author": clean_author,
        "created_at": created_at,
        "status": status,
    }

    pdir = _project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    note_event = {"type": "note", **note}
    with open(pdir / "notes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(note_event) + "\n")

    if supersedes and isinstance(supersedes, str):
        existing_notes = {n["id"] for n in list_notes(project)}
        if supersedes in existing_notes:
            status_event = {
                "type": "status",
                "id": supersedes,
                "status": "superseded",
                "by": note_id,
                "reason": f"superseded by {note_id}",
                "ts": created_at,
            }
            with open(pdir / "notes.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(status_event) + "\n")

    write_cache(project, now=now)
    return note


def review_note(
    project: str,
    note_id: str,
    status: str,
    *,
    reviewer: str | None = None,
    reason: str | None = None,
    now: str | datetime | None = None,
) -> dict:
    validate_project_id(project)
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}', must be one of {STATUSES}")

    all_notes = list_notes(project)
    found_note = None
    for n in all_notes:
        if n["id"] == note_id:
            found_note = n
            break
    if found_note is None:
        raise ValueError(f"Unknown note id: '{note_id}'")

    clean_reviewer = sanitize_text(reviewer) if reviewer is not None else None
    clean_reason = sanitize_text(reason) if reason is not None else None
    ts = _format_now(now)

    status_event = {
        "type": "status",
        "id": note_id,
        "status": status,
        "by": clean_reviewer,
        "reason": clean_reason,
        "ts": ts,
    }
    pdir = _project_dir(project)
    with open(pdir / "notes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(status_event) + "\n")

    write_cache(project, now=now)
    updated_notes = list_notes(project)
    for n in updated_notes:
        if n["id"] == note_id:
            return n
    return status_event


def list_notes(
    project: str,
    statuses: list[str] | set[str] | tuple[str, ...] | str | None = None,
) -> list[dict]:
    validate_project_id(project)
    pdir = _project_dir(project)
    notes_file = pdir / "notes.jsonl"
    if not notes_file.is_file():
        return []

    notes_by_id: dict[str, dict] = {}
    try:
        with open(notes_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "note":
                    nid = event.get("id")
                    if not isinstance(nid, str) or not nid:
                        continue
                    note_data = {
                        "id": nid,
                        "project": event.get("project", project),
                        "kind": event.get("kind"),
                        "text": event.get("text"),
                        "applies_when": event.get("applies_when"),
                        "evidence": event.get("evidence"),
                        "supersedes": event.get("supersedes"),
                        "author": event.get("author"),
                        "created_at": event.get("created_at"),
                        "status": event.get("status", "pending"),
                    }
                    notes_by_id[nid] = note_data
                elif etype == "status":
                    nid = event.get("id")
                    new_status = event.get("status")
                    if isinstance(nid, str) and nid in notes_by_id and isinstance(new_status, str):
                        notes_by_id[nid]["status"] = new_status
    except Exception:
        return []

    res = list(notes_by_id.values())
    if statuses is not None:
        if isinstance(statuses, str):
            allowed = {statuses}
        else:
            allowed = set(statuses)
        res = [n for n in res if n.get("status") in allowed]
    return res


def record_activity(
    project: str,
    host: str,
    kind: str,
    count: int = 1,
    now: str | datetime | None = None,
) -> dict:
    validate_project_id(project)
    if kind not in ACTIVITY_KINDS:
        raise ValueError(f"Invalid activity kind: '{kind}', must be one of {ACTIVITY_KINDS}")
    if not isinstance(host, str):
        raise ValueError("host must be a string")
    clean_host = sanitize_text(host)
    if len(clean_host) > 20 or len(host) > 20:
        raise ValueError(f"host must be <= 20 characters, got {len(host)}")
    try:
        count = int(count)
    except (ValueError, TypeError):
        raise ValueError("count must be an integer")
    if count < 1:
        raise ValueError("count must be >= 1")

    ts = _format_now(now)
    event = {
        "ts": ts,
        "host": clean_host,
        "kind": kind,
        "count": count,
    }
    pdir = _project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    with open(pdir / "activity.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    return event


def activity_summary(
    project: str,
    now: str | datetime | None = None,
    limit_lines: int = 500,
) -> list[dict]:
    validate_project_id(project)
    pdir = _project_dir(project)
    act_file = pdir / "activity.jsonl"
    if not act_file.is_file():
        return []

    try:
        with open(act_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    if limit_lines > 0 and len(lines) > limit_lines:
        lines = lines[-limit_lines:]

    hosts: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        host = rec.get("host")
        kind = rec.get("kind")
        count = rec.get("count", 1)
        ts = rec.get("ts")
        if not isinstance(host, str) or not host:
            continue
        if kind not in ACTIVITY_KINDS:
            continue
        try:
            count = int(count)
        except (ValueError, TypeError):
            count = 1

        if host not in hosts:
            hosts[host] = {
                "host": host,
                "touches": 0,
                "edits": 0,
                "notes": 0,
                "last_ts": None,
            }

        if kind == "touch":
            hosts[host]["touches"] += count
        elif kind == "edit":
            hosts[host]["edits"] += count
        elif kind == "note":
            hosts[host]["notes"] += count

        if ts and isinstance(ts, str):
            curr_last = hosts[host]["last_ts"]
            if curr_last is None or ts > curr_last:
                hosts[host]["last_ts"] = ts

    return list(sorted(hosts.values(), key=lambda h: h["host"]))


def _format_activity_ts(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        clean_ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def _get_entry_field(entry: Any, field: str, default: Any = None) -> Any:
    if entry is None:
        return default
    if isinstance(entry, dict):
        return entry.get(field, default)
    return getattr(entry, field, default)


def render_brief(
    project: str,
    entry: Any = None,
    now: str | datetime | None = None,
) -> dict[str, str]:
    validate_project_id(project)

    aliases = _get_entry_field(entry, "aliases") or []
    group = _get_entry_field(entry, "group")
    related = _get_entry_field(entry, "related") or []
    start_here = _get_entry_field(entry, "start_here")

    # --- first_touch rendering ---
    parts: list[str] = []
    if group:
        parts.append(f"group {sanitize_text(str(group))}")
    if related:
        r_strs = [sanitize_text(str(r)) for r in related if str(r).strip()]
        if r_strs:
            parts.append(f"related {', '.join(r_strs)}")
    info_str = f" ({'; '.join(parts)})" if parts else ""

    first_touch = f"Switchboard project: {project}{info_str}. File notes and memory under project={project}."
    if start_here:
        first_touch += f" Start: {sanitize_text(str(start_here))}."
    first_touch += f" Map: code_graph project={project}."

    all_notes = list_notes(project)
    verified_notes = [n for n in all_notes if n.get("status") == "verified"]
    pending_notes = [n for n in all_notes if n.get("status") == "pending"]

    # Filter verified notes of kind 'failed' or 'pitfall'
    avoid_candidates = [n for n in verified_notes if n.get("kind") in ("failed", "pitfall")]
    # Rank by: author == 'owner' first, kind 'failed' before 'pitfall', newest first
    avoid_candidates.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    avoid_candidates.sort(
        key=lambda n: (
            0 if n.get("author") == "owner" else 1,
            0 if n.get("kind") == "failed" else 1,
        )
    )

    accepted_lessons = []
    for cand in avoid_candidates:
        if len(accepted_lessons) >= 3:
            break
        text_clean = sanitize_text(cand.get("text", ""))
        applies_clean = (
            sanitize_text(cand.get("applies_when", ""))
            if cand.get("applies_when")
            else None
        )
        if applies_clean:
            item_body = f"{text_clean} (when {applies_clean})"
        else:
            item_body = text_clean

        next_idx = len(accepted_lessons) + 1
        prefix = " Avoid: " if not accepted_lessons else " "
        addition = f"{prefix}{next_idx}) {item_body}"

        if len(first_touch + addition) <= 700:
            first_touch += addition
            accepted_lessons.append(cand)

    brief_suffix = ("" if first_touch.rstrip().endswith(".") else ".") + f" Brief: project_brief op=get project={project}."
    if len(first_touch + brief_suffix) <= 700:
        first_touch += brief_suffix

    # --- full markdown rendering ---
    id_lines = [f"- ID: {project}"]
    if aliases:
        id_lines.append(f"- Aliases: {', '.join(sanitize_text(str(a)) for a in aliases)}")
    else:
        id_lines.append("- Aliases: none")
    if group:
        id_lines.append(f"- Group: {sanitize_text(str(group))}")
    else:
        id_lines.append("- Group: none")
    if related:
        id_lines.append(f"- Related: {', '.join(sanitize_text(str(r)) for r in related)}")
    else:
        id_lines.append("- Related: none")
    id_lines.append(f"- Filing rule: File notes and memory under project={project}.")
    identity_section = "\n".join(id_lines)

    start_here_section = (
        sanitize_text(str(start_here)) if start_here else "No start guide recorded."
    )

    # Owner decisions (verified decision notes)
    decision_notes = [n for n in verified_notes if n.get("kind") == "decision"]
    decision_notes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    decision_items = [f"- [{n['id']}] {sanitize_text(n['text'])}" for n in decision_notes]

    # Do not repeat (verified failed + pitfall, with applies_when and evidence)
    avoid_items = []
    for n in avoid_candidates:
        parts_d = []
        if n.get("applies_when"):
            parts_d.append(f"when {sanitize_text(n['applies_when'])}")
        if n.get("evidence"):
            parts_d.append(f"evidence: {sanitize_text(n['evidence'])}")
        extra_d = f" ({'; '.join(parts_d)})" if parts_d else ""
        avoid_items.append(f"- [{n['id']}] ({n['kind']}) {sanitize_text(n['text'])}{extra_d}")

    # What worked (verified worked)
    worked_notes = [n for n in verified_notes if n.get("kind") == "worked"]
    worked_notes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    worked_items = [f"- [{n['id']}] {sanitize_text(n['text'])}" for n in worked_notes]

    # Recent activity
    acts = activity_summary(project, now=now)
    act_items = [
        f"- {h['host']}: {h['touches']} touches, {h['edits']} edits, {h['notes']} notes, last {_format_activity_ts(h['last_ts'])}"
        for h in acts
    ]

    # Pending review
    pending_count = len(pending_notes)
    pending_notes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    pending_items = [
        f"- [{n['id']}] {n['kind']}: {sanitize_text(n['text'])} (unverified)"
        for n in pending_notes[:5]
    ]

    def _build_full(
        d_list: list[str],
        a_list: list[str],
        w_list: list[str],
        act_list: list[str],
        p_list: list[str],
    ) -> str:
        sec = [
            f"# Project {project}",
            "",
            "## Identity",
            identity_section,
            "",
            "## Start here",
            start_here_section,
            "",
            "## Owner decisions",
            "\n".join(d_list) if d_list else "None recorded.",
            "",
            "## Do not repeat",
            "\n".join(a_list) if a_list else "None recorded.",
            "",
            "## What worked",
            "\n".join(w_list) if w_list else "None recorded.",
            "",
            "## Recent activity",
            "\n".join(act_list) if act_list else "None recorded.",
            "",
            "## Pending review",
            f"Pending count: {pending_count}",
        ]
        if p_list:
            sec.append("\n".join(p_list))
        return "\n".join(sec)

    lists = [decision_items, avoid_items, worked_items, act_items, pending_items]
    full = _build_full(decision_items, avoid_items, worked_items, act_items, pending_items)

    # When over 4000, drop oldest items from the longest lists first, never splitting an item.
    while len(full) > 4000:
        non_empty = [lst for lst in lists if len(lst) > 0]
        if not non_empty:
            break
        longest = max(non_empty, key=len)
        longest.pop()
        full = _build_full(decision_items, avoid_items, worked_items, act_items, pending_items)

    return {"first_touch": first_touch, "full": full}


def write_cache(
    project: str,
    entry: Any = None,
    now: str | datetime | None = None,
) -> dict:
    validate_project_id(project)
    brief = render_brief(project, entry=entry, now=now)
    built_at = _format_now(now)
    cache_data = {
        "first_touch": brief["first_touch"],
        "full": brief["full"],
        "built_at": built_at,
    }
    pdir = _project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    brief_file = pdir / "brief.json"
    content = json.dumps(cache_data, indent=2, ensure_ascii=False)
    atomic_write_text(brief_file, content, encoding="utf-8")
    return cache_data


def read_cached_first_touch(project: str) -> str | None:
    try:
        if not isinstance(project, str) or not PROJECT_ID_REGEX.fullmatch(project):
            return None
        pdir = _project_dir(project)
        brief_file = pdir / "brief.json"
        if not brief_file.is_file():
            return None
        with open(brief_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            val = data.get("first_touch")
            if isinstance(val, str):
                return val
    except Exception:
        return None
    return None


def read_cached_full(project: str) -> str | None:
    try:
        if not isinstance(project, str) or not PROJECT_ID_REGEX.fullmatch(project):
            return None
        pdir = _project_dir(project)
        brief_file = pdir / "brief.json"
        if not brief_file.is_file():
            return None
        with open(brief_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            val = data.get("full")
            if isinstance(val, str):
                return val
    except Exception:
        return None
    return None
