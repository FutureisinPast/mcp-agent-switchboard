"""Tests for opt-in read-only whole-repo staging (WP-1072-A-REPO-STAGING)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import flash_manifest as fm  # noqa: E402

CRITERIA = ["Focused tests pass.", "No files outside the allowlist change."]
MODEL = "gemini-3.7-flash-high"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")


def _write(root: Path, rel: str, content: str | bytes = "ok\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _payload(package_id: str, paths: list[Path | str], *, status: str = "completed") -> str:
    structured = {
        "package_id": package_id,
        "status": status,
        "summary": "done",
        "acceptance_criteria": [
            {"criterion": c, "status": "passed", "evidence": ["checked"]} for c in CRITERIA
        ],
        "files_changed": [{"path": str(p), "change": "modified"} for p in paths],
        "checks": [],
        "evidence": [],
        "claims": [],
        "research_coverage": [],
        "ambiguities": [],
        "risks": [],
        "next_action": "verify",
        "brain_verification_required": "required",
    }
    return json.dumps({"status": "SUCCESS", "model": MODEL, "structured_output": structured})


def _patch_broker_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_BROKER_CHILD", raising=False)
    monkeypatch.setattr(broker, "BROKER_DIR", tmp_path / "broker")
    monkeypatch.setattr(broker, "DB_PATH", tmp_path / "broker" / "state.sqlite")
    monkeypatch.setattr(broker, "LOG_PATH", tmp_path / "broker" / "agent-broker.log")
    monkeypatch.setattr(broker, "CONFIG_PATH", tmp_path / "broker" / "config.json")
    monkeypatch.setattr(broker, "QUARANTINE_ROOT", tmp_path / "q")
    monkeypatch.setattr(broker, "QUARANTINE_LOCK_PATH", tmp_path / "q.lock")
    monkeypatch.setattr(broker, "WORKER_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(broker, "_flash_apply_dir", lambda: tmp_path / "flash-apply")
    monkeypatch.setattr(broker, "FLASH_TRANSCRIPT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(broker, "_antigravity_cli_home", lambda: tmp_path / "agy")
    monkeypatch.setattr(broker, "_FLASH_APPLY_MEMORY", {})


def _make_package(
    ws: Path,
    *,
    writes: list[Path] | None = None,
    creates: list[Path] | None = None,
    read_context: list[Path] | None = None,
    stage_repo: bool | None = None,
    package_id: str = "WP-REPO-TEST",
) -> dict:
    raw = {
        "work_package_id": package_id,
        "allowed_writes": [str(p) for p in (writes or [])],
        "allowed_creates": [str(p) for p in (creates or [])],
        "read_context": [str(p) for p in (read_context or [])],
        "workspace_root": str(ws),
        "acceptance_criteria": list(CRITERIA),
    }
    if stage_repo is not None:
        raw["stage_repo"] = stage_repo
    return broker.prepare_flash_work_package(raw, "implementation", "Implement bounded change.")


def _run_cli_with_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ws: Path,
    package: dict,
    worker_fn,
    *,
    mode: str = "accept-edits",
) -> tuple[str, dict]:
    _patch_broker_dirs(monkeypatch, tmp_path)
    seen: dict = {}

    def fake_run(command, cwd, *args, **kwargs):
        seen["command"] = command
        seen["cwd"] = Path(cwd)
        seen["env"] = dict(kwargs.get("extra_env") or {})
        out = worker_fn(Path(cwd), seen)
        return (0, out, "")

    monkeypatch.setattr(broker, "load_config", lambda: {})
    monkeypatch.setattr(broker, "discover_antigravity_cli", lambda _cfg=None: "agy")
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))
    monkeypatch.setattr(broker, "run_process", fake_run)
    response = broker.consult_antigravity_cli("p", "bounded prompt", mode, MODEL, "high", 60, package)
    return response, seen


def test_stage_repo_false_matches_default_behaviour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    sibling = _write(ws, "src/sibling.py", "s = 2\n")
    test_file = _write(ws, "tests/test_a.py", "def test_ok(): assert True\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    default_pkg = _make_package(ws, writes=[file_a], stage_repo=None)
    false_pkg = _make_package(ws, writes=[file_a], stage_repo=False)

    m_default = fm.build_manifest(default_pkg, str(ws), implementation_mode=True)
    m_false = fm.build_manifest(false_pkg, str(ws), implementation_mode=True)
    assert m_default == m_false
    assert "stage_repo" not in m_false

    s_default = fm.stage(m_default)
    s_false = fm.stage(m_false, stage_repo=False)
    try:
        assert not (s_false.root / "src/sibling.py").exists()
        assert not (s_false.root / "tests/test_a.py").exists()
        assert (s_false.root / "src/a.py").exists()
        assert s_false.repo_staging is None
        assert s_default.repo_staging is None
        assert fm.prompt_appendix(s_false, real_table=True).replace(str(s_false.root), "<ROOT>") == (
            fm.prompt_appendix(s_default, real_table=True).replace(str(s_default.root), "<ROOT>")
        )
        assert "whole project is staged read-only" not in fm.prompt_appendix(s_false, real_table=True).lower()
        r_default = fm.containment_receipt(s_default, m_default, fm.collect_changes(s_default), None)
        r_false = fm.containment_receipt(s_false, m_false, fm.collect_changes(s_false), None)
        assert r_default == r_false
        assert "repo_staging" not in r_false
    finally:
        shutil.rmtree(s_default.root, ignore_errors=True)
        shutil.rmtree(s_false.root, ignore_errors=True)

    # Default (stage_repo=False) CLI env keeps legacy overrides without PYTHONPYCACHEPREFIX.
    def worker(cwd: Path, _seen: dict) -> str:
        assert not (cwd / "src/sibling.py").exists()
        assert not (cwd / "tests/test_a.py").exists()
        (cwd / "src/a.py").write_text("a = 2\n", encoding="utf-8")
        return _payload(false_pkg["package_id"], [cwd / "src/a.py"])

    resp, seen = _run_cli_with_worker(monkeypatch, tmp_path, ws, false_pkg, worker)
    envelope = json.loads(resp)
    assert envelope["disposition"] == "accepted"
    assert "repo_staging" not in envelope["containment"]
    assert "PYTHONPYCACHEPREFIX" not in seen["env"]
    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "no:cacheprovider" in seen["env"]["PYTEST_ADDOPTS"]
    assert sibling.read_text(encoding="utf-8") == "s = 2\n"
    assert test_file.read_text(encoding="utf-8") == "def test_ok(): assert True\n"


def test_repo_staging_stages_tracked_and_untracked_skips_ignored(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _write(ws, "src/tracked.py", "t = 10\n")
    _write(ws, ".gitignore", "*.log\nignored_dir/\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    _write(ws, "src/untracked.py", "u = 20\n")
    _write(ws, "debug.log", "ignored\n")
    _write(ws, "ignored_dir/skip.py", "ignored = True\n")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)
    staged = fm.stage(manifest)
    try:
        assert (staged.root / "src/a.py").read_text(encoding="utf-8") == "a = 1\n"
        assert (staged.root / "src/tracked.py").read_text(encoding="utf-8") == "t = 10\n"
        assert (staged.root / "src/untracked.py").read_text(encoding="utf-8") == "u = 20\n"
        assert (staged.root / ".gitignore").exists()
        assert not (staged.root / "debug.log").exists()
        assert not (staged.root / "ignored_dir/skip.py").exists()

        for rel in ("src/a.py", "src/tracked.py", "src/untracked.py", ".gitignore"):
            assert staged.baseline[rel] == fm.file_digest(staged.staged_path(rel))

        receipt = fm.containment_receipt(staged, manifest, fm.collect_changes(staged), None)
        rs = receipt["repo_staging"]
        assert rs["enabled"] is True
        assert rs["files"] == 4
        assert rs["bytes"] == staged.staged_bytes
        assert rs["excluded_count"] == 0
        assert rs["skipped_links"] == []
        assert rs["caveat"] is None

        appendix = fm.prompt_appendix(staged, real_table=True)
        assert (
            "The whole project is staged read-only so you can import modules and run tests; "
            "you may change only the allowed files; any other change rejects the package."
        ) in appendix
        # path_map remains restricted to manifest entries only
        assert len(staged.path_map()) == 1
    finally:
        shutil.rmtree(staged.root, ignore_errors=True)


def test_secret_like_and_oversized_files_excluded_even_when_tracked(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    token_doc = _write(ws, "docs/token_spec.md", "manifest takes precedence\n")
    secret_paths = [
        ".env",
        ".ENV.local",
        "certs/server.PEM",
        "certs/private.Key",
        "certs/bundle.p12",
        "certs/store.PFX",
        "certs/vault.kdbx",
        "ssh/id_rsa",
        "ssh/id_ed25519.pub",
        "ssh/known_hosts.old",
        "cfg/my_CREDENTIAL.json",
        "cfg/app_Secret.yaml",
        "cfg/api_TOKEN.txt",
    ]
    for rel in secret_paths:
        _write(ws, rel, "sensitive\n")
    _write(ws, "data/too_big.bin", b"x" * (5 * 1024 * 1024 + 1))
    _write(ws, "data/at_limit.bin", b"y" * (5 * 1024 * 1024))
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "track secrets and big files")

    # Also add an untracked secret file to confirm exclusions apply to both tracked & untracked.
    _write(ws, ".env.production", "PROD=1\n")

    pkg = _make_package(ws, writes=[file_a], read_context=[token_doc], stage_repo=True)
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)
    staged = fm.stage(manifest)
    try:
        for rel in secret_paths + [".env.production", "data/too_big.bin"]:
            assert not (staged.root / rel).exists(), f"{rel} must be excluded"
        assert (staged.root / "data/at_limit.bin").exists()
        assert (staged.root / "src/a.py").exists()
        # Manifest read_context takes precedence over secret-name heuristic.
        assert (staged.root / "docs/token_spec.md").exists()
        assert staged.repo_staging is not None
        assert staged.repo_staging["enabled"] is True
        assert staged.repo_staging["excluded_count"] == len(secret_paths) + 2
    finally:
        shutil.rmtree(staged.root, ignore_errors=True)


def test_excluded_dirs_skipped_for_tracked_and_untracked(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    excluded_tracked = [
        "node_modules/pkg/index.js",
        ".venv/lib/site.py",
        "venv/bin/activate",
        "env/vars.py",
        "pkg/__pycache__/a.cpython-312.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".mypy_cache/3.12/a.meta.json",
        ".tox/py/log.txt",
        "build/lib/mod.py",
        "dist/pkg-0.1.whl",
    ]
    # Git refuses to track or list `.git` paths (`git add -f .` and `git ls-files`
    # never emit `nested/.git`), so it is excluded on disk without incrementing
    # excluded_count; verify `.git` segment filtering directly as well.
    git_unlisted = ["nested/.git"]
    for rel in excluded_tracked + git_unlisted:
        _write(ws, rel, "ignored\n")
    _git(ws, "add", "-f", ".")
    _git(ws, "commit", "-m", "add excluded dirs")

    _write(ws, "node_modules/untracked/x.js", "u\n")
    _write(ws, "src/__pycache__/x.cpython-312.pyc", b"pyc")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)
    staged = fm.stage(manifest)
    try:
        for rel in excluded_tracked + git_unlisted + ["node_modules/untracked/x.js", "src/__pycache__/x.cpython-312.pyc"]:
            assert not (staged.root / rel).exists(), f"{rel} should not be staged"
        assert fm._is_excluded_repo_path("nested/.git") is True
        assert fm._is_excluded_repo_path("nested/.git/config") is True
        assert (staged.root / "src/a.py").exists()
        assert staged.repo_staging is not None
        assert staged.repo_staging["enabled"] is True
        assert staged.repo_staging["excluded_count"] == len(excluded_tracked) + 2
    finally:
        shutil.rmtree(staged.root, ignore_errors=True)


def test_symlinks_and_reparse_points_skipped_and_listed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _write(ws, "src/normal.py", "n = 1\n")
    fake_link = _write(ws, "src/reparse.py", "secret outside\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    real_is_reparse = fm.is_reparse_point
    monkeypatch.setattr(
        fm,
        "is_reparse_point",
        lambda p: True if Path(p) == fake_link else real_is_reparse(p),
    )

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)
    staged = fm.stage(manifest)
    try:
        assert (staged.root / "src/a.py").exists()
        assert (staged.root / "src/normal.py").exists()
        assert not (staged.root / "src/reparse.py").exists()
        assert staged.repo_staging is not None
        assert staged.repo_staging["skipped_links"] == ["src/reparse.py"]
    finally:
        shutil.rmtree(staged.root, ignore_errors=True)


def test_real_os_symlink_skipped_when_supported(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    outside = _write(tmp_path, "outside.txt", "outside\n")
    link_path = ws / "src/escape.py"
    try:
        os.symlink(outside, link_path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation not permitted in this environment: {exc}")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)
    staged = fm.stage(manifest)
    try:
        assert not (staged.root / "src/escape.py").exists()
        assert staged.repo_staging is not None
        assert "src/escape.py" in staged.repo_staging["skipped_links"]
    finally:
        shutil.rmtree(staged.root, ignore_errors=True)


def test_caps_exceeded_falls_back_to_manifest_only_with_caveat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _write(ws, "src/b.py", "b = 2\n")
    _write(ws, "src/c.py", "c = 3\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)

    # File count cap exceeded -> manifest-only + caveat
    monkeypatch.setattr(fm, "REPO_MAX_FILES", 2)
    staged_count = fm.stage(manifest)
    try:
        assert (staged_count.root / "src/a.py").exists()
        assert not (staged_count.root / "src/b.py").exists()
        assert staged_count.repo_staging is not None
        assert staged_count.repo_staging["enabled"] is False
        assert staged_count.repo_staging["caveat"] == (
            "repo_staging_unavailable: caps exceeded; repo tests could not run in the sandbox"
        )
        assert "whole project is staged read-only" not in fm.prompt_appendix(staged_count, real_table=True).lower()
    finally:
        shutil.rmtree(staged_count.root, ignore_errors=True)

    # Total bytes cap exceeded -> manifest-only + caveat
    monkeypatch.setattr(fm, "REPO_MAX_FILES", 5000)
    monkeypatch.setattr(fm, "REPO_MAX_TOTAL_BYTES", 10)
    staged_bytes = fm.stage(manifest)
    try:
        assert (staged_bytes.root / "src/a.py").exists()
        assert not (staged_bytes.root / "src/b.py").exists()
        assert staged_bytes.repo_staging is not None
        assert staged_bytes.repo_staging["enabled"] is False
        assert staged_bytes.repo_staging["caveat"] == (
            "repo_staging_unavailable: caps exceeded; repo tests could not run in the sandbox"
        )
    finally:
        shutil.rmtree(staged_bytes.root, ignore_errors=True)


def test_non_git_root_and_git_unavailable_fallback_with_caveat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "nongit"
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _write(ws, "src/sibling.py", "s = 2\n")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)

    def worker(cwd: Path, _seen: dict) -> str:
        assert (cwd / "src/a.py").exists()
        assert not (cwd / "src/sibling.py").exists()
        (cwd / "src/a.py").write_text("a = 9\n", encoding="utf-8")
        return _payload(pkg["package_id"], [cwd / "src/a.py"])

    resp, _ = _run_cli_with_worker(monkeypatch, tmp_path, ws, pkg, worker)
    envelope = json.loads(resp)
    assert envelope["disposition"] == "accepted_with_caveats"
    assert envelope["worker_status"] == "completed_with_caveats"
    assert any(
        c.get("code") == "repo_staging_unavailable"
        and "repo_staging_unavailable:" in c.get("detail", "")
        and "repo tests could not run in the sandbox" in c.get("detail", "")
        for c in envelope["caveats"]
    )
    rs = envelope["containment"]["repo_staging"]
    assert rs["enabled"] is False
    assert rs["caveat"].startswith("repo_staging_unavailable: ")
    assert rs["caveat"].endswith("; repo tests could not run in the sandbox")
    assert file_a.read_text(encoding="utf-8") == "a = 9\n"

    # Also cover git missing (FileNotFoundError) / timeout fallback.
    manifest = fm.build_manifest(pkg, str(ws), implementation_mode=True)
    for exc in (FileNotFoundError("git"), subprocess.TimeoutExpired(["git"], 30)):
        monkeypatch.setattr(fm.subprocess, "run", mock.Mock(side_effect=exc))
        staged = fm.stage(manifest)
        try:
            assert staged.repo_staging is not None
            assert staged.repo_staging["enabled"] is False
            assert staged.repo_staging["caveat"].startswith("repo_staging_unavailable: ")
            assert staged.repo_staging["caveat"].endswith("; repo tests could not run in the sandbox")
        finally:
            shutil.rmtree(staged.root, ignore_errors=True)


def test_modified_non_allowed_staged_file_is_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    sibling = _write(ws, "src/sibling.py", "s = 2\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)

    def worker(cwd: Path, _seen: dict) -> str:
        (cwd / "src/a.py").write_text("a = 100\n", encoding="utf-8")
        (cwd / "src/sibling.py").write_text("s = 999\n", encoding="utf-8")
        return _payload(pkg["package_id"], [cwd / "src/a.py"])

    resp, _ = _run_cli_with_worker(monkeypatch, tmp_path, ws, pkg, worker)
    assert "structured-output validation failed" in resp
    assert "NOTHING was written back" in resp
    assert "src/sibling.py" in resp
    assert file_a.read_text(encoding="utf-8") == "a = 1\n"
    assert sibling.read_text(encoding="utf-8") == "s = 2\n"


def test_deleted_staged_file_is_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    sibling = _write(ws, "src/sibling.py", "s = 2\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)

    def worker(cwd: Path, _seen: dict) -> str:
        (cwd / "src/a.py").write_text("a = 100\n", encoding="utf-8")
        (cwd / "src/sibling.py").unlink()
        return _payload(pkg["package_id"], [cwd / "src/a.py"])

    resp, _ = _run_cli_with_worker(monkeypatch, tmp_path, ws, pkg, worker)
    assert "structured-output validation failed" in resp
    assert "NOTHING was written back" in resp
    assert "src/sibling.py" in resp
    assert file_a.read_text(encoding="utf-8") == "a = 1\n"
    assert sibling.read_text(encoding="utf-8") == "s = 2\n"


def test_added_file_outside_allowed_creates_is_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _write(ws, "src/sibling.py", "s = 2\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    pkg = _make_package(ws, writes=[file_a], stage_repo=True)

    def worker(cwd: Path, _seen: dict) -> str:
        (cwd / "src/a.py").write_text("a = 100\n", encoding="utf-8")
        (cwd / "src/stray.py").write_text("stray = 1\n", encoding="utf-8")
        return _payload(pkg["package_id"], [cwd / "src/a.py"])

    resp, _ = _run_cli_with_worker(monkeypatch, tmp_path, ws, pkg, worker)
    assert "structured-output validation failed" in resp
    assert "NOTHING was written back" in resp
    assert "src/stray.py" in resp
    assert file_a.read_text(encoding="utf-8") == "a = 1\n"
    assert not (ws / "src/stray.py").exists()


def test_allowed_write_and_create_accepted_with_cache_artifacts_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    sibling = _write(ws, "src/sibling.py", "s = 2\n")
    created = ws / "src/new_mod.py"
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    pkg = _make_package(ws, writes=[file_a], creates=[created], stage_repo=True)

    def worker(cwd: Path, _seen: dict) -> str:
        assert (cwd / "src/sibling.py").read_text(encoding="utf-8") == "s = 2\n"
        (cwd / "src/a.py").write_text("a = 42\n", encoding="utf-8")
        (cwd / "src/new_mod.py").write_text("created = True\n", encoding="utf-8")
        # Cache artifacts left behind in staging remain ignored per _is_cache_artifact_path.
        _write(cwd, "src/__pycache__/sibling.cpython-312.pyc", b"pyc")
        _write(cwd, ".pytest_cache/v/cache/nodeids", "[]")
        return _payload(pkg["package_id"], [cwd / "src/a.py", cwd / "src/new_mod.py"])

    resp, _ = _run_cli_with_worker(monkeypatch, tmp_path, ws, pkg, worker)
    envelope = json.loads(resp)
    assert envelope["disposition"] == "accepted"
    assert sorted(envelope["applied_files"]) == sorted([str(file_a), str(created)])
    assert file_a.read_text(encoding="utf-8") == "a = 42\n"
    assert created.read_text(encoding="utf-8") == "created = True\n"
    assert sibling.read_text(encoding="utf-8") == "s = 2\n"
    assert envelope["containment"]["repo_staging"]["enabled"] is True
    assert envelope["containment"]["staging_diff"]["ignored_cache"] == [
        ".pytest_cache/v/cache/nodeids",
        "src/__pycache__/sibling.cpython-312.pyc",
    ]


def test_stage_repo_agy_env_includes_three_cache_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    monkeypatch.setenv("PYTEST_ADDOPTS", "-x --maxfail=1")
    pkg = _make_package(ws, writes=[file_a], stage_repo=True)

    def worker(cwd: Path, seen: dict) -> str:
        prefix = Path(seen["env"]["PYTHONPYCACHEPREFIX"])
        assert prefix.is_dir()
        assert not prefix.is_relative_to(cwd)
        (cwd / "src/a.py").write_text("a = 2\n", encoding="utf-8")
        return _payload(pkg["package_id"], [cwd / "src/a.py"])

    resp, seen = _run_cli_with_worker(monkeypatch, tmp_path, ws, pkg, worker)
    assert json.loads(resp)["disposition"] == "accepted"
    env = seen["env"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTEST_ADDOPTS"] == "-x --maxfail=1 -p no:cacheprovider"
    assert "PYTHONPYCACHEPREFIX" in env
    prefix = Path(env["PYTHONPYCACHEPREFIX"])
    assert not prefix.is_relative_to(seen["cwd"])
    assert not prefix.exists()  # cleaned up after the run


def test_route_agent_task_schema_and_stage_repo_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route_tool = next(t for t in broker.TOOLS if t["name"] == "route_agent_task")
    prop = route_tool["inputSchema"]["properties"]["stage_repo"]
    assert prop["type"] == "boolean"
    assert prop["description"] == (
        "Stage a read-only copy of the whole package root so the worker can import "
        "sibling modules and run the repo's tests; writes stay limited to allowed files."
    )

    ws = tmp_path / "repo"
    _init_git_repo(ws)
    file_a = _write(ws, "src/a.py", "a = 1\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")

    _patch_broker_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))
    monkeypatch.setattr(
        broker,
        "resolve_model_request",
        lambda *_a, **_k: {
            "status": "resolved",
            "project": "p",
            "topic": None,
            "model_family": "antigravity",
            "target_agent": "antigravity",
            "target_model": MODEL,
            "effort": "high",
            "source": "family_workhorse",
        },
    )
    monkeypatch.setattr(broker, "antigravity_model_for_effort", lambda m, _e: m)
    monkeypatch.setattr(broker, "load_config", lambda: {"compact_task_contract": False})

    captured: list[dict] = []

    def fake_consult(model_family: str, flash_args: dict) -> dict:
        captured.append({"family": model_family, "args": dict(flash_args)})
        return {"status": "ok", "response": "ok"}

    monkeypatch.setattr(broker, "consult", fake_consult)

    broker.route_agent_task(
        {
            "target_agent": "antigravity",
            "surface": "cli",
            "target_model": "hydrogen",
            "mode": "accept-edits",
            "task_kind": "implementation",
            "async": False,
            "work_package_id": "WP-ROUTE-TRUE",
            "allowed_writes": [str(file_a)],
            "workspace_root": str(ws),
            "acceptance_criteria": list(CRITERIA),
            "stage_repo": True,
            "prompt": "Run tests and update a.py",
        }
    )
    broker.route_agent_task(
        {
            "target_agent": "antigravity",
            "surface": "cli",
            "target_model": "hydrogen",
            "mode": "accept-edits",
            "task_kind": "implementation",
            "async": False,
            "work_package_id": "WP-ROUTE-DEFAULT",
            "allowed_writes": [str(file_a)],
            "workspace_root": str(ws),
            "acceptance_criteria": list(CRITERIA),
            "prompt": "Update a.py",
        }
    )
    assert captured[0]["args"]["stage_repo"] is True
    assert captured[1]["args"]["stage_repo"] is False

    # Also verify async queueing persists stage_repo for detached workers.
    monkeypatch.setattr(broker, "start_flash_request_worker", lambda _rid: {"started": False})
    queued = broker.route_agent_task(
        {
            "target_agent": "antigravity",
            "surface": "cli",
            "target_model": "hydrogen",
            "mode": "accept-edits",
            "task_kind": "implementation",
            "work_package_id": "WP-ROUTE-ASYNC",
            "allowed_writes": [str(file_a)],
            "workspace_root": str(ws),
            "acceptance_criteria": list(CRITERIA),
            "stage_repo": True,
            "prompt": "Run tests",
        }
    )
    with broker.db_connect() as conn:
        row = conn.execute(
            "SELECT args_json FROM flash_requests WHERE id = ?", (queued["request_id"],)
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["stage_repo"] is True
