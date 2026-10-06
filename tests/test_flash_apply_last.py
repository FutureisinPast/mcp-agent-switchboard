"""WP-FL1: validate once, apply last, and a truthful apply outcome.

Covers the single pre-apply validation (staged and real path spellings), every
pre-apply rejection leaving the real tree byte-identical, the post-commit invariant
(sync and async finalizer never report "rejected" / "NOT applied" once a real file was
written), apply_changes rollback, the narrow cache exclusions, staged-only worker
prompts, and the PYTEST_ADDOPTS merge. agy, transcripts and the worker start are
fakes; no real Flash worker is ever launched.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import flash_manifest as fm  # noqa: E402
from test_flash_async_lane import FlashAsyncBase  # noqa: E402

CRITERIA = ["Focused tests pass.", "No files outside the allowlist change."]
MODEL = "gemini-3.7-flash-high"
STALE_PHRASES = ("not applied", "nothing was written back", "not_applied")


def _no_stale_claims(testcase: unittest.TestCase, value) -> None:
    text = json.dumps(value, default=str).lower() if not isinstance(value, str) else value.lower()
    for phrase in STALE_PHRASES:
        testcase.assertNotIn(phrase, text)


class ApplyLastBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fl1-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "ws"
        self.file_a = self.ws / "src" / "a.py"
        self.file_b = self.ws / "src" / "b.py"
        self.ctx = self.ws / "ctx" / "ref.txt"
        for path, text in ((self.file_a, "a = 1\n"), (self.file_b, "b = 2\n"), (self.ctx, "reference\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.orig = {p: p.read_bytes() for p in (self.file_a, self.file_b, self.ctx)}
        for p in (
            mock.patch.object(broker, "QUARANTINE_ROOT", self.tmp / "q"),
            mock.patch.object(broker, "QUARANTINE_LOCK_PATH", self.tmp / "q.lock"),
            mock.patch.object(broker, "WORKER_LOG_DIR", self.tmp / "logs"),
            mock.patch.object(broker, "_flash_apply_dir", return_value=self.tmp / "flash-apply"),
            mock.patch.object(broker, "FLASH_TRANSCRIPT_WAIT_SECONDS", 0.0),
            mock.patch.object(broker, "_antigravity_cli_home", return_value=self.tmp / "agy"),
            mock.patch.dict(broker._FLASH_APPLY_MEMORY, {}, clear=True),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.package = broker.prepare_flash_work_package(
            {
                "work_package_id": "WP-FL1-TEST",
                "allowed_writes": [str(self.file_a), str(self.file_b)],
                "read_context": [str(self.ctx)],
                "workspace_root": str(self.ws),
                "acceptance_criteria": list(CRITERIA),
            },
            "implementation",
            "Implement the bounded change.",
        )

    def payload(self, paths, *, status="completed", ambiguities=None, drop=None, model=MODEL, conversation=None):
        structured = {
            "package_id": self.package["package_id"],
            "status": status,
            "summary": "done",
            "acceptance_criteria": [{"criterion": c, "status": "passed", "evidence": ["x"]} for c in CRITERIA],
            "files_changed": [{"path": str(p), "change": "modified"} for p in paths],
            "checks": [],
            "evidence": [],
            "claims": [],
            "research_coverage": [],
            "ambiguities": list(ambiguities or []),
            "risks": [],
            "next_action": "verify",
            "brain_verification_required": "required",
        }
        if drop:
            structured.pop(drop)
        outer = {"status": "SUCCESS", "structured_output": structured}
        if model:
            outer["model"] = model
        if conversation:
            outer["conversation_id"] = conversation
        return json.dumps(outer)

    def run_cli(self, stdout, edit=True, spelling="staged", mode="accept-edits", patches=()):
        """Run consult_antigravity_cli against a fake agy that edits the STAGED files."""
        seen = {}

        def fake_run(command, cwd, *a, **k):
            seen["command"] = command
            seen["env"] = k.get("extra_env")
            if edit:
                for name in ("a", "b"):
                    (Path(cwd) / "src" / f"{name}.py").write_text(f"{name} = 'edited'\n", encoding="utf-8")
            return (0, stdout(Path(cwd)) if callable(stdout) else stdout, "")

        stack = [
            mock.patch.object(broker, "load_config", return_value={}),
            mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"),
            mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))),
            mock.patch.object(broker, "run_process", side_effect=fake_run),
            *patches,
        ]
        for m in stack:
            m.start()
        try:
            response = broker.consult_antigravity_cli("p", "bounded prompt", mode, MODEL, "high", 60, self.package)
        finally:
            for m in reversed(stack):
                m.stop()
        return response, seen

    def assert_untouched(self):
        for path, data in self.orig.items():
            self.assertEqual(path.read_bytes(), data, path)


class ValidateOnceApplyOnceTests(ApplyLastBase):
    def _counted(self, stdout):
        calls = {"validate": 0, "apply": 0}
        real_validate = broker.validate_flash_workhorse_result
        real_apply = fm.apply_changes

        def validate(*a, **k):
            calls["validate"] += 1
            return real_validate(*a, **k)

        def apply(*a, **k):
            calls["apply"] += 1
            return real_apply(*a, **k)

        response, seen = self.run_cli(
            stdout,
            patches=(
                mock.patch.object(broker, "validate_flash_workhorse_result", side_effect=validate),
                mock.patch.object(fm, "apply_changes", side_effect=apply),
            ),
        )
        return response, calls, seen

    def test_staged_path_report_validates_once_and_applies_once(self):
        def stdout(cwd):
            return self.payload([cwd / "src" / "a.py", cwd / "src" / "b.py"])

        response, calls, _ = self._counted(stdout)
        self.assertEqual(calls, {"validate": 1, "apply": 1})
        envelope = json.loads(response)
        self.assertEqual(envelope["disposition"], "accepted")
        self.assertEqual(envelope["applied_files"], sorted(str(p) for p in (self.file_a, self.file_b)))
        self.assertEqual(self.file_a.read_text(encoding="utf-8"), "a = 'edited'\n")
        _no_stale_claims(self, response)

    def test_real_path_report_validates_once_and_applies_once(self):
        response, calls, _ = self._counted(self.payload([self.file_a, self.file_b]))
        self.assertEqual(calls, {"validate": 1, "apply": 1})
        self.assertEqual(json.loads(response)["disposition"], "accepted")
        self.assertEqual(self.file_b.read_text(encoding="utf-8"), "b = 'edited'\n")

    def test_worker_env_and_staging_tree_lifetime(self):
        response, seen = self.run_cli(self.payload([self.file_a]))
        self.assertEqual(json.loads(response)["disposition"], "accepted")
        self.assertEqual(seen["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertIn("no:cacheprovider", seen["env"]["PYTEST_ADDOPTS"])


class PreApplyRejectionTests(ApplyLastBase):
    def _assert_rejected_before_apply(self, stdout, expect, patches=()):
        apply = mock.Mock(side_effect=AssertionError("apply_changes must not run"))
        response, _ = self.run_cli(stdout, patches=(mock.patch.object(fm, "apply_changes", apply), *patches))
        apply.assert_not_called()
        self.assertIn("structured-output validation failed", response)
        self.assertIn(expect, response)
        self.assertIn("NOTHING was written back", response)
        self.assert_untouched()

    def test_schema_failure(self):
        self._assert_rejected_before_apply(self.payload([self.file_a], drop="next_action"), "missing fields")

    def test_out_of_scope_report(self):
        self._assert_rejected_before_apply(
            self.payload([self.file_a, self.ws / "elsewhere.py"]), "out-of-scope file reported"
        )

    def test_contradictory_status(self):
        self._assert_rejected_before_apply(
            self.payload([self.file_a], ambiguities=["unclear"]), "contradicts non-empty ambiguities"
        )

    def test_attestation_conflict(self):
        self._assert_rejected_before_apply(self.payload([self.file_a], model="gemini-3.7-pro-low"), "attested")

    def test_transcript_attestation_conflict(self):
        transcript = self.tmp / "agy" / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            "changed setting `Model Selection` from A to Gemini 3.7 Pro (Low).\n", encoding="utf-8"
        )
        catalog = [{"id": "gemini-3.7-pro-low", "display": "Gemini 3.7 Pro (Low)"}]
        self._assert_rejected_before_apply(
            self.payload([self.file_a], model=None, conversation="conv-1"),
            "attested",
            patches=(mock.patch.object(broker, "discover_antigravity_models", return_value=catalog),),
        )

    def test_unavailable_transcript_rejects_before_mutation(self):
        self._assert_rejected_before_apply(
            self.payload([self.file_a], model=None, conversation="conv-missing"),
            "transcript was not available",
        )


class PostCommitInvariantTests(ApplyLastBase):
    def test_failure_after_commit_in_direct_path_is_applied_then_rejected(self):
        response, _ = self.run_cli(
            self.payload([self.file_a, self.file_b]),
            patches=(mock.patch.object(fm, "containment_receipt", side_effect=RuntimeError("boom")),),
        )
        envelope = json.loads(response)
        self.assertEqual(envelope["disposition"], "applied_then_rejected")
        self.assertEqual(envelope["applied_files"], sorted(str(p) for p in (self.file_a, self.file_b)))
        self.assertEqual(self.file_a.read_text(encoding="utf-8"), "a = 'edited'\n")
        _no_stale_claims(self, response)
        self.assertEqual(broker.classify_flash_outcome(response, "ok", None), "applied_then_rejected")
        progress = broker.flash_terminal_progress("applied_then_rejected", "accept-edits", response)
        apply_phase = [p for p in progress["phases"] if p["phase"] == "workspace_apply"][0]
        self.assertEqual(apply_phase["status"], "applied_then_failed")
        _no_stale_claims(self, progress)

    def test_apply_that_raises_reports_uncertain_with_candidates(self):
        response, _ = self.run_cli(
            self.payload([self.file_a]),
            patches=(mock.patch.object(fm, "apply_changes", side_effect=RuntimeError("disk gone")),),
        )
        envelope = json.loads(response)
        self.assertEqual(envelope["disposition"], "apply_outcome_uncertain")
        self.assertEqual(envelope["candidate_files"], sorted(str(p) for p in (self.file_a, self.file_b)))
        _no_stale_claims(self, response)

    def test_phase_inference_ignores_manifest_and_rejected_words(self):
        text = "Antigravity CLI structured-output validation failed: out of manifest scope; rejected"
        progress = broker.flash_terminal_progress("rejected", "accept-edits", text)
        phases = {p["phase"]: p["status"] for p in progress["phases"]}
        self.assertEqual(phases["containment_staging"], "completed")
        self.assertEqual(phases["worker_execution"], "completed")
        staging = "Antigravity CLI structured-output validation failed: staging failed (x)."
        phases = {p["phase"]: p["status"] for p in broker.flash_terminal_progress("rejected", "accept-edits", staging)["phases"]}
        self.assertEqual(phases["containment_staging"], "failed")
        self.assertEqual(phases["worker_execution"], "not_started")


class AsyncFinalizerPostCommitTests(FlashAsyncBase):
    def _committed(self):
        rid = broker.queue_flash_request(self.impl_args(max_response_chars=100000))["request_id"]
        broker._flash_apply_journal("WP-A", rid, "committed", applied=[str(self.file_a)])
        return rid

    def test_finalizer_failure_after_commit_is_applied_then_rejected(self):
        rid = self._committed()
        real = broker._flash_finalize_row
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return real(*a, **k)

        with mock.patch.object(broker, "_flash_finalize_row", side_effect=flaky):
            result, _ = self.run_worker(rid)
        self.assertEqual(result["status"], "error")
        final = broker.request_result(rid)
        self.assertEqual(final["outcome"], "applied_then_rejected")
        self.assertEqual(final["disposition"], "applied_then_rejected")
        self.assertEqual(final["applied_files"], [str(self.file_a)])
        _no_stale_claims(self, final)

    def test_worker_exception_after_commit_is_not_a_clean_failure(self):
        rid = self._committed()
        with mock.patch.object(broker, "consult", side_effect=RuntimeError("boom")):
            broker.run_flash_request_worker(rid)
        final = broker.request_result(rid)
        self.assertEqual(final["outcome"], "applied_then_rejected")
        self.assertEqual(final["applied_files"], [str(self.file_a)])
        _no_stale_claims(self, final)

    def test_uncertain_journal_lists_candidates(self):
        rid = broker.queue_flash_request(self.impl_args(max_response_chars=100000))["request_id"]
        broker._flash_apply_journal("WP-A", rid, "apply_started", candidates=[str(self.file_a), str(self.file_b)])
        with mock.patch.object(broker, "consult", side_effect=RuntimeError("boom")):
            broker.run_flash_request_worker(rid)
        final = broker.request_result(rid)
        self.assertEqual(final["outcome"], "apply_outcome_uncertain")
        self.assertEqual(final["candidate_files"], [str(self.file_a), str(self.file_b)])
        _no_stale_claims(self, final)

    def test_request_result_corrects_a_stored_clean_rejection(self):
        rid = self._committed()
        stale = json.dumps(
            {
                "status": "error",
                "outcome": "rejected",
                "disposition": "rejected",
                "response": "refused and NOTHING was written back",
                "note_quarantine": "Quarantined artifacts were NOT accepted and NOT applied.",
            }
        )
        broker._flash_finalize_row(rid, "completed", stale, None, None, None)
        final = broker.request_result(rid)
        self.assertEqual(final["disposition"], "applied_then_rejected")
        self.assertNotIn("note_quarantine", final)
        _no_stale_claims(self, final)

    def test_no_journal_means_the_ordinary_failure_stays_pre_mutation(self):
        rid = broker.queue_flash_request(self.impl_args(max_response_chars=100000))["request_id"]
        with mock.patch.object(broker, "consult", side_effect=RuntimeError("boom")):
            broker.run_flash_request_worker(rid)
        self.assertEqual(broker.request_result(rid)["outcome"], "failed_pre_mutation")


class ManifestCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="fl1-fm-"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.roots = []

    def tearDown(self):
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    def write(self, rel, text="original\n"):
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def stage(self, writes=(), creates=(), context=()):
        manifest = fm.build_manifest(
            {
                "allowed_writes": [str(p) for p in writes],
                "allowed_creates": [str(p) for p in creates],
                "read_context": [str(p) for p in context],
            },
            str(self.ws),
            implementation_mode=True,
        )
        staged = fm.stage(manifest)
        self.roots.append(staged.root)
        return staged


class RollbackTests(ManifestCase):
    def _two(self):
        a = self.write("a.py", "A0\n")
        b = self.write("b.py", "B0\n")
        staged = self.stage(writes=[a, b])
        (staged.root / "a.py").write_text("A1\n", encoding="utf-8")
        (staged.root / "b.py").write_text("B1\n", encoding="utf-8")
        return a, b, staged

    def _failing_replace(self, fail_from):
        real = fm.atomic_io._replace_with_retry
        calls = {"n": 0}

        def replace(source, target):
            calls["n"] += 1
            if calls["n"] >= fail_from:
                raise OSError("injected write failure")
            return real(source, target)

        return mock.patch.object(fm.atomic_io, "_replace_with_retry", side_effect=replace)

    def test_clean_apply_reports_applied(self):
        a, b, staged = self._two()
        report = fm.apply_changes(staged, fm.collect_changes(staged))
        self.assertEqual(report["applied"], sorted([str(a), str(b)]))
        self.assertFalse(report["refused"])

    def test_mid_apply_failure_rolls_back_to_baseline(self):
        a, b, staged = self._two()
        changes = fm.collect_changes(staged)
        real = fm.atomic_io._replace_with_retry
        calls = {"n": 0}

        def replace(source, target):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected write failure")
            return real(source, target)

        with mock.patch.object(fm.atomic_io, "_replace_with_retry", side_effect=replace):
            report = fm.apply_changes(staged, changes)
        self.assertTrue(report["rolled_back"])
        self.assertEqual(report["applied"], [])
        self.assertTrue(report["refused"])
        self.assertEqual((a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")), ("A0\n", "B0\n"))

    def test_failed_rollback_reports_rollback_incomplete_with_files(self):
        a, b, staged = self._two()
        changes = fm.collect_changes(staged)
        with self._failing_replace(fail_from=2):
            report = fm.apply_changes(staged, changes)
        self.assertTrue(report["rollback_incomplete"])
        self.assertFalse(report["rolled_back"])
        self.assertEqual(report["affected_files"], [str(a)])
        self.assertEqual(report["applied"], [str(a)])
        self.assertEqual(a.read_text(encoding="utf-8"), "A1\n")
        self.assertEqual(b.read_text(encoding="utf-8"), "B0\n")

    def test_rollback_incomplete_surfaces_as_applied_then_rejected(self):
        truth = {"applied": [], "affected_files": ["x"], "rollback_incomplete": True}
        self.assertEqual(broker._flash_report_committed_files(truth), ["x"])

    def test_refusals_stay_all_or_nothing(self):
        a = self.write("a.py", "A0\n")
        ctx = self.write("ctx.txt", "C0\n")
        staged = self.stage(writes=[a], context=[ctx])
        (staged.root / "a.py").write_text("A1\n", encoding="utf-8")
        (staged.root / "ctx.txt").write_text("C1\n", encoding="utf-8")
        report = fm.apply_changes(staged, fm.collect_changes(staged))
        self.assertTrue(report["refused"])
        self.assertEqual(report["applied"], [])
        self.assertEqual(a.read_text(encoding="utf-8"), "A0\n")


class CacheExclusionTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.a = self.write("src/a.py", "A0\n")
        self.ctx = self.write("src/ref.py", "R0\n")
        self.staged = self.stage(writes=[self.a], context=[self.ctx])

    def _put(self, rel, data=b"x"):
        path = self.staged.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_pyc_and_pytest_cache_are_ignored_and_never_applied(self):
        self._put("src/__pycache__/a.cpython-311.pyc")
        self._put(".pytest_cache/v/cache/lastfailed", b"{}")
        (self.staged.root / "src" / "a.py").write_text("A1\n", encoding="utf-8")
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["undeclared"], [])
        self.assertEqual(changes["created"], [])
        self.assertEqual(
            changes["ignored_cache"], [".pytest_cache/v/cache/lastfailed", "src/__pycache__/a.cpython-311.pyc"]
        )
        report = fm.apply_changes(self.staged, changes)
        self.assertEqual(report["applied"], [str(self.a)])
        self.assertEqual(self.a.read_text(encoding="utf-8"), "A1\n")

    def test_modified_read_context_is_still_refused(self):
        (self.staged.root / "src" / "ref.py").write_text("R1\n", encoding="utf-8")
        self._put("src/__pycache__/a.cpython-311.pyc")
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["read_context_modified"], ["src/ref.py"])
        self.assertTrue(fm.apply_changes(self.staged, changes)["refused"])

    def test_new_undeclared_py_file_is_still_refused(self):
        self._put("src/new_module.py", b"x = 1\n")
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["undeclared"], ["src/new_module.py"])
        self.assertTrue(fm.apply_changes(self.staged, changes)["refused"])

    def test_a_pyc_outside_pycache_or_with_other_suffix_is_not_exempt(self):
        self._put("src/stray.pyc")
        self._put("src/__pycache__/notes.txt")
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["undeclared"], ["src/__pycache__/notes.txt", "src/stray.pyc"])

    def test_declared_file_named_like_cache_is_not_hidden(self):
        creates = self.ws / "pkg" / "__pycache__" / "gen.pyc"
        staged = self.stage(creates=[creates])
        (staged.root / "pkg" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (staged.root / "pkg" / "__pycache__" / "gen.pyc").write_bytes(b"x")
        changes = fm.collect_changes(staged)
        self.assertEqual(changes["created"], ["pkg/__pycache__/gen.pyc"])
        self.assertEqual(changes["ignored_cache"], [])

    def test_symlink_inside_pycache_is_still_refused(self):
        target = self.staged.root / "src" / "a.py"
        link = self.staged.root / "src" / "__pycache__" / "evil.cpython-311.pyc"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation is not permitted in this environment: {exc}")
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["reparse_planted"], ["src/__pycache__/evil.cpython-311.pyc"])
        self.assertTrue(fm.apply_changes(self.staged, changes)["refused"])


class PromptAndEnvTests(ApplyLastBase):
    def test_worker_prompt_has_no_real_workspace_path(self):
        wrapped = broker.wrap_flash_workhorse_prompt(f"Edit {self.file_a} now.", self.package)
        self.assertIn(str(self.file_a), wrapped)  # the brain-side prompt still names real paths

        response, seen = self.run_cli(self.payload([self.file_a]))
        # The agy command's prompt is the last argument.
        prompt = seen["command"][-1]
        # The workspace root's absolute path never reaches the worker.
        self.assertNotIn(str(self.ws).lower(), prompt.lower())
        self.assertNotIn(self.ws.as_posix().lower(), prompt.lower())
        self.assertIn("WORKING COPY", prompt)

    def test_worker_prompt_from_wrapped_package_uses_staged_paths_only(self):
        wrapped = broker.wrap_flash_workhorse_prompt(f"Edit {self.file_a} now.", self.package)

        def fake_run(command, cwd, *a, **k):
            fake_run.prompt = command[-1]
            fake_run.cwd = cwd
            return (0, "", "")

        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", side_effect=fake_run):
            broker.consult_antigravity_cli("p", wrapped, "accept-edits", MODEL, "high", 60, self.package)
        low = fake_run.prompt.lower()
        self.assertNotIn(str(self.ws).lower(), low)
        self.assertNotIn(self.ws.as_posix().lower(), low)
        self.assertIn(Path(fake_run.cwd).as_posix().lower(), low.replace("\\", "/"))

    def test_scope_key_accepts_either_spelling(self):
        manifest = fm.build_manifest(self.package, str(self.ws), implementation_mode=True)
        staged = fm.stage(manifest)
        self.addCleanup(shutil.rmtree, staged.root, True)
        roots = broker._scope_roots(staged, self.package)
        self.assertEqual(
            broker._scope_key(str(self.file_a), roots), broker._scope_key(str(staged.staged_path("src/a.py")), roots)
        )

    def test_pytest_addopts_merge_preserves_existing_value(self):
        merged = broker.flash_worker_env_overrides({"PYTEST_ADDOPTS": "-x --maxfail=2"})
        self.assertEqual(merged["PYTEST_ADDOPTS"], "-x --maxfail=2 -p no:cacheprovider")
        self.assertEqual(merged["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(broker.flash_worker_env_overrides({})["PYTEST_ADDOPTS"], "-p no:cacheprovider")
        again = broker.flash_worker_env_overrides({"PYTEST_ADDOPTS": "-q -p no:cacheprovider"})
        self.assertEqual(again["PYTEST_ADDOPTS"], "-q -p no:cacheprovider")

    def test_run_process_passes_extra_env_to_the_child(self):
        with mock.patch.object(broker.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            broker.run_process(["x"], ".", None, 5, extra_env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")


class NonCompletedWorkerNeverAppliesTests(ApplyLastBase):
    def _with_status(self, status):
        outer = json.loads(self.payload([self.file_a, self.file_b], status=status))
        outer["structured_output"]["ambiguities"] = ["needs a decision"]
        return json.dumps(outer)

    def test_blocked_and_failed_workers_get_zero_apply_calls(self):
        for status in ("blocked", "failed"):
            with self.subTest(status=status):
                apply = mock.Mock(side_effect=AssertionError("apply_changes must not run"))
                response, _ = self.run_cli(
                    self._with_status(status), patches=(mock.patch.object(fm, "apply_changes", apply),)
                )
                apply.assert_not_called()
                self.assert_untouched()
                envelope = json.loads(response)
                self.assertEqual(envelope["worker_status"], status)
                self.assertEqual(envelope["applied_files"], [])
                self.assertIn("never applied", envelope["apply_skipped_reason"])
                self.assertEqual(sorted(envelope["quarantine_files"]), ["files/src/a.py", "files/src/b.py"])
                self.assertEqual(
                    broker.classify_flash_outcome(response, "ok", envelope["structured_output"]),
                    "blocked" if status == "blocked" else "failed_pre_mutation",
                )

    def test_completed_worker_still_applies(self):
        apply = mock.Mock(wraps=fm.apply_changes)
        self.run_cli(
            self.payload([self.file_a, self.file_b]), patches=(mock.patch.object(fm, "apply_changes", apply),)
        )
        apply.assert_called_once()
        self.assertEqual(self.file_a.read_text(encoding="utf-8"), "a = 'edited'\n")


class NeedsContextTests(ApplyLastBase):
    def setUp(self):
        super().setUp()
        (self.ws / "helpers_mod.py").write_text("X = 1\n", encoding="utf-8")
        pkg = self.ws / "pkgmod"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")

    def _stdout(self, checks):
        outer = json.loads(self.payload([self.file_a, self.file_b]))
        outer["structured_output"]["checks"] = checks
        return json.dumps(outer)

    @staticmethod
    def _check(excerpt, status="failed"):
        return {"command": "pytest", "status": status, "exit_code": 1, "output_excerpt": excerpt}

    def _run(self, checks):
        apply = mock.Mock(side_effect=AssertionError("apply_changes must not run"))
        response, _ = self.run_cli(self._stdout(checks), patches=(mock.patch.object(fm, "apply_changes", apply),))
        apply.assert_not_called()
        self.assert_untouched()
        return response

    def test_qualifying_import_error_is_needs_context_with_zero_apply(self):
        response = self._run([
            self._check("ImportError while loading conftest\nModuleNotFoundError: No module named 'helpers_mod'"),
            self._check("ModuleNotFoundError: No module named 'pkgmod'"),
        ])
        self.assertTrue(response.startswith("Antigravity CLI needs_context:"))
        self.assertIn("re-dispatch with these paths in read_context", response)
        self.assertIn("helpers_mod.py", response)
        self.assertIn("__init__.py", response)
        self.assertIn("quarantine_path", response)
        self.assertEqual(broker.classify_flash_outcome(response, "error", None), "needs_context")
        progress = broker.flash_terminal_progress("needs_context", "accept-edits", response)
        self.assertEqual(progress["state"], "needs_context")
        with mock.patch.object(broker, "family_from_caller", return_value="codex"), \
             mock.patch.object(broker, "current_codex_role_model", return_value="m"):
            handoff = broker.native_handoff_for_flash_outcome(
                {"work_package_id": "W"}, "needs_context", "broker:1", None, None, ["/x/helpers_mod.py"]
            )
        self.assertIn("read_context", handoff["action"])
        self.assertIsNone(handoff["flash_skip_reason"])
        self.assertNotIn("brain_review", handoff)

    def test_import_error_for_module_absent_from_workspace_stays_a_rejection(self):
        response = self._run([self._check("ModuleNotFoundError: No module named 'does_not_exist'")])
        self.assertTrue(response.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("contradicts a failed check", response)

    def test_mixed_assertion_failure_and_import_error_stays_a_rejection(self):
        two_checks = self._run([
            self._check("ModuleNotFoundError: No module named 'helpers_mod'"),
            self._check("E   AssertionError: 1 != 2"),
        ])
        self.assertTrue(two_checks.startswith("Antigravity CLI structured-output validation failed:"))
        one_check = self._run([
            self._check("AssertionError: boom\nModuleNotFoundError: No module named 'helpers_mod'")
        ])
        self.assertTrue(one_check.startswith("Antigravity CLI structured-output validation failed:"))

    def test_module_already_staged_is_not_a_staging_limitation(self):
        self.package["read_context"] = [str(self.ws / "helpers_mod.py")]
        response = self._run([self._check("ModuleNotFoundError: No module named 'helpers_mod'")])
        self.assertTrue(response.startswith("Antigravity CLI structured-output validation failed:"))


class JournalIdentityTests(unittest.TestCase):
    """M1-M3: a journal record belongs to ONE run (package id + token), never to a time window."""

    def _write(self, pid, token, state, applied=()):
        broker._flash_apply_journal(pid, token, state, candidates=list(applied), applied=list(applied))

    def test_older_failed_run_is_not_relabelled_by_a_newer_committed_run(self):
        self._write("WP-1", "run-new", "committed", ["/ws/new.py"])
        self.assertIsNone(broker._flash_apply_lookup_truth("WP-1", "run-old"))
        self.assertIsNone(broker._flash_apply_lookup_truth("WP-1", None))
        self.assertIsNone(broker._flash_apply_lookup_truth("WP-1", ""))
        self.assertEqual(broker._flash_apply_lookup_truth("WP-1", "run-new")["files"], ["/ws/new.py"])

    def test_sanitised_ids_stay_isolated(self):
        self._write("WP/3", "t", "committed", ["/ws/a.py"])
        self.assertIsNone(broker._flash_apply_lookup_truth("WP_3", "t"))
        self.assertIsNotNone(broker._flash_apply_lookup_truth("WP/3", "t"))
        # even on disk alone (memory cleared), the exact id is compared
        broker._FLASH_APPLY_MEMORY.clear()
        self.assertIsNone(broker._flash_apply_lookup_truth("WP_3", "t"))
        self.assertIsNotNone(broker._flash_apply_lookup_truth("WP/3", "t"))

    def test_long_ids_with_a_shared_prefix_stay_isolated(self):
        prefix = "WP-" + "x" * 90
        self._write(prefix + "-A", "t", "committed", ["/ws/a.py"])
        self.assertIsNone(broker._flash_apply_lookup_truth(prefix + "-B", "t"))
        self.assertNotEqual(
            broker._flash_apply_record_path(prefix + "-A", "t"), broker._flash_apply_record_path(prefix + "-B", "t")
        )
        broker._FLASH_APPLY_MEMORY.clear()
        self.assertIsNone(broker._flash_apply_lookup_truth(prefix + "-B", "t"))

    def test_concurrent_same_id_run_cannot_erase_a_committed_record(self):
        self._write("WP-C", "run-1", "committed", ["/ws/a.py"])
        self._write("WP-C", "run-2", "no_write")
        self.assertEqual(broker._flash_apply_lookup_truth("WP-C", "run-1")["files"], ["/ws/a.py"])
        self.assertIsNone(broker._flash_apply_lookup_truth("WP-C", "run-2"))

    def test_run_launched_half_a_second_after_an_old_commit_sees_nothing(self):
        self._write("WP-D", "run-old", "committed", ["/ws/a.py"])
        self.assertIsNone(broker._flash_apply_lookup_truth("WP-D", "run-launched-0.5s-later"))


class StickyJournalTests(unittest.TestCase):
    def test_committed_is_never_overwritten_by_a_restarted_worker(self):
        broker._flash_apply_journal("WP-S", "req", "committed", applied=["/ws/first.py"])
        broker._flash_apply_journal("WP-S", "req", "apply_started", candidates=["/ws/second.py"])
        broker._flash_apply_journal("WP-S", "req", "no_write")
        truth = broker._flash_apply_lookup_truth("WP-S", "req")
        self.assertEqual(truth["disposition"], "applied_then_rejected")
        self.assertEqual(truth["files"], ["/ws/first.py"])
        broker._FLASH_APPLY_MEMORY.clear()  # a fresh process reads the disk record
        broker._flash_apply_journal("WP-S", "req", "no_write")
        self.assertEqual(broker._flash_apply_lookup_truth("WP-S", "req")["files"], ["/ws/first.py"])

    def test_rollback_incomplete_is_sticky_and_apply_started_still_advances(self):
        broker._flash_apply_journal("WP-T", "r", "apply_started", candidates=["/ws/a.py"])
        self.assertEqual(broker._flash_apply_lookup_truth("WP-T", "r")["disposition"], "apply_outcome_uncertain")
        broker._flash_apply_journal("WP-T", "r", "rollback_incomplete", applied=["/ws/a.py"])
        broker._flash_apply_journal("WP-T", "r", "no_write")
        self.assertTrue(broker._flash_apply_lookup_truth("WP-T", "r")["rollback_incomplete"])
        broker._flash_apply_journal("WP-U", "r", "apply_started", candidates=["/ws/a.py"])
        broker._flash_apply_journal("WP-U", "r", "rolled_back")
        self.assertIsNone(broker._flash_apply_lookup_truth("WP-U", "r"))


class RemapBoundaryTests(ApplyLastBase):
    def _staged(self):
        manifest = fm.build_manifest(self.package, str(self.ws), implementation_mode=True)
        staged = fm.stage(manifest)
        self.addCleanup(shutil.rmtree, staged.root, True)
        return staged

    def test_declared_path_is_remapped(self):
        staged = self._staged()
        out = fm.remap_to_staged(f"Edit {self.file_a} please.", staged)
        self.assertIn(str(staged.staged_path("src/a.py")), out)
        self.assertNotIn(str(self.file_a), out)

    def test_sibling_prefix_path_is_untouched(self):
        staged = self._staged()
        for tail in ("2", ".bak", "_old", "-x"):
            text = f"see {self.file_a}{tail} and {self.ws}2\\x"
            self.assertEqual(fm.remap_to_staged(text, staged), text)

    def test_non_declared_workspace_path_in_the_body_is_untouched(self):
        staged = self._staged()
        text = f"DATA_DIR={self.ws}\\data and {self.ws.as_posix()}/other.txt"
        self.assertEqual(fm.remap_to_staged(text, staged), text)

    def test_plan_mode_prompt_is_not_remapped_and_has_no_table(self):
        seen = {}

        def fake_run(command, cwd, *a, **k):
            seen["prompt"] = command[-1]
            return (0, "", "")

        plan_pkg = broker.prepare_flash_work_package(
            {"work_package_id": "WP-PLAN", "read_context": [str(self.file_a)], "workspace_root": str(self.ws),
             "research_questions": ["What is in a.py?"]},
            "research", "Investigate.",
        )
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", side_effect=fake_run):
            broker.consult_antigravity_cli("p", f"Look at {self.file_a}", "plan", MODEL, "high", 60, plan_pkg)
        self.assertIn(str(self.file_a), seen["prompt"])
        self.assertNotIn("PATH TABLE", seen["prompt"])

    def test_appendix_table_and_rule_present_in_implementation_mode(self):
        response, seen = self.run_cli(self.payload([self.file_a]))
        prompt = seen["command"][-1]
        self.assertIn("PATH TABLE (workspace-relative path -> staged path). Edit and create files ONLY", prompt)
        self.assertIn("relative to the real workspace", prompt)
        self.assertIn("  src/a.py -> ", prompt)
        self.assertIn("  src/b.py -> ", prompt)
        self.assertNotIn(str(self.ws).lower(), prompt.lower())
        self.assertNotIn(self.ws.as_posix().lower(), prompt.lower())

    def test_non_declared_literal_in_the_body_passes_through_but_is_not_added_by_the_table(self):
        literal = f"DATA_DIR={self.ws}" + chr(92) + "data"
        response, seen = self.run_cli(self.payload([self.file_a]))
        self.assertNotIn("DATA_DIR", seen["command"][-1])
        manifest = fm.build_manifest(self.package, str(self.ws), implementation_mode=True)
        staged = fm.stage(manifest)
        self.addCleanup(shutil.rmtree, staged.root, True)
        appendix = fm.prompt_appendix(staged, real_table=True)
        self.assertNotIn(str(self.ws).lower(), appendix.lower())
        self.assertEqual(fm.remap_to_staged(literal, staged), literal)


class NeedsContextStrictTests(NeedsContextTests):
    def _assert_rejection(self, excerpt):
        response = self._run([self._check(excerpt)])
        self.assertTrue(response.startswith("Antigravity CLI structured-output validation failed:"), response[:200])

    def test_assert_line_without_the_word_assertionerror_is_a_rejection(self):
        self._assert_rejection("E   assert 3 == 4\nImportError: cannot import name 'x' from 'helpers_mod'")

    def test_other_error_type_is_a_rejection(self):
        self._assert_rejection("NameError: name 'q' is not defined\nFAILED t.py::t\nModuleNotFoundError: No module named 'helpers_mod'")

    def test_more_failed_markers_than_import_errors_is_a_rejection(self):
        self._assert_rejection(
            "FAILED a.py::one\nFAILED a.py::two\nFAILED a.py::three\nModuleNotFoundError: No module named 'helpers_mod'"
        )

    def test_package_root_resolution_no_longer_hides_a_wrong_import_root(self):
        nested = self.ws / "vendor"
        nested.mkdir()
        (nested / "deep_mod.py").write_text("Y = 1\n", encoding="utf-8")
        self.package["package_root"] = str(nested)
        self._assert_rejection("ModuleNotFoundError: No module named 'deep_mod'")


class RollbackHardeningTests(ManifestCase):
    def test_runtime_error_mid_apply_still_rolls_back(self):
        a = self.write("a.py", "A0\n")
        b = self.write("b.py", "B0\n")
        staged = self.stage(writes=[a, b])
        (staged.root / "a.py").write_text("A1\n", encoding="utf-8")
        (staged.root / "b.py").write_text("B1\n", encoding="utf-8")
        changes = fm.collect_changes(staged)
        real = fm.atomic_io._replace_with_retry
        calls = {"n": 0}

        def replace(source, target):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("not an OSError")
            return real(source, target)

        with mock.patch.object(fm.atomic_io, "_replace_with_retry", side_effect=replace):
            report = fm.apply_changes(staged, changes)
        self.assertTrue(report["rolled_back"])
        self.assertEqual((a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")), ("A0\n", "B0\n"))

    def test_created_directories_and_temp_files_are_cleaned_up(self):
        a = self.write("a.py", "A0\n")
        new = self.ws / "newdir" / "sub" / "n.py"
        staged = self.stage(writes=[a], creates=[new])
        (staged.root / "a.py").write_text("A1\n", encoding="utf-8")
        (staged.root / "newdir" / "sub").mkdir(parents=True, exist_ok=True)
        (staged.root / "newdir" / "sub" / "n.py").write_text("N\n", encoding="utf-8")
        changes = fm.collect_changes(staged)
        real = fm.atomic_io._replace_with_retry
        calls = {"n": 0}

        def replace(source, target):
            calls["n"] += 1
            if calls["n"] == 2:  # the create fails after a.py was written
                raise OSError("injected")
            return real(source, target)

        with mock.patch.object(fm.atomic_io, "_replace_with_retry", side_effect=replace):
            report = fm.apply_changes(staged, changes)
        self.assertTrue(report["rolled_back"])
        self.assertFalse((self.ws / "newdir").exists())
        self.assertEqual(a.read_text(encoding="utf-8"), "A0\n")
        self.assertEqual([p.name for p in self.ws.rglob("*.tmp")], [])

    def test_failed_restore_leaves_no_rollback_temp(self):
        a = self.write("a.py", "A0\n")
        b = self.write("b.py", "B0\n")
        staged = self.stage(writes=[a, b])
        (staged.root / "a.py").write_text("A1\n", encoding="utf-8")
        (staged.root / "b.py").write_text("B1\n", encoding="utf-8")
        changes = fm.collect_changes(staged)
        real = fm.atomic_io._replace_with_retry
        calls = {"n": 0}

        def replace(source, target):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("always failing")
            return real(source, target)

        with mock.patch.object(fm.atomic_io, "_replace_with_retry", side_effect=replace):
            report = fm.apply_changes(staged, changes)
        self.assertTrue(report["rollback_incomplete"])
        self.assertEqual([p.name for p in self.ws.rglob("*.flash-*.tmp")], [])


class ReparseEverywhereTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.a = self.write("src/a.py", "A0\n")
        self.staged = self.stage(writes=[self.a])

    def _junction_or_skip(self, link, target):
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=True)
            return
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt":
            import subprocess

            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            if made.returncode == 0:
                return
        self.skipTest("neither a directory symlink nor a junction can be created here")

    def test_junction_nested_inside_pycache_under_a_non_cache_name_is_flagged(self):
        outside = self.ws / "outside"
        link = self.staged.root / "src" / "__pycache__" / "innocent_name"
        link.parent.mkdir(parents=True, exist_ok=True)
        self._junction_or_skip(link, outside)
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["reparse_planted"], ["src/__pycache__/innocent_name"])
        self.assertTrue(fm.apply_changes(self.staged, changes)["refused"])

    def test_plain_junction_anywhere_in_staging_is_flagged(self):
        outside = self.ws / "outside2"
        link = self.staged.root / "src" / "plain_link"
        self._junction_or_skip(link, outside)
        changes = fm.collect_changes(self.staged)
        self.assertEqual(changes["reparse_planted"], ["src/plain_link"])
        self.assertTrue(fm.apply_changes(self.staged, changes)["refused"])


class StructuredRollbackPhaseTests(unittest.TestCase):
    def test_rolled_back_comes_from_structured_state_not_text(self):
        text = "Antigravity CLI structured-output validation failed: ... ROLLED BACK ..."
        def phase(progress):
            return {p["phase"]: p["status"] for p in progress["phases"]}["workspace_apply"]
        self.assertEqual(phase(broker.flash_terminal_progress("rejected", "accept-edits", text)), "not_applied")
        self.assertEqual(
            phase(broker.flash_terminal_progress("rejected", "accept-edits", text, apply_state="rolled_back")),
            "rolled_back",
        )
        self.assertEqual(
            phase(broker.flash_terminal_progress("rejected", "accept-edits", "plain", apply_state="rolled_back")),
            "rolled_back",
        )


class StaleJournalTests(FlashAsyncBase):
    """A committed record belonging to ANOTHER run must never colour a new run of the same id."""

    REJECTION = "Antigravity CLI structured-output validation failed: out-of-scope file reported: x.py"

    def setUp(self):
        super().setUp()
        broker._flash_apply_journal("WP-A", "some-other-run", "committed", applied=[str(self.file_a)])

    def _consult(self):
        with mock.patch.object(broker, "consult_antigravity_cli", return_value=self.REJECTION), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", side_effect=lambda m, e: m), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}):
            return broker.consult("antigravity", self.impl_args(max_response_chars=100000))

    def test_direct_consult_path_reports_clean_rejection(self):
        result = self._consult()
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["disposition"], "rejected")
        self.assertNotIn("applied_files", result)
        self.assertNotIn("applied_then_rejected", json.dumps(result, default=str))

    def test_direct_consult_passes_a_unique_run_token(self):
        tokens = []

        def capture(*a, **k):
            tokens.append(k.get("run_token"))
            return self.REJECTION

        with mock.patch.object(broker, "consult_antigravity_cli", side_effect=capture), \
             mock.patch.object(broker, "resolve_cli_model_and_effort", return_value=("gemini-3.7-flash-high", "high")), \
             mock.patch.object(broker, "antigravity_model_for_effort", side_effect=lambda m, e: m), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}):
            broker.consult("antigravity", self.impl_args(max_response_chars=100000))
            broker.consult("antigravity", self.impl_args(max_response_chars=100000))
        self.assertEqual(len(set(tokens)), 2)
        self.assertTrue(all(tokens))

    def test_async_finalizer_reports_clean_rejection_and_failure(self):
        rid = broker.queue_flash_request(self.impl_args(max_response_chars=100000))["request_id"]
        self.run_worker(rid, agy_response=self.REJECTION)
        final = broker.request_result(rid)
        self.assertEqual(final["outcome"], "rejected")
        self.assertNotIn("applied_files", final)
        rid2 = broker.queue_flash_request(self.impl_args(self.file_b, "WP-A", max_response_chars=100000))["request_id"]
        with mock.patch.object(broker, "consult", side_effect=RuntimeError("boom")):
            broker.run_flash_request_worker(rid2)
        final2 = broker.request_result(rid2)
        self.assertEqual(final2["outcome"], "failed_pre_mutation")
        self.assertNotIn("applied_files", final2)

    def test_newer_committed_run_does_not_relabel_an_older_failed_request(self):
        rid_old = broker.queue_flash_request(self.impl_args(max_response_chars=100000))["request_id"]
        stale = json.dumps({"status": "error", "outcome": "rejected", "disposition": "rejected",
                            "response": self.REJECTION})
        broker._flash_finalize_row(rid_old, "completed", stale, None, None, None)
        broker._flash_apply_journal("WP-A", "a-newer-request", "committed", applied=[str(self.file_b)])
        final = broker.request_result(rid_old)
        self.assertEqual(final["disposition"], "rejected")
        self.assertNotIn("applied_files", final)
        self.assertNotIn(str(self.file_b), json.dumps(final, default=str))


if __name__ == "__main__":
    unittest.main()
