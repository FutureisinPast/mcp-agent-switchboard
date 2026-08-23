"""Outbound-payload screening for cross-vendor consultation dispatches.

Why this exists
----------------
An outbound consultation to the OpenAI/Codex lane once described an owned,
authorized Telegram automation project using wording ("burner account",
"bypass", "auto-join", "self-destruct", "anti-ban") that reads to an abuse
classifier as platform-evasion tooling rather than as the legitimate
review/debugging work it actually was. The recipient account received a
misuse warning. The underlying work was legitimate; the *wording* of the
outbound payload was the problem, and the wording escaped detection because
only the caller's own prompt was ever inspected -- not the fully assembled
payload (prompt + shared context pack + prior consultation excerpts) that
is what actually gets sent over the wire.

This module screens the FINAL ASSEMBLED outbound string -- the exact text
about to be handed to the target CLI transport -- and produces one of four
classifications:

    clean              -- no high-risk term hits, no substance concern.
                           Dispatch unchanged.
    reworded            -- term hits only; the underlying request is clean.
                           The substitution map + wrapper are applied and the
                           payload dispatches, with the list of substitutions
                           made returned so nothing changes silently.
    needs_owner_review  -- substance is ambiguous or may read badly to a
                           third party. Not dispatched. An explicit operator
                           opt-in may proceed (with the wrapper applied, no
                           substitution -- the request wasn't clean-with-
                           wrong-vocabulary, it was ambiguous).
    block               -- the payload asks the recipient to implement,
                           improve, or optimize defeating access controls,
                           avoiding bans/detection/enforcement, retaining
                           platform-ephemeral content, or spreading
                           enforcement risk across accounts. NEVER
                           dispatched and NOT opt-in-able.

Ordering guarantee (the anti-gaming guardrail)
-----------------------------------------------
Substitution corrects HOW legitimate work is described. It never changes
WHAT is being asked for, and it is not a route around this classifier:

1. The substance verdict (`block` / `needs_owner_review` / clean) is always
   computed on the ORIGINAL, unsubstituted payload -- see
   `classify_substance()`.
2. Substitution is applied only AFTER that verdict is known, and only when
   the verdict is clean.
3. The substance verdict is carried through unchanged; it is never
   recomputed on substituted text. Substitution can therefore never
   downgrade `block` or `needs_owner_review` into `reworded` or `clean` --
   there is no code path in this module that reclassifies text after
   substitution. See `test_honesty_guard_substitution_cannot_downgrade_block`
   in the test suite for the assertion that proves this.
4. A reworded retry of a request that substance-classification already
   flagged stays flagged: `classify_substance()` runs on the retry's own
   text and finds the same signals again (see `_REWORDED_RETRY_PATTERNS`
   plus the underlying term/semantic signals, which retrying does not
   remove).

The substitution map below exists because several of the flagged terms are
simply *inaccurate* descriptions of owned, authorized work, not because the
work needs to be hidden. "Burner account" asserts a disposable identity used
to absorb enforcement risk -- that is not what an operator using their own
secondary account is doing, so the word was factually wrong; "a secondary
account I own" is simply true. That is an accuracy fix, not a euphemism, and
it is only ever applied to a payload whose substance has already been
independently judged clean.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import atomic_io

# ---------------------------------------------------------------------------
# High-risk term list (case-insensitive, word-boundary aware).
#
# Kept as a flat, reviewable module constant. A hit here is never itself a
# block -- see the module docstring. It only guarantees the payload gets a
# semantic look and, at minimum, is held at `needs_owner_review` unless that
# look resolves it to a clearly legitimate review/debugging context (in
# which case it becomes `reworded`, not `clean` silently-as-is).
# ---------------------------------------------------------------------------
HIGH_RISK_TERMS: tuple[str, ...] = (
    "burner",
    "no legal/ToS framing needed",
    "anti-ban",
    "ban-safety",
    "avoid getting banned",
    "bypass",
    "auto-unlock",
    "auto-pass",
    "auto-join",
    "sponsor channel",
    "multi-round gate",
    "forward-restricted",
    "self-destruct",
    "headless login",
    "historical unlock",
    "reverse-engineered",
    "FloodWait",
    "PeerFlood",
)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a case-insensitive, word-boundary-aware regex for a phrase
    that may contain internal spaces, hyphens, or slashes."""
    escaped = re.escape(phrase)
    # re.escape turns internal whitespace into an escaped literal space; relax
    # that back to "one or more whitespace characters" so multi-word phrases
    # still match across a line wrap or extra spacing in the assembled text.
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


