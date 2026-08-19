"""Focused stdlib-only tests for the Antigravity model-discovery cache (WP-F4-TESTS).

Regression under test: `agy models` is a network round trip that used to run on
essentially every route with a 75s timeout, and the previously observed catalog
was only consulted AFTER the probe failed -- so the correct model list sat on
disk while every request waited out the timeout. Worse, a failed/slow probe used
to fall straight back to the bundled static list, silently DOWNGRADING the model
(3.7 -> 3.6). The fix consults the disk catalog first and opens a circuit breaker
after repeated failures, while never dropping the previously observed slugs.

ISOLATION: every test monkeypatches `agent_broker_mcp.ANTIGRAVITY_CATALOG_PATH` to
a file inside a TemporaryDirectory and restores the original value in teardown.
No test reads or writes the real `~/.agent-broker/antigravity-models.json`. No
test calls `discover_antigravity_models()` (real network call) or invokes `agy`.
"""
from __future__ import annotations

import calendar
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def _stamp(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


class AntigravityCatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = broker.ANTIGRAVITY_CATALOG_PATH
        broker.ANTIGRAVITY_CATALOG_PATH = Path(self._tmpdir.name) / "antigravity-models.json"

    def tearDown(self):
        broker.ANTIGRAVITY_CATALOG_PATH = self._orig_path
        self._tmpdir.cleanup()

    def _write_state(self, state: dict) -> None:
        broker.ANTIGRAVITY_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        broker.ANTIGRAVITY_CATALOG_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 1. No catalog ever observed -> must probe (the probe is the only source of truth).
    def test_should_probe_when_no_catalog_ever_observed(self):
        self.assertFalse(broker.ANTIGRAVITY_CATALOG_PATH.exists())
        self.assertTrue(broker._should_probe_antigravity_models())

    # 2. Fresh catalog -> must NOT probe. Guards the 75s-per-route regression where
    # a live-observed disk catalog was ignored and every route paid the full timeout.
    def test_should_not_probe_when_catalog_is_fresh(self):
        self._write_state(
            {
                "slugs": ["antigravity-flash-3.7"],
                "observed_at": broker.utc_now(),
                "consecutive_failures": 0,
                "breaker_opened_at": None,
            }
        )
        self.assertFalse(broker._should_probe_antigravity_models())

    # 3. Catalog older than the soft TTL -> must probe again.
    def test_should_probe_when_catalog_older_than_soft_ttl(self):
        stale_epoch = time.time() - (broker.ANTIGRAVITY_CATALOG_SOFT_TTL_SECONDS + 60)
        self._write_state(
            {
                "slugs": ["antigravity-flash-3.7"],
                "observed_at": _stamp(stale_epoch),
                "consecutive_failures": 0,
                "breaker_opened_at": None,
            }
        )
        self.assertTrue(broker._should_probe_antigravity_models())

    # 4. Breaker open, even with a stale catalog -> must NOT probe. Guards re-paying
    # the timeout against a binary that is not answering.
    def test_should_not_probe_when_breaker_open_even_if_catalog_stale(self):
        stale_epoch = time.time() - (broker.ANTIGRAVITY_CATALOG_SOFT_TTL_SECONDS + 60)
        self._write_state(
            {
                "slugs": ["antigravity-flash-3.7"],
                "observed_at": _stamp(stale_epoch),
                "consecutive_failures": 5,
                "breaker_opened_at": broker.utc_now(),
            }
        )
        self.assertTrue(broker._antigravity_breaker_open(broker._read_antigravity_catalog_state()))
        self.assertFalse(broker._should_probe_antigravity_models())

    # 5. Two consecutive failures open the breaker (threshold=2).
    def test_two_failures_open_the_breaker(self):
        broker._record_antigravity_discovery_failure()
        state = broker._read_antigravity_catalog_state()
        self.assertEqual(state.get("consecutive_failures"), 1)
        self.assertIsNone(state.get("breaker_opened_at"))
        self.assertFalse(broker._antigravity_breaker_open(state))

        broker._record_antigravity_discovery_failure()
        state = broker._read_antigravity_catalog_state()
        self.assertEqual(state.get("consecutive_failures"), 2)
        self.assertIsNotNone(state.get("breaker_opened_at"))
        self.assertTrue(broker._antigravity_breaker_open(state))

    # 6. CRITICAL: after failures open the breaker, the previously observed slugs
    # must still load. Dropping them forces the bundled static list -- the silent
    # model downgrade (3.7 -> 3.6) this whole mechanism exists to prevent.
    def test_load_catalog_keeps_observed_slugs_after_breaker_opens(self):
        broker._save_antigravity_catalog(["antigravity-flash-3.7"])
        broker._record_antigravity_discovery_failure()
        broker._record_antigravity_discovery_failure()
        state = broker._read_antigravity_catalog_state()
        self.assertTrue(broker._antigravity_breaker_open(state))
        self.assertEqual(broker._load_antigravity_catalog(), ["antigravity-flash-3.7"])

    # 7. A successful save resets consecutive_failures and clears breaker_opened_at.
    def test_save_catalog_resets_failures_and_clears_breaker(self):
        broker._record_antigravity_discovery_failure()
        broker._record_antigravity_discovery_failure()
        state = broker._read_antigravity_catalog_state()
        self.assertTrue(broker._antigravity_breaker_open(state))

        broker._save_antigravity_catalog(["antigravity-flash-3.7", "antigravity-pro-1.0"])
        state = broker._read_antigravity_catalog_state()
        self.assertEqual(state.get("consecutive_failures"), 0)
        self.assertIsNone(state.get("breaker_opened_at"))
        self.assertFalse(broker._antigravity_breaker_open(state))
        self.assertEqual(
            broker._load_antigravity_catalog(),
            ["antigravity-flash-3.7", "antigravity-pro-1.0"],
        )

    # 8. The breaker auto-closes once ANTIGRAVITY_DISCOVERY_BREAKER_SECONDS elapse.
    def test_breaker_closes_after_breaker_seconds_elapse(self):
        old_epoch = time.time() - (broker.ANTIGRAVITY_DISCOVERY_BREAKER_SECONDS + 30)
        state = {
            "slugs": ["antigravity-flash-3.7"],
            "observed_at": broker.utc_now(),
            "consecutive_failures": 2,
            "breaker_opened_at": _stamp(old_epoch),
        }
        self.assertFalse(broker._antigravity_breaker_open(state))

    # 9a. Status reports catalog_fresh=True for a freshly observed catalog.
    def test_status_reports_fresh_true_for_fresh_catalog(self):
        broker._save_antigravity_catalog(["antigravity-flash-3.7"])
        status = broker.antigravity_catalog_status()
        self.assertTrue(status["catalog_fresh"])
        self.assertEqual(status["catalog_source"], "disk_cache")
        self.assertFalse(status["discovery_breaker_open"])
        self.assertEqual(status["consecutive_discovery_failures"], 0)

    # 9b. Status reports catalog_fresh=False for a stale catalog.
    def test_status_reports_fresh_false_for_stale_catalog(self):
        stale_epoch = time.time() - (broker.ANTIGRAVITY_CATALOG_SOFT_TTL_SECONDS + 60)
        self._write_state(
            {
                "slugs": ["antigravity-flash-3.7"],
                "observed_at": _stamp(stale_epoch),
                "consecutive_failures": 0,
                "breaker_opened_at": None,
            }
        )
        status = broker.antigravity_catalog_status()
        self.assertFalse(status["catalog_fresh"])
        self.assertEqual(status["catalog_source"], "disk_cache")

    # 9c. Status never raises on a missing catalog file (bundled_static source, no crash).
    def test_status_never_raises_on_missing_catalog_file(self):
        self.assertFalse(broker.ANTIGRAVITY_CATALOG_PATH.exists())
        status = broker.antigravity_catalog_status()
        self.assertEqual(status["catalog_source"], "bundled_static")
        self.assertIsNone(status["catalog_observed_at"])
        self.assertIsNone(status["catalog_age_seconds"])
        self.assertFalse(status["catalog_fresh"])

    # 9d. Status never raises on a corrupt (non-JSON) catalog file.
    def test_status_never_raises_on_corrupt_catalog_file(self):
        broker.ANTIGRAVITY_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        broker.ANTIGRAVITY_CATALOG_PATH.write_text("{not valid json::", encoding="utf-8")
        status = broker.antigravity_catalog_status()
        self.assertEqual(status["catalog_source"], "bundled_static")
        self.assertFalse(status["catalog_fresh"])
        self.assertFalse(status["discovery_breaker_open"])


if __name__ == "__main__":
    unittest.main()
