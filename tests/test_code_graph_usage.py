"""Unit tests for code graph usage events, schema, and reporting.

Covers WP-CGN-2 acceptance criteria:
- Bridge forwarding of text/files and the new ops (find_text, context_for).
- Schema enum and properties in agent_broker_mcp.TOOLS.
- Gate exemption for find_text and context_for in routing_gate.py (and refresh still counted).
- Exactly one usage event per call containing none of query/text/files values.
- Event-write failure does not fail the tool call.
- code-graph-usage report aggregates synthetic events correctly and prints the caveat line.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import venv
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp
import code_graph_bridge
import routing_gate

FAKE_ADAPTER_SCRIPT = """
import sys
import json

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    req_id = req.get("id")
    op = req.get("op")

    resp = {
        "id": req_id,
        "ok": True,
        "op": op,
        "project": req.get("project", "test_proj"),
        "snapshot": "fake_snap",
        "stale": False,
        "chars": 100,
        "hits": 2,
        "echoed_keys": sorted(list(req.keys())),
    }
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def fake_code_graph_home(tmp_path: Path) -> Path:
    home = tmp_path / "code-graph"
    home.mkdir(parents=True, exist_ok=True)
    venv.create(home / "venv", with_pip=False)
    adapter = home / "gfy_adapter.py"
    adapter.write_text(FAKE_ADAPTER_SCRIPT, encoding="utf-8")
    projects = home / "projects.json"
    projects.write_text("{}", encoding="utf-8")
    return home


def test_bridge_forwarding_text_and_files_and_new_ops(fake_code_graph_home: Path):
    bridge = code_graph_bridge.CodeGraphBridge(home=fake_code_graph_home)
    try:
        # 1. find_text forwarding text and project
        r1 = bridge.call({
            "op": "find_text",
            "project": "proj-1",
            "text": "error occurred",
            "limit": 5,
        })
        assert r1["ok"] is True
        assert r1["op"] == "find_text"
        assert "text" in r1["echoed_keys"]
        assert "limit" in r1["echoed_keys"]

        # 2. context_for forwarding files and project
        r2 = bridge.call({
            "op": "context_for",
            "project": "proj-1",
            "files": ["src/main.py", "src/util.py"],
            "max_chars": 2000,
        })
        assert r2["ok"] is True
        assert r2["op"] == "context_for"
        assert "files" in r2["echoed_keys"]
    finally:
        bridge.close()


def test_schema_enum_and_properties():
    tools = [t for t in agent_broker_mcp.TOOLS if t.get("name") == "code_graph"]
    assert len(tools) == 1
    tool = tools[0]

    desc = tool.get("description", "")
    assert "find_text" in desc
    assert "context_for" in desc

    compact_desc = agent_broker_mcp.COMPACT_TOOL_DESCRIPTIONS.get("code_graph", "")
    assert "find_text" in compact_desc
    assert "context_for" in compact_desc

    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})

    # op enum contains find_text and context_for
    op_enum = props["op"]["enum"]
    assert "find_text" in op_enum
    assert "context_for" in op_enum
    assert "locate" in op_enum

    # text property
    assert "text" in props
    text_prop = props["text"]
    assert text_prop.get("type") == "string"
    assert text_prop.get("minLength") == 3
    assert text_prop.get("maxLength") == 200

    # files property
    assert "files" in props
    files_prop = props["files"]
    assert files_prop.get("type") == "array"
    assert files_prop.get("items", {}).get("type") == "string"
    assert files_prop.get("minItems") == 1
    assert files_prop.get("maxItems") == 5


