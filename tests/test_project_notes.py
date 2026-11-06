"""tests/test_project_notes.py

Tests for project_notes module. Uses synthetic tmp_path fixtures only.
No real user names, machine paths, or private project names.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from project_notes import (
    NOTE_KINDS,
    STATUSES,
    add_note,
    activity_summary,
    list_notes,
    read_cached_first_touch,
    read_cached_full,
    record_activity,
    render_brief,
    review_note,
    sanitize_text,
    validate_project_id,
    write_cache,
)


@pytest.fixture(autouse=True)
def _isolate_broker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure all tests run strictly against tmp_path and never touch ~/.agent-broker."""
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))


def test_id_validation():
    # Valid project IDs
    valid_ids = ["alpha", "proj-1", "my_project.v2", "a", "123", "a" + "b" * 63]
    for pid in valid_ids:
        validate_project_id(pid)

    # Invalid project IDs
    invalid_ids = [
        "",
        "_invalid",
        "-invalid",
        ".invalid",
        "ProjectWithCapitals",
        "has spaces",
        "a" * 65,
        "proj/slash",
        "proj\\backslash",
        "invalid@char",
        "invalid#id",
    ]
    for pid in invalid_ids:
        with pytest.raises(ValueError):
            validate_project_id(pid)

        with pytest.raises(ValueError):
            add_note(pid, "failed", "test note")

        with pytest.raises(ValueError):
            review_note(pid, "n_1234567890ab", "verified")

        with pytest.raises(ValueError):
            list_notes(pid)

        with pytest.raises(ValueError):
            record_activity(pid, "host1", "touch")

        with pytest.raises(ValueError):
            activity_summary(pid)

        with pytest.raises(ValueError):
            render_brief(pid)

        with pytest.raises(ValueError):
            write_cache(pid)

        # Fail-open cached reads return None without raising
        assert read_cached_first_touch(pid) is None
        assert read_cached_full(pid) is None


def test_caps_raise_value_error():
    pid = "proj-caps"

    # text cap: 1..400
    with pytest.raises(ValueError):
        add_note(pid, "failed", "")
    with pytest.raises(ValueError):
        add_note(pid, "failed", "x" * 401)
    # exactly 400 is allowed
    n_ok = add_note(pid, "failed", "x" * 400)
    assert len(n_ok["text"]) == 400

    # applies_when cap: <= 200
    with pytest.raises(ValueError):
        add_note(pid, "failed", "valid text", applies_when="a" * 201)
    add_note(pid, "failed", "valid text", applies_when="a" * 200)

    # evidence cap: <= 200
    with pytest.raises(ValueError):
        add_note(pid, "failed", "valid text", evidence="e" * 201)
    add_note(pid, "failed", "valid text", evidence="e" * 200)

    # author cap: <= 60
    with pytest.raises(ValueError):
        add_note(pid, "failed", "valid text", author="u" * 61)
    add_note(pid, "failed", "valid text", author="u" * 60)

    # host in record_activity: <= 20
    with pytest.raises(ValueError):
        record_activity(pid, "h" * 21, "touch")
    record_activity(pid, "h" * 20, "touch")


