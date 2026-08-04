"""Shared atomic-write + cross-process file lock helpers.

Dependency-free (stdlib only). Extracted from the write-then-``os.replace()``
pattern already used ad hoc in ``setup.py`` (``install_self_if_frozen``) so
every WP3 write path (installer marked blocks, role files, hook merges) uses
the same, tested primitive instead of duplicating it.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def _replace_with_retry(source: Path, target: Path) -> None:
    """Tolerate brief Windows scanner/indexer locks without hiding real errors."""
    delays = (0.0, 0.02, 0.05, 0.1)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            os.replace(str(source), str(target))
            return
        except PermissionError:
            if index == len(delays) - 1:
                raise


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically: stage to a unique sibling temp
    file, fsync it, then ``os.replace()`` it into place. Never leaves ``path``
    partially written if the process dies mid-write. Preserves ``path``'s
    existing file permissions where possible, and always cleans up the temp
    file if anything goes wrong before the replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        try:
            mode = path.stat().st_mode
        except OSError:
            pass
        else:
            try:
                os.chmod(str(tmp), mode)
            except OSError:
                pass
        _replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


DEFAULT_STALE_SECONDS = 120.0


class FileLock:
    """A minimal cross-process advisory lock backed by exclusive file creation
    (``O_CREAT | O_EXCL``). Not reentrant-safe across threads; good enough for
    the single-process installer/hook use cases here (bounded wait, and it
    never silently proceeds without the lock)."""

    def __init__(
        self,
        path: Path,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.stale_seconds = stale_seconds
        self._fd: int | None = None
        self._owned = False

    def _remove_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age <= self.stale_seconds:
            return
        # Only a lock file this old is considered abandoned (e.g. the owning
        # process crashed without releasing it). Best-effort removal; if
        # another process wins the race to recreate it, our next O_EXCL
        # create attempt will simply fail again and we keep polling.
        try:
            self.path.unlink()
        except OSError:
            pass

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode("utf-8"))
                self._owned = True
                return True
            except FileExistsError:
                self._remove_if_stale()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        # Only unlink the lock file if we're the process that created it --
        # never remove a lock another (still-live) process currently owns.
        if self._owned:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._owned = False

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise TimeoutError(f"Timed out waiting for lock: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