def test_gate_exemption_find_text_and_context_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_dir = tmp_path / "routing-gate"
    state_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = tmp_path / "context-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.sqlite"

    monkeypatch.setattr(routing_gate, "STATE_DIR", state_dir)
    monkeypatch.setattr(routing_gate, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(routing_gate, "DB_PATH", db_path)
    monkeypatch.setattr(routing_gate, "DIRECT_LABOUR_LIMIT", 1)
    monkeypatch.delenv("AGENT_BROKER_CHILD", raising=False)

    # find_text and context_for are exempt
    assert routing_gate._direct_labour_category(
        "mcp__agent_switchboard__code_graph", {"op": "find_text", "text": "err"}
    ) is None
    assert routing_gate._direct_labour_category(
        "mcp__agent_switchboard__code_graph", {"op": "context_for", "files": ["a.py"]}
    ) is None

    # refresh is counted as evidence
    assert routing_gate._direct_labour_category(
        "mcp__agent_switchboard__code_graph", {"op": "refresh"}
    ) == "evidence"

    # PreToolUse with find_text doesn't increment labour
    p_find = {
        "session_id": "sess-gate-1",
        "tool_use_id": "u1",
        "tool_name": "mcp__agent_switchboard__code_graph",
        "tool_input": {"op": "find_text", "text": "foo"},
        "_switchboard_host": "codex",
    }
    assert routing_gate.pre_tool_use(p_find) == {}
    st1 = routing_gate._read_state("sess-gate-1")
    assert int(st1.get("direct_labour_count") or 0) == 0

    # PreToolUse with context_for doesn't increment labour
    p_ctx = {
        "session_id": "sess-gate-1",
        "tool_use_id": "u2",
        "tool_name": "mcp__agent_switchboard__code_graph",
        "tool_input": {"op": "context_for", "files": ["f.py"]},
        "_switchboard_host": "codex",
    }
    assert routing_gate.pre_tool_use(p_ctx) == {}
    st2 = routing_gate._read_state("sess-gate-1")
    assert int(st2.get("direct_labour_count") or 0) == 0

    # Refresh is counted
    p_ref1 = {
        "session_id": "sess-gate-1",
        "tool_use_id": "u3",
        "tool_name": "mcp__agent_switchboard__code_graph",
        "tool_input": {"op": "refresh"},
        "_switchboard_host": "codex",
    }
    assert routing_gate.pre_tool_use(p_ref1) == {}
    st3 = routing_gate._read_state("sess-gate-1")
    assert int(st3.get("direct_labour_count") or 0) == 1

    # Second refresh exceeds limit of 1 and is denied
    p_ref2 = {
        "session_id": "sess-gate-1",
        "tool_use_id": "u4",
        "tool_name": "mcp__agent_switchboard__code_graph",
        "tool_input": {"op": "refresh"},
        "_switchboard_host": "codex",
    }
    denied = routing_gate.pre_tool_use(p_ref2)
    assert denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_exactly_one_usage_event_written_without_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_file = tmp_path / "state.sqlite"
    monkeypatch.setattr(agent_broker_mcp, "DB_PATH", db_file)
    monkeypatch.setattr(routing_gate, "DB_PATH", db_file)
    monkeypatch.setattr(agent_broker_mcp, "_MCP_CLIENT_NAME", "codex-test-client")
    monkeypatch.setenv("CODEX_SESSION_ID", "test-session-xyz")

    agent_broker_mcp.init_db()

    mock_resp = {
        "ok": True,
        "op": "find_text",
        "project": "proj-xyz",
        "snapshot": "snap-99",
        "hits": 3,
        "stale": False,
        "truncated": False,
        "chars": 150,
        "matches": [{"file": "a.py", "line": 10}],
    }
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(return_value=mock_resp))

    call_args = {
        "op": "find_text",
        "project": "proj-xyz",
        "text": "SECRET_ERROR_STRING_NEVER_LOG",
        "query": "SECRET_QUERY",
        "files": ["secret_path.py"],
        "symbol": "SECRET_SYMBOL",
        "node_id": "SECRET_NODE_ID",
        "source": "SECRET_SOURCE",
        "target": "SECRET_TARGET",
    }
    res = agent_broker_mcp.handle_tool("code_graph", call_args)
    assert "content" in res

    # Verify event in SQLite
    with agent_broker_mcp.db_connect() as conn:
        rows = conn.execute(
            "SELECT id, event_type, summary, details, created_at FROM agent_events WHERE event_type = 'code_graph'"
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "code_graph"
    details = json.loads(row[3])

    # Required fields
    assert details["kind"] == "code_graph"
    assert details["host"] == "codex-test-client"
    assert details["session"] == "test-session-xyz"
    assert details["op"] == "find_text"
    assert details["project"] == "proj-xyz"
    assert details["ok"] is True
    assert details["snapshot"] == "snap-99"
    assert details["hits"] == 3
    assert details["stale"] is False
    assert details["truncated"] is False
    assert details["chars"] == 150
    assert isinstance(details["ms"], (int, float))
    assert isinstance(details["ts"], str)

    # Privacy verification: MUST NEVER include query, text, files, symbol, node_id, source, target
    forbidden_keys = {"query", "text", "files", "symbol", "node_id", "source", "target"}
    for k in forbidden_keys:
        assert k not in details

    # Verify none of the secret values leaked into the raw details JSON
    raw_details = row[3]
    raw_summary = row[2]
    for secret in (
        "SECRET_ERROR_STRING_NEVER_LOG",
        "SECRET_QUERY",
        "secret_path.py",
        "SECRET_SYMBOL",
        "SECRET_NODE_ID",
        "SECRET_SOURCE",
        "SECRET_TARGET",
    ):
        assert secret not in raw_details
        assert secret not in raw_summary


def test_event_write_failure_does_not_fail_call(monkeypatch: pytest.MonkeyPatch):
    mock_resp = {"ok": True, "op": "locate", "hits": 1}
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(return_value=mock_resp))

    def boom(*args, **kwargs):
        raise RuntimeError("Database is locked")

    monkeypatch.setattr(agent_broker_mcp, "record_agent_event", boom)

    # Tool call must succeed despite event write failure
    res = agent_broker_mcp.handle_tool("code_graph", {"op": "locate", "query": "foo"})
    assert "content" in res
    assert json.loads(res["content"][0]["text"]) == mock_resp


