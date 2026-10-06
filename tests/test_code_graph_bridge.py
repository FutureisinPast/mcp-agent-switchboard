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
    if query == "__slow__":
        time.sleep(0.8)
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
    assert set(props["op"]["enum"]) == {
        "locate", "expand", "path", "stats", "refresh", "health", "find_text", "context_for",
    }
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


# ---------------------------------------------------------------- hot reload (release marker)

def _release_script(tag: str, *, health_ok: bool = True, broken: bool = False) -> str:
    if broken:
        return "def broken(:\n"
    script = FAKE_ADAPTER_SCRIPT.replace('"fake_snap"', repr(tag))
    if not health_ok:
        needle = '    query = req.get("query", "")\n'
        inject = (
            '    if op == "health":\n'
            '        sys.stdout.write(json.dumps({"id": req_id, "ok": False}) + "\n")\n'
            '        sys.stdout.flush()\n'
            '        continue\n'
        )
        assert needle in script
        script = script.replace(needle, needle + inject, 1)
    return script


def _install_release(home: Path, release: str, script: str) -> None:
    rel = home / "releases" / release
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "gfy_adapter.py").write_text(script, encoding="utf-8")


def _write_marker(home: Path, release: str) -> None:
    tmp = home / "runtime.json.tmp"
    tmp.write_text(json.dumps({"release": release, "hash": release * 4}), encoding="utf-8")
    os.replace(tmp, home / "runtime.json")


def test_no_marker_uses_legacy_adapter(fake_code_graph_home: Path):
    bridge = CodeGraphBridge(home=fake_code_graph_home)
    try:
        r = bridge.call({"op": "health"})
        assert r["ok"] and r["snapshot"] == "fake_snap"
        assert "adapter_update_pending" not in r
        assert bridge._resolve_paths()[2] == fake_code_graph_home / "gfy_adapter.py"
    finally:
        bridge.close()


def test_marker_selects_release_and_change_switches_exactly_once(fake_code_graph_home: Path):
    home = fake_code_graph_home
    _install_release(home, "aaa111", _release_script("rel_a"))
    _write_marker(home, "aaa111")
    bridge = CodeGraphBridge(home=home)
    try:
        assert bridge.call({"op": "locate", "query": "x"})["snapshot"] == "rel_a"
        pid_a = bridge._child.pid
        # unchanged marker never respawns
        for _ in range(3):
            assert bridge.call({"op": "locate", "query": "x"})["snapshot"] == "rel_a"
        assert bridge._child.pid == pid_a

        _install_release(home, "bbb222", _release_script("rel_b"))
        _write_marker(home, "bbb222")
        r = bridge.call({"op": "locate", "query": "x"})
        assert r["snapshot"] == "rel_b" and "adapter_update_pending" not in r
        pid_b = bridge._child.pid
        assert pid_b != pid_a
        assert bridge._child.poll() is None
        for _ in range(3):
            bridge.call({"op": "locate", "query": "x"})
        assert bridge._child.pid == pid_b
    finally:
        bridge.close()


@pytest.mark.parametrize("variant", ["syntax", "health"])
def test_invalid_release_keeps_old_child_and_sets_pending(fake_code_graph_home: Path, variant: str):
    home = fake_code_graph_home
    _install_release(home, "aaa111", _release_script("rel_a"))
    _write_marker(home, "aaa111")
    bridge = CodeGraphBridge(home=home)
    try:
        assert bridge.call({"op": "locate", "query": "x"})["snapshot"] == "rel_a"
        pid_a = bridge._child.pid
        if variant == "syntax":
            _install_release(home, "bad000", _release_script("rel_bad", broken=True))
        else:
            _install_release(home, "bad000", _release_script("rel_bad", health_ok=False))
        _write_marker(home, "bad000")
        r = bridge.call({"op": "locate", "query": "x"})
        assert r["ok"] is True and r["snapshot"] == "rel_a"
        assert r["adapter_update_pending"]
        assert bridge._child.pid == pid_a
        # failed marker is not retried on every call, and the pending flag persists
        r2 = bridge.call({"op": "locate", "query": "x"})
        assert r2["snapshot"] == "rel_a" and r2["adapter_update_pending"]
        # a later valid marker clears pending and switches
        _install_release(home, "ccc333", _release_script("rel_c"))
        _write_marker(home, "ccc333")
        r3 = bridge.call({"op": "locate", "query": "x"})
        assert r3["snapshot"] == "rel_c" and "adapter_update_pending" not in r3
    finally:
        bridge.close()


def test_reload_waits_for_in_flight_call(fake_code_graph_home: Path):
    import threading

    home = fake_code_graph_home
    _install_release(home, "aaa111", _release_script("rel_a"))
    _write_marker(home, "aaa111")
    bridge = CodeGraphBridge(home=home)
    results: dict[str, dict] = {}
    try:
        bridge.call({"op": "locate", "query": "warm"})
        t1 = threading.Thread(
            target=lambda: results.__setitem__("slow", bridge.call({"op": "locate", "query": "__slow__"}))
        )
        t1.start()
        time.sleep(0.25)  # slow call is in flight inside the adapter
        _install_release(home, "bbb222", _release_script("rel_b"))
        _write_marker(home, "bbb222")
        t2 = threading.Thread(
            target=lambda: results.__setitem__("next", bridge.call({"op": "locate", "query": "x"}))
        )
        t2.start()
        t1.join(10)
        t2.join(10)
        assert results["slow"]["ok"] is True and results["slow"]["snapshot"] == "rel_a"
        assert results["next"]["ok"] is True and results["next"]["snapshot"] == "rel_b"
    finally:
        bridge.close()


def test_hot_reloads_do_not_consume_crash_cap(fake_code_graph_home: Path):
    home = fake_code_graph_home
    _install_release(home, "r0", _release_script("rel_0"))
    _write_marker(home, "r0")
    bridge = CodeGraphBridge(home=home)
    try:
        assert bridge.call({"op": "locate", "query": "x"})["snapshot"] == "rel_0"
        for i in range(1, 6):
            _install_release(home, f"r{i}", _release_script(f"rel_{i}"))
            _write_marker(home, f"r{i}")
            assert bridge.call({"op": "locate", "query": "x"})["snapshot"] == f"rel_{i}"
        assert bridge._restart_times == []
        # the full crash budget is still available: 3 crash restarts allowed, 4th denied
        assert bridge.call({"op": "locate", "query": "__crash__"})["error"] == "code_graph_crashed"
        for _ in range(3):
            assert bridge.call({"op": "locate", "query": "__crash__"})["error"] == "code_graph_crashed"
        assert bridge.call({"op": "locate", "query": "x"})["error"] == "code_graph_unavailable"
    finally:
        bridge.close()


def test_handle_tool_code_graph_never_writes_real_broker_db(monkeypatch: pytest.MonkeyPatch):
    """Guard: the code_graph handle_tool path (which records a usage event) must not add
    rows to the real ~/.agent-broker/state.sqlite. Read-only check of max(id) before/after."""
    import sqlite3

    real_db = Path.home() / ".agent-broker" / "state.sqlite"
    if not real_db.exists():
        pytest.skip("no real broker DB on this machine")

    def max_id() -> int:
        conn = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM agent_events WHERE event_type = 'code_graph'").fetchone()
            return int(row[0])
        finally:
            conn.close()

    before = max_id()
    monkeypatch.setattr(code_graph_bridge, "call", mock.Mock(return_value={"ok": True, "op": "locate", "hits": 0}))
    res = agent_broker_mcp.handle_tool("code_graph", {"op": "locate", "query": "guard"})
    assert "content" in res
    assert max_id() == before
