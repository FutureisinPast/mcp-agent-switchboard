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
