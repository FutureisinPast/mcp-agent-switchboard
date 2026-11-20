"""Focused pytest tests for Gemini Hydrogen Antigravity workhorse routing (WP-HYD-1-WORKHORSE-CORE)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


LIVE_HYDROGEN_CATALOG_SLUGS = (
    "gemini-4-hydrogen-low",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
)


def _catalog(*live_slugs: str) -> list[dict]:
    return [
        broker.antigravity_model_entry_from_slug(broker.ANTIGRAVITY_DEFAULT_MODEL, "static"),
        *[broker.antigravity_model_entry_from_slug(slug, "antigravity-cli") for slug in live_slugs],
    ]


@pytest.fixture(autouse=True)
def _reset_antigravity_catalog_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog_path = tmp_path / "antigravity-models.json"
    monkeypatch.setattr(broker, "ANTIGRAVITY_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(broker, "ANTIGRAVITY_CATALOG_LOCK_PATH", catalog_path.with_suffix(".lock"))
    monkeypatch.setattr(broker, "_ANTIGRAVITY_MODEL_CACHE", None)
    monkeypatch.setattr(broker, "_ANTIGRAVITY_MODEL_CACHE_AT", 0.0)
    monkeypatch.setattr(broker, "_ANTIGRAVITY_LAST_DISCOVERY_STATUS", {})
    monkeypatch.delenv("AGENT_BROKER_ANTIGRAVITY_WORKHORSE", raising=False)


def _mock_agy_discovery(monkeypatch: pytest.MonkeyPatch, *live_slugs: str) -> None:
    stdout = "\n".join(live_slugs)
    proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(broker, "load_config", lambda: {})
    monkeypatch.setattr(broker, "discover_antigravity_cli", lambda *_a, **_kw: "agy")
    monkeypatch.setattr(broker, "_should_probe_antigravity_models", lambda **_kw: True)
    monkeypatch.setattr(broker, "_load_antigravity_catalog", lambda: [])
    monkeypatch.setattr(broker.subprocess, "run", lambda *_a, **_kw: proc)
    monkeypatch.setattr(broker.shutil, "which", lambda *_a, **_kw: None)


def test_case_a_hydrogen_beats_flash_high_when_both_live():
    roles = broker.antigravity_roles_from_models(
        _catalog("gemini-3.8-flash-high", "gemini-4-hydrogen-high")
    )
    assert roles["workhorse"]["id"] == "gemini-4-hydrogen-high"
    assert roles["source"] == "antigravity-cli"
    assert roles["frontier"] is None
    assert roles["authoritative"] is False
    assert roles["peer_brain_eligible"] is False
    assert "Gemini Hydrogen High" in roles["rule"]
    assert "Flash High as fallback" in roles["rule"]


def test_case_b_higher_hydrogen_version_wins_numerically():
    roles = broker.antigravity_roles_from_models(
        _catalog(
            "gemini-4.9-hydrogen-high",
            "gemini-4.10-hydrogen-high",
            "gemini-99-hydrogen-high-preview",
        )
    )
    assert roles["workhorse"]["id"] == "gemini-4.10-hydrogen-high"


def test_case_c_catalog_without_hydrogen_falls_back_to_newest_flash_high():
    roles = broker.antigravity_roles_from_models(
        _catalog("gemini-3.7-flash-high", "gemini-3.8-flash-high")
    )
    assert roles["workhorse"]["id"] == "gemini-3.8-flash-high"
    assert roles["source"] == "antigravity-cli"


def test_case_d_env_flash_override_selects_flash_high_when_hydrogen_live(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENT_BROKER_ANTIGRAVITY_WORKHORSE", "flash")
    roles = broker.antigravity_roles_from_models(
        _catalog("gemini-3.8-flash-high", "gemini-4-hydrogen-high")
    )
    assert roles["workhorse"]["id"] == "gemini-3.8-flash-high"


def test_case_e_env_exact_live_slug_pins_and_non_live_falls_through(
    monkeypatch: pytest.MonkeyPatch,
):
    catalog = _catalog(*LIVE_HYDROGEN_CATALOG_SLUGS)

    monkeypatch.setenv("AGENT_BROKER_ANTIGRAVITY_WORKHORSE", "gemini-3.8-flash-high")
    pinned = broker.antigravity_roles_from_models(catalog)
    assert pinned["workhorse"]["id"] == "gemini-3.8-flash-high"

    monkeypatch.setenv("AGENT_BROKER_ANTIGRAVITY_WORKHORSE", "gemini-9.9-missing-high")
    fallback = broker.antigravity_roles_from_models(catalog)
    assert fallback["workhorse"]["id"] == "gemini-4-hydrogen-high"


def test_case_f_antigravity_model_for_effort_mappings(monkeypatch: pytest.MonkeyPatch):
    _mock_agy_discovery(monkeypatch, *LIVE_HYDROGEN_CATALOG_SLUGS)

    assert (
        broker.antigravity_model_for_effort("gemini-4-hydrogen-high", "xhigh")
        == "gemini-4-hydrogen-xhigh"
    )
    assert (
        broker.antigravity_model_for_effort("gemini-4-argon-high", "xhigh")
        == "gemini-4-argon-high"
    )
    assert (
        broker.antigravity_model_for_effort("gemini-4-hydrogen-xhigh", "high")
        == "gemini-4-hydrogen-high"
    )
    assert (
        broker.antigravity_model_for_effort("gemini-4-hydrogen-high", "medium")
        == "gemini-4-hydrogen-medium"
    )


def test_case_g_effort_ladder_and_resolve_cli_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
):
    assert broker.FAMILY_EFFORTS["antigravity"] == ["low", "medium", "high", "xhigh"]
    _mock_agy_discovery(monkeypatch, *LIVE_HYDROGEN_CATALOG_SLUGS)

    assert broker.resolve_cli_model_and_effort("antigravity", "gemini flash", None, None) == (
        "gemini-4-hydrogen-high",
        "high",
    )
    assert broker.resolve_cli_model_and_effort("antigravity", "gemini flash", "xhigh", None) == (
        "gemini-4-hydrogen-high",
        "xhigh",
    )
    assert broker.resolve_cli_model_and_effort("antigravity", "hydrogen xhigh", None, None) == (
        "gemini-4-hydrogen-high",
        "xhigh",
    )


def test_generic_aliases_and_explicit_flash_pin(monkeypatch: pytest.MonkeyPatch):
    expected_generic = {
        "hydrogen",
        "gemini hydrogen",
        "gemini hydrogen high",
        "hydrogen high",
        "workhorse",
        "gemini flash",
        "gemini flash high",
        "flash high",
    }
    assert expected_generic <= broker.GENERIC_MODEL_REQUESTS

    _mock_agy_discovery(monkeypatch, *LIVE_HYDROGEN_CATALOG_SLUGS)
    monkeypatch.setattr(broker, "find_model_default", lambda *_a, **_kw: None)
    monkeypatch.setattr(broker, "resolve_project", lambda *_a, **_kw: broker.ProjectInfo("p", "."))

    for req in ("hydrogen", "gemini hydrogen", "gemini hydrogen high", "hydrogen high", "workhorse", "gemini flash"):
        res = broker.resolve_model_request({"target_agent": "antigravity", "target_model": req})
        assert res["target_model"] == "gemini-4-hydrogen-high"
        assert res["effort"] == "high"
        assert res["source"] == "family_workhorse"

    res_xhigh = broker.resolve_model_request(
        {"target_agent": "antigravity", "target_model": "hydrogen xhigh"}
    )
    assert res_xhigh["target_model"] == "gemini-4-hydrogen-xhigh"
    assert res_xhigh["effort"] == "xhigh"

    exact = broker.resolve_model_request(
        {"target_agent": "antigravity", "target_model": "gemini-3.8-flash-high"}
    )
    assert exact["target_model"] == "gemini-3.8-flash-high"
    assert exact["effort"] == "high"
    assert exact["source"] == "explicit_request"

    argon_xhigh = broker.resolve_model_request(
        {"target_agent": "antigravity", "target_model": "gemini-4-argon-high", "effort": "xhigh"}
    )
    assert argon_xhigh["target_model"] == "gemini-4-argon-high"
    assert argon_xhigh["effort"] == "high"


def test_codex_and_claude_effort_and_model_behavior_unchanged(
    monkeypatch: pytest.MonkeyPatch,
):
    assert broker.FAMILY_EFFORTS["codex"][-1] == "max"
    assert broker.FAMILY_EFFORTS["claude"][-1] == "max"
    monkeypatch.setattr(broker, "current_codex_role_model", lambda *_a, **_kw: "gpt-6-astra")

    assert broker.resolve_cli_model_and_effort("codex", "codex", None, None) == (
        broker.pick_cli_model("codex", "codex"),
        "max",
    )
    assert broker.resolve_cli_model_and_effort("claude", "claude", None, None) == (
        broker.pick_cli_model("claude", "claude"),
        "max",
    )


def test_explicit_slug_level_wins_when_no_effort_given(monkeypatch: pytest.MonkeyPatch):
    _mock_agy_discovery(monkeypatch, *LIVE_HYDROGEN_CATALOG_SLUGS)

    assert broker.resolve_cli_model_and_effort(
        "antigravity", "gemini-4-hydrogen-xhigh", None, None
    ) == ("gemini-4-hydrogen-xhigh", "xhigh")
    explicit = broker.resolve_cli_model_and_effort(
        "antigravity", "gemini-4-hydrogen-xhigh", "high", None
    )
    assert explicit[1] == "high"
    assert broker.resolve_cli_model_and_effort("antigravity", "gemini flash", None, None) == (
        "gemini-4-hydrogen-high",
        "high",
    )


@pytest.mark.parametrize(
    ("model_name", "requested_effort", "expected_effort"),
    [
        ("gemini-4-hydrogen-high", "high", "high"),
        ("gemini-4-hydrogen-high", "xhigh", "high"),
        ("gemini-4-hydrogen-xhigh", "high", "xhigh"),
        ("gemini-4-hydrogen-xhigh", None, "xhigh"),
        ("gemini-4-argon-high", "xhigh", "high"),
        ("gemini-4-hydrogen-medium", "high", "medium"),
        ("gemini-3.8-flash-low", "high", "low"),
    ],
)
def test_case_h_agy_command_line_model_suffix_never_differs_from_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_name: str,
    requested_effort: str | None,
    expected_effort: str,
):
    target_file = tmp_path / "sample.py"
    target_file.write_text("x = 1\n", encoding="utf-8")
    package = broker.prepare_flash_work_package(
        {"work_package_id": "WP-HYD-TEST", "allowed_files": [str(target_file)]},
        "quick_check",
        "Inspect sample.py",
    )
    outer = {
        "conversation_id": "conv-hyd-1",
        "status": "SUCCESS",
        "model": model_name,
        "structured_output": {
            "package_id": "WP-HYD-TEST",
            "status": "completed",
            "summary": "Checked sample.py.",
            "acceptance_criteria": [],
            "files_changed": [],
            "checks": [],
            "evidence": [
                {"claim": "x is 1", "path": str(target_file), "line": "1", "observation": "x = 1"}
            ],
            "claims": [
                {"statement": "x is 1", "basis": "observed", "evidence": [f"{target_file}:1"]}
            ],
            "research_coverage": [],
            "ambiguities": [],
            "risks": [],
            "next_action": "Done.",
            "brain_verification_required": "required",
        },
        "duration_seconds": 1.0,
        "num_turns": 1,
        "usage": {"total_tokens": 10},
    }
    captured_cmds: list[list[str]] = []

    def _fake_run_process(command, *_args, **_kwargs):
        captured_cmds.append(list(command))
        return 0, json.dumps(outer), ""

    monkeypatch.setattr(broker, "load_config", lambda: {})
    monkeypatch.setattr(broker, "discover_antigravity_cli", lambda *_a, **_kw: "agy")
    monkeypatch.setattr(
        broker, "resolve_project", lambda *_a, **_kw: broker.ProjectInfo("p", str(tmp_path))
    )
    monkeypatch.setattr(broker, "run_process", _fake_run_process)

    raw_envelope = broker.consult_antigravity_cli(
        "p", "Inspect sample.py", "plan", model_name, requested_effort, 60, package
    )
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    sent_model = cmd[cmd.index("--model") + 1]
    sent_effort = cmd[cmd.index("--effort") + 1]
    assert sent_model == model_name
    assert sent_effort == expected_effort
    assert sent_model.endswith(f"-{sent_effort}")

    envelope = json.loads(raw_envelope)
    assert envelope["cli"]["effort"] == sent_effort


def test_wording_updates_present():
    assert "Gemini Hydrogen" in broker.TOOL_DESCRIPTION_OVERRIDES["route_agent_task"]
    assert "xhigh" in broker.TOOL_DESCRIPTION_OVERRIDES["route_agent_task"]
    assert "hydrogen" in broker.TOOL_DESCRIPTION_OVERRIDES["route_agent_task"]
    assert "gemini flash" in broker.TOOL_DESCRIPTION_OVERRIDES["route_agent_task"]
