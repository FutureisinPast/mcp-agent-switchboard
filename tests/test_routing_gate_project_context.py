"""tests/test_routing_gate_project_context.py

Tests for WP-CGX-I5 project context injection in routing_gate.py.
Uses synthetic tmp_path fixtures only: no real user names, machine paths,
or private project names.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

import project_resolver
import routing_gate


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure broker state and project registry are completely isolated to tmp_path."""
    broker_home = tmp_path / "broker-home"
    state_dir = broker_home / "routing-gate"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_BROKER_HOME", str(broker_home))
    monkeypatch.setattr(routing_gate, "BROKER_HOME", broker_home)
    monkeypatch.setattr(routing_gate, "STATE_DIR", state_dir)
    monkeypatch.setattr(routing_gate, "DB_PATH", broker_home / "state.sqlite")
    monkeypatch.setattr(routing_gate, "EVIDENCE_DIR", broker_home / "context-evidence")
    monkeypatch.delenv("AGENT_BROKER_CHILD", raising=False)
    monkeypatch.delenv("AGENT_BROKER_PROJECT_CONTEXT", raising=False)
    project_resolver.clear_cache()
    yield
    project_resolver.clear_cache()


@pytest.fixture
def synthetic_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    root_a = tmp_path / "synthetic-repos" / "synth-alpha"
    out_a = tmp_path / "synthetic-out" / "synth-alpha"
    root_b = tmp_path / "synthetic-repos" / "synth-beta"
    out_b = tmp_path / "synthetic-out" / "synth-beta"

    for d in (root_a, out_a, root_b, out_b):
        d.mkdir(parents=True, exist_ok=True)

    data = {
        "synth-alpha": {
            "root": str(root_a),
            "out_dir": str(out_a),
            "aliases": ["alpha-alias", "alpha-svc"],
            "group": "synth-group",
            "related": ["synth-beta"],
            "label": "Synthetic Alpha Service",
        },
        "synth-beta": {
            "root": str(root_b),
            "out_dir": str(out_b),
            "aliases": ["beta-alias", "beta-svc"],
            "group": "synth-group",
            "related": ["synth-alpha"],
            "label": "Synthetic Beta Service",
        },
    }
    reg_file = tmp_path / "code-graph" / "projects.json"
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(reg_file))
    project_resolver.clear_cache()
    return data


