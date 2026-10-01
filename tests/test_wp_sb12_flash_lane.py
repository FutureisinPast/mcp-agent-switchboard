"""WP-SB12 items 4-6: tolerant acceptance-criteria matching, quarantine salvage of a
rejected accept-edits package, and a meaningful async progress last_action."""
from __future__ import annotations

import json
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

CRITERIA = [
    "Focused tests pass.",
    "No files outside the allowlist change.",
    "The retry helper returns the previous lock holder when it is still alive.",
]


def _package(workspace=None, target=None):
    args = {
        "work_package_id": "WP-SB12-TEST",
        "allowed_files": ["src/worker.py"],
        "acceptance_criteria": list(CRITERIA),
    }
    if target is not None:
        args = {
            "work_package_id": "WP-SB12-TEST",
            "allowed_files": [str(target)],
            "allowed_writes": [str(target)],
            "workspace_root": str(workspace),
            "acceptance_criteria": list(CRITERIA),
        }
    return broker.prepare_flash_work_package(args, "implementation", "Implement the bounded change.")


def _output(package_id, criteria_text=None, statuses=None):
    texts = criteria_text if criteria_text is not None else list(CRITERIA)
    statuses = statuses or ["passed"] * len(texts)
    return {
        "status": "SUCCESS",
        "structured_output": {
            "package_id": package_id,
            "status": "completed",
            "summary": "done",
            "acceptance_criteria": [
                {"criterion": t, "status": s, "evidence": ["x"]} for t, s in zip(texts, statuses)
            ],
            "files_changed": [],
            "checks": [],
            "evidence": [],
            "claims": [],
            "research_coverage": [],
            "ambiguities": [],
            "risks": [],
            "next_action": "verify",
            "brain_verification_required": "required",
        },
    }


class AcceptanceCriteriaMatchingTests(unittest.TestCase):
    def _validate(self, texts, statuses=None):
        package = _package()
        caveats: list = []
        _, errors = broker.validate_flash_workhorse_result(
            _output(package["package_id"], texts, statuses), package, caveats_out=caveats
        )
        return errors, caveats

    def test_exact_match_has_no_errors_or_caveats(self):
        errors, caveats = self._validate(list(CRITERIA))
        self.assertEqual((errors, caveats), ([], []))

    def test_whitespace_quote_and_case_normalised(self):
        errors, caveats = self._validate(
            ["  FOCUSED   tests pass. ", "No files outside the `allowlist` change.", CRITERIA[2]]
        )
        self.assertEqual((errors, caveats), ([], []))

    def test_paraphrase_at_same_index_is_a_caveat_not_a_rejection(self):
        errors, caveats = self._validate(
            [
                CRITERIA[0],
                CRITERIA[1],
                "Retry helper returns previous lock holder when still alive (verified by test).",
            ]
        )
        self.assertEqual(errors, [])
        self.assertEqual([c["code"] for c in caveats], ["acceptance_criterion_paraphrased"])

    def test_count_mismatch_rejects(self):
        errors, _ = self._validate(CRITERIA[:2])
        self.assertTrue(any("count differs" in e for e in errors))

    def test_unrelated_criterion_is_missing_and_rejects(self):
        errors, _ = self._validate([CRITERIA[0], CRITERIA[1], "Something else entirely."])
        self.assertTrue(any("criterion 3" in e for e in errors))

    def test_reordered_criteria_reject(self):
        errors, _ = self._validate([CRITERIA[1], CRITERIA[0], CRITERIA[2]])
        self.assertTrue(errors)

    def test_failed_criterion_still_rejects(self):
        errors, _ = self._validate(list(CRITERIA), ["passed", "failed", "passed"])
        self.assertTrue(any("every acceptance criterion to pass" in e for e in errors))


class QuarantineSalvageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sb12-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.workspace = self.tmp / "ws"
        self.target = self.workspace / "src" / "worker.py"
        self.target.parent.mkdir(parents=True)
        self.target.write_text("original\n", encoding="utf-8")
        patches = [
            mock.patch.object(broker, "QUARANTINE_ROOT", self.tmp / "q"),
            mock.patch.object(broker, "QUARANTINE_LOCK_PATH", self.tmp / "q.lock"),
            mock.patch.object(broker, "WORKER_LOG_DIR", self.tmp / "logs"),
            mock.patch.object(broker, "BROKER_DIR", self.tmp / "b"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, package, stdout):
        def fake_run(command, cwd, *a, **k):
            (Path(cwd) / "src" / "worker.py").write_text("edited by worker\n", encoding="utf-8")
            return (0, stdout, "")

        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.workspace))), \
             mock.patch.object(broker, "run_process", side_effect=fake_run):
            return broker.consult_antigravity_cli(
                "p", "bounded", "accept-edits", "gemini-3.7-flash-high", "high", 60, package
            )

    def test_rejected_package_keeps_edited_files_and_diff_unapplied(self):
        package = _package(self.workspace, self.target)
        bad = _output(package["package_id"], ["Wrong criterion text.", "Nope.", "Nothing."])
        response = self._run(package, json.dumps(bad))
        self.assertIn("validation failed", response)
        # never applied
        self.assertEqual(self.target.read_text(encoding="utf-8"), "original\n")
        files, diff = broker._quarantine_salvage_from_response(response)
        self.assertEqual(files, ["files/src/worker.py"])
        self.assertEqual(diff, "changes.diff")
        path = Path(broker._QUARANTINE_SUFFIX_RE.search(response).group("path"))
        self.assertEqual((path / "files/src/worker.py").read_text(encoding="utf-8"), "edited by worker\n")
        text = (path / "changes.diff").read_text(encoding="utf-8")
        self.assertIn("-original", text)
        self.assertIn("+edited by worker", text)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("files/src/worker.py", manifest["files"])

    def test_native_handoff_carries_quarantine_fields(self):
        with mock.patch.object(broker, "family_from_caller", return_value="codex"), \
             mock.patch.object(broker, "current_codex_role_model", return_value="m"):
            handoff = broker.native_handoff_for_flash_outcome(
                {"work_package_id": "W"}, "rejected", "broker:1", ["files/a.py"], "changes.diff"
            )
        self.assertEqual(handoff["quarantine_files"], ["files/a.py"])
        self.assertEqual(handoff["quarantine_diff"], "changes.diff")

    def test_suffix_omits_salvage_fields_when_empty(self):
        suffix = broker._quarantine_suffix(
            {"quarantine_path": "p", "quarantine_sha256": "a" * 64, "quarantine_files": [], "quarantine_diff": None}
        )
        self.assertNotIn("quarantine_files", suffix)


class ProgressLastActionTests(unittest.TestCase):
    def test_last_action_from_real_transcript_structure(self):
        tmp = Path(tempfile.mkdtemp(prefix="sb12-tr-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        lines = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
             "content": "Package ID: WP-X"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "thinking": "t", "tool_calls": [{"name": "view_file", "args": {
                 "AbsolutePath": "/a/b.py", "toolAction": "Viewing file", "toolSummary": "b.py"}}]},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "..."},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest", "toolSummary": "Running tests"}}]},
            {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "out"},
        ]
        path = tmp / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        steps, last = broker._read_flash_timeout_transcript_steps(path)
        self.assertEqual(steps, 4)
        self.assertEqual(last, "Running tests")

    def test_falls_back_to_tool_name_and_target(self):
        action = broker._flash_transcript_line_action(
            {"tool_calls": [{"name": "write_to_file", "args": {"TargetFile": "C:\\x\\y\\new.py"}}]}
        )
        self.assertEqual(action, "write_to_file new.py")

    def test_legacy_top_level_toolaction_still_works(self):
        self.assertEqual(broker._flash_transcript_line_action({"toolAction": "Reading"}), "Reading")


if __name__ == "__main__":
    unittest.main()
