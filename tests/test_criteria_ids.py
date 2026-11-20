"""Tests for WP-1074-NO-WASTED-BUILDS: trailing malformed tool-call caveat and numbered AC1..ACn criteria."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402
import switchboard_version  # noqa: E402

MODEL = "gemini-3.7-flash-high"
MALFORMED_ERROR = (
    "Your previous response contained an improperly formatted function call: "
    "Malformed function call: Failed to parse function call: "
    "Function call is empty - no input to parse"
)


def _impl_package(
    ws: Path | None = None,
    criteria: list[str] | None = None,
    *,
    allowed_writes: list[str] | None = None,
    stage_repo: bool = False,
) -> dict:
    crit = list(criteria if criteria is not None else ["Focused tests pass.", "No outside files change."])
    args: dict = {
        "work_package_id": "WP-CRIT-TEST",
        "allowed_writes": allowed_writes or ["src/worker.py"],
        "acceptance_criteria": crit,
    }
    if ws is not None:
        args["workspace_root"] = str(ws)
    if stage_repo:
        args["stage_repo"] = True
    return {
        **broker.prepare_flash_work_package(args, "implementation", "Implement the bounded change."),
        "mode": "accept-edits",
    }


def _ro_package(ws: Path | None = None, criteria: list[str] | None = None, *, stage_repo: bool = False) -> dict:
    args: dict = {
        "work_package_id": "WP-CRIT-RO",
        "read_context": [str(ws / "src" / "worker.py")] if ws else ["src/worker.py"],
    }
    if criteria is not None:
        args["acceptance_criteria"] = list(criteria)
    if ws is not None:
        args["workspace_root"] = str(ws)
    if stage_repo:
        args["stage_repo"] = True
    return {
        **broker.prepare_flash_work_package(args, "quick_check", "Inspect the bounded scope."),
        "mode": "plan",
    }


def _criteria_entries(package: dict, *, with_ids: bool = True) -> list[dict]:
    items = []
    for index, text in enumerate(package.get("acceptance_criteria") or [], 1):
        entry: dict = {
            "criterion": text,
            "status": "passed",
            "evidence": [f"checked criterion {index}"],
        }
        if with_ids:
            entry = {"id": f"AC{index}", **entry}
        items.append(entry)
    return items


def _outer_payload(package: dict, *, with_ids: bool = True, status: str = "SUCCESS", error: str | None = None, **overrides) -> dict:
    structured = {
        "package_id": package["package_id"],
        "status": "completed",
        "summary": "Completed the bounded work.",
        "acceptance_criteria": _criteria_entries(package, with_ids=with_ids),
        "files_changed": [{"path": "src/worker.py", "change": "Updated value."}] if package.get("allowed_writes") else [],
        "checks": [
            {"command": "pytest tests/test_worker.py", "status": "passed", "exit_code": 0, "output_excerpt": "1 passed"}
        ],
        "evidence": [
            {"claim": "Updated worker.", "path": "src/worker.py", "line": "1", "observation": "x = 2"}
        ],
        "claims": [
            {"statement": "Worker change verified.", "basis": "observed", "evidence": ["src/worker.py:1"]}
        ],
        "research_coverage": [],
        "scope_gaps": [],
        "ambiguities": [],
        "risks": [],
        "next_action": "Brain verifies diff and check output.",
        "brain_verification_required": "required",
    }
    structured.update(overrides)
    outer: dict = {
        "conversation_id": "conv-crit-1",
        "status": status,
        "model": MODEL,
        "structured_output": structured,
        "duration_seconds": 1.8,
        "num_turns": 2,
        "usage": {"total_tokens": 120},
    }
    if error is not None:
        outer["error"] = error
    return outer


class TrailingMalformedToolCallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_root = Path(self._tmp.name)
        self.ws = tmp_root / "ws"
        self.worker_file = self.ws / "src" / "worker.py"
        self.worker_file.parent.mkdir(parents=True, exist_ok=True)
        self.worker_file.write_text("VALUE = 1\n", encoding="utf-8")
        patches = (
            mock.patch.object(broker, "BROKER_DIR", tmp_root / "broker"),
            mock.patch.object(broker, "QUARANTINE_ROOT", tmp_root / "q" / "rejected"),
            mock.patch.object(broker, "QUARANTINE_LOCK_PATH", tmp_root / "q" / ".prune.lock"),
            mock.patch.object(broker, "WORKER_LOG_DIR", tmp_root / "flash-logs"),
            mock.patch.object(broker, "_flash_apply_dir", return_value=tmp_root / "flash-apply"),
            mock.patch.object(broker, "_quarantine_last_prune_at", 0.0),
            mock.patch.dict(broker._FLASH_APPLY_MEMORY, {}, clear=True),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _dispatch(self, package: dict, outer: dict, *, edit_staged: bool = True, edit_extra: str | None = None, mode: str = "accept-edits") -> str:
        def fake_run(command, cwd, *args, **kwargs):
            if edit_staged:
                (Path(cwd) / "src" / "worker.py").write_text("VALUE = 2\n", encoding="utf-8")
            if edit_extra:
                extra = Path(cwd) / edit_extra
                extra.parent.mkdir(parents=True, exist_ok=True)
                extra.write_text("extra = 1\n", encoding="utf-8")
            return (0, json.dumps(outer), "")

        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.ws))), \
             mock.patch.object(broker, "run_process", side_effect=fake_run):
            return broker.consult_antigravity_cli(
                "p", "bounded prompt", mode, MODEL, "high", 60, package
            )

    def test_malformed_tool_call_with_valid_payload_is_accepted_with_caveat(self):
        package = _impl_package(self.ws, allowed_writes=[str(self.worker_file)])
        long_err = MALFORMED_ERROR + " " + ("X" * 120)
        for pattern_variant in (
            long_err,
            "IMPROPERLY FORMATTED FUNCTION CALL in trailing turn",
            "Malformed Function Call: empty",
            "FUNCTION CALL IS EMPTY",
        ):
            with self.subTest(error=pattern_variant[:40]):
                outer = _outer_payload(
                    package,
                    status="ERROR",
                    error=pattern_variant,
                    files_changed=[{"path": str(self.worker_file), "change": "Updated VALUE."}],
                )
                caveats: list = []
                structured, errors = broker.validate_flash_workhorse_result(outer, package, caveats_out=caveats)
                self.assertEqual(errors, [])
                self.assertIsNotNone(structured)
                self.assertEqual(len(caveats), 1)
                self.assertEqual(caveats[0]["code"], "agy_malformed_tool_call")
                self.assertEqual(caveats[0]["detail"], pattern_variant[:200])
                self.assertLessEqual(len(caveats[0]["detail"]), 200)

        # End-to-end through consult_antigravity_cli also applies and returns accepted_with_caveats.
        outer = _outer_payload(
            package,
            status="ERROR",
            error=long_err,
            files_changed=[{"path": str(self.worker_file), "change": "Updated VALUE."}],
        )
        raw = self._dispatch(package, outer)
        envelope = json.loads(raw)
        self.assertEqual(envelope["disposition"], "accepted_with_caveats")
        self.assertEqual(envelope["worker_status"], "completed_with_caveats")
        self.assertEqual(envelope["caveats"], [{"code": "agy_malformed_tool_call", "detail": long_err[:200]}])
        self.assertEqual(self.worker_file.read_text(encoding="utf-8"), "VALUE = 2\n")

    def test_malformed_tool_call_with_validation_failures_still_rejects(self):
        package = _impl_package(self.ws, allowed_writes=[str(self.worker_file)])
        # 1) Failed check -> rejected
        failed_check = _outer_payload(
            package,
            status="ERROR",
            error=MALFORMED_ERROR,
            checks=[{"command": "pytest", "status": "failed", "exit_code": 1, "output_excerpt": "1 failed"}],
        )
        resp_check = self._dispatch(package, failed_check)
        self.assertTrue(resp_check.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("contradicts a failed check", resp_check)
        self.assertEqual(self.worker_file.read_text(encoding="utf-8"), "VALUE = 1\n")

        # 2) Write scope violation -> rejected
        scope_bad = _outer_payload(
            package,
            status="ERROR",
            error=MALFORMED_ERROR,
            files_changed=[{"path": str(self.ws / "src" / "outside.py"), "change": "Sneaky edit."}],
        )
        resp_scope = self._dispatch(package, scope_bad)
        self.assertTrue(resp_scope.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("out-of-scope file reported", resp_scope)
        self.assertEqual(self.worker_file.read_text(encoding="utf-8"), "VALUE = 1\n")

        # 3) Missing criterion -> rejected
        missing_crit = _outer_payload(
            package,
            status="ERROR",
            error=MALFORMED_ERROR,
            acceptance_criteria=_criteria_entries(package)[:1],
        )
        resp_crit = self._dispatch(package, missing_crit)
        self.assertTrue(resp_crit.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("missing acceptance criterion id(s): AC2", resp_crit)
        self.assertEqual(self.worker_file.read_text(encoding="utf-8"), "VALUE = 1\n")

        # 4) Model attestation conflict -> rejected
        bad_model = _outer_payload(package, status="ERROR", error=MALFORMED_ERROR)
        bad_model["model"] = "gemini-3.7-pro-low"
        resp_model = self._dispatch(package, bad_model)
        self.assertTrue(resp_model.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("attested", resp_model)
        self.assertEqual(self.worker_file.read_text(encoding="utf-8"), "VALUE = 1\n")

        # 5) Staged-repo scope refusal (read-only stage_repo with modified file in staging) -> rejected
        if shutil.which("git") is not None:
            subprocess.run(["git", "init"], cwd=str(self.ws), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.ws), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.ws), check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=str(self.ws), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.ws), check=True, capture_output=True)
            ro_pkg = _ro_package(self.ws, ["Check worker."], stage_repo=True)
            ro_outer = _outer_payload(ro_pkg, status="ERROR", error=MALFORMED_ERROR, files_changed=[])
            resp_ro_scope = self._dispatch(ro_pkg, ro_outer, edit_staged=False, edit_extra="src/stray.py", mode="plan")
            self.assertTrue(resp_ro_scope.startswith("Antigravity CLI structured-output validation failed:"))

    def test_different_agy_error_text_with_valid_payload_is_a_caveat(self):
        # Brain decision 1.0.74: a fully valid payload is never discarded because agy
        # reported some other error; it stays a caveat as before (no new rejections).
        package = _impl_package(self.ws, allowed_writes=[str(self.worker_file)])
        outer = _outer_payload(
            package,
            status="ERROR",
            error="Internal backend RPC timeout before completion",
            files_changed=[{"path": str(self.worker_file), "change": "Updated VALUE."}],
        )
        caveats: list = []
        _, errors = broker.validate_flash_workhorse_result(outer, package, caveats_out=caveats)
        self.assertEqual(errors, [])
        self.assertTrue(caveats)
        self.assertFalse(any(c.get("code") == "agy_malformed_tool_call" for c in caveats))

    def test_malformed_tool_call_without_structured_output_still_rejects(self):
        package = _impl_package(self.ws, allowed_writes=[str(self.worker_file)])
        outer = {"status": "ERROR", "error": MALFORMED_ERROR, "structured_output": None}
        structured, errors = broker.validate_flash_workhorse_result(outer, package)
        self.assertIsNone(structured)
        self.assertTrue(any("structured_output is missing" in e for e in errors))


class NumberedCriteriaAndValidationTests(unittest.TestCase):
    def test_prompt_numbers_criteria_ac1_to_acn_and_adds_instruction(self):
        criteria = ["First check passes.", "Second check passes.", "Third check passes."]
        package = _impl_package(criteria=criteria)
        prompt = broker.wrap_flash_workhorse_prompt("Run the work.", package)
        self.assertIn(
            "Report each criterion exactly once with its id (AC1..AC3), in order; never merge or split criteria.",
            prompt,
        )
        self.assertIn("- AC1: First check passes.", prompt)
        self.assertIn("- AC2: Second check passes.", prompt)
        self.assertIn("- AC3: Third check passes.", prompt)

    def test_schema_requires_id_enum_without_pattern(self):
        criteria = ["C1", "C2", "C3"]
        package = _impl_package(criteria=criteria)
        schema = broker.flash_workhorse_output_schema(package)
        item_schema = schema["properties"]["acceptance_criteria"]["items"]
        self.assertEqual(item_schema["required"], ["id", "criterion", "status", "evidence"])
        id_prop = item_schema["properties"]["id"]
        self.assertEqual(id_prop, {"type": "string", "enum": ["AC1", "AC2", "AC3"]})
        self.assertNotIn("pattern", id_prop)

        # Zero-criteria read-only package does not require id.
        ro_empty = _ro_package(criteria=None)
        ro_schema = broker.flash_workhorse_output_schema(ro_empty)
        self.assertNotIn("id", ro_schema["properties"]["acceptance_criteria"]["items"]["required"])

    def test_id_matching_accepts_reordered_entries(self):
        package = _impl_package(criteria=["First check.", "Second check.", "Third check."])
        entries = _criteria_entries(package, with_ids=True)
        reordered = [entries[2], entries[0], entries[1]]
        outer = _outer_payload(package, acceptance_criteria=reordered)
        caveats: list = []
        _, errors = broker.validate_flash_workhorse_result(outer, package, caveats_out=caveats)
        self.assertEqual(errors, [])
        self.assertEqual(caveats, [])

    def test_missing_id_is_rejected(self):
        package = _impl_package(criteria=["First check.", "Second check."])
        entries = _criteria_entries(package, with_ids=True)
        outer = _outer_payload(package, acceptance_criteria=[entries[0]])
        _, errors = broker.validate_flash_workhorse_result(outer, package)
        self.assertTrue(any("missing acceptance criterion id" in e and "AC2" in e for e in errors), errors)

        # Explicit empty id on single-criterion package also rejects.
        single_pkg = _impl_package(criteria=["Only check."])
        single_outer = _outer_payload(
            single_pkg,
            acceptance_criteria=[{"id": "", "criterion": "Only check.", "status": "passed", "evidence": ["ok"]}],
        )
        _, single_errors = broker.validate_flash_workhorse_result(single_outer, single_pkg)
        self.assertTrue(any("AC1" in e for e in single_errors), single_errors)

    def test_duplicate_id_is_rejected(self):
        package = _impl_package(criteria=["First check.", "Second check."])
        entries = _criteria_entries(package, with_ids=True)
        outer = _outer_payload(package, acceptance_criteria=[entries[0], dict(entries[0]), entries[1]])
        _, errors = broker.validate_flash_workhorse_result(outer, package)
        self.assertTrue(any("duplicate acceptance criterion id: AC1" in e for e in errors), errors)

    def test_unknown_id_is_rejected(self):
        package = _impl_package(criteria=["First check."])
        entries = _criteria_entries(package, with_ids=True)
        for bad_id in ("AC2", "AC99", "ac1", "EXTRA"):
            with self.subTest(bad_id=bad_id):
                outer = _outer_payload(
                    package,
                    acceptance_criteria=[
                        entries[0],
                        {"id": bad_id, "criterion": "Other", "status": "passed", "evidence": ["ok"]},
                    ],
                )
                _, errors = broker.validate_flash_workhorse_result(outer, package)
                self.assertTrue(any(f"unknown acceptance criterion id: {bad_id}" in e for e in errors), errors)

    def test_conflicting_criterion_text_for_id_is_rejected(self):
        package = _impl_package(criteria=["Focused tests pass.", "No files outside allowlist change."])
        swapped = [
            {"id": "AC1", "criterion": "No files outside allowlist change.", "status": "passed", "evidence": ["x"]},
            {"id": "AC2", "criterion": "Focused tests pass.", "status": "passed", "evidence": ["y"]},
        ]
        outer = _outer_payload(package, acceptance_criteria=swapped)
        _, errors = broker.validate_flash_workhorse_result(outer, package)
        self.assertTrue(any("carries the text of another dispatched criterion" in e for e in errors), errors)

    def test_entry_with_empty_evidence_is_rejected(self):
        package = _impl_package(criteria=["First check."])
        for empty_ev in ([], ["   ", ""]):
            with self.subTest(evidence=empty_ev):
                outer = _outer_payload(
                    package,
                    acceptance_criteria=[
                        {"id": "AC1", "criterion": "First check.", "status": "passed", "evidence": empty_ev}
                    ],
                )
                _, errors = broker.validate_flash_workhorse_result(outer, package)
                self.assertTrue(any("non-empty evidence" in e for e in errors), errors)

    def test_extra_entry_without_id_and_not_failed_becomes_caveat_and_failed_rejects(self):
        package = _impl_package(criteria=["First check."])
        entries = _criteria_entries(package, with_ids=True)
        for extra_status in ("passed", "not_checked"):
            with self.subTest(extra_status=extra_status):
                caveats: list = []
                outer = _outer_payload(
                    package,
                    acceptance_criteria=[
                        entries[0],
                        {"criterion": "Extra note", "status": extra_status, "evidence": ["info"]},
                    ],
                )
                _, errors = broker.validate_flash_workhorse_result(outer, package, caveats_out=caveats)
                self.assertEqual(errors, [])
                self.assertEqual([c["code"] for c in caveats], ["extra_acceptance_entries"])

        failed_extra = _outer_payload(
            package,
            acceptance_criteria=[
                entries[0],
                {"criterion": "Extra check failed", "status": "failed", "evidence": ["err"]},
            ],
        )
        caveats_failed: list = []
        _, failed_errors = broker.validate_flash_workhorse_result(
            failed_extra, package, caveats_out=caveats_failed
        )
        self.assertTrue(any("failed status" in e for e in failed_errors), failed_errors)
        self.assertEqual(caveats_failed, [])

    def test_no_ids_uses_unchanged_text_matching_fallback(self):
        twelve = [f"Criterion {i} passes." for i in range(1, 13)]
        package = _impl_package(criteria=twelve)
        reported_eight = _criteria_entries(package, with_ids=False)[:8]
        outer = _outer_payload(package, with_ids=False, acceptance_criteria=reported_eight)
        _, errors = broker.validate_flash_workhorse_result(outer, package)
        self.assertTrue(
            any("reported acceptance criteria do not match the dispatched package: count differs" in e for e in errors),
            errors,
        )

    def test_read_only_package_with_criteria_supports_ids_and_fallback(self):
        ro_pkg = _ro_package(criteria=["Question 1 answered.", "Primary citations included."])
        schema = broker.flash_workhorse_output_schema(ro_pkg)
        self.assertEqual(schema["properties"]["acceptance_criteria"]["minItems"], 0)
        self.assertEqual(schema["properties"]["acceptance_criteria"]["items"]["properties"]["id"]["enum"], ["AC1", "AC2"])

        entries = _criteria_entries(ro_pkg, with_ids=True)
        outer_ids = _outer_payload(ro_pkg, acceptance_criteria=[entries[1], entries[0]], files_changed=[])
        _, errs_ids = broker.validate_flash_workhorse_result(outer_ids, ro_pkg)
        self.assertEqual(errs_ids, [])

        outer_no_ids = _outer_payload(ro_pkg, with_ids=False, files_changed=[])
        _, errs_no_ids = broker.validate_flash_workhorse_result(outer_no_ids, ro_pkg)
        self.assertEqual(errs_no_ids, [])

    def test_broker_version_is_1_0_74(self):
        self.assertGreaterEqual(tuple(int(x) for x in str(switchboard_version.BROKER_VERSION).split(".")), (1, 0, 74))


if __name__ == "__main__":
    unittest.main()
