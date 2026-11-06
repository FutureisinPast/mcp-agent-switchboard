"""tests/test_routing_gate_project_brief.py

Tests for WP-PB-I4 project brief and lesson integration in routing_gate.py.
Uses synthetic tmp_path fixtures only: no real user names, machine paths,
or private project names.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

import project_notes
import project_resolver
import routing_gate


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure broker state and project registry are completely isolated to tmp_path."""
    broker_home = tmp_path / "broker-home"
    state_dir = broker_home / "routing-gate"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_BROKER_HOME", str(broker_home))
    monkeypatch.setattr(routing_gate, "BROKER_HOME", broker_home)
    monkeypatch.setattr(routing_gate, "STATE_DIR", state_dir)
    monkeypatch.setattr(routing_gate, "DB_PATH", broker_home / "state.sqlite")
    monkeypatch.setattr(routing_gate, "EVIDENCE_DIR", broker_home / "context-evidence")
    monkeypatch.delenv("AGENT_BROKER_CHILD", raising=False)
    monkeypatch.delenv("AGENT_BROKER_PROJECT_CONTEXT", raising=False)
    monkeypatch.delenv("AGENT_BROKER_PROJECT_NOTE_REMINDER", raising=False)
    project_resolver.clear_cache()
    yield
    project_resolver.clear_cache()


@pytest.fixture
def synthetic_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    root_a = tmp_path / "synthetic-repos" / "synth-alpha"
    out_a = tmp_path / "synthetic-out" / "synth-alpha"
    root_b = tmp_path / "synthetic-repos" / "synth-beta"
    out_b = tmp_path / "synthetic-out" / "synth-beta"

    for d in (root_a, out_a, root_b, out_b):
        d.mkdir(parents=True, exist_ok=True)

    data = {
        "synth-alpha": {
            "root": str(root_a),
            "out_dir": str(out_a),
            "aliases": ["alpha-alias", "alpha-svc"],
            "group": "synth-group",
            "related": ["synth-beta"],
            "label": "Synthetic Alpha Service",
        },
        "synth-beta": {
            "root": str(root_b),
            "out_dir": str(out_b),
            "aliases": ["beta-alias", "beta-svc"],
            "group": "synth-group",
            "related": ["synth-alpha"],
            "label": "Synthetic Beta Service",
        },
    }
    reg_file = tmp_path / "code-graph" / "projects.json"
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(reg_file))
    project_resolver.clear_cache()
    return data


