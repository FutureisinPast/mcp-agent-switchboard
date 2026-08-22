"""WP-VP-1: the structured payload is PRIMARY, the CLI process/transport
status is noise. Confirmed defect regression guard: a run whose structured
payload is present, schema-valid, and in scope must be demoted (not
rejected) purely because ``outer["status"]`` disagreed with it. Genuine
semantic failures -- missing/invalid payload, schema violation, scope
violation, safety-rule failure -- must still reject, and every rejection
must quarantine the raw worker output before the verdict is returned.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def _flash_package() -> dict:
    return broker.prepare_flash_work_package(
        {
            "work_package_id": "WP-VP-TEST",
            "allowed_files": ["src/worker.py", "tests/test_worker.py"],
            "acceptance_criteria": ["Focused tests pass."],
        },
        "implementation",
        "Implement the approved bounded change.",
    )


def _flash_output(package_id: str, **overrides) -> dict:
    structured = {
        "package_id": package_id,
        "status": "completed",
        "summary": "Implemented the bounded package.",
        "acceptance_criteria": [
            {"criterion": "Focused tests pass.", "status": "passed", "evidence": ["pytest: passed"]}
        ],
        "files_changed": [{"path": "src/worker.py", "change": "Applied bounded fix."}],
        "checks": [
            {"command": "pytest tests/test_worker.py", "status": "passed", "exit_code": 0, "output_excerpt": "1 passed"}
        ],
        "evidence": [
            {"claim": "Change is present", "path": "src/worker.py", "line": "12", "observation": "Guard added."}
        ],
        "claims": [
            {"statement": "Guard is present.", "basis": "observed", "evidence": ["src/worker.py:12"]}
        ],
        "ambiguities": [],
        "risks": [],
        "next_action": "Brain verifies diff and test output.",
        "brain_verification_required": "required",
    }
    structured.update(overrides)
    return {
        "conversation_id": "conv-1",
        "status": "SUCCESS",
        "structured_output": structured,
        "duration_seconds": 2.5,
        "num_turns": 1,
        "usage": {"total_tokens": 100},
    }


class ValidatorPrecedenceTests(unittest.TestCase):
    """Direct unit coverage of validate_flash_workhorse_result's own precedence."""

    def test_exit_status_mismatch_alone_is_demoted_to_a_caveat_not_an_error(self):
        package = _flash_package()
        outer = _flash_output(package["package_id"])
        outer["status"] = "ERROR"  # the confirmed-defect signal: transport says ERROR
        caveats: list = []
        structured, errors = broker.validate_flash_workhorse_result(outer, package, caveats_out=caveats)
        self.assertEqual(errors, [])
        self.assertIsNotNone(structured)
        self.assertEqual(structured["status"], "completed")
        self.assertEqual(len(caveats), 1)
        self.assertEqual(caveats[0]["code"], "cli_exit_status_mismatch")
        self.assertIn("ERROR", caveats[0]["detail"])

    def test_exit_status_mismatch_plus_a_real_error_still_rejects(self):
        package = _flash_package()
        outer = _flash_output(
            package["package_id"],
            files_changed=[{"path": "src/outside.py", "change": "Expanded scope."}],
        )
        outer["status"] = "ERROR"
        caveats: list = []
        _, errors = broker.validate_flash_workhorse_result(outer, package, caveats_out=caveats)
        self.assertTrue(any("out-of-scope file reported" in e for e in errors))
        # the exit-status note rides along as diagnostic context on a genuine rejection
        self.assertTrue(any("agy status was ERROR" in e for e in errors))
        self.assertEqual(caveats, [])

    def test_missing_structured_output_still_rejects_regardless_of_exit_status(self):
        package = _flash_package()
        outer = {"status": "ERROR", "structured_output": None}
        structured, errors = broker.validate_flash_workhorse_result(outer, package)
        self.assertIsNone(structured)
        self.assertTrue(any("structured_output is missing" in e for e in errors))


