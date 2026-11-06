"""tests/test_broker_layout.py

Unit tests for broker_layout.py.
Covers all layout helpers, retention rules, CLI wrapper, and safety constraints.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
import pytest

import broker_layout as layout


def _set_age_days(path: Path, days: float, base_time: float | None = None) -> None:
    now_ts = base_time if base_time is not None else time.time()
    target_ts = now_ts - (days * 86400)
    os.utime(str(path), (target_ts, target_ts))


def test_broker_home_env_and_default(tmp_path, monkeypatch):
    custom_home = tmp_path / "custom-broker-home"
    monkeypatch.setenv("AGENT_BROKER_HOME", str(custom_home))
    assert layout.broker_home() == custom_home

    monkeypatch.delenv("AGENT_BROKER_HOME", raising=False)
    default_home = layout.broker_home()
    assert default_home == Path.home() / ".agent-broker"


def test_project_slug_shapes():
    assert layout.project_slug(None) == "unscoped"
    assert layout.project_slug("") == "unscoped"
    assert layout.project_slug("   ") == "unscoped"
    assert layout.project_slug("###@@@$$$") == "unscoped"
    assert layout.project_slug("My-Test-Project") == "my-test-project"
    assert layout.project_slug("Project / Foo_Bar.1") == "project-foo_bar.1"

    long_name = "x" * 100
    slug = layout.project_slug(long_name)
    assert len(slug) == 64
    assert slug == "x" * 64


def test_project_slug_canonical_resolution(monkeypatch):
    # If project_resolver canonical_project resolves, use its result
    class DummyResolver:
        @staticmethod
        def canonical_project(name, **kwargs):
            if name == "known-alias":
                return ("resolved-canonical-id", "alias", [])
            return (None, "unregistered", [])

    import sys
    monkeypatch.setitem(sys.modules, "project_resolver", DummyResolver)
    assert layout.project_slug("known-alias") == "resolved-canonical-id"
    assert layout.project_slug("unregistered-one") == "unregistered-one"


def test_run_dir_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))

    fixed_time = 1700000000.0  # 2023-11-14T22:13:20 local or similar
    dt = datetime.fromtimestamp(fixed_time)
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H%M%S")

    p = layout.run_dir("My Project", "test-step", when=fixed_time)
    expected = tmp_path / "runs" / date_str / "my-project" / f"{time_str}-test-step"
    assert p == expected
    assert p.is_dir()

    # Unscoped and None ref fallback
    p_unscoped = layout.run_dir(None, None, when=fixed_time)
    expected_unscoped = tmp_path / "runs" / date_str / "unscoped" / f"{time_str}-run"
    assert p_unscoped == expected_unscoped
    assert p_unscoped.is_dir()

    # Safe ref slugging and max 80 chars
    long_ref = "A" * 120
    p_long = layout.run_dir("proj", long_ref, when=fixed_time)
    assert p_long.name == f"{time_str}-" + ("A" * 80)
    assert p_long.is_dir()


def test_log_day_dir_and_archive_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))

    fixed_time = 1700000000.0
    dt = datetime.fromtimestamp(fixed_time)
    date_str = dt.strftime("%Y-%m-%d")

    log_dir = layout.log_day_dir(when=fixed_time)
    assert log_dir == tmp_path / "logs" / date_str
    assert log_dir.is_dir()

    arch_dir = layout.archive_dir(when=fixed_time)
    assert arch_dir == tmp_path / "archive" / date_str
    assert arch_dir.is_dir()


def test_rule_1_tmp_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    old_log = tmp_dir / "old_job.log"
    old_log.write_text("old log content", encoding="utf-8")
    _set_age_days(old_log, 20)

    new_log = tmp_dir / "new_job.log"
    new_log.write_text("new log content", encoding="utf-8")
    _set_age_days(new_log, 2)

    non_log = tmp_dir / "old_job.txt"
    non_log.write_text("not a log", encoding="utf-8")
    _set_age_days(non_log, 20)

    res = layout.run_retention(force=True)
    assert res["tmp"]["items"] == 1
    assert res["tmp"]["bytes"] == len("old log content")
    assert not old_log.exists()
    assert new_log.exists()
    assert non_log.exists()


def test_rule_2_runs_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)

    old_run = runs_dir / "2026-08-01" / "proj1" / "010000-run"
    old_run.mkdir(parents=True)
    (old_run / "artifact.txt").write_text("data1234", encoding="utf-8")
    _set_age_days(runs_dir / "2026-08-01", 45)

    new_run = runs_dir / "2026-10-10" / "proj1" / "010000-run"
    new_run.mkdir(parents=True)
    (new_run / "artifact.txt").write_text("data5678", encoding="utf-8")
    _set_age_days(runs_dir / "2026-10-10", 5)

    non_date_run = runs_dir / "non-date-folder"
    non_date_run.mkdir(parents=True)
    (non_date_run / "file.txt").write_text("data", encoding="utf-8")
    _set_age_days(non_date_run, 45)

    res = layout.run_retention(force=True)
    assert res["runs"]["items"] == 1
    assert res["runs"]["bytes"] == len("data1234")
    assert not (runs_dir / "2026-08-01").exists()
    assert (runs_dir / "2026-10-10").exists()
    assert non_date_run.exists()


def test_rule_3_gate_logs_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    gate_dir = tmp_path / "routing-gate"
    gate_dir.mkdir(parents=True)

    old_log = gate_dir / "events.log.jsonl"
    old_log.write_text("log line 1\n", encoding="utf-8")
    _set_age_days(old_log, 40)

    new_log = gate_dir / "recent.log.jsonl"
    new_log.write_text("log line 2\n", encoding="utf-8")
    _set_age_days(new_log, 5)

    protected_json = gate_dir / "policy.json"
    protected_json.write_text("{}", encoding="utf-8")
    _set_age_days(protected_json, 40)

    protected_lock = gate_dir / "gate.lock"
    protected_lock.write_text("1234", encoding="utf-8")
    _set_age_days(protected_lock, 40)

    res = layout.run_retention(force=True)
    assert res["gate_logs"]["items"] == 1
    assert not old_log.exists()
    assert new_log.exists()
    assert protected_json.exists()
    assert protected_lock.exists()


def test_rule_4_flash_apply_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    fa_dir = tmp_path / "flash-apply"
    fa_dir.mkdir(parents=True)

    old_json = fa_dir / "patch1.json"
    old_json.write_text('{"status": "done"}', encoding="utf-8")
    _set_age_days(old_json, 40)

    new_json = fa_dir / "patch2.json"
    new_json.write_text('{"status": "running"}', encoding="utf-8")
    _set_age_days(new_json, 5)

    non_json = fa_dir / "old_notes.txt"
    non_json.write_text("notes", encoding="utf-8")
    _set_age_days(non_json, 40)

    res = layout.run_retention(force=True)
    assert res["flash_apply"]["items"] == 1
    assert not old_json.exists()
    assert new_json.exists()
    assert non_json.exists()


def test_rule_5_setup_backups_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    sb_dir = tmp_path / "setup-backups"
    sb_dir.mkdir(parents=True)

    folders = []
    for i in range(13):
        f = sb_dir / f"backup-{i:02d}"
        f.mkdir(parents=True)
        (f / "info.txt").write_text(f"backup-{i}", encoding="utf-8")
        _set_age_days(f, 30 - i)  # backup-00 oldest, backup-12 newest
        folders.append(f)

    res = layout.run_retention(force=True)
    assert res["setup_backups"]["items"] == 3
    # Oldest 3 (backup-00, backup-01, backup-02) should be deleted
    assert not folders[0].exists()
    assert not folders[1].exists()
    assert not folders[2].exists()
    # Newest 10 (backup-03 to backup-12) should survive
    for f in folders[3:]:
        assert f.exists()


def test_rule_6_quarantine_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    q_dir = tmp_path / "quarantine"
    sub = q_dir / "rejected"
    sub.mkdir(parents=True)

    old_bundle = sub / "old-bundle-dir"
    old_bundle.mkdir(parents=True)
    (old_bundle / "file.txt").write_text("bundle_data", encoding="utf-8")
    _set_age_days(old_bundle, 25)

    new_bundle = sub / "new-bundle-dir"
    new_bundle.mkdir(parents=True)
    (new_bundle / "file.txt").write_text("bundle_data_new", encoding="utf-8")
    _set_age_days(new_bundle, 2)

    old_file_bundle = sub / "old-bundle.json"
    old_file_bundle.write_text("{}", encoding="utf-8")
    _set_age_days(old_file_bundle, 25)

    prune_lock = q_dir / ".prune.lock"
    prune_lock.write_text("lock", encoding="utf-8")
    _set_age_days(prune_lock, 25)

    sub_lock = sub / ".prune.lock"
    sub_lock.write_text("lock", encoding="utf-8")
    _set_age_days(sub_lock, 25)

    res = layout.run_retention(force=True)
    assert res["quarantine"]["items"] == 2
    assert not old_bundle.exists()
    assert not old_file_bundle.exists()
    assert new_bundle.exists()
    assert prune_lock.exists()
    assert sub_lock.exists()


def test_rule_7_archive_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    arch_dir = tmp_path / "archive"
    arch_dir.mkdir(parents=True)

    old_arch = arch_dir / "2026-08-01"
    old_arch.mkdir(parents=True)
    (old_arch / "notes.txt").write_text("archived content", encoding="utf-8")
    _set_age_days(old_arch, 50)

    new_arch = arch_dir / "2026-10-10"
    new_arch.mkdir(parents=True)
    (new_arch / "notes.txt").write_text("recent archive", encoding="utf-8")
    _set_age_days(new_arch, 5)

    res = layout.run_retention(force=True)
    assert res["archive"]["items"] == 1
    assert not old_arch.exists()
    assert new_arch.exists()


def test_rule_8_stray_exe_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))

    # agent-switchboard.exe must be untouched
    main_exe = tmp_path / "agent-switchboard.exe"
    main_exe.write_text("main binary content", encoding="utf-8")
    _set_age_days(main_exe, 100)

    # 4 stray exe files
    exe1 = tmp_path / "agent-switchboard-old.exe"
    exe1.write_text("old1", encoding="utf-8")
    _set_age_days(exe1, 20)

    exe2 = tmp_path / "agent-switchboard.exe.bak"
    exe2.write_text("old2", encoding="utf-8")
    _set_age_days(exe2, 15)

    exe3 = tmp_path / "agent-switchboard-v2.exe"
    exe3.write_text("recent1", encoding="utf-8")
    _set_age_days(exe3, 5)

    exe4 = tmp_path / "agent-switchboard.exe.new"
    exe4.write_text("recent2", encoding="utf-8")
    _set_age_days(exe4, 2)

    fixed_time = 1700000000.0
    dt = datetime.fromtimestamp(fixed_time)
    today_str = dt.strftime("%Y-%m-%d")

    res = layout.run_retention(now=fixed_time, force=True)
    assert res["stray_exe"]["items"] == 2
    # Main exe untouched
    assert main_exe.exists()
    # Newest 2 stay in place (exe4 and exe3)
    assert exe4.exists()
    assert exe3.exists()
    # Older 2 moved to archive/<today>/old-exe/
    assert not exe1.exists()
    assert not exe2.exists()
    dest = tmp_path / "archive" / today_str / "old-exe"
    assert (dest / "agent-switchboard-old.exe").exists()
    assert (dest / "agent-switchboard.exe.bak").exists()


def test_every_never_item_survives(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))

    never_files = [
        tmp_path / "agent-switchboard.exe",
        tmp_path / "config.json",
        tmp_path / "state.sqlite",
        tmp_path / "code-graph" / "graph.db",
        tmp_path / "routing-gate" / "rules.json",
        tmp_path / "routing-gate" / "active.lock",
        tmp_path / "context-evidence" / "session_1" / "ev.txt",
        tmp_path / "codex-inbox" / "req.json",
        tmp_path / "claude-inbox" / "req.json",
        tmp_path / "projects" / "p1" / "brief.json",
    ]

    for nf in never_files:
        nf.parent.mkdir(parents=True, exist_ok=True)
        nf.write_text("vital data", encoding="utf-8")
        _set_age_days(nf, 150)

    # Also make sure parent folders have old age
    for folder_name in ("code-graph", "context-evidence", "codex-inbox", "claude-inbox", "projects"):
        _set_age_days(tmp_path / folder_name, 150)

    res = layout.run_retention(force=True)

    for nf in never_files:
        assert nf.exists(), f"Protected item {nf} was deleted!"


def test_once_per_day_stamp_prevents_second_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    log1 = tmp_dir / "job1.log"
    log1.write_text("log1", encoding="utf-8")
    _set_age_days(log1, 20)

    res1 = layout.run_retention()
    assert res1["tmp"]["items"] == 1
    assert not log1.exists()

    stamp = tmp_path / ".retention-stamp"
    assert stamp.is_file()
    today_str = datetime.now().strftime("%Y-%m-%d")
    assert stamp.read_text(encoding="utf-8").strip() == today_str

    # Create another old log; second run without force must be skipped
    log2 = tmp_dir / "job2.log"
    log2.write_text("log2", encoding="utf-8")
    _set_age_days(log2, 20)

    res2 = layout.run_retention()
    assert res2["tmp"]["items"] == 0
    assert log2.exists()

    # With force=True, second run proceeds
    res3 = layout.run_retention(force=True)
    assert res3["tmp"]["items"] == 1
    assert not log2.exists()


def test_dry_run_reports_counts_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    log1 = tmp_dir / "dry_test.log"
    log1.write_text("sample content", encoding="utf-8")
    _set_age_days(log1, 20)

    res = layout.run_retention(dry_run=True)
    assert res["tmp"]["items"] == 1
    assert res["tmp"]["bytes"] == len("sample content")

    # File still exists and stamp is not written
    assert log1.exists()
    stamp = tmp_path / ".retention-stamp"
    assert not stamp.exists()


def test_env_overrides_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    # Custom override: 5 days
    monkeypatch.setenv("AGENT_BROKER_TMP_RETENTION_DAYS", "5")
    log_7d = tmp_dir / "log_7d.log"
    log_7d.write_text("7d", encoding="utf-8")
    _set_age_days(log_7d, 7)

    log_3d = tmp_dir / "log_3d.log"
    log_3d.write_text("3d", encoding="utf-8")
    _set_age_days(log_3d, 3)

    res = layout.run_retention(force=True)
    assert res["tmp"]["items"] == 1
    assert not log_7d.exists()
    assert log_3d.exists()

    # Invalid env override falls back to default 14
    monkeypatch.setenv("AGENT_BROKER_TMP_RETENTION_DAYS", "invalid_number")
    log_10d = tmp_dir / "log_10d.log"
    log_10d.write_text("10d", encoding="utf-8")
    _set_age_days(log_10d, 10)

    log_20d = tmp_dir / "log_20d.log"
    log_20d.write_text("20d", encoding="utf-8")
    _set_age_days(log_20d, 20)

    res2 = layout.run_retention(force=True)
    assert res2["tmp"]["items"] == 1
    assert log_10d.exists()  # < 14 days, retained
    assert not log_20d.exists()  # > 14 days, deleted


def test_run_retention_missing_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    # Directory is completely empty
    res = layout.run_retention(force=True)
    assert isinstance(res, dict)
    for rule in ("tmp", "runs", "gate_logs", "flash_apply", "setup_backups", "quarantine", "archive", "stray_exe"):
        assert res[rule]["items"] == 0
        assert res[rule]["bytes"] == 0


def _take_os_lock(lock_file: Path) -> int:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
    if sys.platform == "win32":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _release_os_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def test_lock_file_skips_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    old_log = tmp_dir / "locked_test.log"
    old_log.write_text("data", encoding="utf-8")
    _set_age_days(old_log, 25)

    lock_file = tmp_path / ".retention.lock"
    fd = _take_os_lock(lock_file)
    try:
        res = layout.run_retention(force=True)
        assert res == {"skipped": "locked"}
        assert old_log.exists()
        assert not (tmp_path / ".retention-stamp").exists()
    finally:
        _release_os_lock(fd)


def test_leftover_lock_file_does_not_block_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    old_log = tmp_dir / "dead_proc_test.log"
    old_log.write_text("data", encoding="utf-8")
    _set_age_days(old_log, 25)

    # A leftover .retention.lock file from a dead process (just create the file)
    lock_file = tmp_path / ".retention.lock"
    lock_file.write_text("99999", encoding="utf-8")

    res = layout.run_retention(force=True)
    # Does not block a run: rules run, stamp written, lock file still exists afterwards
    assert res != {"skipped": "locked"}
    assert res["tmp"]["items"] == 1
    assert not old_log.exists()
    assert (tmp_path / ".retention-stamp").is_file()
    assert lock_file.is_file()


def test_os_lock_held_skips_and_release_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    old_log = tmp_dir / "test.log"
    old_log.write_text("data", encoding="utf-8")
    _set_age_days(old_log, 25)

    lock_file = tmp_path / ".retention.lock"
    fd = _take_os_lock(lock_file)
    try:
        # while one open handle in the test holds the OS lock, run_retention returns {'skipped': 'locked'} and writes no stamp
        res = layout.run_retention(force=True)
        assert res == {"skipped": "locked"}
        assert not (tmp_path / ".retention-stamp").exists()
        assert old_log.exists()
    finally:
        _release_os_lock(fd)

    # after releasing, a run succeeds
    res2 = layout.run_retention(force=True)
    assert res2 != {"skipped": "locked"}
    assert res2["tmp"]["items"] == 1
    assert not old_log.exists()
    assert (tmp_path / ".retention-stamp").is_file()
    assert lock_file.is_file()


def test_tidy_cli_when_locked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    lock_file = tmp_path / ".retention.lock"
    fd = _take_os_lock(lock_file)
    try:
        code = layout.tidy_cli(["--force"])
        assert code == 0
        out = capsys.readouterr().out
        assert out.strip() == "skipped: locked"
    finally:
        _release_os_lock(fd)


def test_tidy_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))

    code = layout.tidy_cli(["--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "tmp:" in out
    assert "runs:" in out
    assert "stray_exe:" in out

    code2 = layout.tidy_cli(["--force"])
    assert code2 == 0
