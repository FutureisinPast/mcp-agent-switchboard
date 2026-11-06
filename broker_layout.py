"""broker_layout.py

Predictable, date/project-organised layout for runtime files under the broker
home plus automatic retention for Agent Switchboard.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

# NEVER list: items that must never be moved or deleted by retention
NEVER: tuple[str, ...] = (
    "agent-switchboard.exe",
    "config.json",
    "state.sqlite",
    "code-graph",
    "routing-gate/*.json",
    "routing-gate/*.lock",
    "context-evidence",
    "codex-inbox",
    "claude-inbox",
    "projects",
)


def broker_home() -> Path:
    """Return the broker home directory from AGENT_BROKER_HOME or default ~/.agent-broker."""
    env = os.environ.get("AGENT_BROKER_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agent-broker"


def project_slug(project: Any) -> str:
    """Return canonical project id, or safe lowercase slug, or 'unscoped'."""
    if project is None:
        return "unscoped"
    if isinstance(project, str) and not project.strip():
        return "unscoped"

    # Try resolving via project_resolver lazily
    try:
        from project_resolver import canonical_project
        res = canonical_project(project)
        if isinstance(res, tuple) and res and res[0]:
            return str(res[0])
    except Exception:
        pass

    s = str(project).strip().lower()
    if not s:
        return "unscoped"
    # Safe lowercase slug of name ([a-z0-9._-], max 64)
    slug = re.sub(r"[^a-z0-9._-]+", "-", s)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        return "unscoped"
    return slug[:64]


def _to_local_datetime(when: float | int | datetime | None) -> datetime:
    if when is None:
        return datetime.now()
    if isinstance(when, (int, float)):
        return datetime.fromtimestamp(when)
    if isinstance(when, datetime):
        if when.tzinfo is not None:
            return when.astimezone()
        return when
    return datetime.now()


def _safe_ref(ref: Any) -> str:
    if ref is None:
        return "run"
    s = str(ref).strip()
    if not s:
        return "run"
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    clean = re.sub(r"-+", "-", clean).strip("-")
    if not clean:
        return "run"
    return clean[:80]


def run_dir(project: Any, ref: Any, when: float | int | datetime | None = None) -> Path:
    """Create and return <home>/runs/YYYY-MM-DD/<project_slug>/<HHMMSS>-<safe ref>/."""
    dt = _to_local_datetime(when)
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H%M%S")
    p_slug = project_slug(project)
    ref_slug = _safe_ref(ref)

    path = broker_home() / "runs" / date_str / p_slug / f"{time_str}-{ref_slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_day_dir(when: float | int | datetime | None = None) -> Path:
    """Create and return <home>/logs/YYYY-MM-DD/."""
    dt = _to_local_datetime(when)
    date_str = dt.strftime("%Y-%m-%d")
    path = broker_home() / "logs" / date_str
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_dir(when: float | int | datetime | None = None) -> Path:
    """Create and return <home>/archive/YYYY-MM-DD/."""
    dt = _to_local_datetime(when)
    date_str = dt.strftime("%Y-%m-%d")
    path = broker_home() / "archive" / date_str
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_never_item(path: Path, home: Path) -> bool:
    """Check if path is protected by the NEVER list."""
    try:
        rel = path.resolve().relative_to(home.resolve())
    except Exception:
        return False
    parts = rel.parts
    if not parts:
        return True
    top = parts[0]
    if len(parts) == 1:
        if top in ("agent-switchboard.exe", "config.json", "state.sqlite"):
            return True
    if top in ("code-graph", "context-evidence", "codex-inbox", "claude-inbox", "projects"):
        return True
    if top == "routing-gate":
        if not path.name.endswith(".log.jsonl"):
            return True
    return False


def _get_retention_days(env_var: str, default: int) -> int:
    val = os.environ.get(env_var)
    if val is not None:
        try:
            days = int(val.strip())
            if days >= 0:
                return days
        except (ValueError, TypeError):
            pass
    return default


def _safe_stat(p: Path) -> os.stat_result | None:
    try:
        return p.stat()
    except OSError:
        return None


def _folder_size(path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _remove_tree(path: Path) -> None:
    def _onerror(func: Any, p: Any, exc_info: Any) -> None:
        try:
            os.chmod(p, 0o777)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=_onerror)
    except OSError:
        pass


def _acquire_lock(lock_file: Path) -> int | None:
    """Acquire a non-blocking exclusive OS-level lock on lock_file. Never raises."""
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
    except OSError:
        return None

    try:
        if sys.platform == "win32":
            if msvcrt is not None:
                try:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"0")
                except OSError:
                    pass
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ValueError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _release_lock(lock_file_or_fd: Any = None, fd: int | None = None) -> None:
    """Release the OS-level lock and close the file handle. Never deletes the lock file."""
    real_fd = lock_file_or_fd if isinstance(lock_file_or_fd, int) else fd
    if real_fd is None:
        return
    try:
        if sys.platform == "win32":
            if msvcrt is not None:
                try:
                    os.lseek(real_fd, 0, os.SEEK_SET)
                    msvcrt.locking(real_fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        elif fcntl is not None:
            try:
                fcntl.flock(real_fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(real_fd)
        except OSError:
            pass


def run_retention(
    now: float | int | datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply retention rules. Never raises."""
    summary: dict[str, dict[str, int]] = {
        "tmp": {"items": 0, "bytes": 0},
        "runs": {"items": 0, "bytes": 0},
        "gate_logs": {"items": 0, "bytes": 0},
        "flash_apply": {"items": 0, "bytes": 0},
        "setup_backups": {"items": 0, "bytes": 0},
        "quarantine": {"items": 0, "bytes": 0},
        "archive": {"items": 0, "bytes": 0},
        "stray_exe": {"items": 0, "bytes": 0},
    }

    try:
        dt = _to_local_datetime(now)
        today_str = dt.strftime("%Y-%m-%d")
        now_ts = dt.timestamp()
        today_date = dt.date()

        home = broker_home()
        stamp_file = home / ".retention-stamp"
        lock_file = home / ".retention.lock"

        # Check once-per-day stamp unless force
        if not force:
            try:
                if stamp_file.is_file():
                    content = stamp_file.read_text(encoding="utf-8").strip()
                    if content == today_str:
                        return summary
            except OSError:
                pass

        # Acquire cross-process lock (return {'skipped': 'locked'} immediately if locked)
        fd = _acquire_lock(lock_file)
        if fd is None:
            return {"skipped": "locked"}

        try:
            if not force:
                try:
                    if stamp_file.is_file():
                        content = stamp_file.read_text(encoding="utf-8").strip()
                        if content == today_str:
                            return summary
                except OSError:
                    pass

            # Rule 1: tmp: <home>/tmp/*.log older than AGENT_BROKER_TMP_RETENTION_DAYS (14) -> delete.
            try:
                tmp_days = _get_retention_days("AGENT_BROKER_TMP_RETENTION_DAYS", 14)
                cutoff = now_ts - (tmp_days * 86400)
                tmp_dir = home / "tmp"
                if tmp_dir.is_dir():
                    for p in tmp_dir.iterdir():
                        if p.is_file() and p.name.endswith(".log") and not is_never_item(p, home):
                            st = _safe_stat(p)
                            if st is not None and st.st_mtime < cutoff:
                                summary["tmp"]["items"] += 1
                                summary["tmp"]["bytes"] += st.st_size
                                if not dry_run:
                                    try:
                                        p.unlink()
                                    except OSError:
                                        pass
            except Exception:
                pass

            # Rule 2: runs: <home>/runs/<YYYY-MM-DD> folders whose date is older than AGENT_BROKER_RUNS_RETENTION_DAYS (30) -> delete.
            try:
                runs_days = _get_retention_days("AGENT_BROKER_RUNS_RETENTION_DAYS", 30)
                runs_dir = home / "runs"
                if runs_dir.is_dir():
                    for p in runs_dir.iterdir():
                        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and not is_never_item(p, home):
                            is_old = False
                            try:
                                folder_date = datetime.strptime(p.name, "%Y-%m-%d").date()
                                if (today_date - folder_date).days > runs_days:
                                    is_old = True
                            except ValueError:
                                pass
                            st = _safe_stat(p)
                            if st is not None and (now_ts - st.st_mtime) > (runs_days * 86400):
                                is_old = True
                            if is_old:
                                b = _folder_size(p)
                                summary["runs"]["items"] += 1
                                summary["runs"]["bytes"] += b
                                if not dry_run:
                                    _remove_tree(p)
            except Exception:
                pass

            # Rule 3: gate_logs: <home>/routing-gate/*.log.jsonl older than AGENT_BROKER_GATE_LOG_RETENTION_DAYS (30) -> delete.
            try:
                gate_days = _get_retention_days("AGENT_BROKER_GATE_LOG_RETENTION_DAYS", 30)
                cutoff = now_ts - (gate_days * 86400)
                gate_dir = home / "routing-gate"
                if gate_dir.is_dir():
                    for p in gate_dir.iterdir():
                        if p.is_file() and p.name.endswith(".log.jsonl") and not is_never_item(p, home):
                            st = _safe_stat(p)
                            if st is not None and st.st_mtime < cutoff:
                                summary["gate_logs"]["items"] += 1
                                summary["gate_logs"]["bytes"] += st.st_size
                                if not dry_run:
                                    try:
                                        p.unlink()
                                    except OSError:
                                        pass
            except Exception:
                pass

            # Rule 4: flash_apply: <home>/flash-apply/*.json older than 30 -> delete.
            try:
                flash_days = _get_retention_days("AGENT_BROKER_FLASH_APPLY_RETENTION_DAYS", 30)
                cutoff = now_ts - (flash_days * 86400)
                flash_dir = home / "flash-apply"
                if flash_dir.is_dir():
                    for p in flash_dir.iterdir():
                        if p.is_file() and p.name.endswith(".json") and not is_never_item(p, home):
                            st = _safe_stat(p)
                            if st is not None and st.st_mtime < cutoff:
                                summary["flash_apply"]["items"] += 1
                                summary["flash_apply"]["bytes"] += st.st_size
                                if not dry_run:
                                    try:
                                        p.unlink()
                                    except OSError:
                                        pass
            except Exception:
                pass

            # Rule 5: setup_backups: keep the newest 10 folders in <home>/setup-backups (by mtime), delete the rest.
            try:
                setup_keep = _get_retention_days("AGENT_BROKER_SETUP_BACKUPS_KEEP", 10)
                setup_dir = home / "setup-backups"
                if setup_dir.is_dir():
                    folders: list[tuple[float, Path]] = []
                    for p in setup_dir.iterdir():
                        if p.is_dir() and not is_never_item(p, home):
                            st = _safe_stat(p)
                            mtime = st.st_mtime if st is not None else 0.0
                            folders.append((mtime, p))
                    folders.sort(key=lambda item: item[0], reverse=True)
                    to_delete = [p for _, p in folders[setup_keep:]]
                    for p in to_delete:
                        b = _folder_size(p)
                        summary["setup_backups"]["items"] += 1
                        summary["setup_backups"]["bytes"] += b
                        if not dry_run:
                            _remove_tree(p)
            except Exception:
                pass

            # Rule 6: quarantine: <home>/quarantine/<subfolder>/<bundle> older than 14 (AGENT_BROKER_QUARANTINE_RETENTION_DAYS) -> delete (skip the .prune.lock file).
            try:
                q_days = _get_retention_days("AGENT_BROKER_QUARANTINE_RETENTION_DAYS", 14)
                cutoff = now_ts - (q_days * 86400)
                q_dir = home / "quarantine"
                if q_dir.is_dir():
                    for sub in q_dir.iterdir():
                        if sub.is_dir() and sub.name != ".prune.lock" and not is_never_item(sub, home):
                            for bundle in sub.iterdir():
                                if bundle.name == ".prune.lock" or is_never_item(bundle, home):
                                    continue
                                if bundle.is_dir() and re.fullmatch(r"\d{8}|\d{4}-\d{2}-\d{2}", bundle.name):
                                    for subbundle in bundle.iterdir():
                                        if subbundle.name == ".prune.lock" or is_never_item(subbundle, home):
                                            continue
                                        st = _safe_stat(subbundle)
                                        if st is not None and st.st_mtime < cutoff:
                                            if subbundle.is_dir():
                                                b = _folder_size(subbundle)
                                                summary["quarantine"]["items"] += 1
                                                summary["quarantine"]["bytes"] += b
                                                if not dry_run:
                                                    _remove_tree(subbundle)
                                            else:
                                                summary["quarantine"]["items"] += 1
                                                summary["quarantine"]["bytes"] += st.st_size
                                                if not dry_run:
                                                    try:
                                                        subbundle.unlink()
                                                    except OSError:
                                                        pass
                                else:
                                    st = _safe_stat(bundle)
                                    if st is not None and st.st_mtime < cutoff:
                                        if bundle.is_dir():
                                            b = _folder_size(bundle)
                                            summary["quarantine"]["items"] += 1
                                            summary["quarantine"]["bytes"] += b
                                            if not dry_run:
                                                _remove_tree(bundle)
                                        else:
                                            summary["quarantine"]["items"] += 1
                                            summary["quarantine"]["bytes"] += st.st_size
                                            if not dry_run:
                                                try:
                                                    bundle.unlink()
                                                except OSError:
                                                    pass
            except Exception:
                pass

            # Rule 7: archive: <home>/archive/<YYYY-MM-DD> folders older than AGENT_BROKER_ARCHIVE_RETENTION_DAYS (30) -> delete.
            try:
                archive_days = _get_retention_days("AGENT_BROKER_ARCHIVE_RETENTION_DAYS", 30)
                archive_base = home / "archive"
                if archive_base.is_dir():
                    for p in archive_base.iterdir():
                        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and not is_never_item(p, home):
                            is_old = False
                            try:
                                folder_date = datetime.strptime(p.name, "%Y-%m-%d").date()
                                if (today_date - folder_date).days > archive_days:
                                    is_old = True
                            except ValueError:
                                pass
                            st = _safe_stat(p)
                            if st is not None and (now_ts - st.st_mtime) > (archive_days * 86400):
                                is_old = True
                            if is_old:
                                b = _folder_size(p)
                                summary["archive"]["items"] += 1
                                summary["archive"]["bytes"] += b
                                if not dry_run:
                                    _remove_tree(p)
            except Exception:
                pass

            # Rule 8: stray_exe: any file in the home root matching agent-switchboard*.exe or agent-switchboard.exe.* other than exactly agent-switchboard.exe: keep the newest 2 in place, move the rest into archive_dir()/old-exe/ (skip silently if locked/in use).
            try:
                stray_candidates: list[tuple[float, Path]] = []
                for p in home.iterdir():
                    if not p.is_file():
                        continue
                    name = p.name
                    if name == "agent-switchboard.exe":
                        continue
                    if (name.startswith("agent-switchboard") and name.endswith(".exe")) or name.startswith("agent-switchboard.exe."):
                        st = _safe_stat(p)
                        mtime = st.st_mtime if st is not None else 0.0
                        stray_candidates.append((mtime, p))
                stray_candidates.sort(key=lambda item: item[0], reverse=True)
                to_archive = [p for _, p in stray_candidates[2:]]
                if to_archive:
                    dest_dir = archive_dir(when=now) / "old-exe"
                    if not dry_run:
                        try:
                            dest_dir.mkdir(parents=True, exist_ok=True)
                        except OSError:
                            pass
                    for p in to_archive:
                        st = _safe_stat(p)
                        size = st.st_size if st is not None else 0
                        if dry_run:
                            summary["stray_exe"]["items"] += 1
                            summary["stray_exe"]["bytes"] += size
                        else:
                            try:
                                target = dest_dir / p.name
                                if target.exists():
                                    target.unlink(missing_ok=True)
                                shutil.move(str(p), str(target))
                                summary["stray_exe"]["items"] += 1
                                summary["stray_exe"]["bytes"] += size
                            except OSError:
                                pass
            except Exception:
                pass

            # Write stamp if not dry_run
            if not dry_run:
                try:
                    stamp_file.write_text(today_str, encoding="utf-8")
                except OSError:
                    pass

        finally:
            _release_lock(lock_file, fd)

    except Exception:
        pass

    return summary


def tidy_cli(argv: list[str] | None = None) -> int:
    """CLI wrapper: parse --dry-run and --force, call run_retention, print summary."""
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Agent Switchboard broker layout retention")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without changing files")
    parser.add_argument("--force", action="store_true", help="Force retention even if already run today")
    args = parser.parse_args(argv)

    summary = run_retention(force=args.force, dry_run=args.dry_run)
    if isinstance(summary, dict) and summary.get("skipped") == "locked":
        print("skipped: locked")
        return 0
    for rule, stats in summary.items():
        print(f"{rule}: {stats['items']} items, {stats['bytes']} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(tidy_cli())