def test_first_touch_with_cached_brief_contains_verified_lesson(synthetic_registry: dict, tmp_path: Path):
    """With a cached brief, first-touch contains the verified lesson and stays <= 700 chars."""
    note = project_notes.add_note(
        "synth-alpha",
        "pitfall",
        "Never bypass token verifier in auth middleware.",
        author="owner",
    )
    project_notes.review_note("synth-alpha", note["id"], "verified")
    reg = project_resolver.load_registry()
    project_notes.write_cache("synth-alpha", entry=reg["synth-alpha"])

    pointer = routing_gate._project_context_pointer(
        "sess-cached-brief-p",
        "please check alpha-alias for issues",
        str(tmp_path),
        host_label="claude",
    )
    assert "Never bypass token verifier in auth middleware." in pointer
    assert len(pointer) <= 700

    payload = {
        "session_id": "sess-cached-brief",
        "prompt": "please check alpha-alias for issues",
        "cwd": str(tmp_path),
        "_switchboard_host": "claude",
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Never bypass token verifier in auth middleware." in ctx


def test_first_touch_without_cached_brief_uses_existing_format_pointer(synthetic_registry: dict, tmp_path: Path):
    """Without a cached brief, the standard format_pointer pointer line appears."""
    pointer = routing_gate._project_context_pointer(
        "sess-nocache-brief-p",
        "please inspect beta-alias",
        str(tmp_path),
        host_label="claude",
    )
    assert "Switchboard project context: synth-beta" in pointer
    assert "Map: code_graph project=synth-beta" in pointer
    assert len(pointer) <= 700

    payload = {
        "session_id": "sess-nocache-brief",
        "prompt": "please inspect beta-alias",
        "cwd": str(tmp_path),
        "_switchboard_host": "claude",
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Switchboard project context: synth-beta" in ctx
    assert "Map: code_graph project=synth-beta" in ctx


def test_second_matched_id_appended_when_it_fits(synthetic_registry: dict, tmp_path: Path):
    """When multiple projects match, additional IDs are appended if total <= 700 chars."""
    pointer = routing_gate._project_context_pointer(
        "sess-multi-match-p",
        "please check alpha-alias and beta-alias together",
        str(tmp_path),
        host_label="claude",
    )
    assert " Also matched: synth-beta (project_brief op=get project=<id>)." in pointer
    assert len(pointer) <= 700

    payload = {
        "session_id": "sess-multi-match",
        "prompt": "please check alpha-alias and beta-alias together",
        "cwd": str(tmp_path),
        "_switchboard_host": "claude",
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert " Also matched: synth-beta (project_brief op=get project=<id>)." in ctx


def test_touch_activity_recorded_for_all_matched_ids(synthetic_registry: dict, tmp_path: Path):
    """Touch activity is recorded per matched ID with the host label."""
    payload = {
        "session_id": "sess-touch-act",
        "prompt": "please check alpha-alias and beta-alias",
        "cwd": str(tmp_path),
        "_switchboard_host": "claude",
    }
    routing_gate.user_prompt_submit(payload)
    for pid in ("synth-alpha", "synth-beta"):
        acts = project_notes.activity_summary(pid)
        claude_acts = [h for h in acts if h["host"] == "claude"]
        assert len(claude_acts) >= 1
        assert claude_acts[0]["touches"] >= 1


def test_edit_tool_increments_project_edits_and_records_activity(synthetic_registry: dict, tmp_path: Path):
    """Edit tool with file_path inside a root increments project_edits and records edit activity."""
    root_a = Path(synthetic_registry["synth-alpha"]["root"])
    target_file = root_a / "sub" / "module.py"

    payload = {
        "session_id": "sess-edit-track",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target_file)},
        "_switchboard_host": "codex",
    }
    routing_gate.post_tool_use(payload)

    state = routing_gate._read_state("sess-edit-track")
    assert state.get("project_edits", {}).get("synth-alpha") == 1

    acts = project_notes.activity_summary("synth-alpha")
    codex_acts = [h for h in acts if h["host"] == "codex"]
    assert len(codex_acts) >= 1
    assert codex_acts[0]["edits"] >= 1


def test_apply_patch_attributed_via_cwd(synthetic_registry: dict):
    """apply_patch text with '*** Update File: rel/x.py' plus payload cwd inside a root is attributed."""
    root_a = Path(synthetic_registry["synth-alpha"]["root"])
    patch_text = "*** Update File: rel/x.py\n@@ -1,1 +1,2 @@\n+line"

    payload = {
        "session_id": "sess-patch-track",
        "tool_name": "apply_patch",
        "tool_input": {"patch": patch_text},
        "cwd": str(root_a),
        "_switchboard_host": "claude",
    }
    routing_gate.post_tool_use(payload)

    state = routing_gate._read_state("sess-patch-track")
    assert state.get("project_edits", {}).get("synth-alpha") == 1

    acts = project_notes.activity_summary("synth-alpha")
    claude_acts = [h for h in acts if h["host"] == "claude"]
    assert len(claude_acts) >= 1
    assert claude_acts[0]["edits"] >= 1


def test_path_outside_every_root_is_ignored(synthetic_registry: dict, tmp_path: Path):
    """A file path outside every project root does not increment project_edits."""
    outside_file = tmp_path / "outside-dir" / "random.py"
    payload = {
        "session_id": "sess-outside-track",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(outside_file)},
        "_switchboard_host": "claude",
    }
    routing_gate.post_tool_use(payload)

    state = routing_gate._read_state("sess-outside-track")
    assert not state.get("project_edits")


