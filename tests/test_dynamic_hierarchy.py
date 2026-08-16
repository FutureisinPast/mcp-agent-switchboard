"""Focused tests for future-proof role selection and Claude fallbacks/modes."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def claude_stream(model: str, response: str = "ok") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"model": model, "content": [{"text": response}]},
                }
            ),
            json.dumps({"type": "result", "result": response}),
        ]
    )


class DynamicCodexRoleTests(unittest.TestCase):
    def test_live_priority_and_descriptions_select_all_roles(self):
        roles = broker.codex_roles_from_models(
            [
                broker.model_entry(
                    "gpt-8-brain", source="codex-debug", metadata={"priority": 1, "visibility": "list", "description": "frontier"}
                ),
                broker.model_entry(
                    "gpt-8-worker", source="codex-debug", metadata={"priority": 4, "visibility": "list", "description": "balanced everyday workhorse"}
                ),
                broker.model_entry(
                    "gpt-8-reader", source="codex-debug", metadata={"priority": 9, "visibility": "list", "description": "affordable cost-efficient fast"}
                ),
            ]
        )
        self.assertEqual(roles["frontier"]["id"], "gpt-8-brain")
        self.assertEqual(roles["workhorse"]["id"], "gpt-8-worker")
        self.assertEqual(roles["reader"]["id"], "gpt-8-reader")

    def test_cost_policies_use_dynamic_roles(self):
        with mock.patch.object(
            broker,
            "current_codex_role_model",
            side_effect=lambda role: {"reader": "gpt-8-reader", "workhorse": "gpt-8-worker"}[role],
        ):
            cheap = broker.apply_codex_model_policy(
                {"model_policy": "cheap_read"}, "read files", "research", None, None
            )
            balanced = broker.apply_codex_model_policy(
                {"model_policy": "workhorse"}, "write tests", "implementation", None, None
            )
        self.assertEqual(cheap[:2], ("gpt-8-reader", broker.CODEX_CHEAP_EFFORT))
        self.assertEqual(balanced[:2], ("gpt-8-worker", "medium"))


class DynamicAntigravityRoleTests(unittest.TestCase):
    @staticmethod
    def _catalog(*live_slugs: str) -> list[dict]:
        return [
            broker.antigravity_model_entry_from_slug(
                broker.ANTIGRAVITY_DEFAULT_MODEL, "static"
            ),
            *[
                broker.antigravity_model_entry_from_slug(slug, "antigravity-cli")
                for slug in live_slugs
            ],
        ]

    def test_live_37_outranks_36_and_is_never_a_brain(self):
        roles = broker.antigravity_roles_from_models(
            self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        )
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertIsNone(roles["frontier"])
        self.assertFalse(roles["authoritative"])
        self.assertFalse(roles["peer_brain_eligible"])
        self.assertEqual(roles["capability_tier"], "workhorse")

    def test_live_cli_tabular_catalog_is_parsed_and_drives_workhorse(self):
        stdout = "\n".join(
            [
                "gemini-3.7-flash-high\tGemini 3.7 Flash (High)",
                "gemini-3.6-flash-high\tGemini 3.6 Flash (High)",
            ]
        )
        proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE", None), \
             mock.patch.object(broker, "_ANTIGRAVITY_MODEL_CACHE_AT", 0.0), \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker.subprocess, "run", return_value=proc), \
             mock.patch.object(broker.shutil, "which", return_value=None):
            models = broker.discover_antigravity_models()
        roles = broker.antigravity_roles_from_models(models)
        by_id = {item["id"]: item for item in models}
        self.assertEqual(by_id["gemini-3.7-flash-high"]["source"], "antigravity-cli")
        self.assertEqual(by_id["gemini-3.6-flash-high"]["source"], "antigravity-cli")
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertEqual(roles["source"], "antigravity-cli")

    def test_future_versions_sort_numerically_and_preview_never_promotes(self):
        roles = broker.antigravity_roles_from_models(
            self._catalog(
                "gemini-3.9-flash-high",
                "gemini-3.10-flash-high",
                "gemini-99.0-flash-high-preview",
            )
        )
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.10-flash-high")

    def test_static_36_is_offline_fallback_only(self):
        roles = broker.antigravity_roles_from_models(self._catalog())
        self.assertEqual(roles["workhorse"]["id"], broker.ANTIGRAVITY_DEFAULT_MODEL)
        self.assertEqual(roles["source"], "offline-fallback")

    def test_generic_flash_moves_to_latest_but_explicit_version_stays_exact(self):
        catalog = self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        common = {
            "target_agent": "antigravity",
            "project": "p",
            "topic": "routing-test",
        }
        with mock.patch.object(broker, "discover_antigravity_models", return_value=catalog), \
             mock.patch.object(broker, "find_model_default", return_value=None), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")):
            bare = broker.resolve_model_request(common)
            generic = broker.resolve_model_request({**common, "target_model": "gemini flash"})
            exact = broker.resolve_model_request(
                {**common, "target_model": "gemini-3.6-flash-high"}
            )
        self.assertEqual(bare["target_model"], "gemini-3.7-flash-high")
        self.assertEqual(bare["source"], "family_workhorse")
        self.assertEqual(generic["target_model"], "gemini-3.7-flash-high")
        self.assertEqual(generic["source"], "family_workhorse")
        self.assertEqual(exact["target_model"], "gemini-3.6-flash-high")
        self.assertEqual(exact["source"], "explicit_request")

    def test_catalog_and_guide_expose_non_authoritative_workhorse_role(self):
        models = self._catalog("gemini-3.6-flash-high", "gemini-3.7-flash-high")
        with mock.patch.object(broker, "discover_antigravity_models", return_value=models):
            catalog = broker.list_agent_models("antigravity")
            guide = broker.get_model_routing_guide("antigravity")
        roles = catalog["catalogs"]["antigravity"]["roles"]
        policy = guide["defaults"]["antigravity_cli"]
        self.assertEqual(roles["workhorse"]["id"], "gemini-3.7-flash-high")
        self.assertFalse(roles["peer_brain_eligible"])
        self.assertEqual(policy["role"], "workhorse")
        self.assertFalse(policy["authoritative"])
        self.assertFalse(policy["peer_brain_eligible"])
        self.assertFalse(policy["native_child_agent"])
        self.assertIn("bounded search/read/extraction/summaries/drafting", policy["recommended_for"])
        self.assertEqual(policy["failure_fallback"]["codex"], ["explorer", "worker"])
        self.assertEqual(policy["failure_fallback"]["claude"], ["Explore", "economy-worker"])
        self.assertTrue(policy["failure_fallback"]["record_fallback"])
        self.assertIn("proactively consider", policy["rule"])
        self.assertIn("not a native child agent", policy["rule"])
        self.assertIn("missing, quota-limited, times out, mismatches, or fails", policy["rule"])
        self.assertIn("concurrently only on independent packages", policy["rule"])
        self.assertIn("writes are serial unless demonstrably isolated", policy["rule"])
        self.assertIn("brain reviews evidence/diffs", policy["rule"])

        examples = guide["caller_examples"]
        read_args = examples["antigravity_external_read"]["args"]
        write_args = examples["antigravity_external_implementation"]["args"]
        for args in (read_args, write_args):
            self.assertEqual(args["target_agent"], "antigravity")
            self.assertEqual(args["surface"], "cli")
            self.assertEqual(args["target_model"], "gemini flash")
            self.assertEqual(args["effort"], "high")
        self.assertEqual(read_args["mode"], "plan")
        self.assertEqual(write_args["mode"], "accept-edits")
        self.assertIn("approved isolated package", write_args["prompt"])
        self.assertEqual(write_args["work_package_id"], "WP-1")
        self.assertLessEqual(len(write_args["allowed_files"]), broker.FLASH_WORKHORSE_MAX_ALLOWED_FILES)
        self.assertTrue(write_args["acceptance_criteria"])
        self.assertIn("one work package per call", " ".join(policy["hard_requirements"]))
        self.assertIn("schema-enforced JSON", " ".join(policy["hard_requirements"]))

    @staticmethod
    def _flash_package() -> dict:
        return broker.prepare_flash_work_package(
            {
                "work_package_id": "WP-TEST",
                "allowed_files": ["src/worker.py", "tests/test_worker.py"],
                "acceptance_criteria": ["Focused tests pass."],
            },
            "implementation",
            "Implement the approved bounded change.",
        )

    @staticmethod
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

    def test_flash_implementation_requires_a_bounded_envelope(self):
        with self.assertRaisesRegex(ValueError, "work_package_id"):
            broker.prepare_flash_work_package({}, "implementation", "Implement everything.")
        with self.assertRaisesRegex(ValueError, "1-5 allowed_files"):
            broker.prepare_flash_work_package(
                {"work_package_id": "WP-X", "acceptance_criteria": ["tests pass"]},
                "implementation",
                "Implement everything.",
            )
        with self.assertRaisesRegex(ValueError, "acceptance_criteria"):
            broker.prepare_flash_work_package(
                {"work_package_id": "WP-X", "allowed_files": ["src/x.py"]},
                "implementation",
                "Implement everything.",
            )

    def test_agy_cli_always_uses_schema_and_returns_validated_structure(self):
        package = self._flash_package()
        stdout = json.dumps(self._flash_output(package["package_id"]))
        staging = broker.Path(broker.tempfile.mkdtemp(prefix="flash-test-staging-"))
        try:
            with mock.patch.object(broker, "load_config", return_value={}), \
                 mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
                 mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
                 mock.patch.object(broker, "_prepare_staging", return_value=(staging, {}, None)), \
                 mock.patch.object(broker, "run_process", return_value=(0, stdout, "")) as run:
                response = broker.consult_antigravity_cli(
                    "p", "bounded prompt", "accept-edits", "gemini-3.7-flash-high", "high", 60, package
                )
        finally:
            broker.shutil.rmtree(staging, ignore_errors=True)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        schema = json.loads(command[command.index("--json-schema") + 1])
        self.assertIn("brain_verification_required", schema["required"])
        self.assertEqual(schema["properties"]["package_id"]["enum"], ["WP-TEST"])
        # agy grants NO tool access headlessly without this flag -- settings allow-rules
        # are ignored in print mode and there is no scoped read-only grant -- so the old
        # assertion that it is never passed would now simply forbid the worker from
        # reading anything. The safety property moved rather than disappeared: the flag
        # is acceptable only while the worker is confined to a disposable copy, so that
        # confinement is what this test guards.
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertEqual(str(run.call_args.args[1]), str(staging))
        parsed = json.loads(response)
        self.assertEqual(parsed["worker_status"], "completed")
        self.assertEqual(parsed["structured_output"]["package_id"], "WP-TEST")

    def test_agy_cli_never_runs_in_the_real_project_tree(self):
        """The worker must never be pointed at the user's own files.

        With permission checks disabled a stray write or command would otherwise land
        on the real project, so this asserts the working directory is a staging copy
        and not the resolved project root.
        """
        package = self._flash_package()
        stdout = json.dumps(self._flash_output(package["package_id"]))
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "run_process", return_value=(0, stdout, "")) as run:
            broker.consult_antigravity_cli(
                "p", "bounded prompt", "plan", "gemini-3.7-flash-high", "high", 60, package
            )
        cwd = broker.Path(str(run.call_args.args[1])).resolve()
        self.assertNotEqual(cwd, broker.Path(".").resolve())
        self.assertIn("flash-staging-", str(cwd))

    def test_write_back_refuses_to_clobber_a_file_changed_underneath(self):
        """The worker's edit must not overwrite someone else's concurrent edit.

        A two-way staged-vs-live compare cannot tell "the worker changed this" from
        "the real file moved on while the package ran", so it silently destroys the
        newer content. This pins the three-way check that prevents it.
        """
        real = broker.Path(broker.tempfile.mkdtemp(prefix="flash-real-"))
        staging = broker.Path(broker.tempfile.mkdtemp(prefix="flash-stage-"))
        try:
            target = real / "shared.py"
            target.write_text("original\n", encoding="utf-8")
            baseline = {"shared.py": broker._file_digest(target)}

            # The worker edited its copy...
            (staging / "shared.py").write_text("worker edit\n", encoding="utf-8")
            # ...while someone else edited the real file during the run.
            target.write_text("concurrent human edit\n", encoding="utf-8")

            report = broker._apply_staged_changes(
                staging, real, {str(target.resolve())}, baseline
            )

            self.assertEqual(report["applied"], [])
            self.assertEqual(report["conflicted_changed_underneath"], [str(target.resolve())])
            self.assertEqual(target.read_text(encoding="utf-8"), "concurrent human edit\n")
        finally:
            broker.shutil.rmtree(real, ignore_errors=True)
            broker.shutil.rmtree(staging, ignore_errors=True)

    def test_write_back_applies_a_clean_worker_edit(self):
        real = broker.Path(broker.tempfile.mkdtemp(prefix="flash-real-"))
        staging = broker.Path(broker.tempfile.mkdtemp(prefix="flash-stage-"))
        try:
            target = real / "shared.py"
            target.write_text("original\n", encoding="utf-8")
            baseline = {"shared.py": broker._file_digest(target)}
            (staging / "shared.py").write_text("worker edit\n", encoding="utf-8")

            report = broker._apply_staged_changes(
                staging, real, {str(target.resolve())}, baseline
            )

            self.assertEqual(report["applied"], [str(target.resolve())])
            self.assertEqual(target.read_text(encoding="utf-8"), "worker edit\n")
        finally:
            broker.shutil.rmtree(real, ignore_errors=True)
            broker.shutil.rmtree(staging, ignore_errors=True)

    def test_write_back_reports_a_deletion_instead_of_performing_it(self):
        real = broker.Path(broker.tempfile.mkdtemp(prefix="flash-real-"))
        staging = broker.Path(broker.tempfile.mkdtemp(prefix="flash-stage-"))
        try:
            target = real / "gone.py"
            target.write_text("still here\n", encoding="utf-8")
            baseline = {"gone.py": broker._file_digest(target)}
            # staging is empty: the worker deleted it

            report = broker._apply_staged_changes(
                staging, real, {str(target.resolve())}, baseline
            )

            self.assertEqual(report["deleted_in_staging_not_applied"], [str(target.resolve())])
            self.assertTrue(target.exists(), "a deletion must never be propagated")
        finally:
            broker.shutil.rmtree(real, ignore_errors=True)
            broker.shutil.rmtree(staging, ignore_errors=True)

    def test_flash_danger_full_access_is_rejected_before_agy_starts(self):
        package = self._flash_package()
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "run_process") as run:
            response = broker.consult_antigravity_cli(
                "p", "deploy everything", "danger-full-access", "gemini-3.7-flash-high", "high", 60, package
            )
        self.assertTrue(response.startswith("Antigravity CLI Flash safety policy rejected"))
        run.assert_not_called()

    def test_direct_accept_edits_cannot_bypass_the_package_envelope(self):
        with mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "discover_antigravity_cli", return_value="agy"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "run_process") as run:
            with self.assertRaisesRegex(ValueError, "work_package_id"):
                broker.consult_antigravity_cli(
                    "p", "edit the project", "accept-edits", "gemini-3.7-flash-high", "high", 60
                )
        run.assert_not_called()

    def test_flash_validation_rejects_contradiction_scope_and_design_rationalization(self):
        package = self._flash_package()
        outer = self._flash_output(
            package["package_id"],
            ambiguities=["Plan does not specify retry semantics."],
            files_changed=[{"path": "src/outside.py", "change": "Expanded scope."}],
            acceptance_criteria=[{"criterion": "A different easier criterion.", "status": "passed", "evidence": []}],
            claims=[{"statement": "The duplicate query is intentional by design.", "basis": "assumption", "evidence": []}],
        )
        _, errors = broker.validate_flash_workhorse_result(outer, package)
        joined = " | ".join(errors)
        self.assertIn("completed contradicts non-empty ambiguities", joined)
        self.assertIn("out-of-scope file reported", joined)
        self.assertIn("acceptance criteria do not exactly match", joined)
        self.assertIn("intentional/by-design claim lacks observed primary evidence", joined)

    def test_consult_marks_flash_completion_pending_brain_verification(self):
        package = self._flash_package()
        normalized = json.dumps(
            {
                "package_id": package["package_id"],
                "worker_status": "completed",
                "structured_output": self._flash_output(package["package_id"])["structured_output"],
                "cli": {},
            }
        )
        args = {
            "prompt": "Implement the approved bounded change.",
            "task_kind": "implementation",
            "mode": "accept-edits",
            "target_model": "gemini-3.7-flash-high",
            "effort": "high",
            "work_package_id": package["package_id"],
            "allowed_files": package["allowed_files"],
            "acceptance_criteria": package["acceptance_criteria"],
        }
        with mock.patch.object(broker, "load_config", return_value={"compact_task_contract": False}), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", ".")), \
             mock.patch.object(broker, "consult_antigravity_cli", return_value=normalized), \
             mock.patch.object(broker, "store_consultation"):
            result = broker.consult("antigravity", args)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["brain_verification"]["status"], "pending")
        self.assertTrue(result["structured_output_enforced"])


class ClaudeFrontierFallbackTests(unittest.TestCase):
    def _consult(self, runs):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value="claude"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "claude_empty_mcp_config_path", return_value=Path(tmpdir) / "empty.json"), \
             mock.patch.object(broker, "run_process", side_effect=runs) as run:
            result = broker.consult_claude(tmpdir, "check", model_name="best", effort="max")
            return result, run

    def test_best_alias_attests_any_structured_claude_model(self):
        self.assertTrue(broker.claude_model_attested("best", "claude-fable-5"))
        self.assertTrue(broker.claude_model_attested("best", "claude-opus-6-1"))
        self.assertFalse(broker.claude_model_attested("best", "unknown-provider-model"))

    def test_best_unavailable_falls_back_to_fable(self):
        result, run = self._consult(
            [
                (1, "", "Invalid model name: model 'best' is not available"),
                (0, claude_stream("claude-fable-5", "approved"), ""),
            ]
        )
        self.assertEqual(result.initial_model, "best")
        self.assertEqual(result.requested_model, "fable")
        self.assertEqual(result.actual_model, "claude-fable-5")
        self.assertEqual(result.attempted_models, ("best", "fable"))
        self.assertTrue(result.model_attested)
        self.assertEqual(run.call_count, 2)

    def test_best_and_fable_unavailable_fall_back_to_opus(self):
        result, run = self._consult(
            [
                (1, "", "Unknown model: best"),
                (1, "", "Model fable is unavailable for this subscription"),
                (0, claude_stream("claude-opus-6-1", "approved"), ""),
            ]
        )
        self.assertEqual(result.requested_model, "opus")
        self.assertEqual(result.attempted_models, ("best", "fable", "opus"))
        self.assertEqual(run.call_count, 3)

    def test_general_failure_does_not_retry(self):
        result, run = self._consult([(1, "", "Network connection reset")])
        self.assertFalse(result.model_attested)
        self.assertEqual(result.attempted_models, ("best",))
        self.assertEqual(run.call_count, 1)


class ClaudeQueuedModeTests(unittest.TestCase):
    def test_queue_persists_implementation_mode(self):
        # sqlite3's context manager commits but does not close the connection;
        # tolerate delayed handle release on Windows test cleanup.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            broker_home = root / "broker"
            db_path = broker_home / "state.sqlite"
            with mock.patch.object(broker, "BROKER_DIR", broker_home), \
                 mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "CONFIG_PATH", broker_home / "config.json"):
                result = broker.queue_claude_request(
                    str(root),
                    "implement the approved patch",
                    target_model="sonnet",
                    cli_model="sonnet",
                    autorun=False,
                    mode="acceptEdits",
                )
                with sqlite3.connect(db_path) as conn:
                    stored = conn.execute(
                        "SELECT mode FROM claude_requests WHERE id = ?", (result["id"],)
                    ).fetchone()[0]
            self.assertEqual(result["mode"], "acceptEdits")
            self.assertEqual(stored, "acceptEdits")

    def test_worker_reuses_persisted_implementation_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            broker_home = root / "broker"
            db_path = broker_home / "state.sqlite"
            with mock.patch.object(broker, "BROKER_DIR", broker_home), \
                 mock.patch.object(broker, "DB_PATH", db_path), \
                 mock.patch.object(broker, "CONFIG_PATH", broker_home / "config.json"):
                queued = broker.queue_claude_request(
                    str(root),
                    "implement the approved patch",
                    target_model="sonnet",
                    cli_model="sonnet",
                    autorun=False,
                    mode="acceptEdits",
                )
                response = broker.ClaudeConsultResult(
                    response="implemented",
                    requested_model="sonnet",
                    actual_model="claude-sonnet-5",
                    model_attested=True,
                    initial_model="sonnet",
                    attempted_models=("sonnet",),
                )
                with mock.patch.object(broker, "consult_claude", return_value=response) as consult, \
                     mock.patch.object(broker, "store_consultation"), \
                     mock.patch.object(broker, "record_agent_event"), \
                     mock.patch.object(broker, "render_request_ledger"):
                    result = broker.run_claude_request_worker(queued["id"])
            self.assertEqual(consult.call_args.args[2], "acceptEdits")
            self.assertEqual(result["mode"], "acceptEdits")
            self.assertEqual(result["actual_model"], "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
