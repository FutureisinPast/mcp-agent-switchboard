"""Tests for code_graph op=resolve local dispatcher integration.

Covers WP-CGX-I6b acceptance criteria:
- Schema enum contains 'resolve' and description mentions query mapping.
- Dispatching code_graph op=resolve with an alias resolves via project_resolver (never bridge).
- Returns compact JSON with ok, op, projects, ambiguous, registered, hint.
- Output never contains root, out_dir, absolute paths, or label.
- Empty or missing registry returns ok: true with empty lists.
- Exceptions return {"ok": False, "op": "resolve", "error": "resolve_failed"} with no traceback.
- Usage events are recorded through _record_code_graph_usage_event.
- max_chars trimming trims registered, then related.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp
import code_graph_bridge
import project_resolver
import routing_gate


@pytest.fixture(autouse=True)
def clean_resolver_cache():
    project_resolver.clear_cache()
    yield
    project_resolver.clear_cache()


@pytest.fixture
def synthetic_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root_alpha = tmp_path / "roots" / "synth_alpha"
    root_alpha.mkdir(parents=True, exist_ok=True)
    out_alpha = tmp_path / "out" / "synth_alpha"
    pub_alpha = out_alpha / "published"
    pub_alpha.mkdir(parents=True, exist_ok=True)
    (pub_alpha / "current.json").write_text(
        json.dumps({"built_at": "2026-10-07T12:00:00Z"}), encoding="utf-8"
    )

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
            "related": ["synth-beta"],
            "label": "Synthetic Alpha Label Never Leak",
        },
        "synth-beta": {
            "root": str(root_beta),
            "out_dir": str(out_beta),
            "group": "synth-group",
            "aliases": ["beta widget", "beta alias"],
            "related": ["synth-alpha"],
            "label": "Synthetic Beta Label Never Leak",
        },
    }

    reg_file = tmp_path / "projects.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(reg_file))
    project_resolver.clear_cache()
    return reg_file


def test_schema_enum_and_description():
    tools = [t for t in agent_broker_mcp.TOOLS if t.get("name") == "code_graph"]
    assert len(tools) == 1
    tool = tools[0]

    # Description mentions resolve
    desc = tool.get("description", "")
    assert "resolve" in desc
    assert "resolve maps free text in query" in desc

    # Compact description mentions resolve
    compact_desc = agent_broker_mcp.COMPACT_TOOL_DESCRIPTIONS.get("code_graph", "")
    assert "resolve" in compact_desc
    assert "resolve maps free text in query" in compact_desc

    # Schema op enum contains resolve
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    op_enum = props["op"]["enum"]
    assert "resolve" in op_enum


def test_resolve_alias_query(synthetic_registry: Path, monkeypatch: pytest.MonkeyPatch):
    # Ensure bridge is never called
    bridge_mock = mock.Mock(side_effect=AssertionError("code_graph_bridge.call must not be called"))
    monkeypatch.setattr(code_graph_bridge, "call", bridge_mock)

    call_args = {"op": "resolve", "query": "inspect alpha alias now"}
    res = agent_broker_mcp.handle_tool("code_graph", call_args)

    assert "content" in res
    raw_text = res["content"][0]["text"]
    data = json.loads(raw_text)

    assert data["ok"] is True
    assert data["op"] == "resolve"
    assert len(data["projects"]) == 1

    proj = data["projects"][0]
    assert proj["id"] == "synth-alpha"
    assert proj["reason"] == "alias"
    assert proj["group"] == "synth-group"
    assert proj["related"] == ["synth-beta"]
    assert isinstance(proj["freshness"], str)
    assert proj["freshness"].startswith("built ")

    assert data["ambiguous"] == []
    assert data["registered"] == ["synth-alpha", "synth-beta"]
    assert data["hint"] == (
        "Use code_graph project=<id> (locate/find_text) and get_context_pack project=<id> "
        "for prior memory; results are locators."
    )
    bridge_mock.assert_not_called()


def test_output_contains_no_paths_or_labels(
    synthetic_registry: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(side_effect=RuntimeError("no bridge")))

    call_args = {"op": "resolve", "query": "alpha alias"}
    res = agent_broker_mcp.handle_tool("code_graph", call_args)
    raw_text = res["content"][0]["text"]
    data = json.loads(raw_text)

    # Dictionary checks
    for proj in data["projects"]:
        assert "root" not in proj
        assert "out_dir" not in proj
        assert "label" not in proj

    # Serialized text checks
    assert "root" not in data
    assert "out_dir" not in data
    assert "label" not in data
    assert "Synthetic Alpha Label" not in raw_text
    assert "Synthetic Beta Label" not in raw_text
    assert "synth_alpha" not in raw_text
    assert "synth_beta" not in raw_text
    assert ":\\" not in raw_text
    assert ":/" not in raw_text


def test_empty_registry_returns_ok_true_with_empty_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing_file = tmp_path / "absent_projects.json"
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(missing_file))
    project_resolver.clear_cache()

    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(side_effect=RuntimeError("no bridge")))

    res = agent_broker_mcp.handle_tool("code_graph", {"op": "resolve", "query": "anything"})
    assert "content" in res
    data = json.loads(res["content"][0]["text"])

    assert data["ok"] is True
    assert data["op"] == "resolve"
    assert data["projects"] == []
    assert data["ambiguous"] == []
    assert data["registered"] == []
    assert "hint" in data


def test_code_graph_bridge_never_invoked_for_resolve(
    synthetic_registry: Path, monkeypatch: pytest.MonkeyPatch
):
    bridge_called = False

    def explode(*args, **kwargs):
        nonlocal bridge_called
        bridge_called = True
        raise RuntimeError("bridge called")

    monkeypatch.setattr(code_graph_bridge, "call", explode)

    res = agent_broker_mcp.handle_tool("code_graph", {"op": "resolve", "query": "alpha alias"})
    assert bridge_called is False
    assert "content" in res
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is True


def test_usage_event_recorded_for_resolve(
    synthetic_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_file = tmp_path / "state.sqlite"
    monkeypatch.setattr(agent_broker_mcp, "DB_PATH", db_file)
    monkeypatch.setattr(routing_gate, "DB_PATH", db_file)
    monkeypatch.setattr(agent_broker_mcp, "_MCP_CLIENT_NAME", "synth-test-client")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("CODEX_SESSION_ID", "synth-session-42")

    agent_broker_mcp.init_db()
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(side_effect=RuntimeError("no bridge")))

    call_args = {"op": "resolve", "query": "SECRET_QUERY_TEXT alpha alias"}
    res = agent_broker_mcp.handle_tool("code_graph", call_args)
    assert "content" in res

    with agent_broker_mcp.db_connect() as conn:
        rows = conn.execute(
            "SELECT id, event_type, summary, details, created_at FROM agent_events WHERE event_type = 'code_graph'"
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "code_graph"
    assert "code_graph resolve: ok=True hits=1" in row[2]

    details = json.loads(row[3])
    assert details["kind"] == "code_graph"
    assert details["op"] == "resolve"
    assert details["host"] == "synth-test-client"
    assert details["session"] == "synth-session-42"
    assert details["ok"] is True
    assert details["hits"] == 1
    assert details["chars"] > 0
    assert "SECRET_QUERY_TEXT" not in row[3]
    assert "SECRET_QUERY_TEXT" not in row[2]


def test_exception_returns_resolve_failed(
    synthetic_registry: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        project_resolver, "resolve", mock.Mock(side_effect=RuntimeError("resolver crashed"))
    )
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(side_effect=RuntimeError("no bridge")))

    res = agent_broker_mcp.handle_tool("code_graph", {"op": "resolve", "query": "alpha"})
    assert "content" in res
    raw_text = res["content"][0]["text"]
    data = json.loads(raw_text)

    assert data == {"ok": False, "op": "resolve", "error": "resolve_failed"}
    assert "resolver crashed" not in raw_text
    assert "Traceback" not in raw_text


def test_max_chars_trimming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root_alpha = tmp_path / "roots" / "synth_alpha"
    root_alpha.mkdir(parents=True, exist_ok=True)
    out_alpha = tmp_path / "out" / "synth_alpha"
    out_alpha.mkdir(parents=True, exist_ok=True)

    reg_data = {
        f"synth-proj-{i:03d}": {
            "root": str(root_alpha),
            "out_dir": str(out_alpha),
            "group": "synth-group",
            "aliases": [f"alias-{i:03d}"],
            "related": [f"synth-proj-{(i+1)%50:03d}" for i in range(10)],
            "label": f"Project {i}",
        }
        for i in range(50)
    }

    reg_file = tmp_path / "large_projects.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(reg_file))
    project_resolver.clear_cache()

    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(side_effect=RuntimeError("no bridge")))

    max_chars = 600
    res = agent_broker_mcp.handle_tool(
        "code_graph", {"op": "resolve", "query": "alias-000", "max_chars": max_chars}
    )
    raw_text = res["content"][0]["text"]
    assert len(raw_text) <= max_chars
    data = json.loads(raw_text)
    assert data["ok"] is True
    assert data["op"] == "resolve"
    # Verify registered list was trimmed
    assert len(data["registered"]) < 50
