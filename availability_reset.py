"""Provider availability reset parsing, latch duration, and UTC formatting.

Stdlib only; no function in this module ever raises.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone

_UNIT_MULTIPLIERS = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}

_KEY_VALUE_RESETS_AT_RE = re.compile(
    r"""["']?(?:resetsAt|resets_at|reset_at|resetAt)["']?\s*[:=]\s*["']?([^"',;}\s]+)["']?""",
    re.IGNORECASE,
)
_KEY_VALUE_RETRY_AFTER_RE = re.compile(
    r"""["']?(?:retry_after|retryAfter|retry-after)["']?\s*[:=]\s*["']?(\d+(?:\.\d+)?)["']?""",
    re.IGNORECASE,
)
_TRY_AGAIN_IN_RE = re.compile(
    r"\btry\s+again\s+in\s+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|day|days?)\b",
    re.IGNORECASE,
)
_RETRY_AFTER_PHRASE_RE = re.compile(
    r"\bretry\s+after\s+(\d+(?:\.\d+)?)(?:\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|day|days?))?\b",
    re.IGNORECASE,
)
_CLOCK_RESET_RE = re.compile(
    r"\b(?:resets?\s+at|resets)\s+(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?(?:\s*\(?\s*(UTC|GMT|Z|[A-Za-z]+/[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)?)\b\s*\)?)?",
    re.IGNORECASE,
)
_RESET_ISO_RE = re.compile(
    r"\breset\w*\b.{0,40}?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE | re.DOTALL,
)


def _validate_epoch(candidate: float | None, now: float) -> float | None:
    if candidate is None:
        return None
    try:
        val = float(candidate)
        if val <= now:
            return None
        if val > now + 8 * 86400.0:
            return None
        return val
    except (TypeError, ValueError):
        return None


def parse_reset_epoch(text: str | None, now: float | None = None) -> float | None:
    """Parse epoch seconds of provider reset.

    Supports:
    (a) key/value forms: resetsAt / resets_at / reset_at / resetAt (epoch or ISO-8601)
        and retry_after / retryAfter / retry-after (relative seconds).
    (b) 'try again in N <unit>' with units s/sec/second(s), m/min/minute(s), h/hr/hour(s), d/day(s).
    (c) 'retry after N [unit]' (default seconds).
    (d) 'reset(s) at HH:MM[ am|pm]' and 'reset(s) at H am|pm' and 'resets HH:MM' (UTC/GMT/Z or local; rollover).
    (e) ISO-8601 timestamp within 40 chars after 'reset'.

    Returns None for past/present epochs or results more than 8 days ahead. Never raises.
    """
    try:
        if not text or not isinstance(text, str):
            return None
        t_now = time.time() if now is None else float(now)
    except Exception:
        return None

    try:
        # (a) key/value retry_after
        for match in _KEY_VALUE_RETRY_AFTER_RE.finditer(text):
            try:
                seconds = float(match.group(1))
                validated = _validate_epoch(t_now + seconds, t_now)
                if validated is not None:
                    return validated
            except (ValueError, TypeError):
                continue

        # (a) key/value resets_at
        for match in _KEY_VALUE_RESETS_AT_RE.finditer(text):
            raw_val = match.group(1).strip()
            # Try numeric
            try:
                num = float(raw_val)
                epoch = num / 1000.0 if num > 1e12 else num
                validated = _validate_epoch(epoch, t_now)
                if validated is not None:
                    return validated
            except (ValueError, TypeError):
                pass
            # Try ISO-8601
            try:
                iso_clean = raw_val.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                validated = _validate_epoch(dt.timestamp(), t_now)
                if validated is not None:
                    return validated
            except Exception:
                pass

        # (b) try again in N <unit>
        for match in _TRY_AGAIN_IN_RE.finditer(text):
            try:
                amount = float(match.group(1))
                unit = match.group(2).lower()
                multiplier = _UNIT_MULTIPLIERS.get(unit, 1.0)
                validated = _validate_epoch(t_now + amount * multiplier, t_now)
                if validated is not None:
                    return validated
            except (ValueError, TypeError):
                continue

        # (c) retry after N [unit]
        for match in _RETRY_AFTER_PHRASE_RE.finditer(text):
            try:
                amount = float(match.group(1))
                unit = match.group(2).lower() if match.group(2) else "s"
                multiplier = _UNIT_MULTIPLIERS.get(unit, 1.0)
                validated = _validate_epoch(t_now + amount * multiplier, t_now)
                if validated is not None:
                    return validated
            except (ValueError, TypeError):
                continue

        # (d) 'reset(s) at HH:MM[ am|pm]' / 'reset(s) at H am|pm' / 'resets HH:MM'
        for match in _CLOCK_RESET_RE.finditer(text):
            try:
                hour = int(match.group(1))
                min_str = match.group(2)
                ampm_str = match.group(3)
                tz_str = match.group(4)

                # If minute is absent and am/pm is absent, not a valid clock reset pattern
                if min_str is None and ampm_str is None:
                    continue

                minute = int(min_str) if min_str is not None else 0
                if ampm_str:
                    ampm = ampm_str.lower()
                    if ampm == "pm" and hour < 12:
                        hour += 12
                    elif ampm == "am" and hour == 12:
                        hour = 0

                if hour > 23 or minute > 59:
                    continue

                is_utc = bool(tz_str and tz_str.upper() in {"UTC", "GMT", "Z"})
                zone = None
                if tz_str and "/" in tz_str:
                    try:  # IANA name such as Region/City; unknown or no tz database -> local time
                        from zoneinfo import ZoneInfo
                        zone = ZoneInfo(tz_str)
                    except Exception:
                        zone = None
                if is_utc or zone is not None:
                    dt_now = datetime.fromtimestamp(t_now, tz=timezone.utc if is_utc else zone)
                    target = dt_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target.timestamp() <= t_now:
                        target += timedelta(days=1)
                    epoch = target.timestamp()
                else:
                    dt_now = datetime.fromtimestamp(t_now).astimezone()
                    target = dt_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target.timestamp() <= t_now:
                        target += timedelta(days=1)
                    epoch = target.timestamp()

                validated = _validate_epoch(epoch, t_now)
                if validated is not None:
                    return validated
            except Exception:
                continue

        # (e) ISO-8601 timestamp within 40 chars after the word reset
        for match in _RESET_ISO_RE.finditer(text):
            try:
                raw_iso = match.group(1).strip().replace(" ", "T")
                iso_clean = raw_iso.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                validated = _validate_epoch(dt.timestamp(), t_now)
                if validated is not None:
                    return validated
            except Exception:
                continue

        return None
    except Exception:
        return None


def latch_until(kind: str, text: str | None = None, now: float | None = None) -> float:
    """Compute timestamp until which provider should be latched.

    For kind 'quota', uses parsed reset epoch or now + AGENT_BROKER_QUOTA_LATCH_MINUTES
    (default 30). For other kinds, uses now + AGENT_BROKER_UNAVAILABLE_RECHECK_MINUTES
    (default 60). Never raises.
    """
    try:
        t_now = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        t_now = time.time()

    kind_str = str(kind or "").strip().lower()
    if kind_str == "quota":
        parsed = parse_reset_epoch(text, now=t_now)
        if parsed is not None:
            return float(parsed)
        minutes = 30.0
        env_val = os.environ.get("AGENT_BROKER_QUOTA_LATCH_MINUTES")
        if env_val is not None:
            try:
                val = float(env_val.strip())
                if val > 0:
                    minutes = val
            except (ValueError, TypeError):
                pass
        return t_now + minutes * 60.0

    minutes = 60.0
    env_val = os.environ.get("AGENT_BROKER_UNAVAILABLE_RECHECK_MINUTES")
    if env_val is not None:
        try:
            val = float(env_val.strip())
            if val > 0:
                minutes = val
        except (ValueError, TypeError):
            pass
    return t_now + minutes * 60.0


def fmt_utc(epoch: float, now: float | None = None) -> str:
    """Format epoch as 'HH:MM UTC' if same UTC date as now, else 'Mon DD HH:MM UTC'.

    Never raises.
    """
    try:
        t_now = time.time() if now is None else float(now)
        t_epoch = float(epoch)
        dt_epoch = datetime.fromtimestamp(t_epoch, tz=timezone.utc)
        dt_now = datetime.fromtimestamp(t_now, tz=timezone.utc)
        if dt_epoch.date() == dt_now.date():
            return dt_epoch.strftime("%H:%M UTC")
        return dt_epoch.strftime("%b %d %H:%M UTC")
    except Exception:
        return ""
