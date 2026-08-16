"""Manifest-based staging for Flash worker packages.

WHY THIS EXISTS
---------------
The previous design derived the worker's world from a DIRECTORY: implementation
packages staged the smallest common ancestor of ``allowed_files``, and plan
packages staged the entire project root. On a real workspace that is fatal --
a one-file read package against a 1.4 GB project was refused with "package root
... is too large to stage safely" before the worker ever started. The scope
primitive was wrong: a package is a set of FILES, not a tree that happens to
contain them.

Here the manifest IS the scope. Nothing is ever walked. Every path is declared,
canonicalised and hashed up front, and the disposable tree contains exactly the
declared files.

WHAT THIS DOES *NOT* DO
-----------------------
This is ISOLATION, not containment, and the receipt must say so. ``agy`` only
grants tool access under a skip-permissions flag, so the worker runs as the
owner's Windows user with command execution: absolute paths, the profile, the
registry and the network stay reachable no matter what this module stages.
Staging bounds ACCIDENTS (a stray relative write, a botched rewrite); it is not
a security boundary and no field here may claim one. Paths outside the manifest
are reported as ``not_watched`` -- never as "unchanged", which would be a claim
this module cannot support.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import atomic_io

# Writes stay at the historical Flash bound: a package the brain cannot describe
# in five files is a package the brain has not finished decomposing. Read context
# is allowed to be wider because reading is cheap, and undeclared reading is the
# behaviour that made workers wander in the first place.
MANIFEST_MAX_WRITES = 5
MANIFEST_MAX_CREATES = 5
MANIFEST_MAX_READ_CONTEXT = 20
MANIFEST_MAX_FILES = 25
MANIFEST_MAX_BYTES = 8 * 1024 * 1024

_DRIVE_PREFIX_LEN = 2  # "C:" -- the only legitimate colon in a Windows path


class ManifestRejection(Exception):
    """A refusal the brain can act on without a second round trip.

    A bare "too large" string forced the brain to guess which limit it hit and
    re-scope blind. Every refusal here names the cap, the measurement, the
    threshold, what scope produced it, and the concrete next move.
    """

    def __init__(
        self,
        cap_id: str,
        measured: Any,
        threshold: Any,
        scope: str,
        why_selected: str,
        rescope_hint: str,
    ) -> None:
        self.cap_id = cap_id
        self.measured = measured
        self.threshold = threshold
        self.scope = scope
        self.why_selected = why_selected
        self.rescope_hint = rescope_hint
        super().__init__(self.message())

    def message(self) -> str:
        return (
            f"{self.cap_id}: measured {self.measured}, limit {self.threshold} "
            f"(scope: {self.scope}). {self.why_selected} {self.rescope_hint}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cap_id": self.cap_id,
            "measured": self.measured,
            "threshold": self.threshold,
            "scope": self.scope,
            "why_selected": self.why_selected,
            "rescope_hint": self.rescope_hint,
        }


def is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions and every other reparse point.

    Anything unclassifiable counts as one: refusing an ordinary file is
    recoverable, following a link out of the manifest is not.
    """
    try:
        st = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def file_digest(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _reject(cap_id: str, raw: Any, why: str, hint: str) -> ManifestRejection:
    return ManifestRejection(
        cap_id=cap_id,
        measured=raw,
        threshold="n/a",
        scope="manifest path",
        why_selected=why,
        rescope_hint=hint,
    )


def canonical_path(raw: str, workspace_root: Path) -> Path:
    """Resolve one declared path, or refuse it.

    Every refusal below is a way a declared path could reach data the package
    never named -- which is the whole point of declaring paths in the first place.
    """
    # Leading whitespace and surrounding quotes are cosmetic and get removed.
    # TRAILING whitespace deliberately does not: Win32 strips a trailing space or
    # dot when it opens the file, so "a.py " and "a.py" are the same file with
    # different spellings. Normalising that here would let a declared path carry a
    # digest for one name and be written under another, which is exactly the alias
    # this check exists to catch -- so it is refused loudly instead.
    text = str(raw or "").lstrip()
    quote = chr(34)
    if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
        text = text[1:-1].lstrip()
    if not text:
        raise _reject(
            "manifest.path.empty",
            raw,
            "An empty path was declared.",
            "Remove it, or name a real file.",
        )

    # Device and UNC namespaces sidestep normalisation entirely, so a later
    # containment check would compare two spellings of different things.
    if text.startswith("\\\\") or text.startswith("//"):
        raise _reject(
            "manifest.path.unc_or_device",
            text,
            "UNC and device paths bypass path normalisation.",
            "Declare a local path under the workspace root.",
        )

    # Alternate data streams: "file.txt:hidden" reads and writes bytes that the
    # digest of "file.txt" never covers.
    tail = text[_DRIVE_PREFIX_LEN:] if len(text) > _DRIVE_PREFIX_LEN and text[1:2] == ":" else text
    if ":" in tail:
        raise _reject(
            "manifest.path.alternate_data_stream",
            text,
            "Alternate data streams are not covered by the file digest.",
            "Declare the plain file path.",
        )

    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(workspace_root) / candidate

    # Trailing dots and spaces are stripped by the Win32 layer, so "a.py " and
    # "a.py" name one file but compare unequal -- an allowlist bypass.
    for part in candidate.parts:
        if part in (".", ".."):
            continue
        if part.endswith(":\\") or part.endswith(":/"):
            continue
        if part != part.rstrip() or part.endswith("."):
            raise _reject(
                "manifest.path.trailing_dot_or_space",
                text,
                "Windows silently strips trailing dots and spaces, so this aliases another path.",
                "Declare the exact file name.",
            )

    try:
        resolved = Path(os.path.normpath(str(candidate))).absolute()
    except (OSError, ValueError) as exc:
        raise _reject(
            "manifest.path.unresolvable",
            text,
            f"Path could not be normalised: {exc}",
            "Declare an absolute local path.",
        )

    try:
        root_resolved = Path(os.path.normpath(str(workspace_root))).absolute()
    except (OSError, ValueError):
        root_resolved = Path(workspace_root)

    # Traversal: the manifest may only describe files inside the workspace it
    # declared. Anything else is a package reaching outside its own scope.
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise _reject(
            "manifest.path.escapes_workspace",
            str(resolved),
            f"Path resolves outside the declared workspace_root ({root_resolved}).",
            "Declare a path inside the workspace, or set workspace_root to the tree you mean.",
        )

    # A reparse point ANYWHERE in the chain redirects the real read or write, so
    # checking only the leaf would miss a junctioned parent directory.
    probe = resolved
    while True:
        if probe.exists() or probe.is_symlink():
            if is_reparse_point(probe):
                raise _reject(
                    "manifest.path.reparse_point",
                    str(probe),
                    "A symlink, junction or reparse point in this path redirects the real target.",
                    "Declare the physical path.",
                )
        if probe.parent == probe or probe == root_resolved:
            break
        probe = probe.parent
    return resolved


@dataclass
class StagedPackage:
    """The disposable tree, plus everything needed to judge what came back."""

    root: Path
    workspace_root: Path
    baseline: dict[str, str] = field(default_factory=dict)
    write_targets: dict[str, Path] = field(default_factory=dict)
    create_targets: dict[str, Path] = field(default_factory=dict)
    read_only: set[str] = field(default_factory=set)
    staged_bytes: int = 0
    containment_mode: str = "snapshot"

    def staged_path(self, rel: str) -> Path:
        return self.root / rel

    def path_map(self) -> dict[str, str]:
        """Real path -> staged path, for telling the worker where to work."""
        mapping: dict[str, str] = {}
        for rel, real in list(self.write_targets.items()) + list(self.create_targets.items()):
            mapping[str(real)] = str(self.staged_path(rel))
        for rel in sorted(self.read_only):
            mapping[str(self.workspace_root / rel)] = str(self.staged_path(rel))
        return mapping


def build_manifest(
    package: dict[str, Any],
    project_root: str,
    implementation_mode: bool,
) -> dict[str, Any]:
    """Turn a work-package envelope into a validated, canonical manifest."""
    workspace_raw = package.get("workspace_root") or project_root
    workspace_root = Path(os.path.normpath(str(workspace_raw))).absolute()

    # `allowed_files` is the legacy spelling and maps ONLY to writes. Silently
    # merging it with the new fields would let two envelopes disagree about what
    # the package may touch, so a genuine conflict is refused rather than guessed.
    legacy = [str(x) for x in (package.get("allowed_files") or [])]
    writes_raw = [str(x) for x in (package.get("allowed_writes") or [])]
    if legacy and writes_raw and sorted(set(legacy)) != sorted(set(writes_raw)):
        raise ManifestRejection(
            cap_id="manifest.fields.conflict",
            measured=f"allowed_files={sorted(set(legacy))} vs allowed_writes={sorted(set(writes_raw))}",
            threshold="identical sets",
            scope="envelope",
            why_selected="allowed_files is the legacy alias for allowed_writes, and the two disagree.",
            rescope_hint="Send allowed_writes only, or make the two lists identical.",
        )
    if not writes_raw:
        writes_raw = legacy

    creates_raw = [str(x) for x in (package.get("allowed_creates") or [])]
    context_raw = [str(x) for x in (package.get("read_context") or [])]

    if not implementation_mode:
        # A read package writes nothing, so whatever it declared as writable is
        # really just context. Legacy plan callers pass `allowed_files` to mean
        # "the files this question is about"; treating that as a write allowlist
        # would refuse the package over a change it never intended to make, and a
        # create target could not be staged at all because it does not yet exist.
        context_raw = list(dict.fromkeys(context_raw + writes_raw))
        writes_raw = []
        creates_raw = []

    for label, values, cap in (
        ("allowed_writes", writes_raw, MANIFEST_MAX_WRITES),
        ("allowed_creates", creates_raw, MANIFEST_MAX_CREATES),
        ("read_context", context_raw, MANIFEST_MAX_READ_CONTEXT),
    ):
        if len(values) > cap:
            raise ManifestRejection(
                cap_id=f"manifest.{label}.count",
                measured=len(values),
                threshold=cap,
                scope=label,
                why_selected=f"{label} exceeds the per-field bound.",
                rescope_hint=f"Split this into packages of at most {cap} {label} entries each.",
            )

    writes = [canonical_path(p, workspace_root) for p in writes_raw]
    creates = [canonical_path(p, workspace_root) for p in creates_raw]
    context = [canonical_path(p, workspace_root) for p in context_raw]

    # NTFS is case-insensitive: two spellings differing only by case name one
    # file, so they would collide on write-back and apply the wrong content.
    seen: dict[str, str] = {}
    for label, group in (
        ("allowed_writes", writes),
        ("allowed_creates", creates),
        ("read_context", context),
    ):
        for item in group:
            key = str(item).casefold()
            if key in seen:
                raise ManifestRejection(
                    cap_id="manifest.path.collision",
                    measured=str(item),
                    threshold="one field per path",
                    scope=f"{seen[key]} and {label}",
                    why_selected="The same path (case-insensitively) appears twice; write-back could not tell which rule applies.",
                    rescope_hint="Declare each path exactly once, in one field.",
                )
            seen[key] = label

    for item in writes:
        if not item.is_file():
            raise ManifestRejection(
                cap_id="manifest.allowed_writes.missing",
                measured=str(item),
                threshold="existing file",
                scope="allowed_writes",
                why_selected="allowed_writes names a file that does not exist.",
                rescope_hint="Use allowed_creates for a file the worker must create.",
            )
    for item in creates:
        if item.exists():
            raise ManifestRejection(
                cap_id="manifest.allowed_creates.exists",
                measured=str(item),
                threshold="absent path",
                scope="allowed_creates",
                why_selected="allowed_creates names a path that already exists.",
                rescope_hint="Move it to allowed_writes to edit the existing file.",
            )
    for item in context:
        if not item.is_file():
            raise ManifestRejection(
                cap_id="manifest.read_context.missing",
                measured=str(item),
                threshold="existing file",
                scope="read_context",
                why_selected="read_context names a file that does not exist.",
                rescope_hint="Remove it, or correct the path.",
            )

    if implementation_mode and not writes and not creates:
        raise ManifestRejection(
            cap_id="manifest.implementation.empty",
            measured=0,
            threshold="1-5 writes or creates",
            scope="envelope",
            why_selected="An implementation package declared nothing it may change.",
            rescope_hint="Declare allowed_writes (existing files) or allowed_creates (new files).",
        )

    total_files = len(writes) + len(creates) + len(context)
    if total_files > MANIFEST_MAX_FILES:
        raise ManifestRejection(
            cap_id="manifest.total.count",
            measured=total_files,
            threshold=MANIFEST_MAX_FILES,
            scope="allowed_writes + allowed_creates + read_context",
            why_selected="The manifest names more files than one bounded package should carry.",
            rescope_hint="Split the package; keep each one answerable from a handful of files.",
        )

    total_bytes = 0
    for item in writes + context:
        try:
            total_bytes += item.stat().st_size
        except OSError:
            continue
    if total_bytes > MANIFEST_MAX_BYTES:
        raise ManifestRejection(
            cap_id="manifest.total.bytes",
            measured=total_bytes,
            threshold=MANIFEST_MAX_BYTES,
            scope="staged file contents",
            why_selected="The declared files are larger than one package can carry.",
            rescope_hint="Name fewer or smaller files; a package should be answerable from a bounded excerpt.",
        )

    return {
        "workspace_root": workspace_root,
        "writes": writes,
        "creates": creates,
        "read_context": context,
        "files": total_files,
        "bytes": total_bytes,
    }


def stage(manifest: dict[str, Any], containment_mode: str = "snapshot") -> StagedPackage:
    """Copy exactly the declared files into a disposable tree."""
    workspace_root: Path = manifest["workspace_root"]
    staging = Path(tempfile.mkdtemp(prefix="flash-manifest-"))
    staged = StagedPackage(
        root=staging,
        workspace_root=workspace_root,
        containment_mode=containment_mode,
    )
    try:
        for real in manifest["writes"]:
            rel = real.relative_to(workspace_root).as_posix()
            staged.write_targets[rel] = real
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real, destination)
            digest = file_digest(destination)
            if digest is None:
                raise ManifestRejection(
                    cap_id="manifest.stage.unreadable",
                    measured=str(real),
                    threshold="readable file",
                    scope="allowed_writes",
                    why_selected="The file could not be read while staging.",
                    rescope_hint="Check the path and permissions, then resend.",
                )
            staged.baseline[rel] = digest
            staged.staged_bytes += destination.stat().st_size

        for real in manifest["creates"]:
            rel = real.relative_to(workspace_root).as_posix()
            staged.create_targets[rel] = real
            # Creates are never copied -- they must be absent. Only the parent
            # is made, so the worker can write there without a mkdir of its own.
            (staging / rel).parent.mkdir(parents=True, exist_ok=True)

        for real in manifest["read_context"]:
            rel = real.relative_to(workspace_root).as_posix()
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real, destination)
            digest = file_digest(destination)
            if digest is None:
                raise ManifestRejection(
                    cap_id="manifest.stage.unreadable",
                    measured=str(real),
                    threshold="readable file",
                    scope="read_context",
                    why_selected="The file could not be read while staging.",
                    rescope_hint="Check the path and permissions, then resend.",
                )
            staged.baseline[rel] = digest
            staged.read_only.add(rel)
            staged.staged_bytes += destination.stat().st_size
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staged


