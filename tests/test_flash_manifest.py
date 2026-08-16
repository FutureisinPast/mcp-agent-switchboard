"""Acceptance tests for manifest-based Flash staging.

The case list is the one Codex gpt-5.6-sol/max named as the minimum that would
convince it the ancestor-scan removal is safe (consult 9533ea6e). Cases the
author would most likely skip -- concurrent owner edit, create-after-staging
race, undeclared delete/rename, path aliasing -- are included deliberately.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flash_manifest as fm


class ManifestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.workspace = Path(tempfile.mkdtemp(prefix="fm-workspace-"))
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.staged_roots: list[Path] = []

    def tearDown(self) -> None:
        for root in self.staged_roots:
            shutil.rmtree(root, ignore_errors=True)

    def write(self, rel: str, content: str = "original\n") -> Path:
        path = self.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def stage(self, manifest):
        staged = fm.stage(manifest)
        self.staged_roots.append(staged.root)
        return staged


class TestScopeIsTheManifest(ManifestTestCase):
    def test_one_file_package_ignores_a_huge_sibling_tree(self):
        """Case 1: the defect that started this. A one-file package must not be
        priced by the tree that happens to contain it."""
        target = self.write("src/small.py")
        noise = self.workspace / "vendor"
        for i in range(300):
            path = noise / f"pkg{i % 20}" / f"f{i}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 4096)

        manifest = fm.build_manifest(
            {"read_context": [str(target)]}, str(self.workspace), implementation_mode=False
        )
        self.assertEqual(manifest["files"], 1)
        staged = self.stage(manifest)
        # The staging tree holds the declared file and nothing else.
        staged_files = [
            p for p in staged.root.rglob("*") if p.is_file()
        ]
        self.assertEqual(len(staged_files), 1)
        self.assertEqual(staged_files[0].read_text(encoding="utf-8"), "original\n")

    def test_files_in_unrelated_subtrees_do_not_pull_in_their_ancestor(self):
        """The old common-ancestor rule staged everything between two files."""
        a = self.write("alpha/deep/one.py")
        b = self.write("beta/other/two.py")
        for i in range(50):
            self.write(f"gamma/junk{i}.txt", "junk")

        manifest = fm.build_manifest(
            {"allowed_writes": [str(a), str(b)]}, str(self.workspace), implementation_mode=True
        )
        staged = self.stage(manifest)
        self.assertEqual(len([p for p in staged.root.rglob("*") if p.is_file()]), 2)
        self.assertTrue((staged.root / "alpha/deep/one.py").is_file())
        self.assertTrue((staged.root / "beta/other/two.py").is_file())


class TestStructuredRejections(ManifestTestCase):
    def test_count_cap_at_threshold_and_threshold_plus_one(self):
        """Case 10: boundary on both sides, and every rejection field present."""
        files = [str(self.write(f"w{i}.py")) for i in range(fm.MANIFEST_MAX_WRITES)]
        manifest = fm.build_manifest(
            {"allowed_writes": files}, str(self.workspace), implementation_mode=True
        )
        self.assertEqual(len(manifest["writes"]), fm.MANIFEST_MAX_WRITES)

        files.append(str(self.write("one_too_many.py")))
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.build_manifest(
                {"allowed_writes": files}, str(self.workspace), implementation_mode=True
            )
        payload = ctx.exception.to_dict()
        self.assertEqual(payload["cap_id"], "manifest.allowed_writes.count")
        self.assertEqual(payload["measured"], fm.MANIFEST_MAX_WRITES + 1)
        self.assertEqual(payload["threshold"], fm.MANIFEST_MAX_WRITES)
        for key in ("scope", "why_selected", "rescope_hint"):
            self.assertTrue(payload[key], f"{key} must be populated for a one-step re-scope")

    def test_byte_cap_names_the_measurement(self):
        big = self.write("big.bin", "y" * (fm.MANIFEST_MAX_BYTES + 10))
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.build_manifest(
                {"read_context": [str(big)]}, str(self.workspace), implementation_mode=False
            )
        self.assertEqual(ctx.exception.cap_id, "manifest.total.bytes")
        self.assertGreater(ctx.exception.measured, fm.MANIFEST_MAX_BYTES)

    def test_implementation_with_nothing_writable_is_refused(self):
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.build_manifest({}, str(self.workspace), implementation_mode=True)
        self.assertEqual(ctx.exception.cap_id, "manifest.implementation.empty")


class TestPathValidation(ManifestTestCase):
    """Case 11: every way a declared path could name something else."""

    def test_traversal_escaping_the_workspace_is_refused(self):
        outside = self.workspace.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.canonical_path("../outside.txt", self.workspace)
        self.assertEqual(ctx.exception.cap_id, "manifest.path.escapes_workspace")

    def test_absolute_path_outside_workspace_is_refused(self):
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.canonical_path(r"C:\Windows\System32\drivers\etc\hosts", self.workspace)
        self.assertEqual(ctx.exception.cap_id, "manifest.path.escapes_workspace")

    def test_alternate_data_stream_is_refused(self):
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.canonical_path("notes.txt:hidden", self.workspace)
        self.assertEqual(ctx.exception.cap_id, "manifest.path.alternate_data_stream")

    def test_unc_and_device_paths_are_refused(self):
        for raw in (r"\\server\share\f.txt", r"\\?\C:\f.txt", "//server/share/f.txt"):
            with self.assertRaises(fm.ManifestRejection) as ctx:
                fm.canonical_path(raw, self.workspace)
            self.assertEqual(ctx.exception.cap_id, "manifest.path.unc_or_device", raw)

    def test_trailing_dot_or_space_alias_is_refused(self):
        for raw in ("file.py ", "file.py."):
            with self.assertRaises(fm.ManifestRejection) as ctx:
                fm.canonical_path(raw, self.workspace)
            self.assertEqual(ctx.exception.cap_id, "manifest.path.trailing_dot_or_space", raw)

    def test_case_collision_across_fields_is_refused(self):
        target = self.write("Case.py")
        other = str(target).replace("Case.py", "case.py")
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.build_manifest(
                {"allowed_writes": [str(target)], "read_context": [other]},
                str(self.workspace),
                implementation_mode=True,
            )
        self.assertEqual(ctx.exception.cap_id, "manifest.path.collision")

    @unittest.skipUnless(os.name == "nt", "reparse points are a Windows concern here")
    def test_symlink_in_the_path_is_refused(self):
        real = self.write("real/target.py")
        link = self.workspace / "link"
        try:
            link.symlink_to(real.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation requires privilege on this box")
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.canonical_path(str(link / "target.py"), self.workspace)
        self.assertEqual(ctx.exception.cap_id, "manifest.path.reparse_point")


class TestLegacyAlias(ManifestTestCase):
    """Case 12: alias-only, new-only, identical mixed, conflicting mixed."""

    def test_legacy_alias_only_maps_to_writes(self):
        target = self.write("a.py")
        manifest = fm.build_manifest(
            {"allowed_files": [str(target)]}, str(self.workspace), implementation_mode=True
        )
        self.assertEqual(manifest["writes"], [target.absolute()])

    def test_new_fields_only(self):
        target = self.write("a.py")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(target)]}, str(self.workspace), implementation_mode=True
        )
        self.assertEqual(manifest["writes"], [target.absolute()])

    def test_identical_mixed_is_accepted(self):
        target = self.write("a.py")
        manifest = fm.build_manifest(
            {"allowed_files": [str(target)], "allowed_writes": [str(target)]},
            str(self.workspace),
            implementation_mode=True,
        )
        self.assertEqual(manifest["writes"], [target.absolute()])

    def test_conflicting_mixed_is_refused(self):
        one = self.write("a.py")
        two = self.write("b.py")
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.build_manifest(
                {"allowed_files": [str(one)], "allowed_writes": [str(two)]},
                str(self.workspace),
                implementation_mode=True,
            )
        self.assertEqual(ctx.exception.cap_id, "manifest.fields.conflict")


class TestWriteBack(ManifestTestCase):
    def test_declared_edit_and_create_are_applied(self):
        """Case 4: the happy path still works."""
        edit = self.write("edit.py", "before\n")
        create = self.workspace / "made.py"
        manifest = fm.build_manifest(
            {"allowed_writes": [str(edit)], "allowed_creates": [str(create)]},
            str(self.workspace),
            implementation_mode=True,
        )
        staged = self.stage(manifest)
        (staged.root / "edit.py").write_text("after\n", encoding="utf-8")
        (staged.root / "made.py").write_text("new\n", encoding="utf-8")

        changes = fm.collect_changes(staged)
        report = fm.apply_changes(staged, changes)
        self.assertFalse(report["refused"], report["reasons"])
        self.assertEqual(edit.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(create.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(report["integrity"], "verified")

    def test_no_op_worker_writes_nothing(self):
        """Case 16."""
        edit = self.write("edit.py", "before\n")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(edit)]}, str(self.workspace), implementation_mode=True
        )
        staged = self.stage(manifest)
        changes = fm.collect_changes(staged)
        report = fm.apply_changes(staged, changes)
        self.assertEqual(report["applied"], [])
        self.assertFalse(report["refused"])
        self.assertEqual(edit.read_text(encoding="utf-8"), "before\n")

    def test_undeclared_create_refuses_the_whole_dispatch(self):
        """Case 7: the declared edit must NOT land either."""
        edit = self.write("edit.py", "before\n")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(edit)]}, str(self.workspace), implementation_mode=True
        )
        staged = self.stage(manifest)
        (staged.root / "edit.py").write_text("after\n", encoding="utf-8")
        (staged.root / "sneaky.py").write_text("undeclared\n", encoding="utf-8")

        changes = fm.collect_changes(staged)
        self.assertIn("sneaky.py", changes["undeclared"])
        report = fm.apply_changes(staged, changes)
        self.assertTrue(report["refused"])
        self.assertEqual(report["applied"], [])
        self.assertEqual(edit.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(report["integrity"], "indeterminate")
        self.assertFalse((self.workspace / "sneaky.py").exists())

    def test_deletion_is_refused_and_applies_nothing(self):
        """Case 17 + the standing 'never propagate a deletion' rule."""
        one = self.write("one.py", "one\n")
        two = self.write("two.py", "two\n")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(one), str(two)]},
            str(self.workspace),
            implementation_mode=True,
        )
        staged = self.stage(manifest)
        (staged.root / "one.py").write_text("edited\n", encoding="utf-8")
        (staged.root / "two.py").unlink()

        changes = fm.collect_changes(staged)
        self.assertIn("two.py", changes["deleted"])
        report = fm.apply_changes(staged, changes)
        self.assertTrue(report["refused"])
        self.assertEqual(one.read_text(encoding="utf-8"), "one\n")
        self.assertTrue(two.is_file())

    def test_modified_read_context_is_refused(self):
        """Case 9: read context is read-only, and saying so is not enough."""
        edit = self.write("edit.py", "before\n")
        context = self.write("reference.py", "reference\n")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(edit)], "read_context": [str(context)]},
            str(self.workspace),
            implementation_mode=True,
        )
        staged = self.stage(manifest)
        (staged.root / "edit.py").write_text("after\n", encoding="utf-8")
        (staged.root / "reference.py").write_text("tampered\n", encoding="utf-8")

        changes = fm.collect_changes(staged)
        report = fm.apply_changes(staged, changes)
        self.assertTrue(report["refused"])
        self.assertEqual(context.read_text(encoding="utf-8"), "reference\n")
        self.assertEqual(edit.read_text(encoding="utf-8"), "before\n")

    def test_concurrent_owner_edit_conflicts_without_overwriting(self):
        """Case 5: the owner's editor saved while the package ran."""
        edit = self.write("edit.py", "before\n")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(edit)]}, str(self.workspace), implementation_mode=True
        )
        staged = self.stage(manifest)
        (staged.root / "edit.py").write_text("worker version\n", encoding="utf-8")
        edit.write_text("owner version\n", encoding="utf-8")

        changes = fm.collect_changes(staged)
        report = fm.apply_changes(staged, changes)
        self.assertTrue(report["refused"])
        self.assertIn("changed underneath", " ".join(report["reasons"]))
        self.assertEqual(edit.read_text(encoding="utf-8"), "owner version\n")

    def test_create_after_staging_race_is_refused(self):
        """Case 6: the create target appeared between staging and commit."""
        create = self.workspace / "made.py"
        manifest = fm.build_manifest(
            {"allowed_creates": [str(create)]}, str(self.workspace), implementation_mode=True
        )
        staged = self.stage(manifest)
        (staged.root / "made.py").write_text("worker\n", encoding="utf-8")
        create.write_text("owner got there first\n", encoding="utf-8")

        changes = fm.collect_changes(staged)
        report = fm.apply_changes(staged, changes)
        self.assertTrue(report["refused"])
        self.assertEqual(create.read_text(encoding="utf-8"), "owner got there first\n")

    def test_allowed_creates_naming_an_existing_path_is_refused_up_front(self):
        existing = self.write("already.py")
        with self.assertRaises(fm.ManifestRejection) as ctx:
            fm.build_manifest(
                {"allowed_creates": [str(existing)]},
                str(self.workspace),
                implementation_mode=True,
            )
        self.assertEqual(ctx.exception.cap_id, "manifest.allowed_creates.exists")