class AntigravityDispatchDispositionTests(unittest.TestCase):
    """End-to-end coverage through consult_antigravity_cli: this is the exact
    call path the confirmed defect broke."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_root = Path(self._tmp.name)
        # Isolate quarantine writes from the real user home directory.
        patch_root = mock.patch.object(broker, "QUARANTINE_ROOT", tmp_root / "quarantine" / "rejected")
        patch_lock = mock.patch.object(broker, "QUARANTINE_LOCK_PATH", tmp_root / "quarantine" / ".prune.lock")
        patch_throttle = mock.patch.object(broker, "_quarantine_last_prune_at", 0.0)
        patch_root.start()
        patch_lock.start()
        patch_throttle.start()
        self.addCleanup(patch_root.stop)
        self.addCleanup(patch_lock.stop)
        self.addCleanup(patch_throttle.stop)

        # A real workspace with the files the package declares, matching the
        # pattern flash_manifest staging requires (a manifest read_context
        # entry that names a file that doesn't exist on disk is refused
        # before the worker ever runs, which would mask what these tests
        # are actually exercising).
        self._workspace = tmp_root / "workspace"
        (self._workspace / "src").mkdir(parents=True)
        (self._workspace / "tests").mkdir(parents=True)
        (self._workspace / "src" / "worker.py").write_text("original\n", encoding="utf-8")
        (self._workspace / "tests" / "test_worker.py").write_text("original\n", encoding="utf-8")

    def _dispatch(self, package: dict, stdout: str, stderr: str = ""):
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self._workspace))), \
             mock.patch.object(broker, "run_process", return_value=(0, stdout, stderr)):
            response = broker.consult_antigravity_cli(
                "p", "bounded prompt", "plan", "gemini-3.7-flash-high", "high", 60, package
            )
        return response

    def test_1_valid_payload_plus_cli_exit_error_is_accepted_with_caveats(self):
        """Regression guard for the confirmed defect: a fully valid, in-scope
        structured payload was previously discarded and the whole dispatch
        rejected purely because agy's own status field said ERROR."""
        package = _flash_package()
        package_id = package["package_id"]
        outer = _flash_output(package_id)
        outer["status"] = "ERROR"
        response = self._dispatch(package, json.dumps(outer))
        self.assertFalse(response.startswith("Antigravity CLI structured-output validation failed:"))
        normalized = json.loads(response)
        self.assertEqual(normalized["disposition"], "accepted_with_caveats")
        self.assertEqual(normalized["worker_status"], "completed_with_caveats")
        self.assertTrue(normalized["caveats"])
        self.assertEqual(normalized["caveats"][0]["code"], "cli_exit_status_mismatch")
        # the work product itself must be preserved, not thrown away
        self.assertEqual(normalized["structured_output"]["package_id"], package_id)
        self.assertEqual(normalized["structured_output"]["status"], "completed")
        self.assertEqual(
            normalized["structured_output"]["files_changed"],
            [{"path": "src/worker.py", "change": "Applied bounded fix."}],
        )

    def test_2_schema_violation_still_rejected(self):
        # Build a genuinely schema-broken payload: drop a required field.
        package = _flash_package()
        outer = _flash_output(package["package_id"])
        del outer["structured_output"]["brain_verification_required"]
        response = self._dispatch(package, json.dumps(outer))
        self.assertTrue(response.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("brain_verification_required", response)
        self.assertIn("quarantine_path:", response)
        self.assertIn("quarantine_sha256:", response)

    def test_3_scope_violation_still_rejected(self):
        package = _flash_package()
        outer = _flash_output(package["package_id"], files_changed=[{"path": "src/outside.py", "change": "x"}])
        response = self._dispatch(package, json.dumps(outer))
        self.assertTrue(response.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("out-of-scope file reported", response)
        self.assertIn("quarantine_path:", response)

    def test_4_rejection_writes_a_quarantine_bundle_with_matching_sha256(self):
        package = _flash_package()
        outer = _flash_output(package["package_id"], files_changed=[{"path": "src/outside.py", "change": "x"}])
        response = self._dispatch(package, json.dumps(outer))
        match = broker._QUARANTINE_SUFFIX_RE.search(response)
        self.assertIsNotNone(match)
        bundle_dir = Path(match.group("path"))
        self.assertTrue(bundle_dir.is_dir())
        manifest_path = bundle_dir / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["artifact_disposition"], "quarantined_untrusted")
        self.assertTrue(manifest["validation_failures"])
        self.assertIn("stdout.txt", manifest["files"])
        self.assertIn("structured_output.json", manifest["files"])
        import hashlib
        self.assertEqual(
            hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(), match.group("sha")
        )
        # Quarantine is untrusted storage, never described as containment or an OS boundary.
        self.assertIn("not containment", manifest_text.lower())
        self.assertIn("not an os security boundary", manifest_text.lower())
        self.assertIn("NOT accepted", response)
        self.assertIn("NOT applied", response)

    def test_5_rejection_receipt_surfaces_through_consult(self):
        package = _flash_package()
        outer = _flash_output(package["package_id"], files_changed=[{"path": "src/outside.py", "change": "x"}])
        stdout = json.dumps(outer)
        with mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self._workspace))), \
             mock.patch.object(broker, "run_process", return_value=(0, stdout, "")), \
             mock.patch.object(broker, "store_consultation"):
            result = broker.consult(
                "antigravity",
                {
                    "prompt": "Implement the approved bounded change.",
                    "task_kind": "implementation",
                    "mode": "plan",
                    "target_model": "gemini-3.7-flash-high",
                    "effort": "high",
                    "work_package_id": package["package_id"],
                    "allowed_files": package["allowed_files"],
                    "acceptance_criteria": package["acceptance_criteria"],
                },
            )
        self.assertEqual(result["disposition"], "rejected")
        self.assertEqual(result["artifact_disposition"], "quarantined_untrusted")
        self.assertTrue(result["quarantine_path"])
        self.assertTrue(result["quarantine_sha256"])
        self.assertTrue(Path(result["quarantine_path"]).is_dir())
        self.assertTrue(result["validation_failures"])
        self.assertEqual(result["outcome"], "rejected")