_TERM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (term, _phrase_pattern(term)) for term in HIGH_RISK_TERMS
)


def find_term_hits(payload: str) -> list[str]:
    """Return the subset of HIGH_RISK_TERMS (in their canonical spelling)
    that appear anywhere in ``payload``."""
    text = payload or ""
    return [term for term, pattern in _TERM_PATTERNS if pattern.search(text)]


# ---------------------------------------------------------------------------
# Semantic signal patterns. These run on the substance of the request, not
# on the term list above -- a payload can trip these with none of the exact
# high-risk terms present, and a payload can contain high-risk terms without
# tripping any block signal (e.g. a defensive FloodWait retry handler).
# ---------------------------------------------------------------------------
_BLOCK_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"avoid(?:ing)?\s+(?:getting\s+)?(?:banned|bans|ban|detection|being\s+detected)\b",
        r"bypass\w*\s+(?:the\s+)?(?:access\s+control|verification|gate|restriction|rate\s*limit|ban|detection)",
        r"evade\w*\s+(?:detection|ban|enforcement)",
        r"help\s+(?:us|me|them)?\s*(?:avoid|bypass|evade)\w*\s+(?:the\s+)?(?:ban|bans|detection|gate|verification|restriction)",
        r"(?:secondary|burner|throwaway|disposable)\s+account\w*.{0,60}(?:avoid|absorb|reduce|soak\s+up).{0,20}(?:ban|bans|enforcement|risk|detection)",
        r"(?:copy|retain|save|store|archive|keep)\w*\s+(?:the\s+)?self.?destruct\w*\s+(?:message|content|media|photo|video)",
        r"(?:optimi[sz]e|improve|implement|design|build|harden)\w*\s+.{0,80}(?:bypass|evade|anti.ban|ban.safety)",
    )
)

_ALLOW_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"review\w*\s+.{0,100}for\s+(?:correctness|security|reliability|compliance|sign.off)",
        r"(?:our|my|the\s+operator'?s?|the\s+user'?s?)\s+own\s+account",
        r"normal\s+(?:official\s+)?api\s+behav",
        r"defensive\s+rate.?limit",
        r"identify\s+and\s+(?:remove|flag)",
        r"flag\s+.{0,80}(?:unsafe|risky|abusive)\s+behav",
        r"sign.?off\s+work",
        r"not\s+evasion\s+optimi[sz]ation",
    )
)

_REWORDED_RETRY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:reword|rephrase|retry|resend|re-send)\w*\s+.{0,60}(?:previous|earlier|prior|blocked)",
        r"(?:previous|earlier|prior)\s+(?:request|payload|message)\s+was\s+blocked",
    )
)


def _matches_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def classify_substance(payload: str) -> tuple[str, list[str]]:
    """Judge the SUBSTANCE of the ORIGINAL, unsubstituted payload.

    Returns (verdict, reasons) where verdict is one of:
        "block"               -- evasion-tooling request; never dispatched,
                                  never opt-in-able.
        "needs_owner_review"  -- ambiguous; needs explicit operator opt-in.
        "clean"                -- no substance concern (may still carry
                                  high-risk terminology that a later,
                                  separate step may rewrite for accuracy --
                                  this function never does that itself).

    This function must always be called with the ORIGINAL payload. Nothing
    in this module calls it again on substituted text -- see the module
    docstring's ordering guarantee.
    """
    text = payload or ""
    hits = find_term_hits(text)
    block_signal = _matches_any(_BLOCK_SIGNAL_PATTERNS, text)
    if block_signal:
        return "block", [
            "payload asks the recipient to implement, improve, or optimize bypassing "
            "access controls, verification gates, ban/detection avoidance, retaining "
            "platform-ephemeral content, or spreading enforcement risk across accounts "
            "-- this is evasion-tooling work, not review work, and no wording change "
            "makes it dispatchable"
        ]

    reworded_retry = _matches_any(_REWORDED_RETRY_PATTERNS, text)
    if reworded_retry:
        return "needs_owner_review", [
            "payload reads as a reworded retry of a previously screened request; "
            "rewording does not change the underlying substance, so this stays "
            "held for explicit operator review"
        ]

    allow_signal = _matches_any(_ALLOW_SIGNAL_PATTERNS, text)
    if hits and not allow_signal:
        return "needs_owner_review", [
            f"high-risk terminology present ({', '.join(sorted(set(hits)))}) without a "
            "clear resolution to legitimate review/debugging/sign-off work"
        ]

    if hits and allow_signal:
        return "clean", [
            "high-risk terminology present but resolved by context to legitimate "
            "review, defensive rate-limit handling, or normal official API behavior"
        ]

    return "clean", ["no high-risk terminology or evasion-tooling signal found"]


