"""
Tests for code_graph_bridge and its integration into agent_broker_mcp.
"""
from __future__ import annotations

import json
import os
import sys
import time
import venv
from pathlib import Path
from unittest import mock

import pytest

import agent_broker_mcp
import code_graph_bridge
from code_graph_bridge import CodeGraphBridge

FAKE_ADAPTER_SCRIPT = """
import sys
import json
import time

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
    query = req.get("query", "")

    if query == "__crash__":
        sys.exit(1)
    if query == "__hang__":
        time.sleep(2)
        continue
    if query == "__non_json__":
        sys.stdout.write("NON_JSON_MALFORMED_LINE\\n")
        sys.stdout.flush()
        continue
    if query == "__unrelated_id__":
        # Output an unrelated id line first, then the matching id line
        sys.stdout.write(json.dumps({"id": 999999, "ok": True, "op": op, "chars": 10}) + "\\n")
        sys.stdout.flush()
    if query == "__oversize__":
        oversized_str = "x" * 8000
        sys.stdout.write(json.dumps({"id": req_id, "ok": True, "op": op, "data": oversized_str}) + "\\n")
        sys.stdout.flush()
        continue

    resp = {
        "id": req_id,
        "ok": True,
        "op": op,
        "project": req.get("project", "test_proj"),
        "snapshot": "fake_snap",
        "built_at": "2026-10-06T00:00:00Z",
        "stale": False,
        "stale_reason": None,
        "chars": 100,
        "echoed_max_chars": req.get("max_chars"),
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


def test_not_installed_error(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist"
    bridge = CodeGraphBridge(home=non_existent)
    res = bridge.call({"op": "locate", "query": "test"})
    assert res["ok"] is False
    assert res["error"] == "code_graph_not_installed"
    assert res["home"] == str(non_existent)
    # Also verify missing adapter in existing home
    missing_adapter_home = tmp_path / "partial"
    missing_adapter_home.mkdir()
    venv.create(missing_adapter_home / "venv", with_pip=False)
    bridge2 = CodeGraphBridge(home=missing_adapter_home)
    res2 = bridge2.call({"op": "locate", "query": "test"})
    assert res2["ok"] is False
    assert res2["error"] == "code_graph_not_installed"


def test_invalid_op_rejected(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        assert bridge.call({"op": "unknown_op"}) == {"ok": False, "error": "invalid_op"}
        assert bridge.call({}) == {"ok": False, "error": "invalid_op"}
        assert bridge.call("not_a_dict") == {"ok": False, "error": "invalid_op"}  # type: ignore[arg-type]
    finally:
        bridge.close()


def test_locate_round_trip_with_id_matching(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        res1 = bridge.call({"op": "locate", "query": "find_sym", "project": "p1"})
        assert res1["ok"] is True
        assert res1["op"] == "locate"
        assert res1["id"] == 1
        assert res1["project"] == "p1"

        res2 = bridge.call({"op": "stats", "project": "p1"})
        assert res2["ok"] is True
        assert res2["op"] == "stats"
        assert res2["id"] == 2
    finally:
        bridge.close()


def test_out_of_order_unrelated_id_lines_skipped(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        res = bridge.call({"op": "locate", "query": "__unrelated_id__"})
        assert res["ok"] is True
        assert res["id"] == 1
    finally:
        bridge.close()


def test_timeout_kills_and_next_call_restarts(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home, timeout_override=0.15)
    try:
        res1 = bridge.call({"op": "locate", "query": "__hang__"})
        assert res1["ok"] is False
        assert res1["error"] == "code_graph_timeout"

        # Next call restarts the child process and succeeds
        res2 = bridge.call({"op": "locate", "query": "normal_after_timeout"})
        assert res2["ok"] is True
    finally:
        bridge.close()


def test_crash_returns_crashed_then_restarts(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        # Child process exits unexpectedly
        res1 = bridge.call({"op": "locate", "query": "__crash__"})
        assert res1["ok"] is False
        assert res1["error"] == "code_graph_crashed"

        # Next call restarts and succeeds
        res2 = bridge.call({"op": "locate", "query": "recovered"})
        assert res2["ok"] is True

        # Non-JSON response line also triggers code_graph_crashed and restarts
        res3 = bridge.call({"op": "locate", "query": "__non_json__"})
        assert res3["ok"] is False
        assert res3["error"] == "code_graph_crashed"

        res4 = bridge.call({"op": "locate", "query": "recovered_again"})
        assert res4["ok"] is True
    finally:
        bridge.close()


RAW_BYTE_ADAPTER_SCRIPT = """
import sys
import json

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    body = json.dumps({"id": req["id"], "ok": True, "op": req.get("op"), "label": "ZZ"}).encode("ascii")
    # raw cp1252 ellipsis byte (0x85) is invalid UTF-8
    sys.stdout.buffer.write(body.replace(b"ZZ", b"a\\x85b") + b"\\n")
    sys.stdout.buffer.flush()
