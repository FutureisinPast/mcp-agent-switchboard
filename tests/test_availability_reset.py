"""Tests for availability_reset.py."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import availability_reset  # noqa: E402


class AvailabilityResetTests(unittest.TestCase):
    def setUp(self):
        # Fixed base timestamp for deterministic tests:
        # 2026-10-07 12:00:00 UTC
        self.base_dt_utc = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.now = self.base_dt_utc.timestamp()

    def test_key_value_resets_at_numeric_seconds(self):
        future_epoch = self.now + 3600.0
        for key in ("resetsAt", "resets_at", "reset_at", "resetAt"):
            with self.subTest(key=key):
                text = f'{{"{key}": {future_epoch}}}'
                result = availability_reset.parse_reset_epoch(text, now=self.now)
                self.assertIsNotNone(result)
                self.assertAlmostEqual(result, future_epoch, places=2)

    def test_key_value_resets_at_numeric_milliseconds(self):
        future_epoch = self.now + 3600.0
        future_ms = int(future_epoch * 1000)
        text = f"resets_at: {future_ms}"
        result = availability_reset.parse_reset_epoch(text, now=self.now)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, future_epoch, places=2)

    def test_key_value_resets_at_iso8601(self):
        future_iso = "2026-10-07T15:30:00Z"
        expected_epoch = datetime(2026, 10, 7, 15, 30, 0, tzinfo=timezone.utc).timestamp()
        text = f'resets_at = "{future_iso}"'
        result = availability_reset.parse_reset_epoch(text, now=self.now)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, expected_epoch, places=2)

    def test_key_value_retry_after_seconds(self):
        for key in ("retry_after", "retryAfter", "retry-after"):
            with self.subTest(key=key):
                text = f"{key}: 120"
                result = availability_reset.parse_reset_epoch(text, now=self.now)
                self.assertIsNotNone(result)
                self.assertAlmostEqual(result, self.now + 120.0, places=2)

    def test_try_again_in_units(self):
        cases = (
            ("try again in 45 s", 45.0),
            ("try again in 30 sec", 30.0),
            ("try again in 1 second", 1.0),
            ("try again in 10 seconds", 10.0),
            ("try again in 5 m", 300.0),
            ("try again in 2 min", 120.0),
            ("try again in 1 minute", 60.0),
            ("try again in 15 minutes", 900.0),
            ("try again in 2 h", 7200.0),
            ("try again in 1 hr", 3600.0),
            ("try again in 3 hours", 10800.0),
            ("try again in 1 d", 86400.0),
            ("try again in 2 days", 172800.0),
        )
        for phrase, offset in cases:
            with self.subTest(phrase=phrase):
                result = availability_reset.parse_reset_epoch(phrase, now=self.now)
                self.assertIsNotNone(result)
                self.assertAlmostEqual(result, self.now + offset, places=2)

    def test_retry_after_units_and_default(self):
        # Default unit is seconds
        res_default = availability_reset.parse_reset_epoch("retry after 90", now=self.now)
        self.assertIsNotNone(res_default)
        self.assertAlmostEqual(res_default, self.now + 90.0, places=2)

        # Explicit units
        res_min = availability_reset.parse_reset_epoch("retry after 5 minutes", now=self.now)
        self.assertIsNotNone(res_min)
        self.assertAlmostEqual(res_min, self.now + 300.0, places=2)

        res_hr = availability_reset.parse_reset_epoch("retry after 1 hr", now=self.now)
        self.assertIsNotNone(res_hr)
        self.assertAlmostEqual(res_hr, self.now + 3600.0, places=2)

    def test_clock_reset_utc_rollover(self):
        # now is 12:00:00 UTC
        # 14:30 UTC is later today
        res_today = availability_reset.parse_reset_epoch("resets at 14:30 UTC", now=self.now)
        expected_today = datetime(2026, 10, 7, 14, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNotNone(res_today)
        self.assertAlmostEqual(res_today, expected_today, places=2)

        # 10:00 UTC is earlier today, so rolls over to tomorrow
        res_tomorrow = availability_reset.parse_reset_epoch("reset at 10:00 am UTC", now=self.now)
        expected_tomorrow = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNotNone(res_tomorrow)
        self.assertAlmostEqual(res_tomorrow, expected_tomorrow, places=2)

        # 12:00 UTC (exact clock match <= now) rolls over to tomorrow
        res_exact = availability_reset.parse_reset_epoch("resets at 12:00 UTC", now=self.now)
        expected_exact = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNotNone(res_exact)
        self.assertAlmostEqual(res_exact, expected_exact, places=2)

        # GMT and Z synonyms
        res_gmt = availability_reset.parse_reset_epoch("resets 16:00 GMT", now=self.now)
        expected_gmt = datetime(2026, 10, 7, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(res_gmt, expected_gmt, places=2)

        res_z = availability_reset.parse_reset_epoch("resets 16:00 Z", now=self.now)
        self.assertAlmostEqual(res_z, expected_gmt, places=2)

    def test_clock_reset_h_am_pm(self):
        # reset at H am|pm
        res_pm = availability_reset.parse_reset_epoch("reset at 3 pm UTC", now=self.now)
        expected_pm = datetime(2026, 10, 7, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(res_pm, expected_pm, places=2)

        res_am = availability_reset.parse_reset_epoch("resets at 9 am UTC", now=self.now)
        # 9 am UTC < 12 pm UTC -> tomorrow
        expected_am = datetime(2026, 10, 8, 9, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(res_am, expected_am, places=2)

    def test_clock_reset_local_time(self):
        # When no timezone token is given, interpret as local time
        now_local = datetime.fromtimestamp(self.now).astimezone()
        # Choose target 2 hours ahead in local time
        target_local = now_local.replace(hour=(now_local.hour + 2) % 24, minute=0, second=0, microsecond=0)
        if target_local.timestamp() <= self.now:
            target_local += timedelta(days=1)
        ampm = "am" if target_local.hour < 12 else "pm"
        hour_12 = target_local.hour % 12 or 12
        text = f"resets at {hour_12}:00 {ampm}"
        res = availability_reset.parse_reset_epoch(text, now=self.now)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res, target_local.timestamp(), places=2)

    def test_iso8601_after_reset_word(self):
        future_iso = "2026-10-08T18:00:00Z"
        expected = datetime(2026, 10, 8, 18, 0, 0, tzinfo=timezone.utc).timestamp()
        text = f"Your quota limit will reset at approximate time {future_iso}."
        result = availability_reset.parse_reset_epoch(text, now=self.now)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, expected, places=2)

    def test_past_and_too_far_bounds(self):
        # Absolute result in the past -> None
        past_iso = "2026-10-06T12:00:00Z"
        self.assertIsNone(
            availability_reset.parse_reset_epoch(f"reset at {past_iso}", now=self.now)
        )

        # More than 8 days ahead -> None
        nine_days_future = self.base_dt_utc + timedelta(days=9)
        too_far_iso = nine_days_future.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(
            availability_reset.parse_reset_epoch(f"reset at {too_far_iso}", now=self.now)
        )
        self.assertIsNone(
            availability_reset.parse_reset_epoch("retry after 800000", now=self.now)
        )

    def test_latch_until_quota_with_text_and_fallback(self):
        # Quota with parseable reset text
        text = "resets at 14:00 UTC"
        expected = datetime(2026, 10, 7, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        res = availability_reset.latch_until("quota", text, now=self.now)
        self.assertAlmostEqual(res, expected, places=2)

        # Quota with unparseable text -> default 30 min
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_BROKER_QUOTA_LATCH_MINUTES", None)
            res_default = availability_reset.latch_until("quota", "unparseable", now=self.now)
            self.assertAlmostEqual(res_default, self.now + 1800.0, places=2)

        # Quota with env override
        with mock.patch.dict(os.environ, {"AGENT_BROKER_QUOTA_LATCH_MINUTES": "45"}):
            res_override = availability_reset.latch_until("quota", "unparseable", now=self.now)
            self.assertAlmostEqual(res_override, self.now + 2700.0, places=2)

    def test_latch_until_non_quota_kinds(self):
        for kind in ("subscription_disabled", "organization_disabled", "cli_missing", "family_access_unavailable", "provider_unavailable"):
            with self.subTest(kind=kind):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("AGENT_BROKER_UNAVAILABLE_RECHECK_MINUTES", None)
                    res = availability_reset.latch_until(kind, "irrelevant text", now=self.now)
                    self.assertAlmostEqual(res, self.now + 3600.0, places=2)

        # Env override for recheck
        with mock.patch.dict(os.environ, {"AGENT_BROKER_UNAVAILABLE_RECHECK_MINUTES": "90"}):
            res_override = availability_reset.latch_until("provider_unavailable", None, now=self.now)
            self.assertAlmostEqual(res_override, self.now + 5400.0, places=2)

    def test_bad_env_values_fall_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_QUOTA_LATCH_MINUTES": "invalid-non-numeric"}):
            res_quota = availability_reset.latch_until("quota", None, now=self.now)
            self.assertAlmostEqual(res_quota, self.now + 1800.0, places=2)

        with mock.patch.dict(os.environ, {"AGENT_BROKER_UNAVAILABLE_RECHECK_MINUTES": "-10"}):
            res_unavail = availability_reset.latch_until("provider_unavailable", None, now=self.now)
            self.assertAlmostEqual(res_unavail, self.now + 3600.0, places=2)

    def test_fmt_utc(self):
        # Same date in UTC: HH:MM UTC
        same_date_epoch = datetime(2026, 10, 7, 14, 45, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            availability_reset.fmt_utc(same_date_epoch, now=self.now),
            "14:45 UTC",
        )

        # Different date in UTC: Mon DD HH:MM UTC
        next_date_epoch = datetime(2026, 10, 8, 9, 15, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            availability_reset.fmt_utc(next_date_epoch, now=self.now),
            "Oct 08 09:15 UTC",
        )

    def test_garbage_input_never_raises(self):
        for bad in (None, "", "     ", 123, [], {}, "some random error message with no numbers"):
            self.assertIsNone(availability_reset.parse_reset_epoch(bad, now=self.now))

        self.assertIsInstance(availability_reset.latch_until(None, None, now="invalid"), float)
        self.assertEqual(availability_reset.fmt_utc("invalid", now=None), "")


if __name__ == "__main__":
    unittest.main()


def test_parenthesised_utc_clock_reset():
    from datetime import datetime as _dt, timezone as _tz
    import availability_reset as _ar
    now = _dt(2026, 10, 6, 15, 56, tzinfo=_tz.utc).timestamp()
    expected = _dt(2026, 10, 6, 17, 0, tzinfo=_tz.utc).timestamp()
    assert _ar.parse_reset_epoch("Your limit resets at 5pm (UTC).", now=now) == expected
    assert _ar.parse_reset_epoch("limit will reset at 5pm (GMT)", now=now) == expected


def test_iana_zone_clock_reset_or_local_fallback():
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import availability_reset as _ar
    now = _dt(2026, 10, 6, 6, 0, tzinfo=_tz.utc).timestamp()
    got = _ar.parse_reset_epoch("Your limit will reset at 5pm (Etc/GMT-7)", now=now)
    assert got is not None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Etc/GMT-7")
    except Exception:
        return  # no tz database on this interpreter: local-time fallback is the documented behaviour
    assert got == _dt(2026, 10, 6, 10, 0, tzinfo=_tz.utc).timestamp()  # 17:00 at UTC+7