def test_stop_blocks_once_with_reminder_then_second_allows(synthetic_registry: dict):
    """Stop blocks once with the reminder after edits and no note; second Stop does not block."""
    root_a = Path(synthetic_registry["synth-alpha"]["root"])
    routing_gate.post_tool_use({
        "session_id": "sess-stop-reminder",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root_a / "file.py")},
        "_switchboard_host": "claude",
    })

    stop_res1 = routing_gate.stop({"session_id": "sess-stop-reminder", "_switchboard_host": "claude"})
    assert stop_res1.get("decision") == "block"
    reason = stop_res1.get("reason", "")
    assert "Before finishing: you changed files in synth-alpha this session." in reason
    assert "project_brief op=add project=<id> kind=worked|failed|decision|pitfall" in reason

    # Second Stop for same session does not block
    stop_res2 = routing_gate.stop({"session_id": "sess-stop-reminder", "_switchboard_host": "claude"})
    assert stop_res2 == {}


def test_project_brief_or_work_memory_prevents_reminder(synthetic_registry: dict):
    """Calling project_brief op=add or record_work_memory before Stop prevents reminder block."""
    root_a = Path(synthetic_registry["synth-alpha"]["root"])

    # Session 1: project_brief op=add
    routing_gate.post_tool_use({
        "session_id": "sess-brief-noted",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root_a / "file.py")},
        "_switchboard_host": "claude",
    })
    routing_gate.post_tool_use({
        "session_id": "sess-brief-noted",
        "tool_name": "project_brief",
        "tool_input": {"op": "add", "project": "synth-alpha", "kind": "worked", "text": "Tested fix."},
        "_switchboard_host": "claude",
    })
    assert routing_gate.stop({"session_id": "sess-brief-noted", "_switchboard_host": "claude"}) == {}

    # Session 2: record_work_memory
    routing_gate.post_tool_use({
        "session_id": "sess-wm-noted",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root_a / "file.py")},
        "_switchboard_host": "codex",
    })
    routing_gate.post_tool_use({
        "session_id": "sess-wm-noted",
        "tool_name": "record_work_memory",
        "tool_input": {"project": "synth-alpha", "summary": "Tested change."},
        "_switchboard_host": "codex",
    })
    assert routing_gate.stop({"session_id": "sess-wm-noted", "_switchboard_host": "codex"}) == {}


def test_env_disables_project_note_reminder(synthetic_registry: dict, monkeypatch: pytest.MonkeyPatch):
    """AGENT_BROKER_PROJECT_NOTE_REMINDER=0 disables reminder check at Stop."""
    monkeypatch.setenv("AGENT_BROKER_PROJECT_NOTE_REMINDER", "0")
    root_a = Path(synthetic_registry["synth-alpha"]["root"])
    routing_gate.post_tool_use({
        "session_id": "sess-env-disabled",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root_a / "file.py")},
        "_switchboard_host": "claude",
    })
    assert routing_gate.stop({"session_id": "sess-env-disabled", "_switchboard_host": "claude"}) == {}


def test_stop_hook_active_never_blocks(synthetic_registry: dict):
    """stop_hook_active true never blocks."""
    root_a = Path(synthetic_registry["synth-alpha"]["root"])
    routing_gate.post_tool_use({
        "session_id": "sess-hook-active",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root_a / "file.py")},
        "_switchboard_host": "claude",
    })
    res = routing_gate.stop({
        "session_id": "sess-hook-active",
        "stop_hook_active": True,
        "_switchboard_host": "claude",
    })
    assert res == {}


