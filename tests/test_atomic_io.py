"""Focused stdlib-only tests for atomic_io.py (WP3a)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import atomic_io  # noqa: E402


class AtomicWriteTextTests(unittest.TestCase):
    def test_writes_content_and_leaves_no_temp_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            atomic_io.atomic_write_text(target, "hello world")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello world")
            leftovers = [p for p in Path(tmp).iterdir() if p != target]
            self.assertEqual(leftovers, [])

    def test_overwrite_uses_unique_temp_name_not_fixed_new_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            seen_tmp_names = []
            real_replace = os.replace

            def spy_replace(src, dst):
                seen_tmp_names.append(Path(src).name)
                return real_replace(src, dst)

            with mock.patch.object(atomic_io.os, "replace", side_effect=spy_replace):
                atomic_io.atomic_write_text(target, "one")
                atomic_io.atomic_write_text(target, "two")
            self.assertEqual(len(seen_tmp_names), 2)
            self.assertNotEqual(seen_tmp_names[0], seen_tmp_names[1])
            for name in seen_tmp_names:
                self.assertNotEqual(name, "out.txt.new")

    def test_replaces_existing_content_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            atomic_io.atomic_write_text(target, "first")
            atomic_io.atomic_write_text(target, "second")
            self.assertEqual(target.read_text(encoding="utf-8"), "second")

    def test_cleans_up_temp_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            with mock.patch.object(atomic_io.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    atomic_io.atomic_write_text(target, "content")
            leftover_tmp = [p for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftover_tmp, [])
            self.assertFalse(target.exists())

    def test_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            real_replace = os.replace
            attempts = 0

            def transient_replace(src, dst):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("brief scanner lock")
                return real_replace(src, dst)

            with mock.patch.object(atomic_io.os, "replace", side_effect=transient_replace):
                atomic_io.atomic_write_text(target, "content")
            self.assertEqual(attempts, 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "content")

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits aren't meaningfully preserved on Windows")
    def test_preserves_existing_posix_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            target.write_text("orig", encoding="utf-8")
            os.chmod(str(target), 0o640)
            atomic_io.atomic_write_text(target, "new content")
            mode = os.stat(str(target)).st_mode & 0o777
            self.assertEqual(mode, 0o640)

    def test_fsync_called_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            calls = []
            real_fsync = os.fsync

            def spy_fsync(fd):
                calls.append("fsync")
                return real_fsync(fd)

            real_replace = os.replace

            def spy_replace(src, dst):
                calls.append("replace")
                return real_replace(src, dst)

            with mock.patch.object(atomic_io.os, "fsync", side_effect=spy_fsync), \
                 mock.patch.object(atomic_io.os, "replace", side_effect=spy_replace):
                atomic_io.atomic_write_text(target, "data")
            self.assertEqual(calls, ["fsync", "replace"])


class FileLockTests(unittest.TestCase):
    def test_acquire_succeeds_when_unlocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            lock = atomic_io.FileLock(lock_path, timeout=1.0)
            self.assertTrue(lock.acquire())
            self.assertTrue(lock_path.exists())
            lock.release()
            self.assertFalse(lock_path.exists())

    def test_second_lock_times_out_while_first_holds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            first = atomic_io.FileLock(lock_path, timeout=1.0)
            self.assertTrue(first.acquire())
            second = atomic_io.FileLock(lock_path, timeout=0.2, poll_interval=0.05)
            self.assertFalse(second.acquire())
            first.release()

    def test_context_manager_raises_timeout_error_and_never_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            first = atomic_io.FileLock(lock_path, timeout=1.0)
            first.acquire()
            second = atomic_io.FileLock(lock_path, timeout=0.2, poll_interval=0.05)
            with self.assertRaises(TimeoutError):
                with second:
                    self.fail("must never enter the protected block after a timeout")
            first.release()

    def test_timed_out_lock_never_unlinks_owning_processs_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            first = atomic_io.FileLock(lock_path, timeout=1.0)
            self.assertTrue(first.acquire())
            second = atomic_io.FileLock(lock_path, timeout=0.2, poll_interval=0.05)
            self.assertFalse(second.acquire())
            second.release()  # must be a no-op: second never owned the lock
            self.assertTrue(lock_path.exists(), "release() on a non-owner must not delete the lock")
            first.release()
            self.assertFalse(lock_path.exists())

    def test_stale_lock_is_removed_and_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            lock_path.write_text("99999999", encoding="utf-8")
            old_time = time.time() - 3600
            os.utime(str(lock_path), (old_time, old_time))
            lock = atomic_io.FileLock(lock_path, timeout=1.0, stale_seconds=1.0)
            self.assertTrue(lock.acquire())
            lock.release()

    def test_fresh_lock_is_not_treated_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            holder = atomic_io.FileLock(lock_path, timeout=1.0)
            self.assertTrue(holder.acquire())
            waiter = atomic_io.FileLock(lock_path, timeout=0.3, poll_interval=0.05, stale_seconds=3600.0)
            self.assertFalse(waiter.acquire())
            self.assertTrue(lock_path.exists())
            holder.release()


if __name__ == "__main__":
    unittest.main()
