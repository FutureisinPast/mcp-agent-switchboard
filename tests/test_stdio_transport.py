"""End-to-end stdio transport tests that actually cross the process boundary.

Every other test in this suite calls into the server's Python objects
in-process. That is a blind spot: the defects this transport was rewritten to
fix (locale-codec mojibake, an unserializable payload killing the loop, a
lost id making a failure uncorrelatable) only show up once JSON-RPC frames
travel through a real OS pipe to a real child process, decoded by whatever
codec the platform hands us. These tests launch the packaged entry point the
same way a host does and talk to it over its actual stdin/stdout pipes.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "agent_broker_entry.py"

TIMEOUT = 10


class _ServerProcess:
    """A `serve` subprocess with its stdout drained on a background thread.

    A plain `proc.stdout.readline()` would block forever if the server never
    answers, and there is no cross-platform `select()` over a Windows pipe.
    Draining on a thread into a queue lets each test apply its own bounded
    `queue.get(timeout=...)` instead of hanging the whole run.
    """

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(ENTRY_POINT), "serve"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._lines: "queue.Queue[bytes]" = queue.Queue()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self._lines.put(line)

    def send(self, raw: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = TIMEOUT) -> dict:
        line = self._lines.get(timeout=timeout)
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=TIMEOUT)
        except Exception:  # noqa: BLE001
            self.proc.kill()
            self.proc.wait(timeout=TIMEOUT)


def _isolated_env(home_dir: Path) -> dict[str, str]:
    """Env for a `serve` child that cannot touch this machine's real state.

    `AGENT_BROKER_HOME` isolates the broker's own db/log directory, but the
    server's `serve` path also runs a hierarchy refresh keyed off
    `Path.home()` (host dotfiles like ~/.codex, ~/.claude), which does not
    read `AGENT_BROKER_HOME` at all. `Path.home()` resolves through the
    platform's home env var, so HOME/USERPROFILE are overridden too -- a
    plain AGENT_BROKER_HOME override would still let the child write into
    the real machine's home directory.
    """
    env = dict(os.environ)
    env["AGENT_BROKER_HOME"] = str(home_dir / "broker-home")
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["PYTHONUTF8"] = "1"
    return env


@pytest.fixture
def server(tmp_path):
    proc = _ServerProcess(_isolated_env(tmp_path))
    try:
        yield proc
    finally:
        proc.close()


def test_initialize_roundtrip(server):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "transport-test"}},
    }
    server.send(json.dumps(request).encode("utf-8"))
    response = server.recv()
    assert response["id"] == 1
    assert response.get("error") is None
    assert response["result"]["serverInfo"]["name"]


def test_golden_bytes_unicode_survives(server):
    """CJK + curly quotes + emoji + a codepoint whose UTF-8 form contains 0x81.

    There is no echo tool in this server's tool surface, so the payload rides
    in the JSON-RPC `id` field of an `initialize` request: `handle_message`
    returns the id verbatim (untouched by the recovery regex, since this
    frame parses cleanly), so a byte-faithful round trip of `id` is exactly
    as strong a proof as an echoed tool argument would be, without needing a
    fixture tool that does not exist in the public tool surface.
    """
    payload = "中文" + "“”" + "\U0001f600" + "ࠁ"
    assert "\x81" in payload.encode("utf-8").decode("latin-1")  # sanity: the 0x81 byte is really in there

    request = {"jsonrpc": "2.0", "id": payload, "method": "initialize", "params": {}}
    server.send(json.dumps(request).encode("utf-8"))
    response = server.recv()

    assert response["id"] == payload  # exact string equality is the real proof
    assert "�" not in response["id"]  # defense in depth: no replacement-character mojibake


def test_invalid_utf8_frame_does_not_kill_server(server):
    # A lone 0xff byte is not valid UTF-8 in any position.
    broken_frame = b'{"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"x": "\xff"}}'
    server.send(broken_frame)

    error = server.recv()
    assert error.get("error") is not None
    assert error["error"]["code"] == -32700

    # The loop must still be alive and answering after the bad frame.
    follow_up = {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}}
    server.send(json.dumps(follow_up).encode("utf-8"))
    response = server.recv()
    assert response["id"] == 3
    assert response.get("error") is None


def test_unparseable_frame_still_correlates_id(server):
    # Valid enough for the id-recovery regex to find "id": 42, but missing
    # the closing brace so json.loads() raises inside handle_message.
    broken_frame = b'{"jsonrpc": "2.0", "id": 42, "method": "initialize", "params": {}'
    server.send(broken_frame)

    response = server.recv()
    assert response["id"] == 42
    assert response.get("error") is not None