def test_sanitize_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Setup fixture redact_terms.json
    cg_dir = tmp_path / "code-graph"
    cg_dir.mkdir(parents=True, exist_ok=True)
    redact_file = cg_dir / "redact_terms.json"
    redact_file.write_text(json.dumps(["secretcorp", "project-x"]), encoding="utf-8")

    # 1. Windows absolute paths
    assert sanitize_text(r"Check C:\Users\tester\app.py now") == "Check <path> now"
    assert sanitize_text(r"See D:\data\files\test.csv.") == "See <path>."
    assert sanitize_text("Path is X:/foo/bar/baz.txt;") == "Path is <path>;"
    assert sanitize_text(r"UNC \\server\share\file.txt here") == "UNC <path> here"

    # 2. POSIX home paths
    assert sanitize_text("Inspect /home/alice/main.py today") == "Inspect <path> today"
    assert sanitize_text("Check /Users/bob/repo/code.") == "Check <path>."
    assert sanitize_text("Config at /root/.bashrc") == "Config at <path>"

    # 3. Redact terms from fixture (case-insensitive and inside relative paths)
    assert sanitize_text("Talking about secretcorp today") == "Talking about <masked> today"
    assert sanitize_text("Talking about SecretCorp today") == "Talking about <masked> today"
    assert sanitize_text("Talking about PROJECT-X today") == "Talking about <masked> today"
    assert sanitize_text("File is src/secretcorp/module.py") == "File is src/<masked>/module.py"

    # 4. AKIA key
    assert sanitize_text('AWS key ' + ('AKIA' + "0" * 16) + ' used') == "AWS key [redacted] used"
    assert sanitize_text('Key ' + ('ASIA' + "0" * 16) + ' active') == "Key [redacted] active"

    # 5. Keeps relative paths
    rel = "Check relative path src/utils/helpers.py and ./tests/conftest.py"
    assert sanitize_text(rel) == rel
    assert sanitize_text("../parent/dir/file.txt") == "../parent/dir/file.txt"

    # 6. Keeps normal URLs
    url = "Docs at https://example.com/docs/api and http://localhost:8080/health"
    assert sanitize_text(url) == url
    url_with_home = "Site at https://example.com/home/user/page"
    assert sanitize_text(url_with_home) == url_with_home

    # 7. Other secrets
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEfake\n-----END RSA PRIVATE KEY-----"
    assert sanitize_text(f"Key:\n{pem}") == "Key:\n[redacted]"

    jwt = 'Token ' + ".".join(("eyJ" + "a" * 20, "a" * 20, "a" * 20))
    assert sanitize_text(jwt) == "Token [redacted]"

    bearer = "Authorization: Bearer mySecretToken123456"
    assert sanitize_text(bearer) == "Authorization: Bearer [redacted]"

    pw = "credentials password=superSecretPass123; api_key='my-api-key-999'"
    assert sanitize_text(pw) == "credentials password=[redacted]; api_key=[redacted]"

    # 8. High entropy whitespace-free run (>=32 chars, 3+ classes, entropy >= 4.0, no ://)
    high_entropy = "Token aB3dE5gH7iJ9kL1mN3oP5qR7sT9uV1wX was leaked"
    assert sanitize_text(high_entropy) == "Token [redacted] was leaked"


def test_add_review_list_folding_and_supersedes():
    pid = "proj-fold"

    # 1. Add note 1
    n1 = add_note(pid, "failed", "First failed attempt", author="alice", now="2026-10-07T10:00:00Z")
    assert n1["status"] == "pending"
    assert n1["id"].startswith("n_")
    assert len(n1["id"]) == 14

    notes = list_notes(pid)
    assert len(notes) == 1
    assert notes[0]["status"] == "pending"

    # 2. Review note 1 -> verified
    r1 = review_note(
        pid,
        n1["id"],
        "verified",
        reviewer="owner",
        reason="verified on local dev",
        now="2026-10-07T10:05:00Z",
    )
    assert r1["status"] == "verified"

    notes = list_notes(pid)
    assert len(notes) == 1
    assert notes[0]["status"] == "verified"

    # 3. Add note 2 which supersedes note 1
    n2 = add_note(
        pid,
        "failed",
        "Refined failure observation",
        supersedes=n1["id"],
        author="alice",
        now="2026-10-07T10:10:00Z",
    )
    assert n2["status"] == "pending"

    # Both notes listed: n1 status folded to 'superseded', n2 is 'pending'
    notes = list_notes(pid)
    assert len(notes) == 2
    n1_curr = next(n for n in notes if n["id"] == n1["id"])
    n2_curr = next(n for n in notes if n["id"] == n2["id"])
    assert n1_curr["status"] == "superseded"
    assert n2_curr["status"] == "pending"

    # Filter by statuses
    assert len(list_notes(pid, statuses=["pending"])) == 1
    assert len(list_notes(pid, statuses=["superseded"])) == 1
    assert len(list_notes(pid, statuses=["verified"])) == 0

    # Reviewing unknown note raises ValueError
    with pytest.raises(ValueError):
        review_note(pid, "n_000000000000", "verified")


