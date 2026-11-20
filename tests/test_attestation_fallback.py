"""Tests for transcript-based model attestation fallback when agy omits both
model and conversation_id (WP-1073-ATTEST-FALLBACK).

All filesystem state lives under pytest's tmp_path and all external CLI /
catalog / home helpers are monkeypatched; no real agy or home files are used.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import flash_manifest as fm  # noqa: E402

MODEL = "gemini-3.7-flash-high"
MODEL_DISPLAY = "Gemini 3.7 Flash (High)"
OTHER_MODEL = "gemini-3.7-pro-low"
OTHER_MODEL_DISPLAY = "Gemini 3.7 Pro (Low)"
CRITERIA = ["Focused tests pass.", "No files outside the allowlist change."]


def _catalog() -> list[dict]:
    return [
        broker.model_entry(MODEL, MODEL_DISPLAY),
        broker.model_entry(OTHER_MODEL, OTHER_MODEL_DISPLAY),
    ]


def _patch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    agy_home = tmp_path / "agy-home"
    monkeypatch.setenv("AGENT_BROKER_AGY_HOME", str(agy_home))
    monkeypatch.setattr(broker, "_antigravity_cli_home", lambda: agy_home)
    monkeypatch.setattr(broker, "QUARANTINE_ROOT", tmp_path / "q" / "rejected")
    monkeypatch.setattr(broker, "QUARANTINE_LOCK_PATH", tmp_path / "q" / ".prune.lock")
    monkeypatch.setattr(broker, "_quarantine_last_prune_at", 0.0)
    monkeypatch.setattr(broker, "WORKER_LOG_DIR", tmp_path / "flash-logs")
    monkeypatch.setattr(broker, "FLASH_TRANSCRIPT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(broker, "load_config", lambda: {"compact_task_contract": False})
    monkeypatch.setattr(broker, "discover_antigravity_cli", lambda _cfg=None: "agy")
    monkeypatch.setattr(broker, "discover_antigravity_models", _catalog)
    return agy_home


def _setup_workspace(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    target = ws / "src" / "worker.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")
    return ws, target


def _impl_package(ws: Path, target: Path, package_id: str = "WP-ATTEST-IMPL") -> dict:
    return broker.prepare_flash_work_package(
        {
            "work_package_id": package_id,
            "allowed_writes": [str(target)],
            "workspace_root": str(ws),
            "acceptance_criteria": list(CRITERIA),
        },
        "implementation",
        "Update value in worker.py.",
    )


def _ro_package(ws: Path, target: Path, package_id: str = "WP-ATTEST-RO") -> dict:
    return broker.prepare_flash_work_package(
        {
            "work_package_id": package_id,
            "read_context": [str(target)],
            "workspace_root": str(ws),
            "acceptance_criteria": [CRITERIA[0]],
        },
        "quick_check",
        "Inspect worker.py.",
    )


def _outer_payload(
    package: dict,
    *,
    model: str | None = None,
    conversation_id: str | None = None,
    files_changed: list[dict[str, str]] | None = None,
) -> dict:
    structured = {
        "package_id": package["package_id"],
        "status": "completed",
        "summary": "Completed the bounded package.",
        "acceptance_criteria": [
            {"criterion": c, "status": "passed", "evidence": ["src/worker.py:1"]}
            for c in package["acceptance_criteria"]
        ],
        "files_changed": files_changed if files_changed is not None else [],
        "checks": [],
        "evidence": [
            {"claim": "value checked", "path": "src/worker.py", "line": "1", "observation": "value = 1"}
        ],
        "claims": [],
        "research_coverage": [],
        "ambiguities": [],
        "risks": [],
        "next_action": "Brain verifies.",
        "brain_verification_required": "required",
    }
    outer: dict = {
        "status": "SUCCESS",
        "structured_output": structured,
        "duration_seconds": 1.5,
        "num_turns": 1,
        "usage": {"total_tokens": 25},
    }
    if model is not None:
        outer["model"] = model
    if conversation_id is not None:
        outer["conversation_id"] = conversation_id
    return outer


def _write_transcript_full(
    agy_home: Path,
    conv_id: str,
    content: str,
    *,
    mtime: float | None = None,
) -> Path:
    path = agy_home / "brain" / conv_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_transcript_compact(agy_home: Path, conv_id: str, content: str) -> Path:
    path = agy_home / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _selection_line(display: str = MODEL_DISPLAY) -> str:
    # agy's real shape: its own USER_INPUT / USER_EXPLICIT settings record. The
    # reader trusts only this record, never the same sentence inside file contents.
    import json as _json

    return _json.dumps(
        {
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "step_index": 0,
            "content": (
                "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` "
                f"from None to {display}. No need to comment on this change\n</USER_SETTINGS_CHANGE>"
            ),
        }
    )


def test_no_conversation_id_with_one_matching_transcript_attests_impl_and_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))

    impl_pkg = _impl_package(ws, target, "WP-FALLBACK-IMPL-1")

    def fake_run_impl(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 2\n", encoding="utf-8")
        _write_transcript_full(
            agy_home,
            "conv-unique-impl",
            _selection_line() + "\n" + json.dumps({"content": f"Package ID: {impl_pkg['package_id']}"}) + "\n",
        )
        outer = _outer_payload(
            impl_pkg,
            files_changed=[{"path": "src/worker.py", "change": "Updated value to 2."}],
        )
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_impl)
    raw = broker.consult_antigravity_cli("p", "Update value.", "accept-edits", MODEL, "high", 60, impl_pkg)
    envelope = json.loads(raw)
    assert envelope["disposition"] == "accepted"
    assert envelope["cli"]["attestation"] == "transcript"
    assert envelope["cli"]["model"] == MODEL
    assert envelope["cli"]["model_attested"] is True
    assert envelope["cli"]["conversation_id"] == "conv-unique-impl"
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    ro_pkg = _ro_package(ws, target, "WP-FALLBACK-RO-1")

    def fake_run_ro(_command, _cwd, *_args, **_kwargs):
        _write_transcript_full(
            agy_home,
            "conv-unique-ro",
            _selection_line() + "\n" + json.dumps({"content": f"Package ID: {ro_pkg['package_id']}"}) + "\n",
        )
        return (0, json.dumps(_outer_payload(ro_pkg)), "")

    monkeypatch.setattr(broker, "run_process", fake_run_ro)
    raw_ro = broker.consult_antigravity_cli("p", "Inspect.", "plan", MODEL, "high", 60, ro_pkg)
    env_ro = json.loads(raw_ro)
    assert env_ro["disposition"] == "accepted"
    assert env_ro["caveats"] == []
    assert env_ro["cli"]["attestation"] == "transcript"
    assert env_ro["cli"]["model"] == MODEL
    assert env_ro["cli"]["model_attested"] is True
    assert env_ro["cli"]["conversation_id"] == "conv-unique-ro"


def test_no_conversation_id_with_one_matching_transcript_conflict_rejects_before_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))
    pkg = _impl_package(ws, target, "WP-FALLBACK-CONFLICT")

    apply_mock = mock.Mock(side_effect=AssertionError("apply_changes must not run on attestation conflict"))
    monkeypatch.setattr(fm, "apply_changes", apply_mock)

    def fake_run(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 99\n", encoding="utf-8")
        _write_transcript_full(
            agy_home,
            "conv-wrong-model",
            f"Package ID: {pkg['package_id']}\n{_selection_line(OTHER_MODEL_DISPLAY)}\n",
        )
        outer = _outer_payload(pkg, files_changed=[{"path": "src/worker.py", "change": "Edited."}])
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run)
    resp = broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, pkg)
    apply_mock.assert_not_called()
    assert resp.startswith("Antigravity CLI structured-output validation failed:")
    assert "backend attested" in resp
    assert "NOTHING was written back" in resp
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_no_conversation_id_with_two_matching_transcripts_rejects_impl_and_caveats_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))

    pkg = _impl_package(ws, target, "WP-FALLBACK-TWO")
    apply_mock = mock.Mock(side_effect=AssertionError("apply_changes must not run when ambiguous"))
    monkeypatch.setattr(fm, "apply_changes", apply_mock)

    def fake_run_impl(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 3\n", encoding="utf-8")
        for conv in ("conv-dup-1", "conv-dup-2"):
            _write_transcript_full(
                agy_home,
                conv,
                f"Package ID: {pkg['package_id']}\n{_selection_line()}\n",
            )
        outer = _outer_payload(pkg, files_changed=[{"path": "src/worker.py", "change": "Edited."}])
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_impl)
    resp = broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, pkg)
    apply_mock.assert_not_called()
    assert resp.startswith("Antigravity CLI structured-output validation failed:")
    assert "an unattested model is never accepted before a workspace write" in resp
    assert "NOTHING was written back" in resp
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    ro_pkg = _ro_package(ws, target, "WP-FALLBACK-TWO-RO")

    def fake_run_ro(_command, _cwd, *_args, **_kwargs):
        for conv in ("conv-ro-dup-1", "conv-ro-dup-2"):
            _write_transcript_full(
                agy_home,
                conv,
                f"Package ID: {ro_pkg['package_id']}\n{_selection_line()}\n",
            )
        return (0, json.dumps(_outer_payload(ro_pkg)), "")

    monkeypatch.setattr(broker, "run_process", fake_run_ro)
    raw_ro = broker.consult_antigravity_cli("p", "Inspect.", "plan", MODEL, "high", 60, ro_pkg)
    env_ro = json.loads(raw_ro)
    assert env_ro["disposition"] == "accepted_with_caveats"
    assert env_ro["worker_status"] == "completed_with_caveats"
    assert env_ro["cli"]["attestation"] == "none"
    assert env_ro["cli"]["model"] is None
    assert env_ro["cli"]["model_attested"] is False
    assert env_ro["caveats"] == [
        {
            "code": "model_unattested",
            "detail": "agy returned no conversation id and no transcript matched this package",
        }
    ]


def test_no_conversation_id_with_no_transcript_rejects_impl_and_keeps_read_only_with_caveat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))

    pkg = _impl_package(ws, target, "WP-FALLBACK-NONE")
    apply_mock = mock.Mock(side_effect=AssertionError("apply_changes must not run without attestation"))
    monkeypatch.setattr(fm, "apply_changes", apply_mock)

    def fake_run_impl(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 4\n", encoding="utf-8")
        # Unrelated session for a different package id must not match.
        _write_transcript_full(
            agy_home,
            "conv-other",
            f"Package ID: WP-DIFFERENT\n{_selection_line()}\n",
        )
        outer = _outer_payload(pkg, files_changed=[{"path": "src/worker.py", "change": "Edited."}])
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_impl)
    resp = broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, pkg)
    apply_mock.assert_not_called()
    assert resp.startswith("Antigravity CLI structured-output validation failed:")
    assert "an unattested model is never accepted before a workspace write" in resp
    assert "NOTHING was written back" in resp
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    ro_pkg = _ro_package(ws, target, "WP-FALLBACK-NONE-RO")

    def fake_run_ro(_command, _cwd, *_args, **_kwargs):
        return (0, json.dumps(_outer_payload(ro_pkg)), "")

    monkeypatch.setattr(broker, "run_process", fake_run_ro)
    monkeypatch.setattr(broker, "_MCP_CLIENT_NAME", "codex-vscode")
    monkeypatch.setattr(broker, "store_consultation", lambda *a, **k: None)
    result = broker.consult(
        "antigravity",
        {
            "prompt": "Inspect worker.py.",
            "task_kind": "quick_check",
            "mode": "plan",
            "target_model": MODEL,
            "effort": "high",
            "work_package_id": ro_pkg["package_id"],
            "read_context": [str(target)],
            "workspace_root": str(ws),
            "acceptance_criteria": list(ro_pkg["acceptance_criteria"]),
            "async": False,
        },
    )
    assert result["status"] == "ok"
    assert result["outcome"] == "completed_verified"
    assert result["credit_eligible"] is True
    assert result["disposition"] == "accepted_with_caveats"
    assert result["worker_status"] == "completed_with_caveats"
    assert result["brain_verification"]["required"] is True
    assert result["attestation"] == "none"
    assert result["attested_model"] is None
    assert result["model_attested"] is False
    assert result["caveats"] == [
        {
            "code": "model_unattested",
            "detail": "agy returned no conversation id and no transcript matched this package",
        }
    ]


def test_no_conversation_id_matched_transcript_without_model_line_rejects_impl_and_caveats_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))

    pkg = _impl_package(ws, target, "WP-NO-MODEL-LINE-IMPL")
    apply_mock = mock.Mock(side_effect=AssertionError("apply_changes must not run without model line"))
    monkeypatch.setattr(fm, "apply_changes", apply_mock)

    def fake_run_impl(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 5\n", encoding="utf-8")
        _write_transcript_full(agy_home, "conv-no-model", f"Package ID: {pkg['package_id']}\n")
        outer = _outer_payload(pkg, files_changed=[{"path": "src/worker.py", "change": "Edited."}])
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_impl)
    resp = broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, pkg)
    apply_mock.assert_not_called()
    assert "an unattested model is never accepted before a workspace write" in resp
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    ro_pkg = _ro_package(ws, target, "WP-NO-MODEL-LINE-RO")

    def fake_run_ro(_command, _cwd, *_args, **_kwargs):
        _write_transcript_full(agy_home, "conv-ro-no-model", f"Package ID: {ro_pkg['package_id']}\n")
        return (0, json.dumps(_outer_payload(ro_pkg)), "")

    monkeypatch.setattr(broker, "run_process", fake_run_ro)
    env_ro = json.loads(broker.consult_antigravity_cli("p", "Inspect.", "plan", MODEL, "high", 60, ro_pkg))
    assert env_ro["disposition"] == "accepted_with_caveats"
    assert [c["code"] for c in env_ro["caveats"]] == ["model_unattested"]


def test_transcript_older_than_run_start_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))

    pkg = _impl_package(ws, target, "WP-STALE-TRANSCRIPT")
    stale_time = time.time() - 5.0
    _write_transcript_full(
        agy_home,
        "conv-stale",
        f"Package ID: {pkg['package_id']}\n{_selection_line()}\n",
        mtime=stale_time,
    )

    apply_mock = mock.Mock(side_effect=AssertionError("apply_changes must not trust stale transcript"))

    def fake_run_only_stale(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 6\n", encoding="utf-8")
        outer = _outer_payload(pkg, files_changed=[{"path": "src/worker.py", "change": "Edited."}])
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_only_stale)
    with mock.patch.object(fm, "apply_changes", apply_mock):
        resp = broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, pkg)
    apply_mock.assert_not_called()
    assert "an unattested model is never accepted before a workspace write" in resp
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    # When one stale and one fresh transcript both contain the package id,
    # ignoring the stale one leaves the single fresh transcript and attests cleanly.
    pkg2 = _impl_package(ws, target, "WP-STALE-PLUS-FRESH")
    _write_transcript_full(
        agy_home,
        "conv-stale-2",
        f"Package ID: {pkg2['package_id']}\n{_selection_line(OTHER_MODEL_DISPLAY)}\n",
        mtime=time.time() - 5.0,
    )

    def fake_run_with_fresh(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 7\n", encoding="utf-8")
        _write_transcript_full(
            agy_home,
            "conv-fresh",
            f"Package ID: {pkg2['package_id']}\n{_selection_line()}\n",
        )
        outer = _outer_payload(pkg2, files_changed=[{"path": "src/worker.py", "change": "Edited."}])
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_with_fresh)
    resp2 = broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, pkg2)
    env2 = json.loads(resp2)
    assert env2["disposition"] == "accepted"
    assert env2["cli"]["conversation_id"] == "conv-fresh"
    assert env2["cli"]["model"] == MODEL
    assert env2["cli"]["attestation"] == "transcript"
    assert target.read_text(encoding="utf-8") == "value = 7\n"


def test_scan_bounds_newest_50_files_and_last_512kb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    base_time = 2_000_000_000.0
    # 55 transcripts modified after run start; only the 50 newest may be scanned.
    for idx in range(55):
        content = "WP-OUTSIDE-50\n" if idx == 0 else "unrelated\n"
        _write_transcript_full(agy_home, f"conv-{idx:02d}", content, mtime=base_time + idx)
    assert broker._find_flash_transcript_by_package("WP-OUTSIDE-50", base_time) is None

    # A match inside the newest 50 is found.
    _write_transcript_full(agy_home, "conv-54", "WP-INSIDE-50\n", mtime=base_time + 54)
    assert broker._find_flash_transcript_by_package("WP-INSIDE-50", base_time) == "conv-54"

    # File larger than 512 KB: id placed only before the last 512 KB is not scanned.
    padding = "x" * (600 * 1024)
    _write_transcript_full(
        agy_home,
        "conv-head-only",
        "WP-HEAD-ONLY\n" + padding + "\ntail without id\n",
        mtime=base_time + 60,
    )
    assert broker._find_flash_transcript_by_package("WP-HEAD-ONLY", base_time) is None

    # File larger than 512 KB: id in last 512 KB matches, and model selection at start still attests.
    _write_transcript_full(
        agy_home,
        "conv-large-valid",
        _selection_line() + "\n" + padding + "\nWP-TAIL-MATCH\n",
        mtime=base_time + 61,
    )
    assert broker._find_flash_transcript_by_package("WP-TAIL-MATCH", base_time) == "conv-large-valid"
    assert broker._attest_flash_model_from_transcript("conv-large-valid") == MODEL


def test_normal_conversation_id_and_backend_model_paths_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agy_home = _patch_env(monkeypatch, tmp_path)
    ws, target = _setup_workspace(tmp_path)
    monkeypatch.setattr(broker, "resolve_project", lambda _p=None: broker.ProjectInfo("p", str(ws)))

    finder_mock = mock.Mock(side_effect=AssertionError("fallback finder must not run"))
    monkeypatch.setattr(broker, "_find_flash_transcript_by_package", finder_mock)

    # 1. Normal conversation_id + model=None in plan mode without model selection line:
    #    must still yield attestation="none" and NO model_unattested caveat.
    ro_pkg = _ro_package(ws, target, "WP-NORMAL-CONV-RO")
    _write_transcript_compact(agy_home, "conv-existing", "no model selection line\n")
    monkeypatch.setattr(
        broker,
        "run_process",
        lambda *_a, **_k: (0, json.dumps(_outer_payload(ro_pkg, conversation_id="conv-existing")), ""),
    )
    env_ro = json.loads(broker.consult_antigravity_cli("p", "Inspect.", "plan", MODEL, "high", 60, ro_pkg))
    assert env_ro["disposition"] == "accepted"
    assert env_ro["caveats"] == []
    assert env_ro["cli"]["attestation"] == "none"
    assert env_ro["cli"]["conversation_id"] == "conv-existing"
    finder_mock.assert_not_called()

    # 2. Normal conversation_id + model=None in accept-edits with compact transcript selection line:
    impl_pkg = _impl_package(ws, target, "WP-NORMAL-CONV-IMPL")
    _write_transcript_compact(agy_home, "conv-existing-impl", _selection_line() + "\n")

    def fake_run_impl(_command, cwd, *_args, **_kwargs):
        (Path(cwd) / "src" / "worker.py").write_text("value = 10\n", encoding="utf-8")
        outer = _outer_payload(
            impl_pkg,
            conversation_id="conv-existing-impl",
            files_changed=[{"path": "src/worker.py", "change": "Updated value."}],
        )
        return (0, json.dumps(outer), "")

    monkeypatch.setattr(broker, "run_process", fake_run_impl)
    env_impl = json.loads(broker.consult_antigravity_cli("p", "Update.", "accept-edits", MODEL, "high", 60, impl_pkg))
    assert env_impl["disposition"] == "accepted"
    assert env_impl["cli"]["attestation"] == "transcript"
    assert env_impl["cli"]["model"] == MODEL
    assert env_impl["cli"]["conversation_id"] == "conv-existing-impl"
    assert target.read_text(encoding="utf-8") == "value = 10\n"
    finder_mock.assert_not_called()

    # 3. Normal backend model present with conversation_id=None: unchanged backend attestation.
    attest_mock = mock.Mock(side_effect=AssertionError("transcript attestation must not run"))
    monkeypatch.setattr(broker, "_attest_flash_model_from_transcript", attest_mock)
    ro_pkg2 = _ro_package(ws, target, "WP-NORMAL-BACKEND")
    monkeypatch.setattr(
        broker,
        "run_process",
        lambda *_a, **_k: (0, json.dumps(_outer_payload(ro_pkg2, model=MODEL)), ""),
    )
    env_backend = json.loads(broker.consult_antigravity_cli("p", "Inspect.", "plan", MODEL, "high", 60, ro_pkg2))
    assert env_backend["disposition"] == "accepted"
    assert env_backend["caveats"] == []
    assert env_backend["cli"]["attestation"] == "backend"
    assert env_backend["cli"]["model"] == MODEL
    assert env_backend["cli"]["model_attested"] is True
    finder_mock.assert_not_called()
    attest_mock.assert_not_called()
