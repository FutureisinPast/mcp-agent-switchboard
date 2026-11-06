"""project_resolver.py

Fast, dependency-free (stdlib only) project resolver for Agent Switchboard.
Maps user messages and session cwd to registered local projects so hooks
can inject a compact pointer into model context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


@dataclass
class Entry:
    id: str
    root: Path
    out_dir: Path
    aliases: list[str] = field(default_factory=list)
    weak_aliases: list[str] = field(default_factory=list)
    group: str | None = None
    related: list[str] = field(default_factory=list)
    label: str | None = None
    exclude: list[str] = field(default_factory=list)
    legacy_names: list[str] = field(default_factory=list)
    start_here: str | None = None



@dataclass
class Resolution:
    ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    ambiguous: list[str] = field(default_factory=list)


# File cache: path -> (mtime_ns, size, dict[str, Entry])
_CACHE: dict[Path, tuple[int, int, dict[str, Entry]]] = {}

CUE_PRIORITIES = {
    "id": 1,
    "alias": 2,
    "path": 3,
    "cwd": 4,
    "weak-in-group": 5,
}


def clear_cache() -> None:
    """Clear in-memory parsed registry cache."""
    _CACHE.clear()


def registry_path() -> Path:
    """Determine the projects.json path from environment or default home."""
    override = os.environ.get("AGENT_BROKER_CODE_GRAPH_PROJECTS")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("AGENT_BROKER_HOME")
    if home:
        base = Path(home).expanduser()
    else:
        base = Path.home() / ".agent-broker"
    return base / "code-graph" / "projects.json"


def load_registry(path: Path | str | None = None) -> dict[str, Entry]:
    """Load and parse projects registry file. Never raises; returns empty dict on error."""
    try:
        resolved_path = registry_path() if path is None else Path(path).resolve()
        try:
            st = resolved_path.stat()
        except OSError:
            return {}

        cache_key = (st.st_mtime_ns, st.st_size)
        cached = _CACHE.get(resolved_path)
        if cached is not None and cached[0] == cache_key[0] and cached[1] == cache_key[1]:
            return dict(cached[2])

        with open(resolved_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        if not isinstance(raw_data, dict):
            return {}

        registry: dict[str, Entry] = {}
        for pid, val in raw_data.items():
            if not isinstance(pid, str) or not isinstance(val, dict):
                continue
            raw_root = val.get("root")
            raw_out = val.get("out_dir")
            if not isinstance(raw_root, str) or not isinstance(raw_out, str):
                continue
            if not raw_root.strip() or not raw_out.strip():
                continue

            root_path = Path(os.path.realpath(raw_root))
            out_dir_path = Path(raw_out)

            raw_aliases = val.get("aliases")
            aliases = (
                [str(a) for a in raw_aliases if isinstance(a, str)]
                if isinstance(raw_aliases, list)
                else []
            )

            raw_weak = val.get("weak_aliases")
            weak_aliases = (
                [str(w) for w in raw_weak if isinstance(w, str)]
                if isinstance(raw_weak, list)
                else []
            )

            raw_group = val.get("group")
            group = str(raw_group) if isinstance(raw_group, str) and raw_group else None

            raw_related = val.get("related")
            related = (
                [str(r) for r in raw_related if isinstance(r, str)]
                if isinstance(raw_related, list)
                else []
            )

            raw_label = val.get("label")
            label = str(raw_label) if isinstance(raw_label, str) else None

            raw_exclude = val.get("exclude")
            exclude = (
                [str(e) for e in raw_exclude if isinstance(e, str)]
                if isinstance(raw_exclude, list)
                else []
            )

            raw_legacy = val.get("legacy_names")
            legacy_names = (
                [str(a) for a in raw_legacy if isinstance(a, str)]
                if isinstance(raw_legacy, list)
                else []
            )

            raw_start = val.get("start_here")
            start_here = (
                str(raw_start)
                if isinstance(raw_start, str) and len(raw_start) <= 160
                else None
            )

            entry = Entry(
                id=pid,
                root=root_path,
                out_dir=out_dir_path,
                aliases=aliases,
                weak_aliases=weak_aliases,
                group=group,
                related=related,
                label=label,
                exclude=exclude,
                legacy_names=legacy_names,
                start_here=start_here,
            )
            registry[pid] = entry

        _CACHE[resolved_path] = (cache_key[0], cache_key[1], registry)
        return dict(registry)
    except Exception:
        return {}


def _match_phrase(phrase: str, text: str) -> bool:
    """Check if phrase matches text with word boundaries and case-insensitivity."""
    phrase = phrase.strip()
    if len(phrase) < 3:
        return False
    if phrase.lower() not in text.lower():
        return False
    prefix = r"\b" if re.match(r"\w", phrase[0]) else r"(?<!\S)"
    suffix = r"\b" if re.match(r"\w", phrase[-1]) else r"(?!\S)"
    pattern = rf"{prefix}{re.escape(phrase)}{suffix}"
    return bool(re.search(pattern, text, re.IGNORECASE))


def resolve(
    text: str | None,
    cwd: Path | str | None = None,
    registry: dict[str, Entry] | None = None,
    active_ids: Any = (),
) -> Resolution:
    """Resolve projects matching text and cwd. Never raises on invalid input."""
    try:
        if text is None or not isinstance(text, str) or not text:
            return Resolution()

        if registry is None:
            registry = load_registry()
        if not registry:
            return Resolution()

        active_set: set[str] = set(active_ids) if active_ids else set()
        matched: dict[str, tuple[int, str]] = {}

        def add_match(pid: str, reason: str) -> None:
            prio = CUE_PRIORITIES[reason]
            if pid not in matched or prio < matched[pid][0]:
                matched[pid] = (prio, reason)

        # (a) Canonical ID match: whole token outside [A-Za-z0-9_-]
        tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9_-]+", text)}
        for pid in registry:
            if re.fullmatch(r"[A-Za-z0-9_-]+", pid):
                if pid.lower() in tokens:
                    add_match(pid, "id")
            else:
                pattern = rf"(?<![A-Za-z0-9_-]){re.escape(pid)}(?![A-Za-z0-9_-])"
                if re.search(pattern, text, re.IGNORECASE):
                    add_match(pid, "id")

        # (b) Alias phrase match
        alias_map: dict[str, tuple[set[str], str]] = {}
        for pid, entry in registry.items():
            for alias in entry.aliases:
                raw_alias = alias.strip()
                if len(raw_alias) < 3:
                    continue
                norm_alias = raw_alias.lower()
                if norm_alias not in alias_map:
                    alias_map[norm_alias] = (set(), raw_alias)
                alias_map[norm_alias][0].add(pid)

        ambiguous_aliases: list[str] = []
        for norm_alias, (pids, display_phrase) in alias_map.items():
            if _match_phrase(norm_alias, text):
                if len(pids) > 1:
                    if display_phrase not in ambiguous_aliases:
                        ambiguous_aliases.append(display_phrase)
                elif len(pids) == 1:
                    target_pid = next(iter(pids))
                    add_match(target_pid, "alias")

        # (c) Path cue: text contains registered root path
        norm_text = text.replace("\\", "/")
        case_fold = (
            (sys.platform == "win32")
            or (os.name == "nt")
            or any(len(str(e.root)) >= 2 and str(e.root)[1] == ":" for e in registry.values())
        )
        cmp_text = norm_text.lower() if case_fold else norm_text

        for pid, entry in registry.items():
            norm_root = str(entry.root).replace("\\", "/").rstrip("/")
            if not norm_root:
                continue
            cmp_root = norm_root.lower() if case_fold else norm_root

            start = 0
            while True:
                idx = cmp_text.find(cmp_root, start)
                if idx == -1:
                    break
                after = idx + len(cmp_root)
                if after == len(cmp_text) or cmp_text[after] in ("/", "\\"):
                    add_match(pid, "path")
                    break
                start = idx + 1

        # (d) CWD match: longest registered root equal to or containing cwd at directory boundary
        if cwd:
            try:
                cwd_path = Path(os.path.realpath(cwd))
                cwd_parts_lower = [p.lower() for p in cwd_path.parts]
                cwd_candidates: list[Entry] = []
                for entry in registry.values():
                    root_path = Path(entry.root)
                    root_parts_lower = [p.lower() for p in root_path.parts]
                    if (
                        len(cwd_parts_lower) >= len(root_parts_lower)
                        and cwd_parts_lower[: len(root_parts_lower)] == root_parts_lower
                    ):
                        cwd_candidates.append(entry)
                if cwd_candidates:
                    cwd_candidates.sort(
                        key=lambda e: (len(Path(e.root).parts), len(str(e.root)), e.id),
                        reverse=True,
                    )
                    best_cwd_entry = cwd_candidates[0]
                    add_match(best_cwd_entry.id, "cwd")
            except Exception:
                pass

        # (e) Weak aliases: count only when another project of the same group is in active_ids
        # or was selected by (a)-(d) in this same call, and only if unique within group.
        anchors = active_set | set(matched.keys())
        active_groups: dict[str, set[str]] = {}
        for pid in anchors:
            e = registry.get(pid)
            if e and e.group:
                active_groups.setdefault(e.group, set()).add(pid)

        for group, group_anchors in active_groups.items():
            group_projects = [e for e in registry.values() if e.group == group]
            group_weak_map: dict[str, tuple[set[str], str]] = {}
            for e in group_projects:
                for w in e.weak_aliases:
                    raw_w = w.strip()
                    if len(raw_w) < 3:
                        continue
                    norm_w = raw_w.lower()
                    if norm_w not in group_weak_map:
                        group_weak_map[norm_w] = (set(), raw_w)
                    group_weak_map[norm_w][0].add(e.id)

            for norm_w, (pids_in_group, display_phrase) in group_weak_map.items():
                if _match_phrase(norm_w, text):
                    if len(pids_in_group) > 1:
                        if display_phrase not in ambiguous_aliases:
                            ambiguous_aliases.append(display_phrase)
                    elif len(pids_in_group) == 1:
                        cand_pid = next(iter(pids_in_group))
                        other_anchors = [p for p in group_anchors if p != cand_pid]
                        if other_anchors:
                            add_match(cand_pid, "weak-in-group")

        # Sort matches by cue strength then id
        sorted_matches = sorted(matched.items(), key=lambda item: (item[1][0], item[0]))
        final_ids = [pid for pid, _ in sorted_matches[:4]]
        final_reasons = {pid: reason for pid, (_, reason) in sorted_matches[:4]}

        return Resolution(
            ids=final_ids,
            reasons=final_reasons,
            ambiguous=ambiguous_aliases,
        )
    except Exception:
        return Resolution()


def freshness(entry: Entry | None) -> str | None:
    """Read <out_dir>/published/current.json and format freshness.

    Returns:
    - 'built Nm ago' / 'built Nh ago' / 'built Nd ago'
    - 'no map yet' when the file is absent
    - None on any other error
    """
    if entry is None or entry.out_dir is None:
        return None
    try:
        current_path = Path(entry.out_dir) / "published" / "current.json"
        if not current_path.is_file():
            return "no map yet"

        with open(current_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return None

        built_at = data.get("built_at")
        if not isinstance(built_at, str):
            return None

        dt_str = built_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        seconds = (now - dt).total_seconds()
        if seconds < 0:
            seconds = 0

        if seconds < 3600:
            minutes = int(seconds // 60)
            return f"built {minutes}m ago"
        elif seconds < 86400:
            hours = int(seconds // 3600)
            return f"built {hours}h ago"
        else:
            days = int(seconds // 86400)
            return f"built {days}d ago"
    except Exception:
        return None


def format_pointer(
    resolution: Resolution,
    registry: dict[str, Entry] | None,
    freshness_map: dict[str, str | None] | None = None,
    max_chars: int = 300,
) -> str:
    """Format one-line project pointer for model context. Never leaks paths or labels."""
    if not resolution or not resolution.ids:
        return ""

    primary_id = resolution.ids[0]
    entry = registry.get(primary_id) if registry else None

    # Freshness lookup
    if freshness_map is not None and primary_id in freshness_map:
        fresh_str = freshness_map[primary_id]
    elif entry is not None:
        fresh_str = freshness(entry)
    else:
        fresh_str = None

    group = entry.group if entry and entry.group else None
    related = list(entry.related) if entry and entry.related else []
    ids_str = ", ".join(resolution.ids)

    def _render(inc_group: bool, inc_related: bool, inc_freshness: bool, id_list_str: str) -> str:
        parts: list[str] = []
        if inc_group and group:
            parts.append(f"group {group}")
        if inc_related and related:
            parts.append(f"related {', '.join(related)}")

        info_str = f" ({'; '.join(parts)})" if parts else ""
        fresh_part = f" [{fresh_str}]" if inc_freshness and fresh_str else ""

        return (
            f"Switchboard project context: {id_list_str}{info_str}. "
            f"Map: code_graph project={primary_id}{fresh_part}. "
            f"Memory: get_context_pack project={primary_id}. "
            f"Locators only; read cited lines."
        )

    # 1. Try with everything
    line = _render(inc_group=True, inc_related=True, inc_freshness=True, id_list_str=ids_str)
    if len(line) <= max_chars:
        return line

    # 2. Drop related
    line = _render(inc_group=True, inc_related=False, inc_freshness=True, id_list_str=ids_str)
    if len(line) <= max_chars:
        return line

    # 3. Drop freshness
    line = _render(inc_group=True, inc_related=False, inc_freshness=False, id_list_str=ids_str)
    if len(line) <= max_chars:
        return line

    # 4. Drop group
    line = _render(inc_group=False, inc_related=False, inc_freshness=False, id_list_str=ids_str)
    if len(line) <= max_chars:
        return line

    # 5. If still exceeding, drop ids from tail
    curr_ids = list(resolution.ids)
    while len(curr_ids) > 1:
        curr_ids.pop()
        line = _render(
            inc_group=False,
            inc_related=False,
            inc_freshness=False,
            id_list_str=", ".join(curr_ids),
        )
        if len(line) <= max_chars:
            return line

    return line[:max_chars]


def normalize_name(s: str) -> str:
    """Normalize a project name, alias, or legacy name.

    lowercase, strip, collapse any run of whitespace/underscores into '-',
    collapse repeated '-'.
    """
    if not isinstance(s, str):
        return ""
    res = s.lower().strip()
    res = re.sub(r"[\s_]+", "-", res)
    res = re.sub(r"-+", "-", res)
    return res


def _normalize_path_components(p: Path | str | None) -> list[str]:
    if p is None:
        return []
    s = str(p).replace("\\", "/").strip()
    if not s:
        return []
    raw_parts = [seg for seg in s.split("/") if seg and seg != "."]
    parts: list[str] = []
    for seg in raw_parts:
        if seg == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append("..")
        else:
            parts.append(seg)
    case_fold = (
        (sys.platform == "win32")
        or (os.name == "nt")
        or any(len(part) >= 2 and part[1] == ":" for part in parts)
    )
    if case_fold:
        parts = [part.lower() for part in parts]
    return parts


def _match_path_rule(
    target_path: Path | str,
    registry: dict[str, Entry],
    reason_name: str,
) -> tuple[str | None, str, list[str]]:
    target_parts = _normalize_path_components(target_path)
    if not target_parts:
        return (None, "unregistered", [])

    equal_or_containing: list[tuple[int, int, str]] = []
    parent_of_roots: list[str] = []

    for pid, entry in registry.items():
        if not isinstance(entry, Entry) or not entry.root:
            continue
        root_parts = _normalize_path_components(entry.root)
        if not root_parts:
            continue
        if len(target_parts) >= len(root_parts) and target_parts[: len(root_parts)] == root_parts:
            equal_or_containing.append((len(root_parts), len(str(entry.root)), pid))
        elif len(target_parts) < len(root_parts) and root_parts[: len(target_parts)] == target_parts:
            parent_of_roots.append(pid)

    if equal_or_containing:
        equal_or_containing.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return (equal_or_containing[0][2], reason_name, [])

    if parent_of_roots:
        return (None, "ambiguous", sorted(parent_of_roots))

    return (None, "unregistered", [])


def canonical_project(
    name: str | Path | None = None,
    cwd: Path | str | None = None,
    registry: dict[str, Entry] | None = None,
) -> tuple[str | None, str, list[str]]:
    """Determine the canonical project ID for a name, path, or cwd.

    Returns (project_id, reason, candidates).
    Never raises (any error -> (None, 'error', [])).
    """
    try:
        if registry is None:
            registry = load_registry()
        if not isinstance(registry, dict):
            return (None, "error", [])

        name_given = False
        name_str = ""
        if name is not None and not isinstance(name, type):
            if isinstance(name, (str, Path)):
                name_str = str(name).strip()
                if name_str:
                    name_given = True
            elif hasattr(name, "__str__"):
                name_str = str(name).strip()
                if name_str:
                    name_given = True

        if name_given:
            # a. name given and non-empty:
            # 1. equals a registered id (case-insensitive) -> (id, 'id', [])
            name_lower = name_str.lower()
            for pid in registry:
                if isinstance(pid, str) and pid.lower() == name_lower:
                    return (pid, "id", [])

            # 2. normalize_name(name) equals normalize_name of exactly one project's alias -> (id, 'alias', [])
            norm_name = normalize_name(name_str)
            if norm_name:
                alias_matches: list[str] = [
                    pid
                    for pid, entry in registry.items()
                    if isinstance(entry, Entry)
                    and any(normalize_name(a) == norm_name for a in entry.aliases if normalize_name(a))
                ]
                if len(alias_matches) == 1:
                    return (alias_matches[0], "alias", [])
                if len(alias_matches) > 1:
                    return (None, "ambiguous", sorted(alias_matches))

                # 3. normalize_name(name) equals normalize_name of a legacy name listed by exactly one project -> (id, 'legacy', [])
                # A legacy name listed by two or more projects is ambiguous: return (None, 'ambiguous', sorted ids)
                legacy_matches: list[str] = [
                    pid
                    for pid, entry in registry.items()
                    if isinstance(entry, Entry)
                    and any(normalize_name(leg) == norm_name for leg in entry.legacy_names if normalize_name(leg))
                ]
                if len(legacy_matches) == 1:
                    return (legacy_matches[0], "legacy", [])
                if len(legacy_matches) > 1:
                    return (None, "ambiguous", sorted(legacy_matches))

            # 4. name looks like a path (contains / or \):
            if "/" in name_str or "\\" in name_str or isinstance(name, Path):
                res = _match_path_rule(name, registry, "path")
                if res[1] != "unregistered":
                    return res
                if cwd is not None:
                    try:
                        cwd_str = str(cwd).strip()
                        if cwd_str:
                            combined = Path(cwd) / name_str
                            res_comb = _match_path_rule(combined, registry, "path")
                            if res_comb[1] != "unregistered":
                                return res_comb
                    except Exception:
                        pass
                return res

            # 5. otherwise (None, 'unregistered', [])
            return (None, "unregistered", [])

        # b. name empty/None and cwd given: same path rule applied to cwd -> (id, 'cwd', [])
        # or (None, 'ambiguous', ids under cwd) or (None, 'unregistered', [])
        if cwd is not None and not isinstance(cwd, type):
            cwd_str = str(cwd).strip()
            if cwd_str:
                return _match_path_rule(cwd, registry, "cwd")

        # c. nothing given -> (None, 'unregistered', [])
        return (None, "unregistered", [])
    except Exception:
        return (None, "error", [])