def test_pending_disputed_superseded_never_in_first_touch():
    pid = "proj-ft-filter"

    # Add notes of failed/pitfall kind with non-verified statuses
    add_note(pid, "failed", "Pending lesson", status="pending")
    add_note(pid, "pitfall", "Disputed lesson", status="disputed")
    add_note(pid, "failed", "Superseded lesson", status="superseded")
    add_note(pid, "pitfall", "Expired lesson", status="expired")

    brief = render_brief(pid)
    ft = brief["first_touch"]

    assert "Avoid:" not in ft
    assert "Pending lesson" not in ft
    assert "Disputed lesson" not in ft
    assert "Superseded lesson" not in ft
    assert "Expired lesson" not in ft


def test_verified_owner_failed_note_ranks_first():
    pid = "proj-rank"

    # 4 verified notes:
    # 1. Owner pitfall
    add_note(
        pid,
        "pitfall",
        "Owner pitfall note",
        author="owner",
        status="verified",
        now="2026-10-07T10:00:00Z",
    )
    # 2. Non-owner failed
    add_note(
        pid,
        "failed",
        "Coder failed note",
        author="coder",
        status="verified",
        now="2026-10-07T11:00:00Z",
    )
    # 3. Non-owner pitfall
    add_note(
        pid,
        "pitfall",
        "Coder pitfall note",
        author="coder",
        status="verified",
        now="2026-10-07T12:00:00Z",
    )
    # 4. Owner failed (created earlier)
    add_note(
        pid,
        "failed",
        "Owner failed note",
        author="owner",
        status="verified",
        now="2026-10-07T09:00:00Z",
    )

    brief = render_brief(pid)
    ft = brief["first_touch"]

    # Ranked by: author == 'owner' first, then kind failed before pitfall, then newest first
    # Tier 1: Owner failed -> 1)
    # Tier 2: Owner pitfall -> 2)
    # Tier 3: Coder failed -> 3)
    # Tier 4: Coder pitfall -> 4th item (dropped because max 3)
    assert "Avoid: 1) Owner failed note 2) Owner pitfall note 3) Coder failed note" in ft
    assert "Coder pitfall note" not in ft


def test_fourth_lesson_not_included():
    pid = "proj-max3"

    for i in range(1, 6):
        add_note(
            pid,
            "failed",
            f"Lesson number {i}",
            status="verified",
            now=f"2026-10-07T10:0{i}:00Z",
        )

    brief = render_brief(pid)
    ft = brief["first_touch"]

    assert "1)" in ft
    assert "2)" in ft
    assert "3)" in ft
    assert "4)" not in ft
    assert "5)" not in ft


def test_long_lesson_exceeding_700_omitted_whole_and_shorter_kept():
    pid = "proj-fit"

    # Long lesson (newer, evaluated first) has 390 chars
    long_text = "L" * 390
    add_note(
        pid,
        "failed",
        long_text,
        status="verified",
        now="2026-10-07T10:02:00Z",
    )
    # Shorter lesson (older, evaluated later) has 30 chars
    short_text = "Short lesson that fits"
    add_note(
        pid,
        "failed",
        short_text,
        status="verified",
        now="2026-10-07T10:01:00Z",
    )

    # Use entry with long start_here so base is ~350 chars
    # Base (~350) + Long (390) > 700 (omitted whole)
    # Base (~350) + Short (30) <= 700 (fits)
    entry = {"start_here": "S" * 200, "group": "testgroup"}
    brief = render_brief(pid, entry=entry)
    ft = brief["first_touch"]

    assert len(ft) <= 700
    assert long_text not in ft
    assert short_text in ft
    assert f"Avoid: 1) {short_text}" in ft


def test_first_touch_and_full_caps_with_many_notes():
    pid = "proj-many"

    # Add 50 notes of various kinds and statuses
    for i in range(50):
        kind = NOTE_KINDS[i % len(NOTE_KINDS)]
        status = STATUSES[i % len(STATUSES)]
        author = "owner" if i % 3 == 0 else "dev"
        add_note(
            pid,
            kind,
            f"Note {i}: sample text for testing brief length capping " + "x" * 200,
            applies_when=f"condition {i} " + "y" * 100,
            evidence=f"evidence {i} " + "z" * 100,
            author=author,
            status=status,
            now=f"2026-10-07T10:{i:02d}:00Z",
        )

    for i in range(30):
        host = f"host-{i % 5}"
        kind = ("touch", "edit", "note")[i % 3]
        record_activity(pid, host, kind, count=i + 1)

    brief = render_brief(pid)
    assert len(brief["first_touch"]) <= 700
    assert len(brief["full"]) <= 4000