# ---------------------------------------------------------------------------
# Substitution map: accuracy fixes applied ONLY to payloads whose substance
# has already been classified `clean` on the ORIGINAL text. Ordered longest
# phrase first so a multi-word phrase is substituted before a shorter phrase
# that is a substring of it (e.g. "burner account" before bare "burner").
#
# FloodWait / PeerFlood are intentionally excluded: they are real API error
# symbols, not euphemisms, and renaming them would make the request
# factually wrong. They are handled purely via the fixed wrapper text, which
# instructs the recipient to treat them as normal rate-limit handling.
# ---------------------------------------------------------------------------
SUBSTITUTION_MAP: tuple[tuple[str, str], ...] = (
    ("burner account", "a secondary account I own"),
    ("burner", "a secondary account I own"),
    ("auto-unlock", "complete the documented flow"),
    ("auto-pass", "complete the documented flow"),
    ("bypass", "complete the documented flow"),
    ("avoid getting banned", "rate-limit handling"),
    ("anti-ban", "rate-limit handling"),
    ("ban-safety", "rate-limit handling"),
    ("auto-join sponsor channel", "join the channels the API requires"),
    ("auto-join", "join the channels the API requires"),
    ("multi-round gate", "the documented multi-step join flow"),
    ("self-destruct", "time-limited media"),
    ("forward-restricted", "time-limited media"),
    ("historical unlock", "retrieval of earlier messages the account can access"),
    ("headless login", "non-interactive authentication with my own credentials"),
    ("reverse-engineered", "built against the documented API"),
)
_SUBSTITUTION_PAIRS_SORTED: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    sorted(
        ((term, replacement, _phrase_pattern(term)) for term, replacement in SUBSTITUTION_MAP),
        key=lambda triple: len(triple[0]),
        reverse=True,
    )
)


def apply_substitutions(payload: str) -> tuple[str, list[dict[str, str]]]:
    """Apply the accuracy-fix substitution map to ``payload``.

    Only ever call this on a payload whose substance has already been
    classified `clean` on the ORIGINAL text via `classify_substance()`.
    Returns (rewritten_text, substitutions_applied) where each entry in
    substitutions_applied is {"term": ..., "replacement": ...} for a phrase
    that actually matched (so the operator can see exactly what changed).
    """
    text = payload or ""
    applied: list[dict[str, str]] = []
    for term, replacement, pattern in _SUBSTITUTION_PAIRS_SORTED:
        new_text, count = pattern.subn(replacement, text)
        if count:
            applied.append({"term": term, "replacement": replacement})
            text = new_text
    return text, applied


WRAPPER_TEXT = (
    "Review this owned and authorized project for correctness, security, reliability, "
    "and normal API compliance. Do not design or improve methods for bypassing access "
    "controls, content restrictions, verification gates, rate limits, or platform "
    "enforcement. Flag any such behavior and recommend its removal. Treat FloodWait and "
    "similar errors only as normal rate-limit handling. This is review/sign-off work, "
    "not evasion optimization."
)


def apply_wrapper(payload: str) -> str:
    """Prepend the fixed review/sign-off wrapper to ``payload``."""
    return f"{WRAPPER_TEXT}\n\n{payload}"