def collect_changes(staged: StagedPackage) -> dict[str, Any]:
    """Diff the ENTIRE staging tree against the copy-time baseline.

    Scanning only the declared paths would miss the interesting case: a worker
    that created or rewrote something it never mentioned. The staging tree is
    small by construction, so a full walk here is cheap and total.
    """
    present: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(staged.root):
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(staged.root).as_posix()
            if is_reparse_point(path):
                # The worker planted a link where a file belonged; write-back
                # would follow it straight out of the staging tree.
                present[rel] = "__reparse__"
                continue
            digest = file_digest(path)
            present[rel] = digest or "__unreadable__"

    declared_writes = set(staged.write_targets)
    declared_creates = set(staged.create_targets)

    modified: list[str] = []
    created: list[str] = []
    deleted: list[str] = []
    for rel, digest in present.items():
        if rel in staged.baseline:
            if digest != staged.baseline[rel]:
                modified.append(rel)
        else:
            created.append(rel)
    for rel in staged.baseline:
        if rel not in present:
            deleted.append(rel)

    undeclared = sorted(
        [r for r in modified if r not in declared_writes]
        + [r for r in created if r not in declared_creates]
    )
    return {
        "modified": sorted(modified),
        "created": sorted(created),
        "deleted": sorted(deleted),
        "undeclared": undeclared,
        "read_context_modified": sorted(r for r in modified if r in staged.read_only),
        "reparse_planted": sorted(r for r, d in present.items() if d == "__reparse__"),
        "unreadable": sorted(r for r, d in present.items() if d == "__unreadable__"),
    }