def test_full_contains_section_headers_and_pending_list():
    pid = "proj-sections"

    entry = {
        "aliases": ["alias-1", "alias-2"],
        "group": "group-alpha",
        "related": ["proj-beta"],
        "start_here": "README.md",
    }

    add_note(pid, "decision", "Decided on SQLite storage", status="verified")
    add_note(pid, "failed", "Do not run unindexed queries", applies_when="prod load", evidence="timeout trace", status="verified")
    add_note(pid, "worked", "Use atomic writes for caching", status="verified")
    add_note(pid, "pitfall", "Watch for file locks", status="pending")

    record_activity(pid, "codex", "touch", count=3, now="2026-10-07T14:00:00Z")
    record_activity(pid, "codex", "edit", count=12, now="2026-10-07T14:00:00Z")
    record_activity(pid, "codex", "note", count=1, now="2026-10-07T14:00:00Z")

    brief = render_brief(pid, entry=entry)
    full = brief["full"]

    # Verify all section headers
    assert f"# Project {pid}" in full
    assert "## Identity" in full
    assert "- ID: proj-sections" in full
    assert "alias-1, alias-2" in full
    assert "group-alpha" in full
    assert "proj-beta" in full
    assert "Filing rule: File notes and memory under project=proj-sections." in full

    assert "## Start here" in full
    assert "README.md" in full

    assert "## Owner decisions" in full
    assert "Decided on SQLite storage" in full

    assert "## Do not repeat" in full
    assert "Do not run unindexed queries" in full
    assert "when prod load" in full
    assert "evidence: timeout trace" in full

    assert "## What worked" in full
    assert "Use atomic writes for caching" in full

    assert "## Recent activity" in full
    assert "codex: 3 touches, 12 edits, 1 notes, last 2026-10-07 14:00 UTC" in full

    assert "## Pending review" in full
    assert "Pending count: 1" in full
    assert "pitfall: Watch for file locks (unverified)" in full


def test_activity_content_free_and_summarised_per_host(tmp_path: Path):
    pid = "proj-act"

    record_activity(pid, "codex", "touch", count=2, now="2026-10-07T12:00:00Z")
    record_activity(pid, "codex", "edit", count=5, now="2026-10-07T12:05:00Z")
    record_activity(pid, "codex", "note", count=1, now="2026-10-07T12:10:00Z")
    record_activity(pid, "claude", "edit", count=3, now="2026-10-07T13:00:00Z")

    # Inspect raw file: only {'ts','host','kind','count'}
    act_file = tmp_path / "projects" / pid / "activity.jsonl"
    assert act_file.is_file()
    for line in act_file.read_text(encoding="utf-8").strip().splitlines():
        rec = json.loads(line)
        assert set(rec.keys()) == {"ts", "host", "kind", "count"}
        assert "session" not in rec
        assert "path" not in rec
        assert "text" not in rec

    summary = activity_summary(pid)
    assert len(summary) == 2

    # Hosts ordered alphabetically: claude, codex
    claude_sum = summary[0]
    assert claude_sum["host"] == "claude"
    assert claude_sum["touches"] == 0
    assert claude_sum["edits"] == 3
    assert claude_sum["notes"] == 0
    assert claude_sum["last_ts"] == "2026-10-07T13:00:00Z"

    codex_sum = summary[1]
    assert codex_sum["host"] == "codex"
    assert codex_sum["touches"] == 2
    assert codex_sum["edits"] == 5
    assert codex_sum["notes"] == 1
    assert codex_sum["last_ts"] == "2026-10-07T12:10:00Z"


