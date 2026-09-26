"""WP-SB3: agy timeout classification (execution vs. startup-stall vs. a
transcript lookup error) and transcript-based model attestation when agy's
own JSON omits the model. All home-directory access goes through the
AGENT_BROKER_AGY_HOME env override -- no test here touches a real user's
~/.gemini directory.
"""

from __future__ import annotations

import json
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

import agent_broker_mcp as broker  # noqa: E402


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


class AntigravityCliHomeTests(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_AGY_HOME": "/synthetic/agy-home"}, clear=False):
            self.assertEqual(broker._antigravity_cli_home(), Path("/synthetic/agy-home"))

    def test_default_is_home_dot_gemini_antigravity_cli(self):
        env = dict(os.environ)
        env.pop("AGENT_BROKER_AGY_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(Path, "home", return_value=Path("/synthetic-home")):
            self.assertEqual(
                broker._antigravity_cli_home(), Path("/synthetic-home") / ".gemini" / "antigravity-cli"
            )


class ClassifyFlashTimeoutTests(unittest.TestCase):
    """Direct unit coverage of the transcript-based outcome table."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "agy-home"
        patcher = mock.patch.dict(os.environ, {"AGENT_BROKER_AGY_HOME": str(self.home)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _transcript(self, session: str = "session-1") -> Path:
        return self.home / "brain" / session / ".system_generated" / "logs" / "transcript.jsonl"

    def test_missing_brain_directory_is_startup_stall(self):
        result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "startup_stall")
        self.assertEqual(result["outcome"], "unavailable_pre_mutation")
        self.assertNotIn("timeout_evidence", result)

    def test_no_session_names_this_package_is_startup_stall(self):
        _write_lines(self._transcript(), ["Package ID: WP-SOMETHING-ELSE"])
        result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "startup_stall")
        self.assertEqual(result["outcome"], "unavailable_pre_mutation")

    def test_stale_session_folder_outside_the_launch_window_is_ignored(self):
        transcript = self._transcript()
        _write_lines(transcript, ["Package ID: WP-1", json.dumps({"toolAction": "read file"})])
        long_ago = time.time() - 3600
        os.utime(transcript.parent.parent.parent, (long_ago, long_ago))
        result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "startup_stall")

    def test_header_only_transcript_is_zero_steps_startup_stall(self):
        _write_lines(self._transcript(), ["Package ID: WP-1"])
        result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "startup_stall")
        self.assertEqual(result["outcome"], "unavailable_pre_mutation")
        self.assertNotIn("timeout_evidence", result)

    def test_one_or_more_steps_is_timeout_during_execution_with_evidence(self):
        transcript = self._transcript()
        _write_lines(
            transcript,
            [
                "Package ID: WP-1",
                json.dumps({"toolAction": "read src/worker.py"}),
                json.dumps({"toolSummary": "editing src/worker.py to add the bounded fix"}),
            ],
        )
        result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "timeout_during_execution")
        self.assertEqual(result["outcome"], "failed_pre_mutation")
        evidence = result["timeout_evidence"]
        self.assertEqual(evidence["transcript"], str(transcript))
        self.assertEqual(evidence["steps"], 2)
        self.assertEqual(evidence["last_action"], "editing src/worker.py to add the bounded fix")

    def test_last_action_is_capped_at_200_chars(self):
        long_action = "x" * 500
        _write_lines(
            self._transcript(),
            ["Package ID: WP-1", json.dumps({"toolAction": long_action})],
        )
        result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(len(result["timeout_evidence"]["last_action"]), 200)

    def test_lookup_error_is_swallowed_and_classified_as_plain_timeout(self):
        with mock.patch.object(
            broker, "_find_flash_timeout_transcript", side_effect=OSError("permission denied")
        ):
            result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "timeout")
        self.assertEqual(result["outcome"], "failed_pre_mutation")

    def test_read_error_after_finding_transcript_is_also_plain_timeout(self):
        _write_lines(self._transcript(), ["Package ID: WP-1", json.dumps({"toolAction": "x"})])
        with mock.patch.object(
            broker, "_read_flash_timeout_transcript_steps", side_effect=OSError("gone")
        ):
            result = broker._classify_flash_timeout("WP-1", time.time())
        self.assertEqual(result["failure_kind"], "timeout")
        self.assertEqual(result["outcome"], "failed_pre_mutation")


class ClassifyFlashOutcomeFailureKindTests(unittest.TestCase):
    """failure_kind must be consulted BEFORE the text-prefix heuristics."""

    def test_timeout_during_execution_overrides_infrastructure_text(self):
        outcome = broker.classify_flash_outcome(
            "Antigravity CLI timed out after 60 seconds (synchronous MCP call limit).",
            "error",
            None,
            failure_kind="timeout_during_execution",
        )
        self.assertEqual(outcome, "failed_pre_mutation")

    def test_startup_stall_is_unavailable(self):
        outcome = broker.classify_flash_outcome(
            "Antigravity CLI timed out after 60 seconds (synchronous MCP call limit).",
            "error",
            None,
            failure_kind="startup_stall",
        )
        self.assertEqual(outcome, "unavailable_pre_mutation")

    def test_plain_timeout_lookup_error_is_failed(self):
        outcome = broker.classify_flash_outcome(
            "Antigravity CLI timed out after 60 seconds (synchronous MCP call limit).",
            "error",
            None,
            failure_kind="timeout",
        )
        self.assertEqual(outcome, "failed_pre_mutation")

    def test_no_failure_kind_falls_through_to_text_prefixes_unchanged(self):
        outcome = broker.classify_flash_outcome(
            "Antigravity CLI was not found. install it", "error", None
        )
        self.assertEqual(outcome, "unavailable_pre_mutation")


class FlashTerminalProgressTimeoutPhaseTests(unittest.TestCase):
    def test_execution_timeout_marks_resolution_and_staging_completed(self):
        progress = broker.flash_terminal_progress(
            "failed_pre_mutation",
            "accept-edits",
            "Antigravity CLI timed out after 60 seconds (synchronous MCP call limit).",
            failure_kind="timeout_during_execution",
        )
        statuses = {item["phase"]: item["status"] for item in progress["phases"]}
        self.assertEqual(statuses["model_resolution"], "completed")
        self.assertEqual(statuses["containment_staging"], "completed")
        self.assertEqual(statuses["worker_execution"], "timed_out")
        self.assertEqual(statuses["structured_validation"], "not_started")
        self.assertEqual(statuses["workspace_apply"], "not_started")
        self.assertEqual(progress["current_phase"], "worker_execution")

    def test_startup_stall_also_marks_resolution_and_staging_completed(self):
        progress = broker.flash_terminal_progress(
            "unavailable_pre_mutation",
            "plan",
            "Antigravity CLI timed out after 60 seconds (synchronous MCP call limit).",
            failure_kind="startup_stall",
        )
        statuses = {item["phase"]: item["status"] for item in progress["phases"]}
        self.assertEqual(statuses["model_resolution"], "completed")
        self.assertEqual(statuses["containment_staging"], "completed")
        self.assertEqual(statuses["worker_execution"], "timed_out")
        self.assertEqual(progress["current_phase"], "worker_execution")

    def test_real_not_found_unavailability_still_fails_model_resolution(self):
        """A genuine resolution failure (agy missing) is NOT a timeout, so
        model_resolution must still be marked failed -- unchanged behaviour."""
        progress = broker.flash_terminal_progress(
            "unavailable_pre_mutation", "plan", "Antigravity CLI was not found. install it"
        )
        statuses = {item["phase"]: item["status"] for item in progress["phases"]}
        self.assertEqual(statuses["model_resolution"], "failed")
        self.assertEqual(statuses["worker_execution"], "not_started")
        self.assertEqual(progress["current_phase"], "model_resolution")


class ConsultTimeoutMessageCallerNamingTests(unittest.TestCase):
    def test_never_hardcodes_codex_for_a_claude_caller(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_CALLER": "claude-code-cli"}, clear=False), \
             mock.patch.object(broker, "_MCP_CLIENT_NAME", "claude-code-cli"):
            message = broker.consult_timeout_message("Antigravity CLI", 60)
        self.assertNotIn("Codex", message)
        self.assertIn("claude-code-cli", message)
        self.assertIn("Antigravity CLI timed out after 60 seconds (synchronous MCP call limit).", message)

    def test_unknown_caller_omits_caller_line_but_still_never_says_codex(self):
        env = dict(os.environ)
        env.pop("AGENT_BROKER_CALLER", None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(broker, "_MCP_CLIENT_NAME", ""):
            message = broker.consult_timeout_message("Antigravity CLI", 60)
        self.assertNotIn("Codex", message)
        self.assertNotIn("Caller:", message)

    def test_a_real_codex_caller_still_gets_its_own_name(self):
        with mock.patch.dict(os.environ, {"AGENT_BROKER_CALLER": "codex-cli"}, clear=False), \
             mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-cli"):
            message = broker.consult_timeout_message("Codex", 60)
        self.assertIn("Codex timed out after 60 seconds", message)
        self.assertIn("codex-cli", message)


class ConsultAntigravityCliTimeoutIntegrationTests(unittest.TestCase):
    """End-to-end through consult_antigravity_cli and the route_agent_task
    consult() path: exit 124, classification threaded to the caller, terminal
    progress phases, and the native handoff reason."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_root = Path(self._tmp.name)
        self.home = tmp_root / "agy-home"
        patcher = mock.patch.dict(os.environ, {"AGENT_BROKER_AGY_HOME": str(self.home)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        patch_root = mock.patch.object(broker, "QUARANTINE_ROOT", tmp_root / "quarantine" / "rejected")
        patch_lock = mock.patch.object(broker, "QUARANTINE_LOCK_PATH", tmp_root / "quarantine" / ".prune.lock")
        patch_throttle = mock.patch.object(broker, "_quarantine_last_prune_at", 0.0)
        patch_root.start()
        patch_lock.start()
        patch_throttle.start()
        self.addCleanup(patch_root.stop)
        self.addCleanup(patch_lock.stop)
        self.addCleanup(patch_throttle.stop)

        self.workspace = tmp_root / "workspace"
        self.workspace.mkdir(parents=True)

    def _package(self, package_id: str = "WP-TIMEOUT") -> dict:
        return broker.prepare_flash_work_package(
            {"work_package_id": package_id}, "quick_check", "Inspect the bounded package."
        )

    def _write_transcript(self, package_id: str, lines_after_header: list[str]) -> None:
        session = self.home / "brain" / "session-1"
        transcript = session / ".system_generated" / "logs" / "transcript.jsonl"
        _write_lines(transcript, [f"Package ID: {package_id}"] + lines_after_header)

    def test_execution_timeout_populates_timeout_meta_out(self):
        package = self._package()
        self._write_transcript(package["package_id"], [json.dumps({"toolAction": "editing worker.py"})])
        meta: dict = {}
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.workspace))), \
             mock.patch.object(broker, "run_process", return_value=(124, "", "")):
            response = broker.consult_antigravity_cli(
                "p", "bounded prompt", "plan", "gemini-3.7-flash-high", "high", 60, package,
                timeout_meta_out=meta,
            )
        self.assertTrue(response.startswith("Antigravity CLI timed out after 60 seconds"))
        self.assertEqual(meta["failure_kind"], "timeout_during_execution")
        self.assertEqual(meta["outcome"], "failed_pre_mutation")
        self.assertEqual(meta["timeout_evidence"]["steps"], 1)

    def test_startup_stall_populates_timeout_meta_out(self):
        package = self._package()
        # No transcript at all: agy never got as far as writing one.
        meta: dict = {}
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.workspace))), \
             mock.patch.object(broker, "run_process", return_value=(124, "", "")):
            broker.consult_antigravity_cli(
                "p", "bounded prompt", "plan", "gemini-3.7-flash-high", "high", 60, package,
                timeout_meta_out=meta,
            )
        self.assertEqual(meta["failure_kind"], "startup_stall")
        self.assertEqual(meta["outcome"], "unavailable_pre_mutation")

    def test_full_consult_path_yields_flash_failed_handoff_for_execution_timeout(self):
        """The confirmed-defect regression: an agy timeout that ran for minutes
        must hand off flash-failed, not flash-unavailable."""
        package = self._package("WP-TIMEOUT-HANDOFF")
        self._write_transcript(
            package["package_id"], [json.dumps({"toolSummary": "still writing tests"})]
        )
        args = {
            "prompt": "Inspect the bounded package.",
            "task_kind": "quick_check",
            "mode": "plan",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": package["package_id"],
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.workspace))), \
             mock.patch.object(broker, "run_process", return_value=(124, "", "")), \
             mock.patch.object(broker, "store_consultation"), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-live-sol"):
            result = broker.consult("antigravity", args)
        self.assertEqual(result["outcome"], "failed_pre_mutation")
        self.assertFalse(result["credit_eligible"])
        self.assertIn("timeout_evidence", result)
        handoff = result["native_handoff"]
        self.assertTrue(handoff["flash_skip_reason"].startswith("flash-failed:"))
        progress = result["progress"]
        statuses = {item["phase"]: item["status"] for item in progress["phases"]}
        self.assertEqual(statuses["model_resolution"], "completed")
        self.assertEqual(statuses["containment_staging"], "completed")
        self.assertEqual(statuses["worker_execution"], "timed_out")

    def test_full_consult_path_yields_flash_unavailable_handoff_for_startup_stall(self):
        package = self._package("WP-TIMEOUT-STARTUP")
        args = {
            "prompt": "Inspect the bounded package.",
            "task_kind": "quick_check",
            "mode": "plan",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": package["package_id"],
        }
        with mock.patch.object(broker, "_MCP_CLIENT_NAME", "codex-vscode"), \
             mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(self.workspace))), \
             mock.patch.object(broker, "run_process", return_value=(124, "", "")), \
             mock.patch.object(broker, "store_consultation"), \
             mock.patch.object(broker, "current_codex_role_model", return_value="gpt-live-luna"):
            result = broker.consult("antigravity", args)
        self.assertEqual(result["outcome"], "unavailable_pre_mutation")
        handoff = result["native_handoff"]
        self.assertTrue(handoff["flash_skip_reason"].startswith("flash-unavailable:"))
        self.assertNotIn("timeout_evidence", result)