def test_1_alias_mention_injects_pointer(synthetic_registry: dict, tmp_path: Path):
    """Case 1: alias mention -> additionalContext contains 'Switchboard project context: <id>'."""
    payload = {
        "session_id": "session-case-1",
        "prompt": "please inspect alpha-alias for issues",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Switchboard project context: synth-alpha" in ctx


def test_2_same_prompt_dedupes_in_same_session(synthetic_registry: dict, tmp_path: Path):
    """Case 2: same prompt again, same session -> no pointer; standing policy context unchanged versus a baseline call."""
    baseline_payload = {
        "session_id": "session-baseline-2",
        "prompt": "general non-project inquiry",
        "cwd": str(tmp_path),
    }
    baseline_res = routing_gate.user_prompt_submit(baseline_payload)
    baseline_ctx = baseline_res["hookSpecificOutput"]["additionalContext"]

    sess = "session-case-2"
    p = {
        "session_id": sess,
        "prompt": "please inspect alpha-alias for issues",
        "cwd": str(tmp_path),
    }
    # First turn: pointer injected
    turn1_res = routing_gate.user_prompt_submit(p)
    assert "Switchboard project context: synth-alpha" in turn1_res["hookSpecificOutput"]["additionalContext"]

    # Second turn with identical prompt: deduped, no pointer
    turn2_res = routing_gate.user_prompt_submit(p)
    turn2_ctx = turn2_res["hookSpecificOutput"]["additionalContext"]
    assert "Switchboard project context:" not in turn2_ctx
    assert turn2_ctx == baseline_ctx


def test_3_mention_of_other_project_re_emits_pointer(synthetic_registry: dict, tmp_path: Path):
    """Case 3: mention of the other project -> pointer re-emitted for it."""
    sess = "session-case-3"
    # Turn 1: synth-alpha
    p1 = {
        "session_id": sess,
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    turn1 = routing_gate.user_prompt_submit(p1)
    assert "Switchboard project context: synth-alpha" in turn1["hookSpecificOutput"]["additionalContext"]

    # Turn 2: synth-beta
    p2 = {
        "session_id": sess,
        "prompt": "now check beta-alias",
        "cwd": str(tmp_path),
    }
    turn2 = routing_gate.user_prompt_submit(p2)
    turn2_ctx = turn2["hookSpecificOutput"]["additionalContext"]
    assert "Switchboard project context: synth-beta" in turn2_ctx

    state = routing_gate._read_state(sess)
    assert state.get("project_context") == ["synth-beta"]


def test_4_unrelated_prompt_no_pointer_and_stored_context_unchanged(synthetic_registry: dict, tmp_path: Path):
    """Case 4: unrelated prompt -> no pointer and stored project_context unchanged."""
    sess = "session-case-4"
    p1 = {
        "session_id": sess,
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    routing_gate.user_prompt_submit(p1)
    state1 = routing_gate._read_state(sess)
    assert state1.get("project_context") == ["synth-alpha"]

    p2 = {
        "session_id": sess,
        "prompt": "what is the capital of France?",
        "cwd": str(tmp_path),
    }
    turn2 = routing_gate.user_prompt_submit(p2)
    ctx2 = turn2["hookSpecificOutput"]["additionalContext"]
    assert "Switchboard project context:" not in ctx2

    state2 = routing_gate._read_state(sess)
    assert state2.get("project_context") == ["synth-alpha"]


def test_5_agent_broker_child_reminder_only_no_pointer(synthetic_registry: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Case 5: AGENT_BROKER_CHILD=1 -> child reminder only, no pointer."""
    monkeypatch.setenv("AGENT_BROKER_CHILD", "1")
    payload = {
        "session_id": "session-case-5",
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert ctx == "You are a Switchboard child: answer the request; do not delegate or consult."
    assert "Switchboard project context:" not in ctx


def test_6_agent_broker_project_context_disabled_no_pointer(synthetic_registry: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Case 6: AGENT_BROKER_PROJECT_CONTEXT=0 -> no pointer."""
    monkeypatch.setenv("AGENT_BROKER_PROJECT_CONTEXT", "0")
    sess = "session-case-6"
    payload = {
        "session_id": sess,
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "Switchboard project context:" not in ctx
    state = routing_gate._read_state(sess)
    assert "project_context" not in state


def test_7_missing_and_malformed_registry_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Case 7: missing registry file and malformed JSON registry -> no pointer, no exception."""
    # Subcase A: Missing registry file
    missing = tmp_path / "nonexistent" / "projects.json"
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(missing))
    project_resolver.clear_cache()
    p1 = {
        "session_id": "session-missing-reg",
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    res1 = routing_gate.user_prompt_submit(p1)
    assert "Switchboard project context:" not in res1["hookSpecificOutput"]["additionalContext"]

    # Subcase B: Malformed JSON registry
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(malformed))
    project_resolver.clear_cache()
    p2 = {
        "session_id": "session-malformed-reg",
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    res2 = routing_gate.user_prompt_submit(p2)
    assert "Switchboard project context:" not in res2["hookSpecificOutput"]["additionalContext"]


def test_8_resolver_exception_fail_open(synthetic_registry: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Case 8: monkeypatch project_resolver.resolve to raise -> no pointer, standing context intact."""
    base_payload = {
        "session_id": "session-baseline-8",
        "prompt": "general inquiry",
        "cwd": str(tmp_path),
    }
    base_res = routing_gate.user_prompt_submit(base_payload)
    base_ctx = base_res["hookSpecificOutput"]["additionalContext"]

    def boom(*args, **kwargs):
        raise RuntimeError("simulated resolver failure")

    monkeypatch.setattr(project_resolver, "resolve", boom)

    p = {
        "session_id": "session-case-8",
        "prompt": "check alpha-alias",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(p)
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "Switchboard project context:" not in ctx
    assert ctx == base_ctx


def test_9_reset_turn_state_preserves_valid_and_drops_invalid(tmp_path: Path):
    """Case 9: reset_turn_state keeps a valid project_context, drops an invalid one, and still clears other keys."""
    # Valid project_context survives alongside claude_unavailable; other keys cleared
    sess_valid = "sess-valid-9"
    routing_gate._write_state(
        sess_valid,
        {
            "project_context": ["alpha-proj", "beta-proj"],
            "claude_unavailable": "quota",
            "mutated": True,
            "mutation_count": 5,
            "direct_labour_since_relief": 3,
            "decision_receipt_required": True,
        },
    )
    routing_gate.reset_turn_state(sess_valid)
    st = routing_gate._read_state(sess_valid)
    assert st.get("project_context") == ["alpha-proj", "beta-proj"]
    assert st.get("claude_unavailable") == "quota"
    assert "mutated" not in st
    assert "mutation_count" not in st
    assert "direct_labour_since_relief" not in st
    assert "decision_receipt_required" not in st

    # Invalid project_context variants are dropped
    invalid_examples = [
        "not-a-list",
        ["too-long-" + "x" * 60],  # > 64 chars
        [f"id-{i}" for i in range(9)],  # > 8 items
        ["valid-id", 12345],  # non-string element
        {"project": "dict"},  # dict
    ]
    for idx, inv in enumerate(invalid_examples):
        s_id = f"sess-invalid-9-{idx}"
        routing_gate._write_state(
            s_id,
            {
                "project_context": inv,
                "mutated": True,
                "mutation_count": 1,
            },
        )
        routing_gate.reset_turn_state(s_id)
        st_inv = routing_gate._read_state(s_id)
        assert "project_context" not in st_inv
        assert "mutated" not in st_inv


def test_10_pointer_never_contains_root_or_out_dir(synthetic_registry: dict, tmp_path: Path):
    """Case 10: the pointer never contains the registry root or out_dir paths."""
    root_a = synthetic_registry["synth-alpha"]["root"]
    out_a = synthetic_registry["synth-alpha"]["out_dir"]
    root_b = synthetic_registry["synth-beta"]["root"]
    out_b = synthetic_registry["synth-beta"]["out_dir"]

    payload = {
        "session_id": "session-case-10",
        "prompt": "please inspect alpha-alias",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "Switchboard project context: synth-alpha" in ctx

    for path_str in (root_a, out_a, root_b, out_b):
        assert path_str not in ctx
    assert "synthetic-repos" not in ctx
    assert "synthetic-out" not in ctx


def test_standing_context_empty_yields_pointer_only(synthetic_registry: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When standing policy context is empty, user_prompt_submit returns pointer only."""
    monkeypatch.setattr(routing_gate, "_standing_policy_context", lambda _: "")
    payload = {
        "session_id": "session-empty-standing",
        "prompt": "please inspect alpha-alias",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(payload)
    ctx = res.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert ctx.startswith("Switchboard project context: synth-alpha")
    assert "[routing]" not in ctx


def test_both_contexts_empty_returns_empty_dict(synthetic_registry: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When standing policy context is empty and no pointer resolves, returns {}."""
    monkeypatch.setattr(routing_gate, "_standing_policy_context", lambda _: "")
    payload = {
        "session_id": "session-both-empty",
        "prompt": "general non-project inquiry",
        "cwd": str(tmp_path),
    }
    res = routing_gate.user_prompt_submit(payload)
    assert res == {}


def test_gate_log_entry_records_only_ids_and_reasons(synthetic_registry: dict, tmp_path: Path):
    """A gate log entry with decision 'project-context' records only injected ids and reasons."""
    sess = "session-log-check"
    secret_text = "classified-user-token-xyz"
    cwd_path = str(tmp_path / "custom-cwd-dir")
    payload = {
        "session_id": sess,
        "prompt": f"please inspect alpha-alias and {secret_text}",
        "cwd": cwd_path,
    }
    routing_gate.user_prompt_submit(payload)

    log_path = routing_gate._session_log_path(sess)
    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pc_records = [r for r in records if r.get("decision") == "project-context"]
    assert len(pc_records) == 1
    rec = pc_records[0]
    assert rec.get("ids") == ["synth-alpha"]
    assert rec.get("reasons") == {"synth-alpha": "alias"}

    rec_raw = json.dumps(rec)
    assert secret_text not in rec_raw
    assert cwd_path not in rec_raw
    assert "synthetic-repos" not in rec_raw
    assert "synthetic-out" not in rec_raw
