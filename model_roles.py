"""Dependency-free model-role selection.

Pure functions over already-fetched ``codex debug models`` JSON (or any dict
shaped like it) -- no filesystem/subprocess access in this module, so it stays
testable with plain dict fixtures. Callers own fetching and caching the JSON.

HARD INVARIANT: this module has no side effects and never touches
``config.json``'s user-set model fields. It only *selects candidates* for
callers -- it must never rewrite a user's selected main model. Callers are
responsible for honoring that when they consume the selections below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WORKHORSE_KEYWORDS = ("balanced", "everyday", "workhorse")
READER_KEYWORDS = ("affordable", "cost-efficient", "cost efficient", "fast", "repeatable")
FRONTIER_KEYWORDS = ("frontier", "flagship", "most capable")

CLAUDE_PEER_ALIASES = {"frontier": "best", "workhorse": "sonnet", "reader": "haiku"}
# User policy: prefer the moving Fable family at max effort and fall back to
# the moving Opus family only when Fable is explicitly unavailable. Both are
# aliases maintained by Claude Code, so future concrete versions need no patch.
CLAUDE_FRONTIER_FALLBACK_CHAIN = ("fable", "opus")

# Static capability seed used only when the local Codex catalog has not learned about
# Astra yet. Capability classification, rather than provider priority, keeps this
# frontier seed ahead of a live workhorse entry. A future live frontier can replace it.
# The CLI remains the source of truth for actual availability and runtime attestation.
CODEX_FRONTIER_SEED = {
    "id": "gpt-6-astra",
    "display_name": "GPT-6 Astra",
    "description": "Most capable model for the hardest end-to-end work.",
    "priority": 5,
    "visibility": "list",
    "default_reasoning_level": "high",
    "supported_reasoning_levels": [
        {"effort": "low"},
        {"effort": "medium"},
        {"effort": "high"},
        {"effort": "xhigh"},
        {"effort": "max"},
    ],
}


def seed_codex_frontier(models_json: dict) -> dict:
    """Return a shallow catalog copy containing the current flagship seed.

    Live entries win by id, and role selection still uses catalog priority rather
    than lexical model versions.  This keeps older Codex CLIs progressive without
    rewriting the user's selected main model.
    """
    source = models_json if isinstance(models_json, dict) else {}
    result = dict(source)
    models = list(source.get("models") or []) if isinstance(source.get("models"), list) else []
    ids = {_entry_id(item).lower() for item in models if isinstance(item, dict)}
    if CODEX_FRONTIER_SEED["id"].lower() not in ids:
        models.append(dict(CODEX_FRONTIER_SEED))
    result["models"] = models
    return result


def _as_float(value: Any) -> float:
    try:
        if value is None or isinstance(value, bool):
            return float("inf")
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _capability_text(entry: dict) -> str:
    """Flatten whatever capability info an entry carries (list or free text)
    into one lowercase string for keyword matching."""
    if not isinstance(entry, dict):
        return ""
    parts: list[str] = []
    def append_value(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                parts.append(str(key))
                append_value(nested)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                append_value(nested)
        elif value is not None:
            parts.append(str(value))

    append_value(entry.get("capabilities"))
    # Catalogs use different names for semantic role metadata. Keep this
    # permissive so a future live frontier is recognized without a code patch.
    for field in (
        "description", "tier", "category", "label", "capability_tier",
        "capability", "model_tier", "role",
    ):
        val = entry.get(field)
        append_value(val)
    return " ".join(parts).lower()


def _capability_tier(entry: dict) -> str:
    """Classify a catalog entry by capability, independent of priority.

    Priority is provider ordering, not a reliable statement that a workhorse is
    more capable than the current frontier. Unknown fixtures intentionally stay
    ``unknown`` so legacy priority-only catalogs retain deterministic behavior.
    """
    entry_id = _entry_id(entry).lower()
    text = _capability_text(entry)
    if entry_id == CODEX_FRONTIER_SEED["id"] or any(word in text for word in FRONTIER_KEYWORDS):
        return "frontier"
    if any(word in text for word in READER_KEYWORDS):
        return "reader"
    if any(word in text for word in WORKHORSE_KEYWORDS):
        return "workhorse"
    return "unknown"


def _entry_id(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("slug") or entry.get("id") or "").strip()


def _normalize_entry(entry: dict) -> dict | None:
    entry_id = _entry_id(entry)
    if not entry_id:
        return None
    description = entry.get("description")
    return {
        "id": entry_id,
        "display": str(entry.get("display_name") or entry.get("display") or entry_id),
        "description": description if isinstance(description, str) else None,
        "priority": _as_float(entry.get("priority")),
        "default_reasoning_level": entry.get("default_reasoning_level"),
        "supported_reasoning_levels": entry.get("supported_reasoning_levels"),
        "visibility": entry.get("visibility"),
        "capabilities": entry.get("capabilities") if isinstance(entry, dict) else None,
        "raw": entry,
    }


def _visible_entries(models_json: dict) -> list[dict]:
    if not isinstance(models_json, dict):
        return []
    raw_list = models_json.get("models")
    if not isinstance(raw_list, list):
        return []
    out = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        # Keep entries with no visibility field at all, or visibility == "list".
        # Anything else (e.g. "hidden", "experimental") is filtered out.
        visibility = item.get("visibility")
        if visibility is not None and str(visibility).strip().lower() != "list":
            continue
        norm = _normalize_entry(item)
        if norm is not None:
            out.append(norm)
    return out


@dataclass
class CodexRoles:
    frontier: dict | None
    workhorse: dict | None
    reader: dict | None


def select_codex_roles(models_json: dict) -> CodexRoles:
    entries = _visible_entries(models_json)
    if not entries:
        return CodexRoles(frontier=None, workhorse=None, reader=None)

    by_priority = sorted(entries, key=lambda e: (e["priority"], e["id"]))
    explicit_frontiers = [
        entry for entry in by_priority if _capability_tier(entry["raw"]) == "frontier"
    ]
    # Prefer a known/declared frontier. If the catalog exposes no capability
    # signal at all, preserve the previous priority-only behavior for fixtures
    # and older CLIs.
    frontier = explicit_frontiers[0] if explicit_frontiers else by_priority[0]

    non_frontier = [entry for entry in by_priority if entry["id"] != frontier["id"]]

    workhorse = None
    for entry in non_frontier:
        if _capability_tier(entry["raw"]) == "workhorse":
            workhorse = entry
            break
    if workhorse is None:
        workhorse = non_frontier[0] if non_frontier else None

    reader = None
    for entry in non_frontier:
        if _capability_tier(entry["raw"]) == "reader":
            reader = entry
            break
    if reader is None:
        # A two-model catalog may legitimately use the one non-frontier model
        # for both cheap roles. It is still cheaper than silently inheriting the
        # selected frontier brain.
        reader = non_frontier[-1] if non_frontier else None

    # The live catalog may embed full base-instruction templates in each raw
    # entry. They are useful only while matching descriptions and would add
    # tens of thousands of tokens if exposed through list_agent_models.
    def public(entry: dict | None) -> dict | None:
        return {key: value for key, value in entry.items() if key != "raw"} if entry else None

    return CodexRoles(
        frontier=public(frontier),
        workhorse=public(workhorse),
        reader=public(reader),
    )


def select_claude_roles() -> dict:
    """Stable Claude peer-alias candidates, not resolved to concrete model ids
    (resolving aliases to ids stays the CLI's job)."""
    return {
        "frontier": list(CLAUDE_FRONTIER_FALLBACK_CHAIN),
        "workhorse": CLAUDE_PEER_ALIASES["workhorse"],
        "reader": CLAUDE_PEER_ALIASES["reader"],
    }