"""


def test_undecodable_byte_is_replaced_not_crashed(tmp_path: Path):
    home = tmp_path / "code-graph"
    home.mkdir(parents=True, exist_ok=True)
    venv.create(home / "venv", with_pip=False)
    (home / "gfy_adapter.py").write_text(RAW_BYTE_ADAPTER_SCRIPT, encoding="utf-8")
    (home / "projects.json").write_text("{}", encoding="utf-8")
    bridge = CodeGraphBridge(home=home)
    try:
        res = bridge.call({"op": "locate", "query": "x"})
        assert res["ok"] is True
        assert res["label"] == "a�b"
    finally:
        bridge.close()


def test_restart_cap_returns_code_graph_unavailable(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        # Initial call crashes (initial spawn)
        res0 = bridge.call({"op": "locate", "query": "__crash__"})
        assert res0["error"] == "code_graph_crashed"

        # Restart 1 crashes
        res1 = bridge.call({"op": "locate", "query": "__crash__"})
        assert res1["error"] == "code_graph_crashed"

        # Restart 2 crashes
        res2 = bridge.call({"op": "locate", "query": "__crash__"})
        assert res2["error"] == "code_graph_crashed"

        # Restart 3 crashes
        res3 = bridge.call({"op": "locate", "query": "__crash__"})
        assert res3["error"] == "code_graph_crashed"

        # More than 3 restarts within 300s: returns code_graph_unavailable
        res4 = bridge.call({"op": "locate", "query": "will_fail"})
        assert res4["ok"] is False
        assert res4["error"] == "code_graph_unavailable"

        # Advancing time past 300s window allows restart again
        current_time = time.time()
        with mock.patch("time.time", return_value=current_time + 305):
            res5 = bridge.call({"op": "locate", "query": "now_allowed"})
            assert res5["ok"] is True
    finally:
        bridge.close()


def test_max_chars_clamped_and_keys_forwarded(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        # Clamped lower bound (500)
        r1 = bridge.call({"op": "locate", "query": "q", "max_chars": 100})
        assert r1["echoed_max_chars"] == 500

        # Clamped upper bound (6000)
        r2 = bridge.call({"op": "locate", "query": "q", "max_chars": 10000})
        assert r2["echoed_max_chars"] == 6000

        # Default max_chars (1500)
        r3 = bridge.call({"op": "locate", "query": "q"})
        assert r3["echoed_max_chars"] == 1500

        # In-range max_chars preserved
        r4 = bridge.call({"op": "locate", "query": "q", "max_chars": 3200})
        assert r4["echoed_max_chars"] == 3200

        # Unforwarded keys are filtered out; only allowed keys are sent
        r5 = bridge.call({
            "op": "locate",
            "query": "find",
            "forbidden_key": "not_sent",
            "secret_token": "ignore",
            "limit": 5,
            "force": True,
            "include_ids": True,
        })
        assert "forbidden_key" not in r5["echoed_keys"]
        assert "secret_token" not in r5["echoed_keys"]
        assert "limit" in r5["echoed_keys"]
        assert "force" in r5["echoed_keys"]
        assert "include_ids" in r5["echoed_keys"]
    finally:
        bridge.close()


def test_oversize_response_rejected(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        res = bridge.call({"op": "locate", "query": "__oversize__"})
        assert res["ok"] is False
        assert res["error"] == "response_oversize"
        assert res["chars"] > 7500
    finally:
        bridge.close()


def test_module_level_call_with_env(fake_code_graph_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_HOME", str(fake_code_graph_home))
    code_graph_bridge.reset_bridge()
    try:
        res = code_graph_bridge.call({"op": "health"})
        assert res["ok"] is True
        assert res["op"] == "health"
    finally:
        code_graph_bridge.reset_bridge()


def test_tool_registration_and_dispatcher(monkeypatch: pytest.MonkeyPatch):
    # Verify tool in TOOLS
    tools = [t for t in agent_broker_mcp.TOOLS if t.get("name") == "code_graph"]
    assert len(tools) == 1
    tool = tools[0]
    desc = tool.get("description", "")
    assert "locators" in desc
    assert "read the primary lines before acting" in desc
    assert "targeted grep" in desc

    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    assert schema.get("required") == ["op"]
    assert set(props["op"]["enum"]) == {"locate", "expand", "path", "stats", "refresh", "health"}
    assert props["limit"]["type"] == "integer"
    assert props["limit"]["minimum"] == 1 and props["limit"]["maximum"] == 20
    assert props["max_hops"]["type"] == "integer"
    assert props["max_hops"]["minimum"] == 1 and props["max_hops"]["maximum"] == 8
    assert props["max_chars"]["type"] == "integer"
    assert props["max_chars"]["minimum"] == 500 and props["max_chars"]["maximum"] == 6000
    assert props["force"]["type"] == "boolean"

    # Verify tool in CLAUDE_LITE_TOOL_NAMES
    assert "code_graph" in agent_broker_mcp.CLAUDE_LITE_TOOL_NAMES

    # Verify dispatcher routing
    mock_ret = {"ok": True, "op": "locate", "mocked": True}
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(return_value=mock_ret))

    call_args = {"op": "locate", "query": "hello"}
    dispatch_res = agent_broker_mcp.handle_tool("code_graph", call_args)
    code_graph_bridge.call.assert_called_once_with(call_args)
    assert "content" in dispatch_res
    text_content = dispatch_res["content"][0]["text"]
    parsed = json.loads(text_content)
    assert parsed == mock_ret
