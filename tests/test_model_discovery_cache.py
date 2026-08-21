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
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import atomic_io  # noqa: E402


def _stamp(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


class AntigravityCatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = broker.ANTIGRAVITY_CATALOG_PATH
        self._orig_lock_path = broker.ANTIGRAVITY_CATALOG_LOCK_PATH
        self._orig_lock_timeout = broker.ANTIGRAVITY_CATALOG_LOCK_TIMEOUT_SECONDS
        broker.ANTIGRAVITY_CATALOG_PATH = Path(self._tmpdir.name) / "antigravity-models.json"
        broker.ANTIGRAVITY_CATALOG_LOCK_PATH = broker.ANTIGRAVITY_CATALOG_PATH.with_suffix(".lock")
        # Keep the timeout tiny so the lock-contention tests below stay fast.
        broker.ANTIGRAVITY_CATALOG_LOCK_TIMEOUT_SECONDS = 0.3

    def tearDown(self):
        broker.ANTIGRAVITY_CATALOG_PATH = self._orig_path
        broker.ANTIGRAVITY_CATALOG_LOCK_PATH = self._orig_lock_path
        broker.ANTIGRAVITY_CATALOG_LOCK_TIMEOUT_SECONDS = self._orig_lock_timeout
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

    # 10. Regression guard for the observed defect: many host server processes call
    # `_record_antigravity_discovery_failure()` concurrently. Before the lock, each
    # call did an unsynchronised read-modify-write, so concurrent increments raced
    # and dropped updates (the live log showed the breaker opening/clearing
    # spuriously right after a healthy save). With the lock held across the whole
    # read-modify-write, N concurrent calls must land exactly N increments -- no
    # lost updates.
    def test_concurrent_failure_recording_loses_no_updates(self):
        # This test is about correctness under real contention, not the timeout
        # path (that is covered separately below) -- give the lock enough room
        # that 25 threads queuing for a sub-millisecond critical section never
        # time each other out.
        broker.ANTIGRAVITY_CATALOG_LOCK_TIMEOUT_SECONDS = 5.0
        call_count = 25
        barrier = threading.Barrier(call_count)

        def _record():
            barrier.wait(timeout=5)
            broker._record_antigravity_discovery_failure()

        threads = [threading.Thread(target=_record) for _ in range(call_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "a recording thread did not finish in time")

        state = broker._read_antigravity_catalog_state()
        self.assertEqual(state.get("consecutive_failures"), call_count)

    # 11. A save racing a failure record must never leave the catalog file
    # truncated or invalid: whichever write lands last, the file on disk is
    # always complete, parseable JSON with the documented schema.
    def test_save_concurrent_with_failure_record_leaves_valid_json(self):
        broker.ANTIGRAVITY_CATALOG_LOCK_TIMEOUT_SECONDS = 5.0
        broker._save_antigravity_catalog(["antigravity-flash-3.7"])
        iterations = 20
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _saver():
            try:
                barrier.wait(timeout=5)
                for _ in range(iterations):
                    broker._save_antigravity_catalog(["antigravity-flash-3.7", "antigravity-pro-1.0"])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _failer():
            try:
                barrier.wait(timeout=5)
                for _ in range(iterations):
                    broker._record_antigravity_discovery_failure()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_saver), threading.Thread(target=_failer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), "a save/failure thread did not finish in time")

        self.assertEqual(errors, [])
        raw = broker.ANTIGRAVITY_CATALOG_PATH.read_text(encoding="utf-8")
        state = json.loads(raw)  # must never raise: never truncated, never partial
        self.assertIsInstance(state, dict)
        self.assertIn("slugs", state)
        self.assertIn("observed_at", state)
        self.assertIn("consecutive_failures", state)
        self.assertIn("breaker_opened_at", state)
        self.assertIsInstance(state["slugs"], list)
        self.assertIsInstance(state["consecutive_failures"], int)

    # 12a. Lock-timeout path for the failure recorder: if another holder has the
    # lock, the function must return quietly (no raise) and must not touch the
    # existing state on disk.
    def test_record_failure_returns_quietly_when_lock_unavailable(self):
        broker._save_antigravity_catalog(["antigravity-flash-3.7"])
        before = broker._read_antigravity_catalog_state()

        holder = atomic_io.FileLock(broker.ANTIGRAVITY_CATALOG_LOCK_PATH, timeout=1)
        self.assertTrue(holder.acquire())
        try:
            broker._record_antigravity_discovery_failure()  # must not raise
        finally:
            holder.release()

        after = broker._read_antigravity_catalog_state()
        self.assertEqual(after, before)

    # 12b. Lock-timeout path for the catalog saver: same guarantee -- quietly
    # skips the update rather than blocking or corrupting existing state.
    def test_save_catalog_returns_quietly_when_lock_unavailable(self):
        broker._save_antigravity_catalog(["antigravity-flash-3.7"])
        before = broker._read_antigravity_catalog_state()

        holder = atomic_io.FileLock(broker.ANTIGRAVITY_CATALOG_LOCK_PATH, timeout=1)
        self.assertTrue(holder.acquire())
        try:
            broker._save_antigravity_catalog(["antigravity-pro-1.0"])  # must not raise
        finally:
            holder.release()

        after = broker._read_antigravity_catalog_state()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