def apply_changes(staged: StagedPackage, changes: dict[str, Any]) -> dict[str, Any]:
    """Write declared changes back, or nothing at all.

    All-or-nothing on purpose. A partial application leaves the real tree in a
    state neither the worker nor the brain described, and "a failure applies
    nothing" is the only rule that stays true under every refusal below.
    """
    refusals: list[str] = []
    if changes["undeclared"]:
        refusals.append(f"undeclared changes in staging: {changes['undeclared']}")
    if changes["deleted"]:
        refusals.append(f"deletions are not supported: {changes['deleted']}")
    if changes["read_context_modified"]:
        refusals.append(f"read_context was modified: {changes['read_context_modified']}")
    if changes["reparse_planted"]:
        refusals.append(f"reparse points planted: {changes['reparse_planted']}")
    if changes["unreadable"]:
        refusals.append(f"unreadable staged files: {changes['unreadable']}")

    planned: list[tuple[str, Path, Path]] = []
    for rel in changes["modified"]:
        if rel in staged.write_targets:
            planned.append((rel, staged.staged_path(rel), staged.write_targets[rel]))
    for rel in changes["created"]:
        if rel in staged.create_targets:
            planned.append((rel, staged.staged_path(rel), staged.create_targets[rel]))

    # Re-check every precondition against the REAL tree as late as possible.
    # Between staging and now the owner's editor may have saved the same file,
    # and a create target may have appeared; both would be destroyed by a blind
    # copy, and neither is recoverable from a hash.
    conflicted: list[str] = []
    for rel, _source, target in planned:
        if rel in staged.write_targets:
            current = file_digest(target)
            if current is None or current != staged.baseline.get(rel):
                conflicted.append(str(target))
        elif target.exists():
            conflicted.append(str(target))
    if conflicted:
        refusals.append(f"real files changed underneath the package: {conflicted}")

    if refusals:
        return {
            "applied": [],
            "refused": True,
            "reasons": refusals,
            "integrity": "indeterminate" if changes["undeclared"] else "verified",
        }

    applied: list[str] = []
    failed: list[str] = []
    for _rel, source, target in planned:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".flash-apply.tmp")
            shutil.copy2(source, temp)
            atomic_io._replace_with_retry(temp, target)
            applied.append(str(target))
        except OSError as exc:
            failed.append(f"{target}: {exc}")
    return {
        "applied": sorted(applied),
        "refused": bool(failed),
        "reasons": failed,
        "integrity": "verified" if not failed else "indeterminate",
    }


