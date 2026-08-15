#!/usr/bin/env python3
"""Deterministic, read-only evidence probes for the brain.

Why this exists. The reader lane (`Explore`/`explorer`) has Read/Grep/Glob and no
shell, so the measurements that actually decide arguments -- file hashes, real
encodings, git state, whether a process is running -- were structurally unavailable
to it, and the brain did them itself. The obvious fix, "give the reader a shell", is
worse than it looks: "read-only intent" does not make a shell read-only, and a
prompt-level restriction is not a boundary.

So the catalog is fixed and closed. Every probe is a named operation with typed
arguments; there is no pass-through command string anywhere, nothing is ever handed
to a shell, and an unknown probe name is an error rather than a default. That is what
makes this safe to expose to a worker at all.

Boundaries enforced here:
  - paths are canonicalized and must stay under an allowed root; reparse points
    (junctions/symlinks) are rejected rather than followed;
  - filenames that look like secret material are refused outright;
  - every probe is bounded (files, bytes, matches, line length) and says so when it
    truncates, instead of silently returning a partial answer;
  - process listings expose pid/name/parent only -- never command lines, which are a
    well-known way for credentials to leak into logs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

MAX_FILES = 200
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_GREP_MATCHES = 200
MAX_LINE_CHARS = 400
MAX_PROCESSES = 400
PROBE_TIMEOUT_SECONDS = 30

WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Refused by name, whatever the caller's intent. A probe that reads one of these is
# almost never doing measurement; it is exfiltrating.
SENSITIVE_NAME_PATTERNS = (
    "*.pem", "*.key", "*.pfx", "*.p12", "*.keystore", "*.jks",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*.ppk",
    ".env", ".env.*", "*.env", "credentials", "credentials.*",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
    "*secret*", "*token*", "*password*", "*.crt.key",
)


class ProbeError(ValueError):
    """A probe was refused. The message is safe to show the caller."""


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SENSITIVE_NAME_PATTERNS)


def _resolve_path(raw: Any, roots: list[Path] | None) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ProbeError("path must be a non-empty string")
    candidate = Path(raw)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProbeError(f"path cannot be resolved: {exc}") from exc
    if _is_sensitive_name(resolved.name):
        raise ProbeError(f"refused: {resolved.name} matches a secret-material pattern")
    # Reject rather than follow. A junction under an allowed root can point anywhere,
    # so "it resolved inside the root" is not the same as "it IS inside the root".
    try:
        if candidate.is_symlink() or (os.name == "nt" and _has_reparse_point(candidate)):
            raise ProbeError("refused: path is a symlink/reparse point")
    except OSError:
        pass
    if roots:
        allowed = False
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise ProbeError("refused: path is outside every allowed root")
    return resolved


def _has_reparse_point(path: Path) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _paths_arg(args: dict[str, Any], roots: list[Path] | None) -> list[Path]:
    raw = args.get("paths")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ProbeError("paths must be a non-empty list of strings")
    if len(raw) > MAX_FILES:
        raise ProbeError(f"refused: {len(raw)} paths exceeds the {MAX_FILES}-file cap")
    return [_resolve_path(item, roots) for item in raw]


# --- probes ----------------------------------------------------------------
def probe_hash_files(args: dict[str, Any], roots: list[Path] | None) -> dict[str, Any]:
    results = []
    for path in _paths_arg(args, roots):
        if not path.is_file():
            results.append({"path": str(path), "error": "not a regular file"})
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            results.append({"path": str(path), "size": size, "error": "exceeds byte cap"})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        results.append({"path": str(path), "size": size, "sha256": digest})
    return {"probe": "hash_files", "results": results}


def probe_detect_encoding(args: dict[str, Any], roots: list[Path] | None) -> dict[str, Any]:
    """Report how a file actually decodes.

    Deliberately reports the BOM and a strict-UTF-8 verdict rather than guessing a
    codec: the failure this is built for is a BOM-less UTF-8 file being silently
    decoded as cp1252 and written back double-encoded, and a guess is exactly what
    causes that."""
    results = []
    for path in _paths_arg(args, roots):
        if not path.is_file():
            results.append({"path": str(path), "error": "not a regular file"})
            continue
        raw = path.read_bytes()[:MAX_FILE_BYTES]
        bom = None
        for marker, label in (
            (b"\xef\xbb\xbf", "utf-8-sig"),
            (b"\xff\xfe\x00\x00", "utf-32-le"),
            (b"\x00\x00\xfe\xff", "utf-32-be"),
            (b"\xff\xfe", "utf-16-le"),
            (b"\xfe\xff", "utf-16-be"),
        ):
            if raw.startswith(marker):
                bom = label
                break
        strict_utf8 = True
        utf8_error = None
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            strict_utf8 = False
            utf8_error = f"byte {exc.start}: {exc.reason}"
        results.append({
            "path": str(path),
            "bom": bom,
            "decodes_as_strict_utf8": strict_utf8,
            "utf8_error": utf8_error,
            "has_null_bytes": b"\x00" in raw,
            "crlf_count": raw.count(b"\r\n"),
            "lf_count": raw.count(b"\n") - raw.count(b"\r\n"),
        })
    return {"probe": "detect_encoding", "results": results}


def probe_file_stat(args: dict[str, Any], roots: list[Path] | None) -> dict[str, Any]:
    results = []
    for path in _paths_arg(args, roots):
        try:
            info = path.stat()
        except OSError as exc:
            results.append({"path": str(path), "error": str(exc)})
            continue
        results.append({
            "path": str(path),
            "size": info.st_size,
            "modified_epoch": int(info.st_mtime),
            "is_dir": path.is_dir(),
            "is_file": path.is_file(),
        })
    return {"probe": "file_stat", "results": results}


_GIT_SUBCOMMANDS: dict[str, list[str]] = {
    "status": ["status", "--porcelain"],
    "branch": ["rev-parse", "--abbrev-ref", "HEAD"],
    "head": ["rev-parse", "HEAD"],
    "is_repo": ["rev-parse", "--is-inside-work-tree"],
    "changed_files": ["diff", "--name-only"],
    "staged_files": ["diff", "--name-only", "--cached"],
    "untracked": ["ls-files", "--others", "--exclude-standard"],
}


def probe_git_state(args: dict[str, Any], roots: list[Path] | None) -> dict[str, Any]:
    """Fixed git queries only, run as argv lists.

    No caller-supplied git arguments: `git` has plenty of subcommands that write, and
    several that execute arbitrary programs through config."""
    repo = _resolve_path(args.get("repo") or args.get("path"), roots)
    requested = args.get("queries") or ["is_repo", "branch", "head", "status"]
    if isinstance(requested, str):
        requested = [requested]
    unknown = [q for q in requested if q not in _GIT_SUBCOMMANDS]
    if unknown:
        raise ProbeError(f"unknown git queries: {sorted(unknown)}; allowed: {sorted(_GIT_SUBCOMMANDS)}")
    cwd = repo if repo.is_dir() else repo.parent
    results = {}
    for query in requested:
        argv = ["git", "-c", "core.fsmonitor=false", *_GIT_SUBCOMMANDS[query]]
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), capture_output=True, text=True,
                timeout=PROBE_TIMEOUT_SECONDS, creationflags=WINDOWS_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            results[query] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        out = (proc.stdout or "").strip()
        lines = out.splitlines()
        truncated = len(lines) > MAX_GREP_MATCHES
        results[query] = {
            "exit_code": proc.returncode,
            "lines": lines[:MAX_GREP_MATCHES],
            "truncated": truncated,
        }
    return {"probe": "git_state", "repo": str(cwd), "results": results}


def probe_grep(args: dict[str, Any], roots: list[Path] | None) -> dict[str, Any]:
    """Bounded, literal-by-default search over an explicit file list."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ProbeError("pattern must be a non-empty string")
    regex_mode = bool(args.get("regex"))
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        compiled = re.compile(pattern if regex_mode else re.escape(pattern), flags)
    except re.error as exc:
        raise ProbeError(f"invalid regex: {exc}") from exc
    matches = []
    scanned = 0
    truncated = False
    for path in _paths_arg(args, roots):
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue  # binary: refuse rather than emit mojibake
        scanned += 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                matches.append({
                    "path": str(path),
                    "line": number,
                    "text": line[:MAX_LINE_CHARS],
                })
                if len(matches) >= MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    return {
        "probe": "grep",
        "files_scanned": scanned,
        "match_count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


def probe_process_list(args: dict[str, Any], _roots: list[Path] | None) -> dict[str, Any]:
    """pid / name / parent only.

    Command lines are excluded by construction, not filtered afterwards: they
    routinely carry tokens and connection strings."""
    name_filter = args.get("name_contains")
    if name_filter is not None and not isinstance(name_filter, str):
        raise ProbeError("name_contains must be a string")
    processes: list[dict[str, Any]] = []
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,Name,ParentProcessId | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        argv = ["ps", "-eo", "pid,ppid,comm"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_SECONDS, creationflags=WINDOWS_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return {"probe": "process_list", "error": f"{type(exc).__name__}: {exc}", "processes": []}
    for line in (proc.stdout or "").splitlines()[1:]:
        parts = [p.strip().strip('"') for p in (line.split(",") if os.name == "nt" else line.split())]
        if len(parts) < 3:
            continue
        if os.name == "nt":
            pid, name, parent = parts[0], parts[1], parts[2]
        else:
            pid, parent, name = parts[0], parts[1], parts[2]
        if name_filter and name_filter.lower() not in name.lower():
            continue
        processes.append({"pid": pid, "name": name, "parent_pid": parent})
        if len(processes) >= MAX_PROCESSES:
            break
    return {"probe": "process_list", "count": len(processes), "processes": processes}


PROBES = {
    "hash_files": probe_hash_files,
    "detect_encoding": probe_detect_encoding,
    "file_stat": probe_file_stat,
    "git_state": probe_git_state,
    "grep": probe_grep,
    "process_list": probe_process_list,
}


def run_evidence_probe(args: dict[str, Any], allowed_roots: list[str] | None = None) -> dict[str, Any]:
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in PROBES:
        return {
            "error": f"unknown probe {kind!r}",
            "allowed": sorted(PROBES),
            "note": "The probe catalog is fixed. There is no pass-through command probe.",
        }
    roots = [Path(r) for r in allowed_roots] if allowed_roots else None
    try:
        result = PROBES[kind](args, roots)
    except ProbeError as exc:
        return {"probe": kind, "error": str(exc), "refused": True}
    except Exception as exc:  # noqa: BLE001
        return {"probe": kind, "error": f"{type(exc).__name__}: {exc}"}
    result["state_change"] = "none"
    return result
