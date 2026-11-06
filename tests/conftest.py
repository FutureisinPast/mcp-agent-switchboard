"""Suite-wide safety net: no test may spawn a real Flash (agy) worker.

route_agent_task now defaults Flash research/implementation packages to a detached
worker (WP-SB10). A test that routes such a package without mocking would otherwise
launch a real `run-flash-request` process and, through it, a real agy. Tests that
exercise the worker start (tests/test_flash_async_lane.py) restore the real function
themselves and mock subprocess.Popen.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_flash_worker():
    stub = mock.Mock(return_value={"started": False, "reason": "disabled by tests/conftest.py"})
    with mock.patch.object(broker, "start_flash_request_worker", stub):
        yield


@pytest.fixture(autouse=True)
def _isolated_flash_apply_journal(tmp_path):
    """The write-back journal is durable evidence under the broker home; no test may
    read or leave records in the real one, and in-memory records must not leak
    between tests that reuse a work package id."""
    flash_dir = mock.patch.object(broker, "_flash_apply_dir", return_value=tmp_path / "flash-apply")
    memory = mock.patch.dict(broker._FLASH_APPLY_MEMORY, {}, clear=True)
    with flash_dir, memory:
        yield


@pytest.fixture(autouse=True)
def _isolated_broker_event_db(tmp_path, monkeypatch):
    """handle_tool("code_graph") records a usage event through record_agent_event into
    DB_PATH. No test may write the real broker DB, so every test gets a temp one; tests
    that need a specific DB monkeypatch DB_PATH again after this fixture runs."""
    monkeypatch.setattr(broker, "DB_PATH", tmp_path / "isolated-state.sqlite")
    yield


@pytest.fixture(autouse=True)
def _no_live_decision_evaluator(monkeypatch):
    """Tests must never launch the real Antigravity CLI for the decision evaluator.
    Tests that exercise the evaluator mock it and opt in by setting the variable."""
    monkeypatch.setenv("AGENT_BROKER_DECISION_EVALUATOR_MODEL", "off")


@pytest.fixture(autouse=True)
def _isolated_broker_home_and_fast_wait(tmp_path, monkeypatch):
    """Inline-wait for detached workers is capped low, and every path derived from the
    broker home (worker logs, runs/ layout, retention) lands under a per-test tmp dir."""
    home = tmp_path / "broker-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_BROKER_INLINE_WAIT_SECONDS", "15")
    monkeypatch.setenv("AGENT_BROKER_HOME", str(home))
    monkeypatch.setattr(broker, "BROKER_DIR", home)
    yield