def prompt_appendix(staged: StagedPackage) -> str:
    """Tell the worker where its files actually are.

    Without this the worker works from the real paths in the brain's prompt,
    edits the live tree directly, and the staged copy it was given is never
    touched -- which reads back as "the worker changed nothing".
    """
    if not staged.baseline and not staged.create_targets:
        return ""
    lines = [
        "",
        "WORKING COPY -- USE THESE PATHS AND NO OTHERS.",
        "Every file you may read or change has been copied to a disposable tree.",
        "Do not open, read or write the original locations; use the staged path.",
        "",
    ]
    for rel in sorted(staged.write_targets):
        lines.append(f"  EDIT   {staged.staged_path(rel)}")
    for rel in sorted(staged.create_targets):
        lines.append(f"  CREATE {staged.staged_path(rel)}")
    for rel in sorted(staged.read_only):
        lines.append(f"  READ   {staged.staged_path(rel)}")
    lines.append("")
    lines.append("Do not delete or rename anything. Do not create undeclared files.")
    return "\n".join(lines)


def containment_receipt(
    staged: StagedPackage | None,
    manifest: dict[str, Any] | None,
    changes: dict[str, Any] | None,
    apply_report: dict[str, Any] | None,
) -> dict[str, Any]:
    """The honest description of what was and was not verified.

    ``other_paths`` is always ``not_watched``. Nothing here observes the wider
    tree, and reporting silence as "unchanged" would be a claim this module
    cannot support.
    """
    declared = "not_applicable"
    if apply_report is not None:
        declared = apply_report.get("integrity", "indeterminate")
    elif changes is not None:
        declared = "indeterminate" if changes.get("undeclared") else "verified"
    return {
        "containment_mode": staged.containment_mode if staged else "none",
        "os_write_barrier": False,
        "isolation_note": (
            "The worker runs as the owner's Windows user with tool access enabled. "
            "Staging bounds accidents, not a determined process."
        ),
        "manifest": {
            "files": (manifest or {}).get("files", 0),
            "bytes": (manifest or {}).get("bytes", 0),
            "writes": [str(p) for p in (manifest or {}).get("writes", [])],
            "creates": [str(p) for p in (manifest or {}).get("creates", [])],
            "read_context": [str(p) for p in (manifest or {}).get("read_context", [])],
        },
        "integrity": {
            "declared_paths": declared,
            "other_paths": "not_watched",
        },
        "staging_diff": changes or {},
    }