def test_child_session_never_blocks_and_child_calling_project_brief_denied(synthetic_registry: dict, monkeypatch: pytest.MonkeyPatch):
    """Switchboard children never block at Stop and are denied from calling project_brief."""
    monkeypatch.setenv("AGENT_BROKER_CHILD", "1")
    root_a = Path(synthetic_registry["synth-alpha"]["root"])

    # Child never blocks at Stop
    routing_gate.post_tool_use({
        "session_id": "sess-child-gate",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root_a / "file.py")},
        "_switchboard_host": "claude",
    })
    assert routing_gate.stop({"session_id": "sess-child-gate", "_switchboard_host": "claude"}) == {}

    # Child calling project_brief is denied in PreToolUse
    for tool in ("project_brief", "mcp__agent_switchboard__project_brief", "mcp__agent-switchboard__project_brief"):
        res = routing_gate.pre_tool_use({
            "session_id": "sess-child-gate",
            "tool_name": tool,
            "tool_input": {"op": "get", "project": "synth-alpha"},
            "_switchboard_host": "claude",
        })
        assert res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_reset_turn_state_preserves_valid_and_drops_invalid():
    """reset_turn_state preserves valid project dicts and drops invalid shapes."""
    # Valid shapes preserved
    sess_valid = "sess-validate-valid"
    def init_valid(st: dict) -> None:
        st["project_edits"] = {"synth-alpha": 5, "synth-beta": 0}
        st["project_noted"] = {"synth-alpha": 2}
        st["project_reminded"] = {"synth-alpha": True}
        st["mutated"] = True

    routing_gate._update_state(sess_valid, init_valid)
    routing_gate.reset_turn_state(sess_valid)
    st = routing_gate._read_state(sess_valid)
    assert st.get("project_edits") == {"synth-alpha": 5, "synth-beta": 0}
    assert st.get("project_noted") == {"synth-alpha": 2}
    assert st.get("project_reminded") == {"synth-alpha": True}
    assert "mutated" not in st

    # Invalid shapes dropped
    sess_invalid = "sess-validate-invalid"
    def init_invalid(st: dict) -> None:
        st["project_edits"] = {"invalid project!": 1}
        st["project_noted"] = {"synth-alpha": -1}
        st["project_reminded"] = {"synth-alpha": False}

    routing_gate._update_state(sess_invalid, init_invalid)
    routing_gate.reset_turn_state(sess_invalid)
    st_inv = routing_gate._read_state(sess_invalid)
    assert "project_edits" not in st_inv
    assert "project_noted" not in st_inv
    assert "project_reminded" not in st_inv


def test_no_prompt_text_or_file_paths_appear_in_state_or_activity(synthetic_registry: dict, tmp_path: Path):
    """No prompt text, tool output, or file paths appear in state or activity files."""
    canary_prompt = "CANARY_PROMPT_SECRET_STRING_999"
    canary_path_part = "canary_special_subdir_secret"
    root_a = Path(synthetic_registry["synth-alpha"]["root"])
    canary_file = root_a / canary_path_part / "file.py"
    canary_file.parent.mkdir(parents=True, exist_ok=True)
    canary_file.write_text("print('hello')", encoding="utf-8")

    sess = "sess-leak-check"
    routing_gate.user_prompt_submit({
        "session_id": sess,
        "prompt": f"check alpha-alias with {canary_prompt}",
        "cwd": str(tmp_path),
        "_switchboard_host": "claude",
    })
    routing_gate.post_tool_use({
        "session_id": sess,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(canary_file)},
        "_switchboard_host": "claude",
    })

    # Check state file content
    state_file = routing_gate._session_path(sess)
    state_content = state_file.read_text(encoding="utf-8")
    assert canary_prompt not in state_content
    assert canary_path_part not in state_content
    assert str(canary_file) not in state_content

    # Check activity.jsonl
    act_file = tmp_path / "broker-home" / "projects" / "synth-alpha" / "activity.jsonl"
    assert act_file.is_file()
    act_content = act_file.read_text(encoding="utf-8")
    assert canary_prompt not in act_content
    assert canary_path_part not in act_content
    assert str(canary_file) not in act_content