def test_write_cache_and_read_cached_first_touch_round_trip():
    pid = "proj-cache-rt"

    add_note(pid, "failed", "A verified lesson", status="verified", now="2026-10-07T14:00:00Z")
    entry = {"start_here": "docs/start.md"}

    cache_data = write_cache(pid, entry=entry, now="2026-10-07T14:30:00Z")
    assert "first_touch" in cache_data
    assert "full" in cache_data
    assert cache_data["built_at"] == "2026-10-07T14:30:00Z"

    cached_ft = read_cached_first_touch(pid)
    assert cached_ft == cache_data["first_touch"]

    cached_full = read_cached_full(pid)
    assert cached_full == cache_data["full"]


def test_read_cached_first_touch_missing_and_corrupt(tmp_path: Path):
    # 1. Non-existent cache
    assert read_cached_first_touch("non-existent-proj") is None
    assert read_cached_full("non-existent-proj") is None

    # 2. Corrupt JSON in brief.json
    corrupt_pid = "proj-corrupt"
    pdir = tmp_path / "projects" / corrupt_pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "brief.json").write_text("{corrupt json syntax", encoding="utf-8")

    assert read_cached_first_touch(corrupt_pid) is None
    assert read_cached_full(corrupt_pid) is None

    # 3. Missing keys in valid JSON
    missing_pid = "proj-missing-keys"
    pdir2 = tmp_path / "projects" / missing_pid
    pdir2.mkdir(parents=True, exist_ok=True)
    (pdir2 / "brief.json").write_text(json.dumps({"some_other_field": True}), encoding="utf-8")

    assert read_cached_first_touch(missing_pid) is None
    assert read_cached_full(missing_pid) is None

    # 4. Invalid project ID
    assert read_cached_first_touch("INVALID!ID") is None
    assert read_cached_full("INVALID!ID") is None


def test_malformed_jsonl_lines_skipped(tmp_path: Path):
    pid = "proj-malformed"
    pdir = tmp_path / "projects" / pid
    pdir.mkdir(parents=True, exist_ok=True)

    # Corrupt notes.jsonl
    notes_file = pdir / "notes.jsonl"
    lines = [
        "{not valid json",
        "",
        json.dumps(
            {
                "type": "note",
                "id": "n_111111111111",
                "project": pid,
                "kind": "worked",
                "text": "Valid note 1",
                "status": "verified",
            }
        ),
        "12345",
        json.dumps({"unknown_key": "ignored"}),
        json.dumps({"type": "note"}),  # missing id
        json.dumps(
            {
                "type": "status",
                "id": "n_111111111111",
                "status": "superseded",
            }
        ),
        json.dumps(
            {
                "type": "note",
                "id": "n_222222222222",
                "project": pid,
                "kind": "failed",
                "text": "Valid note 2",
                "status": "verified",
            }
        ),
    ]
    notes_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    notes = list_notes(pid)
    assert len(notes) == 2
    assert notes[0]["id"] == "n_111111111111"
    assert notes[0]["status"] == "superseded"
    assert notes[1]["id"] == "n_222222222222"
    assert notes[1]["status"] == "verified"

    # Corrupt activity.jsonl
    act_file = pdir / "activity.jsonl"
    act_lines = [
        "not json",
        json.dumps({"host": "host-ok", "kind": "touch", "count": 2, "ts": "2026-10-07T12:00:00Z"}),
        json.dumps({"invalid_record": True}),
        json.dumps({"host": "host-ok", "kind": "invalid_kind", "count": 1}),
        json.dumps({"host": "host-ok", "kind": "edit", "count": "not-int", "ts": "2026-10-07T12:05:00Z"}),
    ]
    act_file.write_text("\n".join(act_lines) + "\n", encoding="utf-8")

    summary = activity_summary(pid)
    assert len(summary) == 1
    assert summary[0]["host"] == "host-ok"
    assert summary[0]["touches"] == 2
    assert summary[0]["edits"] == 1  # defaulted from invalid count to 1


def test_relative_file_paths_are_not_redacted_but_secrets_are(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    import project_notes as _pn
    long_path = "dev_docs/SOME_PLATFORM_HANDOFF_2026-10-04.md"
    long_name = "AGENT_ACCESS_HANDOFF_2026-07-15_FINAL.md"
    out = _pn.sanitize_text(f"Start: {long_path} and {long_name}; key aB3$kL9#mN2@pQ7!rS5%tU8^vW1&xY4*zZ0")
    assert long_path in out and long_name in out
    assert "[redacted]" in out