class TestReceiptHonesty(ManifestTestCase):
    def test_receipt_never_claims_a_write_barrier_or_a_clean_tree(self):
        target = self.write("a.py")
        manifest = fm.build_manifest(
            {"read_context": [str(target)]}, str(self.workspace), implementation_mode=False
        )
        staged = self.stage(manifest)
        changes = fm.collect_changes(staged)
        receipt = fm.containment_receipt(staged, manifest, changes, None)
        self.assertIs(receipt["os_write_barrier"], False)
        self.assertEqual(receipt["integrity"]["other_paths"], "not_watched")
        self.assertEqual(receipt["containment_mode"], "snapshot")

    def test_receipt_marks_integrity_indeterminate_after_an_undeclared_change(self):
        edit = self.write("edit.py")
        manifest = fm.build_manifest(
            {"allowed_writes": [str(edit)]}, str(self.workspace), implementation_mode=True
        )
        staged = self.stage(manifest)
        (staged.root / "rogue.py").write_text("x", encoding="utf-8")
        changes = fm.collect_changes(staged)
        report = fm.apply_changes(staged, changes)
        receipt = fm.containment_receipt(staged, manifest, changes, report)
        self.assertEqual(receipt["integrity"]["declared_paths"], "indeterminate")


class TestPromptAppendix(ManifestTestCase):
    def test_appendix_names_staged_paths_so_the_worker_cannot_fall_back(self):
        """Codex D4: workers must not silently use real-project paths."""
        edit = self.write("edit.py")
        context = self.write("ref.py")
        create = self.workspace / "new.py"
        manifest = fm.build_manifest(
            {
                "allowed_writes": [str(edit)],
                "allowed_creates": [str(create)],
                "read_context": [str(context)],
            },
            str(self.workspace),
            implementation_mode=True,
        )
        staged = self.stage(manifest)
        text = fm.prompt_appendix(staged)
        self.assertIn(str(staged.staged_path("edit.py")), text)
        self.assertIn(str(staged.staged_path("new.py")), text)
        self.assertIn(str(staged.staged_path("ref.py")), text)
        # The real locations must not be offered as an alternative.
        self.assertNotIn(str(edit), text)


if __name__ == "__main__":
    unittest.main()
