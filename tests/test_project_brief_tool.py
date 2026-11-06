"""tests/test_project_brief_tool.py

Tests for WP-PB-I3:
- Canonical project resolution across MCP tools.
- record_work_memory outcome/note recording and project_hint.
- project_brief MCP tool (get, add, review, pending).
- get_context_pack enhancements for registered projects.
- No absolute path leakage.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

import agent_broker_mcp
import project_notes
import project_resolver
import routing_gate


@pytest.fixture(autouse=True)
def clean_resolver_cache():
    project_resolver.clear_cache()
    yield
    project_resolver.clear_cache()


@pytest.fixture
def synthetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    broker_home = tmp_path / "broker_home"
    broker_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_BROKER_HOME", str(broker_home))
    monkeypatch.setattr(agent_broker_mcp, "BROKER_DIR", broker_home)

    db_file = tmp_path / "test_state.sqlite"
    monkeypatch.setattr(agent_broker_mcp, "DB_PATH", db_file)
    monkeypatch.setattr(routing_gate, "DB_PATH", db_file)
    agent_broker_mcp.init_db()

    root_alpha = tmp_path / "roots" / "synth_alpha"
    root_alpha.mkdir(parents=True, exist_ok=True)
    out_alpha = tmp_path / "out" / "synth_alpha"
    out_alpha.mkdir(parents=True, exist_ok=True)

    root_beta = tmp_path / "roots" / "synth_beta"
    root_beta.mkdir(parents=True, exist_ok=True)
    out_beta = tmp_path / "out" / "synth_beta"
    out_beta.mkdir(parents=True, exist_ok=True)

    reg_data = {
        "synth-alpha": {
            "root": str(root_alpha),
            "out_dir": str(out_alpha),
            "group": "synth-group",
            "aliases": ["alpha widget", "alpha alias"],
            "legacy_names": ["synth-legacy-alpha"],
            "related": ["synth-beta"],
            "start_here": "docs/synth_start.md",
            "label": "Synthetic Alpha",
        },
        "synth-beta": {
            "root": str(root_beta),
            "out_dir": str(out_beta),
            "group": "synth-group",
            "aliases": ["beta widget"],
            "legacy_names": ["synth-legacy-beta"],
            "related": ["synth-alpha"],
            "start_here": "docs/synth_beta_start.md",
            "label": "Synthetic Beta",
        },
    }
    reg_file = tmp_path / "projects.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(reg_file))
    project_resolver.clear_cache()
    return {
        "reg_file": reg_file,
        "root_alpha": root_alpha,
        "root_beta": root_beta,
        "roots_parent": tmp_path / "roots",
        "broker_home": broker_home,
        "db_file": db_file,
    }


def test_resolve_project_canonical(synthetic_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    root_alpha_str = str(synthetic_env["root_alpha"])

    # 1. By alias
    info = agent_broker_mcp.resolve_project("alpha widget")
    assert info.name == "synth-alpha"
    assert info.root_path == root_alpha_str

    # 2. By legacy name
    info = agent_broker_mcp.resolve_project("synth-legacy-alpha")
    assert info.name == "synth-alpha"
    assert info.root_path == root_alpha_str

    # 3. By path inside a root
    subpath = synthetic_env["root_alpha"] / "src" / "deep" / "file.py"
    info = agent_broker_mcp.resolve_project(str(subpath))
    assert info.name == "synth-alpha"
    assert info.root_path == root_alpha_str

    # 4. By cwd inside a root (when project is None or empty)
    monkeypatch.setattr(Path, "cwd", lambda: synthetic_env["root_alpha"] / "src")
    info = agent_broker_mcp.resolve_project(None)
    assert info.name == "synth-alpha"
    assert info.root_path == root_alpha_str

    info_empty = agent_broker_mcp.resolve_project("")
    assert info_empty.name == "synth-alpha"
    assert info_empty.root_path == root_alpha_str


def test_resolve_project_unregistered(synthetic_env: dict[str, Path]):
    info = agent_broker_mcp.resolve_project("unregistered-project-name")
    assert info.name == "unregistered-project-name"


def test_record_work_memory_outcome_creates_pending_note(synthetic_env: dict[str, Path]):
    res = agent_broker_mcp.record_work_memory(
        project="synth-alpha",
        topic="t1",
        agent="codex-worker",
        summary="Verified query planner optimization",
        outcome="worked",
        applies_when="heavy query load",
        evidence="latency graphs",
    )
    assert "note_id" in res
    assert res["note_id"].startswith("n_")
    assert "note_error" not in res

    notes = project_notes.list_notes("synth-alpha")
    assert len(notes) == 1
    assert notes[0]["id"] == res["note_id"]
    assert notes[0]["status"] == "pending"
    assert notes[0]["kind"] == "worked"
    assert notes[0]["text"] == "Verified query planner optimization"
    assert notes[0]["applies_when"] == "heavy query load"

    act = project_notes.activity_summary("synth-alpha")
    assert len(act) == 1
    assert act[0]["host"] == "codex-worker"
    assert act[0]["notes"] == 1


def test_record_work_memory_over_400_summary(synthetic_env: dict[str, Path]):
    long_summary = "L" * 401
    res = agent_broker_mcp.record_work_memory(
        project="synth-alpha",
        topic="t1",
        agent="codex",
        summary=long_summary,
        outcome="failed",
    )
    assert "note_id" not in res
    assert res.get("note_error") == "summary over 400 chars; note not recorded - shorten it or use project_brief op=add"
    assert len(project_notes.list_notes("synth-alpha")) == 0


def test_record_work_memory_omitted_project_ambiguous_cwd(
    synthetic_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Path, "cwd", lambda: synthetic_env["roots_parent"])
    res = agent_broker_mcp.record_work_memory(
        project=None,
        topic="t1",
        agent="codex",
        summary="A normal summary under ambiguous workspace",
    )
    assert "project_hint" in res
    assert res["project_hint"] == (
        "This workspace contains registered projects synth-alpha, synth-beta; "
        "pass project=<id> so other sessions find this note."
    )


def test_project_brief_tool_lifecycle(synthetic_env: dict[str, Path]):
    # 1. add -> pending
    add_call = {
        "op": "add",
        "project": "synth-alpha",
        "kind": "failed",
        "text": "Never bypass authentication middleware",
        "applies_when": "public routes",
        "evidence": "audit log 101",
        "author": "owner",
    }
    res = agent_broker_mcp.handle_tool("project_brief", add_call)
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is True
    assert data["op"] == "add"
    assert data["project"] == "synth-alpha"
    assert data["note"]["status"] == "pending"
    assert data["note"]["kind"] == "failed"
    note_id = data["note"]["id"]

    # 2. pending lists only pending
    res = agent_broker_mcp.handle_tool("project_brief", {"op": "pending", "project": "synth-alpha"})
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is True
    assert data["op"] == "pending"
    assert len(data["pending"]) == 1
    assert data["pending"][0]["id"] == note_id
    assert data["pending"][0]["text"] == "Never bypass authentication middleware"

    # 3. review -> verified
    review_call = {
        "op": "review",
        "project": "synth-alpha",
        "note_id": note_id,
        "status": "verified",
        "reason": "checked in pen test",
        "author": "security-lead",
    }
    res = agent_broker_mcp.handle_tool("project_brief", review_call)
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is True
    assert data["op"] == "review"
    assert data["status"] == "verified"
    assert data["note_id"] == note_id

    # 4. pending now empty
    res = agent_broker_mcp.handle_tool("project_brief", {"op": "pending", "project": "synth-alpha"})
    data = json.loads(res["content"][0]["text"])
    assert len(data["pending"]) == 0

    # 5. get shows verified lesson in first_touch and brief
    res = agent_broker_mcp.handle_tool("project_brief", {"op": "get", "project": "synth-alpha"})
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is True
    assert data["op"] == "get"
    assert data["project"] == "synth-alpha"
    assert "Never bypass authentication middleware" in data["first_touch"]
    assert "Never bypass authentication middleware" in data["brief"]
    assert "## Do not repeat" in data["brief"]


def test_project_brief_unregistered_and_ambiguous(
    synthetic_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    # Unregistered project
    res = agent_broker_mcp.handle_tool("project_brief", {"op": "get", "project": "not-registered"})
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is False
    assert data["error"] == "unregistered"
    assert data["registered"] == ["synth-alpha", "synth-beta"]

    # Ambiguous project (omitted project when cwd is a parent of multiple roots)
    monkeypatch.setattr(Path, "cwd", lambda: synthetic_env["roots_parent"])
    res = agent_broker_mcp.handle_tool("project_brief", {"op": "get", "project": None})
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is False
    assert data["error"] == "project_required"
    assert data["candidates"] == ["synth-alpha", "synth-beta"]


def test_project_brief_responses_contain_no_absolute_paths(
    synthetic_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    ops = [
        {"op": "add", "project": "synth-alpha", "kind": "worked", "text": "Clean code"},
        {"op": "pending", "project": "synth-alpha"},
        {"op": "get", "project": "synth-alpha"},
        {"op": "get", "project": "unregistered-id"},
    ]
    for call_args in ops:
        res = agent_broker_mcp.handle_tool("project_brief", call_args)
        raw_text = res["content"][0]["text"]
        assert ":\\" not in raw_text
        assert ":/" not in raw_text
        assert str(synthetic_env["root_alpha"]) not in raw_text
        assert str(synthetic_env["broker_home"]) not in raw_text

    # Ambiguous case
    monkeypatch.setattr(Path, "cwd", lambda: synthetic_env["roots_parent"])
    res = agent_broker_mcp.handle_tool("project_brief", {"op": "get", "project": None})
    raw_text = res["content"][0]["text"]
    assert ":\\" not in raw_text
    assert ":/" not in raw_text
    assert str(synthetic_env["roots_parent"]) not in raw_text


def test_get_context_pack_registered(synthetic_env: dict[str, Path]):
    # Record event under legacy name
    agent_broker_mcp.record_agent_event(
        project="synth-legacy-alpha",
        topic="legacy-topic",
        agent="old-agent",
        event_type="note",
        summary="Event recorded under legacy name in past session",
    )

    # Record event under unregistered workspace whose topic mentions alias
    agent_broker_mcp.record_agent_event(
        project="workspace-unreg",
        topic="alpha-alias-investigation",
        agent="other-agent",
        event_type="work_memory",
        summary="Working with the alpha alias in separate workspace",
    )

    pack = agent_broker_mcp.get_context_pack("synth-alpha")
    content = pack["content"]

    # 1. Brief section present
    assert "## Project brief" in content

    # 2. Replaced Project path line
    assert "Project path: registered project synth-alpha (use code_graph project=synth-alpha)" in content
    # Absolute path not present on Project path line
    for line in content.splitlines():
        if line.startswith("Project path:"):
            assert ":\\" not in line
            assert ":/" not in line

    # 3. Events recorded earlier under legacy name included
    assert "Event recorded under legacy name in past session" in content

    # 4. Possibly related workspace topics listing
    assert "## Possibly related workspace topics" in content
    assert "project workspace-unreg / topic alpha-alias-investigation" in content


def test_get_context_pack_unregistered(synthetic_env: dict[str, Path]):
    pack = agent_broker_mcp.get_context_pack("unregistered-project")
    content = pack["content"]

    assert "## Project brief" not in content
    assert "registered project unregistered-project" not in content
    assert "## Possibly related workspace topics" not in content


def test_tool_registration_and_schemas():
    tool_names = [t["name"] for t in agent_broker_mcp.TOOLS]
    assert "project_brief" in tool_names

    pb_tool = next(t for t in agent_broker_mcp.TOOLS if t["name"] == "project_brief")
    assert "Cross-host project brief" in pb_tool["description"]
    schema = pb_tool["inputSchema"]
    assert "op" in schema["properties"]
    assert schema["properties"]["op"]["enum"] == ["get", "add", "review", "pending"]

    # COMPACT_TOOL_DESCRIPTIONS
    assert "project_brief" in agent_broker_mcp.COMPACT_TOOL_DESCRIPTIONS
    assert "Cross-host project brief" in agent_broker_mcp.COMPACT_TOOL_DESCRIPTIONS["project_brief"]

    # CLAUDE_LITE_TOOL_NAMES
    assert "project_brief" in agent_broker_mcp.CLAUDE_LITE_TOOL_NAMES

    # record_work_memory schema extensions
    rwm_tool = next(t for t in agent_broker_mcp.TOOLS if t["name"] == "record_work_memory")
    rwm_props = rwm_tool["inputSchema"]["properties"]
    assert "outcome" in rwm_props
    assert "applies_when" in rwm_props
    assert "evidence" in rwm_props
    assert "supersedes" in rwm_props


def test_text_content_masks_redact_terms(tmp_path, monkeypatch):
    import json as _json
    import agent_broker_mcp as _m
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path))
    (tmp_path / "code-graph").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code-graph" / "redact_terms.json").write_text(_json.dumps(["Privatefolder"]), encoding="utf-8")
    out = _m.text_content({"root_path": "C:/Users/x/PRIVATEFOLDER/Some Project", "note": "privatefolder ok"})
    text = out["content"][0]["text"]
    assert "privatefolder" not in text.lower()
    assert text.count("<masked>") == 2
    _json.loads(text)