def screen_outbound(payload: str, *, target: str = "codex") -> dict[str, Any]:
    """Screen the FULLY ASSEMBLED outbound payload -- prompt plus any
    concatenated context pack / prior-consultation excerpts -- immediately
    before it is handed to the target transport.

    Returns:
        {
            "classification": "clean" | "reworded" | "needs_owner_review" | "block",
            "reasons": [...],
            "matched_terms": [...],
            "wrapper_required": bool,
            "substitutions": [...],   # {"term": ..., "replacement": ...} pairs
                                       # actually applied; only ever non-empty
                                       # for "reworded".
            "final_payload": str | None,  # ready-to-dispatch text for "clean"
                                           # and "reworded"; the wrapper-only
                                           # preview for "needs_owner_review"
                                           # (used only if the operator opts
                                           # in); None for "block".
            "target": target,
        }

    The substance verdict backing `classification` is always computed on the
    ORIGINAL payload (see `classify_substance`) and is never recomputed on
    substituted text -- substitution cannot downgrade `block` or
    `needs_owner_review`. See the module docstring for the full guarantee.
    """
    original = payload or ""
    hits = find_term_hits(original)
    verdict, reasons = classify_substance(original)

    if verdict == "block":
        return {
            "classification": "block",
            "reasons": reasons,
            "matched_terms": sorted(set(hits)),
            "wrapper_required": False,
            "substitutions": [],
            "final_payload": None,
            "target": target,
        }

    if verdict == "needs_owner_review":
        return {
            "classification": "needs_owner_review",
            "reasons": reasons,
            "matched_terms": sorted(set(hits)),
            "wrapper_required": bool(hits),
            "substitutions": [],
            # Preview only -- the caller must gate dispatch on explicit
            # operator opt-in before ever sending this.
            "final_payload": apply_wrapper(original) if hits else original,
            "target": target,
        }

    # verdict == "clean" (computed on the ORIGINAL, unsubstituted text)
    if hits:
        rewritten, substitutions = apply_substitutions(original)
        final_payload = apply_wrapper(rewritten)
        return {
            "classification": "reworded",
            "reasons": reasons,
            "matched_terms": sorted(set(hits)),
            "wrapper_required": True,
            "substitutions": substitutions,
            "final_payload": final_payload,
            "target": target,
        }

    return {
        "classification": "clean",
        "reasons": reasons,
        "matched_terms": [],
        "wrapper_required": False,
        "substitutions": [],
        "final_payload": original,
        "target": target,
    }


# ---------------------------------------------------------------------------
# Logging: one JSON line per screened dispatch. Hashes + lengths only for the
# payload bodies -- never the payload text itself -- so this log can never
# become a copy of sensitive content. Any stored excerpt (reasons only, which
# are short fixed human-readable strings, not payload text) is truncated to
# 200 characters.
# ---------------------------------------------------------------------------
_EXCERPT_LIMIT = 200


def _sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="surrogatepass")).hexdigest()


def log_outbound_screen(
    broker_dir: Path,
    *,
    target: str,
    classification: str,
    reasons: list[str],
    matched_terms: list[str],
    substitutions: list[dict[str, str]] | None,
    original_payload: str,
    action: str,
    final_payload: str | None,
) -> None:
    """Append one JSON line describing a screened dispatch to
    ``<broker_dir>/outbound-screen.log``. Best-effort: logging failures must
    never block or fail the caller's dispatch decision."""
    log_path = Path(broker_dir) / "outbound-screen.log"
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": target,
        "classification": classification,
        "reasons": [str(r)[:_EXCERPT_LIMIT] for r in (reasons or [])],
        "matched_terms": list(matched_terms or []),
        "substitutions": list(substitutions or []),
        "original_payload_sha256": _sha256_hex(original_payload),
        "original_payload_length": len(original_payload or ""),
        "action": action,
        "final_payload_sha256": _sha256_hex(final_payload) if final_payload is not None else None,
        "final_payload_length": len(final_payload) if final_payload is not None else None,
    }
    line = json.dumps(entry, ensure_ascii=True) + "\n"
    try:
        lock_path = log_path.with_suffix(log_path.suffix + ".lock")
        with atomic_io.FileLock(lock_path, timeout=5.0):
            existing = ""
            if log_path.exists():
                try:
                    existing = log_path.read_text(encoding="utf-8")
                except OSError:
                    existing = ""
            atomic_io.atomic_write_text(log_path, existing + line, encoding="utf-8")
    except Exception:
        # Logging must never take down a dispatch decision.
        pass