class TranscriptModelAttestationTests(unittest.TestCase):
    """Unit coverage of _attest_flash_model_from_transcript's own mapping,
    plus end-to-end coverage through consult_antigravity_cli."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "agy-home"
        patcher = mock.patch.dict(os.environ, {"AGENT_BROKER_AGY_HOME": str(self.home)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _catalog(self) -> list[dict]:
        return [
            broker.model_entry("gemini-3.6-flash-high", "Gemini 3.6 Flash (High)"),
            broker.model_entry("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
        ]

    def _write_transcript(self, conversation_id: str, line: str) -> Path:
        transcript = self.home / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        _write_lines(transcript, [line])
        return transcript

    def test_match_maps_display_name_to_catalog_id(self):
        self._write_transcript(
            "conv-1",
            "changed setting `Model Selection` from Gemini 3.5 Flash (High) to Gemini 3.6 Flash (High).",
        )
        with mock.patch.object(broker, "discover_antigravity_models", return_value=self._catalog()):
            attested = broker._attest_flash_model_from_transcript("conv-1")
        self.assertEqual(attested, "gemini-3.6-flash-high")

    def test_match_stops_at_first_sentence_even_with_trailing_text_on_the_same_line(self):
        # Real agy transcript line: the model-selection sentence is followed by
        # more sentences on the same physical line. The display regex must stop
        # at the first ". " boundary rather than swallowing the whole line.
        self._write_transcript(
            "conv-real",
            "The user changed setting `Model Selection` from None to Gemini 3.8 Flash (High). "
            "No need to comment on this change if the user doesn't ask about it. "
            "If reporting what model you are, please use a human readable name.",
        )
        catalog = [
            broker.model_entry("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        ]
        with mock.patch.object(broker, "discover_antigravity_models", return_value=catalog):
            attested = broker._attest_flash_model_from_transcript("conv-real")
        self.assertEqual(attested, "gemini-3.8-flash-high")

    def test_no_selection_line_returns_none(self):
        self._write_transcript("conv-2", "some unrelated log line")
        with mock.patch.object(broker, "discover_antigravity_models", return_value=self._catalog()):
            attested = broker._attest_flash_model_from_transcript("conv-2")
        self.assertIsNone(attested)

    def test_display_name_not_in_catalog_returns_none(self):
        self._write_transcript(
            "conv-3",
            "changed setting `Model Selection` from X to Gemini 9.9 Flash (Ultra).",
        )
        with mock.patch.object(broker, "discover_antigravity_models", return_value=self._catalog()):
            attested = broker._attest_flash_model_from_transcript("conv-3")
        self.assertIsNone(attested)

    def test_missing_transcript_is_swallowed_as_none(self):
        with mock.patch.object(broker, "discover_antigravity_models", return_value=self._catalog()):
            attested = broker._attest_flash_model_from_transcript("conv-does-not-exist")
        self.assertIsNone(attested)

    def _dispatch(self, package: dict, stdout: str, workspace: Path):
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", str(workspace))), \
             mock.patch.object(broker, "run_process", return_value=(0, stdout, "")), \
             mock.patch.object(broker, "discover_antigravity_models", return_value=self._catalog()):
            return broker.consult_antigravity_cli(
                "p", "bounded prompt", "plan", "gemini-3.6-flash-high", "high", 60, package
            )

    def _flash_output(self, package_id: str, conversation_id: str) -> dict:
        return {
            "package_id": package_id,
            "conversation_id": conversation_id,
            "status": "SUCCESS",
            "model": None,
            "structured_output": {
                "package_id": package_id,
                "status": "completed",
                "summary": "Inspected the bounded package.",
                "acceptance_criteria": [],
                "files_changed": [],
                "checks": [],
                "evidence": [],
                "claims": [],
                "research_coverage": [],
                "ambiguities": [],
                "risks": [],
                "next_action": "Brain verifies.",
                "brain_verification_required": "required",
            },
            "duration_seconds": 1.2,
            "num_turns": 1,
            "usage": {"total_tokens": 10},
        }

    def test_end_to_end_match_sets_transcript_attestation_and_model_attested_true(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            package = broker.prepare_flash_work_package(
                {"work_package_id": "WP-ATTEST-MATCH"}, "quick_check", "Inspect it."
            )
            self._write_transcript(
                "conv-match",
                "changed setting `Model Selection` from Gemini 3.5 Flash (High) to Gemini 3.6 Flash (High).",
            )
            outer = self._flash_output(package["package_id"], "conv-match")
            response = self._dispatch(package, json.dumps(outer), workspace)
        normalized = json.loads(response)
        self.assertEqual(normalized["cli"]["attestation"], "transcript")
        self.assertEqual(normalized["cli"]["model"], "gemini-3.6-flash-high")
        self.assertTrue(normalized["cli"]["model_attested"])

    def test_end_to_end_absent_selection_line_leaves_attestation_none(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            package = broker.prepare_flash_work_package(
                {"work_package_id": "WP-ATTEST-ABSENT"}, "quick_check", "Inspect it."
            )
            self._write_transcript("conv-absent", "no model selection line here")
            outer = self._flash_output(package["package_id"], "conv-absent")
            response = self._dispatch(package, json.dumps(outer), workspace)
        normalized = json.loads(response)
        self.assertEqual(normalized["cli"]["attestation"], "none")
        self.assertIsNone(normalized["cli"]["model"])
        self.assertFalse(normalized["cli"]["model_attested"])

    def test_end_to_end_mismatch_reuses_existing_conflict_rejection(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            package = broker.prepare_flash_work_package(
                {"work_package_id": "WP-ATTEST-MISMATCH"}, "quick_check", "Inspect it."
            )
            self._write_transcript(
                "conv-mismatch",
                "changed setting `Model Selection` from Gemini 3.6 Flash (High) to Gemini 3.6 Flash (Medium).",
            )
            outer = self._flash_output(package["package_id"], "conv-mismatch")
            response = self._dispatch(package, json.dumps(outer), workspace)
        self.assertTrue(response.startswith("Antigravity CLI structured-output validation failed:"))
        self.assertIn("backend attested", response)


if __name__ == "__main__":
    unittest.main()