def test_code_graph_usage_report_aggregates_synthetic_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "routing-gate"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(routing_gate, "STATE_DIR", state_dir)

    # Set up session state for sess-alpha (reads=3, searches=2 -> Read/Grep=5)
    st_alpha = state_dir / "sess-alpha.json"
    st_alpha.write_text(
        json.dumps({"direct_labour_counts": {"reads": 3, "searches": 2}}), encoding="utf-8"
    )

    # Set up session state for sess-beta (reads=1, searches=0 -> Read/Grep=1)
    st_beta = state_dir / "sess-beta.json"
    st_beta.write_text(
        json.dumps({"direct_labour_counts": {"reads": 1, "searches": 0}}), encoding="utf-8"
    )

    synthetic_events = [
        {
            "ts": "2026-10-06T12:00:00Z",
            "kind": "code_graph",
            "host": "codex",
            "session": "sess-alpha",
            "op": "locate",
            "ok": True,
            "hits": 2,
            "stale": False,
            "truncated": False,
            "ms": 10.0,
        },
        {
            "ts": "2026-10-06T12:01:00Z",
            "kind": "code_graph",
            "host": "codex",
            "session": "sess-alpha",
            "op": "find_text",
            "ok": True,
            "hits": 0,
            "stale": False,
            "truncated": False,
            "ms": 20.0,
        },
        {
            "ts": "2026-10-06T12:02:00Z",
            "kind": "code_graph",
            "host": "claude",
            "session": "sess-beta",
            "op": "context_for",
            "ok": True,
            "hits": 1,
            "stale": True,
            "truncated": True,
            "ms": 30.0,
        },
        {
            "ts": "2026-10-06T12:03:00Z",
            "kind": "code_graph",
            "host": "claude",
            "session": "sess-beta",
            "op": "refresh",
            "ok": False,
            "hits": 0,
            "stale": False,
            "truncated": False,
            "ms": 100.0,
        },
    ]

    report = routing_gate.format_code_graph_usage_report(synthetic_events, days=7)

    # Check aggregations
    assert "Total calls: 4" in report
    assert "Counts by host:" in report
    assert "codex=2" in report
    assert "claude=2" in report

    assert "Counts by op:" in report
    assert "locate=1" in report
    assert "find_text=1" in report
    assert "context_for=1" in report
    assert "refresh=1" in report

    assert "OK rate: 75.0%" in report
    assert "Zero-hit rate: 50.0%" in report
    assert "Stale rate: 25.0%" in report
    assert "Truncated rate: 25.0%" in report
    assert "Median ms: 25.0" in report

    # Check session comparison
    assert "sess-alpha: code_graph=2 vs Read/Grep=5" in report
    assert "sess-beta: code_graph=2 vs Read/Grep=1" in report

    # Check exact caveat line
    caveat = "Descriptive only: Read/Grep counts include required primary-evidence reads, so this does not prove the graph displaced reading."
    assert caveat in report
    assert report.strip().endswith(caveat)

    # Also test CLI execution
    monkeypatch.setattr(routing_gate, "_read_code_graph_usage_events", mock.Mock(return_value=synthetic_events))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    ret = routing_gate.code_graph_usage_cli(["--days", "7"])
    assert ret == 0
    cli_out = buf.getvalue()
    assert "Total calls: 4" in cli_out
    assert caveat in cli_out