class QuarantinePruneTests(unittest.TestCase):
    def test_pruning_honours_the_bundle_count_cap(self):
        import tempfile
        import time as _time
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "quarantine" / "rejected"
            lock_path = Path(tmpdir) / "quarantine" / ".prune.lock"
            day_dir = root / "20260101"
            day_dir.mkdir(parents=True)
            bundle_paths = []
            for index in range(5):
                bundle = day_dir / f"bundle-{index}"
                bundle.mkdir()
                (bundle / "manifest.json").write_text("{}", encoding="utf-8")
                bundle_paths.append(bundle)
            # Give each bundle a distinct, increasing mtime so "oldest first"
            # pruning is deterministic.
            base = _time.time() - 1000
            for offset, bundle in enumerate(bundle_paths):
                stamp = base + offset
                import os
                os.utime(bundle, (stamp, stamp))

            with mock.patch.object(broker, "QUARANTINE_ROOT", root), \
                 mock.patch.object(broker, "QUARANTINE_LOCK_PATH", lock_path), \
                 mock.patch.object(broker, "QUARANTINE_MAX_BUNDLES", 3), \
                 mock.patch.object(broker, "QUARANTINE_MAX_AGE_DAYS", 3650), \
                 mock.patch.object(broker, "_quarantine_last_prune_at", 0.0):
                broker._prune_quarantine()

            remaining = sorted(p.name for p in day_dir.iterdir() if p.is_dir())
            self.assertEqual(len(remaining), 3)
            # the two oldest (index 0, 1) must be the ones pruned
            self.assertEqual(remaining, ["bundle-2", "bundle-3", "bundle-4"])


if __name__ == "__main__":
    unittest.main()
