"""
code_graph_bridge.py

Cross-agent code knowledge graph bridge for Agent Switchboard.
Proxies requests to an external long-lived adapter process (a code-knowledge-graph
locator built on graphify).
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

VALID_OPS = {"locate", "expand", "path", "stats", "refresh", "health"}

FORWARD_KEYS = (
    "project",
    "query",
    "node_id",
    "symbol",
    "file",
    "source",
    "target",
    "source_id",
    "target_id",
    "limit",
    "max_hops",
    "force",
    "include_ids",
)


class CodeGraphBridge:
    def __init__(self, home: Path | str | None = None, timeout_override: float | None = None) -> None:
        self._home = Path(home) if home is not None else None
        self._timeout_override = timeout_override
        self._lock = threading.Lock()
        self._child: subprocess.Popen[str] | None = None
        self._out_q: queue.Queue[str | None] | None = None
        self._reader_thread: threading.Thread | None = None
        self._started_once: bool = False
        self._restart_times: list[float] = []
        self._next_id: int = 1

    def _resolve_paths(self) -> tuple[Path, Path, Path, Path]:
        if self._home is not None:
            home = Path(self._home)
        else:
            env_home = os.environ.get("AGENT_BROKER_CODE_GRAPH_HOME")
            if env_home:
                home = Path(env_home).expanduser()
            else:
                home = Path.home() / ".agent-broker" / "code-graph"

        if sys.platform == "win32":
            python_path = home / "venv" / "Scripts" / "python.exe"
        else:
            python_path = home / "venv" / "bin" / "python"

        adapter_path = home / "gfy_adapter.py"
        projects_path = home / "projects.json"
        return home, python_path, adapter_path, projects_path

    def _kill_child(self) -> None:
        proc = self._child
        self._child = None
        self._out_q = None
        self._reader_thread = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    def _spawn(self, python_path: Path, adapter_path: Path, projects_path: Path) -> None:
        cmd = [str(python_path), str(adapter_path), "--projects", str(projects_path)]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            "shell": False,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        proc = subprocess.Popen(cmd, **kwargs)
        out_q: queue.Queue[str | None] = queue.Queue()

        def _reader_loop() -> None:
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        out_q.put(line)
            except Exception:
                pass
            finally:
                out_q.put(None)

        t = threading.Thread(target=_reader_loop, daemon=True)
        t.start()

        self._child = proc
        self._out_q = out_q
        self._reader_thread = t

    def _ensure_child(
        self, python_path: Path, adapter_path: Path, projects_path: Path
    ) -> dict[str, Any] | None:
        if self._child is not None and self._child.poll() is not None:
            self._kill_child()

        if self._child is None:
            now = time.time()
            self._restart_times = [t for t in self._restart_times if now - t < 300]
            if self._started_once:
                if len(self._restart_times) >= 3:
                    return {"ok": False, "error": "code_graph_unavailable"}
                self._restart_times.append(now)
            else:
                self._started_once = True

            try:
                self._spawn(python_path, adapter_path, projects_path)
            except Exception as exc:
                self._kill_child()
                return {"ok": False, "error": "code_graph_crashed", "detail": str(exc)}

        return None

    def _timeout_for_op(self, op: str) -> float:
        if self._timeout_override is not None:
            return float(self._timeout_override)
        env_to = os.environ.get("AGENT_BROKER_CODE_GRAPH_TIMEOUT")
        if env_to:
            try:
                return float(env_to)
            except ValueError:
                pass
        return 180.0 if op == "refresh" else 30.0

    def call(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            return {"ok": False, "error": "invalid_op"}

        op = args.get("op")
        if op not in VALID_OPS:
            return {"ok": False, "error": "invalid_op"}

        home, python_path, adapter_path, projects_path = self._resolve_paths()
        if not python_path.exists() or not adapter_path.exists():
            return {"ok": False, "error": "code_graph_not_installed", "home": str(home)}

        raw_max_chars = args.get("max_chars")
        if raw_max_chars is None:
            clamped_max_chars = 1500
        else:
            try:
                clamped_max_chars = int(raw_max_chars)
                if clamped_max_chars < 500:
                    clamped_max_chars = 500
                elif clamped_max_chars > 6000:
                    clamped_max_chars = 6000
            except (ValueError, TypeError):
                clamped_max_chars = 1500

        with self._lock:
            err = self._ensure_child(python_path, adapter_path, projects_path)
            if err is not None:
                return err

            req_id = self._next_id
            self._next_id += 1

            payload: dict[str, Any] = {
                "id": req_id,
                "op": op,
                "max_chars": clamped_max_chars,
            }
            for key in FORWARD_KEYS:
                if key in args and args[key] is not None:
                    payload[key] = args[key]

            try:
                req_line = json.dumps(payload, ensure_ascii=False) + "\n"
                assert self._child is not None and self._child.stdin is not None
                self._child.stdin.write(req_line)
                self._child.stdin.flush()
            except Exception:
                self._kill_child()
                return {"ok": False, "error": "code_graph_crashed"}

            timeout = self._timeout_for_op(op)
            deadline = time.time() + timeout
            out_q = self._out_q
            assert out_q is not None

            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._kill_child()
                    return {"ok": False, "error": "code_graph_timeout"}

                try:
                    item = out_q.get(timeout=max(0.005, remaining))
                except queue.Empty:
                    self._kill_child()
                    return {"ok": False, "error": "code_graph_timeout"}

                if item is None:
                    self._kill_child()
                    return {"ok": False, "error": "code_graph_crashed"}

                raw_line = item.rstrip("\r\n")
                if not raw_line.strip():
                    continue

                try:
                    resp = json.loads(raw_line)
                    if not isinstance(resp, dict):
                        raise ValueError("Expected JSON object")
                except Exception:
                    self._kill_child()
                    return {"ok": False, "error": "code_graph_crashed"}

                if resp.get("id") != req_id:
                    continue

                if len(raw_line) > 7500:
                    return {"ok": False, "error": "response_oversize", "chars": len(raw_line)}

                return resp

    def close(self) -> None:
        with self._lock:
            self._kill_child()

    def reset(self) -> None:
        with self._lock:
            self._kill_child()
            self._started_once = False
            self._restart_times.clear()
            self._next_id = 1

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


_bridge_lock = threading.Lock()
_BRIDGE_INSTANCE: CodeGraphBridge | None = None


def get_bridge() -> CodeGraphBridge:
    global _BRIDGE_INSTANCE
    if _BRIDGE_INSTANCE is None:
        with _bridge_lock:
            if _BRIDGE_INSTANCE is None:
                _BRIDGE_INSTANCE = CodeGraphBridge()
    return _BRIDGE_INSTANCE


def call(args: dict[str, Any]) -> dict[str, Any]:
    return get_bridge().call(args)


def reset_bridge() -> None:
    global _BRIDGE_INSTANCE
    with _bridge_lock:
        if _BRIDGE_INSTANCE is not None:
            _BRIDGE_INSTANCE.close()
            _BRIDGE_INSTANCE = None


def _cleanup_atexit() -> None:
    global _BRIDGE_INSTANCE
    if _BRIDGE_INSTANCE is not None:
        _BRIDGE_INSTANCE.close()


atexit.register(_cleanup_atexit)
