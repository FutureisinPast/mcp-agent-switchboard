"""tests/test_project_resolver.py

Tests for project_resolver module. Uses synthetic tmp_path fixtures only.
No real paths, machine paths, or private project names.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
import pytest

from project_resolver import (
    Entry,
    Resolution,
    clear_cache,
    format_pointer,
    freshness,
    load_registry,
    registry_path,
    resolve,
)


@pytest.fixture(autouse=True)
def _reset_resolver_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def sample_registry_data(tmp_path: Path) -> dict:
    root_a = tmp_path / "synthetic-repos" / "alpha-root"
    out_a = tmp_path / "synthetic-out" / "alpha-svc"
    root_b = tmp_path / "synthetic-repos" / "beta-root"
    out_b = tmp_path / "synthetic-out" / "beta-svc"
    root_parent = tmp_path / "synthetic-repos" / "nested-parent"
    root_child = tmp_path / "synthetic-repos" / "nested-parent" / "child-svc"
    out_parent = tmp_path / "synthetic-out" / "nested-parent"
    out_child = tmp_path / "synthetic-out" / "child-svc"

    for d in (root_a, out_a, root_b, out_b, root_parent, root_child, out_parent, out_child):
        d.mkdir(parents=True, exist_ok=True)

    return {
        "alpha-svc": {
            "root": str(root_a),
            "out_dir": str(out_a),
            "aliases": ["alpha-alias", "shared-alias"],
            "weak_aliases": ["worker", "common-weak"],
            "group": "group-alpha",
            "related": ["beta-svc"],
            "label": "Alpha Synthetic Service",
        },
        "beta-svc": {
            "root": str(root_b),
            "out_dir": str(out_b),
            "aliases": ["beta-alias", "shared-alias"],
            "weak_aliases": ["helper", "common-weak"],
            "group": "group-alpha",
            "related": ["alpha-svc"],
            "label": "Beta Synthetic Service",
        },
        "nested-parent": {
            "root": str(root_parent),
            "out_dir": str(out_parent),
            "aliases": ["parent-alias"],
            "group": "group-nested",
            "label": "Parent Project",
        },
        "child-svc": {
            "root": str(root_child),
            "out_dir": str(out_child),
            "aliases": ["child-alias"],
            "group": "group-nested",
            "label": "Child Project",
        },
    }


@pytest.fixture
def configured_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_registry_data: dict) -> Path:
    reg_file = tmp_path / "code-graph" / "projects.json"
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(sample_registry_data), encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(reg_file))
    return reg_file


def test_registry_path_env_and_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(tmp_path / "custom.json"))
    assert registry_path() == tmp_path / "custom.json"

    monkeypatch.delenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", raising=False)
    monkeypatch.setenv("AGENT_BROKER_HOME", str(tmp_path / "broker-home"))
    assert registry_path() == tmp_path / "broker-home" / "code-graph" / "projects.json"


def test_load_registry_caching(configured_registry: Path):
    reg1 = load_registry(configured_registry)
    reg2 = load_registry(configured_registry)
    assert "alpha-svc" in reg1
    assert "beta-svc" in reg1
    assert reg1["alpha-svc"].group == "group-alpha"
    assert reg1["alpha-svc"].label == "Alpha Synthetic Service"
    assert reg1.keys() == reg2.keys()


def test_id_token_match_and_no_match_inside_longer_token(configured_registry: Path):
    reg = load_registry(configured_registry)

    # Whole token matches
    res1 = resolve("please inspect alpha-svc now", registry=reg)
    assert "alpha-svc" in res1.ids
    assert res1.reasons["alpha-svc"] == "id"

    # Exact token without surrounding text
    res2 = resolve("alpha-svc", registry=reg)
    assert res2.ids == ["alpha-svc"]

    # Separated by delimiters
    res3 = resolve("run/alpha-svc:check", registry=reg)
    assert "alpha-svc" in res3.ids

    # Longer token must NOT match (e.g. 'alpha-svcx' or 'xalpha-svc')
    res4 = resolve("please inspect alpha-svcx now", registry=reg)
    assert "alpha-svc" not in res4.ids

    res5 = resolve("please inspect xalpha-svc now", registry=reg)
    assert "alpha-svc" not in res5.ids


def test_unique_alias_selects(configured_registry: Path):
    reg = load_registry(configured_registry)
    res = resolve("deploy alpha-alias to staging", registry=reg)
    assert "alpha-svc" in res.ids
    assert res.reasons["alpha-svc"] == "alias"


def test_alias_shared_by_two_projects_selects_nothing_and_in_ambiguous(configured_registry: Path):
    reg = load_registry(configured_registry)
    res = resolve("please check shared-alias component", registry=reg)
    assert "alpha-svc" not in res.ids
    assert "beta-svc" not in res.ids
    assert "shared-alias" in res.ambiguous


def test_text_containing_registered_root_path_selects_it(configured_registry: Path, sample_registry_data: dict):
    reg = load_registry(configured_registry)
    root_str = sample_registry_data["alpha-svc"]["root"]

    # Forward slash style
    fwd_path = root_str.replace("\\", "/") + "/src/main.py"
    res1 = resolve(f"inspecting {fwd_path} for bugs", registry=reg)
    assert "alpha-svc" in res1.ids
    assert res1.reasons["alpha-svc"] == "path"

    # Backslash style
    bwd_path = root_str.replace("/", "\\") + "\\src\\main.py"
    res2 = resolve(f"inspecting {bwd_path} for bugs", registry=reg)
    assert "alpha-svc" in res2.ids
    assert res2.reasons["alpha-svc"] == "path"

    # Case insensitivity
    upper_path = root_str.replace("\\", "/").upper() + "/src/main.py"
    res3 = resolve(f"inspecting {upper_path} for bugs", registry=reg)
    assert "alpha-svc" in res3.ids
    assert res3.reasons["alpha-svc"] == "path"

    # Substring not at directory boundary must NOT match
    other_path = root_str.replace("\\", "/") + "-extra/src/main.py"
    res4 = resolve(f"inspecting {other_path} for bugs", registry=reg)
    assert "alpha-svc" not in res4.ids


def test_cwd_nested_inside_two_roots_picks_longest(configured_registry: Path, sample_registry_data: dict):
    reg = load_registry(configured_registry)
    child_root = Path(sample_registry_data["child-svc"]["root"])
    deep_cwd = child_root / "subdir" / "deep"
    deep_cwd.mkdir(parents=True, exist_ok=True)

    res = resolve("check current status", cwd=deep_cwd, registry=reg)
    assert "child-svc" in res.ids
    assert "nested-parent" not in res.ids
    assert res.reasons["child-svc"] == "cwd"


def test_weak_alias_rules(configured_registry: Path):
    reg = load_registry(configured_registry)

    # 1. Weak alias alone is ignored
    res_alone = resolve("need a helper for this job", registry=reg)
    assert "beta-svc" not in res_alone.ids

    # 2. Selected when same-group project is in active_ids
    res_active = resolve("need a helper for this job", registry=reg, active_ids=["alpha-svc"])
    assert "beta-svc" in res_active.ids
    assert res_active.reasons["beta-svc"] == "weak-in-group"

    # 3. Selected when same-group project is strongly matched in the same text
    res_strong = resolve("alpha-svc needs a helper", registry=reg)
    assert "alpha-svc" in res_strong.ids
    assert "beta-svc" in res_strong.ids
    assert res_strong.reasons["alpha-svc"] == "id"
    assert res_strong.reasons["beta-svc"] == "weak-in-group"

    # 4. Weak alias shared within same group goes to ambiguous
    res_ambig = resolve("alpha-svc run common-weak", registry=reg)
    assert "common-weak" in res_ambig.ambiguous


def test_related_never_auto_added(configured_registry: Path):
    reg = load_registry(configured_registry)
    res = resolve("alpha-svc status check", registry=reg)
    assert res.ids == ["alpha-svc"]
    assert "beta-svc" not in res.ids


def test_max_four_ids(tmp_path: Path):
    six_projects = {}
    for i in range(6):
        pid = f"proj-{i}"
        r = tmp_path / f"root-{i}"
        o = tmp_path / f"out-{i}"
        r.mkdir(parents=True, exist_ok=True)
        o.mkdir(parents=True, exist_ok=True)
        six_projects[pid] = {
            "root": str(r),
            "out_dir": str(o),
        }
    reg_file = tmp_path / "six.json"
    reg_file.write_text(json.dumps(six_projects), encoding="utf-8")
    reg = load_registry(reg_file)

    prompt = "proj-0 proj-1 proj-2 proj-3 proj-4 proj-5"
    res = resolve(prompt, registry=reg)
    assert len(res.ids) == 4
    assert len(res.reasons) == 4
    assert res.ids == ["proj-0", "proj-1", "proj-2", "proj-3"]


def test_format_pointer_constraints_and_no_paths_or_labels(configured_registry: Path, sample_registry_data: dict):
    reg = load_registry(configured_registry)
    res = Resolution(
        ids=["alpha-svc", "beta-svc"],
        reasons={"alpha-svc": "id", "beta-svc": "alias"},
        ambiguous=[],
    )

    fresh_map = {"alpha-svc": "built 2h ago"}
    pointer = format_pointer(res, reg, freshness_map=fresh_map, max_chars=300)

    assert len(pointer) <= 300
    assert pointer.startswith("Switchboard project context:")
    assert "alpha-svc" in pointer
    assert "beta-svc" in pointer
    assert "group group-alpha" in pointer
    assert "related beta-svc" in pointer
    assert "Map: code_graph project=alpha-svc [built 2h ago]." in pointer
    assert "Memory: get_context_pack project=alpha-svc." in pointer
    assert "Locators only; read cited lines." in pointer

    # Must NEVER include roots, out_dirs, drive letters, absolute paths, or labels
    alpha_root = sample_registry_data["alpha-svc"]["root"]
    alpha_out = sample_registry_data["alpha-svc"]["out_dir"]
    alpha_label = sample_registry_data["alpha-svc"]["label"]

    assert alpha_root not in pointer
    assert alpha_out not in pointer
    assert alpha_label not in pointer
    assert "C:" not in pointer
    assert "D:" not in pointer
    assert "synthetic-repos" not in pointer
    assert "synthetic-out" not in pointer


def test_format_pointer_drop_optional_parts_before_exceeding_max_chars(configured_registry: Path):
    reg = load_registry(configured_registry)
    res = Resolution(
        ids=["alpha-svc", "beta-svc"],
        reasons={"alpha-svc": "id", "beta-svc": "alias"},
    )
    fresh_map = {"alpha-svc": "built 2h ago"}

    # Full pointer
    full = format_pointer(res, reg, freshness_map=fresh_map, max_chars=300)
    assert "related" in full
    assert "built 2h ago" in full
    assert "group" in full

    # Drop related first
    ptr_tight = format_pointer(res, reg, freshness_map=fresh_map, max_chars=len(full) - 15)
    assert len(ptr_tight) <= len(full) - 15
    assert "related" not in ptr_tight

    # Drop freshness next
    ptr_tighter = format_pointer(res, reg, freshness_map=fresh_map, max_chars=len(ptr_tight) - 15)
    assert len(ptr_tighter) <= len(ptr_tight) - 15
    assert "built 2h ago" not in ptr_tighter

    # Drop group next
    ptr_tightest = format_pointer(res, reg, freshness_map=fresh_map, max_chars=len(ptr_tighter) - 15)
    assert len(ptr_tightest) <= len(ptr_tighter) - 15
    assert "group" not in ptr_tightest

    # Empty when no ids
    assert format_pointer(Resolution(), reg) == ""


def test_freshness_from_fixture_current_json_and_absent(tmp_path: Path):
    out_dir = tmp_path / "out"
    published = out_dir / "published"
    published.mkdir(parents=True, exist_ok=True)
    entry = Entry(id="fresh-proj", root=tmp_path / "root", out_dir=out_dir)

    # 1. Absent file
    assert freshness(entry) == "no map yet"

    # 2. Minutes ago
    now = datetime.now(timezone.utc)
    current_json = published / "current.json"

    dt_10m = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    current_json.write_text(json.dumps({"built_at": dt_10m}), encoding="utf-8")
    assert freshness(entry) == "built 10m ago"

    # 3. Hours ago
    dt_3h = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    current_json.write_text(json.dumps({"built_at": dt_3h}), encoding="utf-8")
    assert freshness(entry) == "built 3h ago"

    # 4. Days ago
    dt_5d = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    current_json.write_text(json.dumps({"built_at": dt_5d}), encoding="utf-8")
    assert freshness(entry) == "built 5d ago"

    # 5. Corrupt / invalid
    current_json.write_text("{invalid json", encoding="utf-8")
    assert freshness(entry) is None

    current_json.write_text(json.dumps({"no_built_at": True}), encoding="utf-8")
    assert freshness(entry) is None


def test_missing_and_malformed_registry_empty_without_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    non_existent = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(non_existent))

    reg = load_registry()
    assert reg == {}
    res = resolve("check alpha-svc")
    assert res.ids == []
    assert res.reasons == {}
    assert res.ambiguous == []

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not valid json syntax", encoding="utf-8")
    monkeypatch.setenv("AGENT_BROKER_CODE_GRAPH_PROJECTS", str(malformed))

    reg_bad = load_registry()
    assert reg_bad == {}
    res_bad = resolve("check alpha-svc")
    assert res_bad.ids == []


def test_resolve_none_and_empty_string(configured_registry: Path):
    reg = load_registry(configured_registry)

    res_none = resolve(None, registry=reg)
    assert res_none.ids == []
    assert res_none.reasons == {}
    assert res_none.ambiguous == []

    res_empty = resolve("", registry=reg)
    assert res_empty.ids == []
    assert res_empty.reasons == {}
    assert res_empty.ambiguous == []


def test_performance_10kb_prompt(tmp_path: Path):
    ten_projects = {}
    for i in range(10):
        pid = f"perf-proj-{i}"
        r = tmp_path / f"perf-r-{i}"
        o = tmp_path / f"perf-o-{i}"
        r.mkdir(parents=True, exist_ok=True)
        o.mkdir(parents=True, exist_ok=True)
        ten_projects[pid] = {
            "root": str(r),
            "out_dir": str(o),
            "aliases": [f"alias-{i}", f"perf service {i}"],
            "weak_aliases": [f"weak-{i}"],
            "group": f"perf-group-{i % 2}",
        }

    reg_file = tmp_path / "perf.json"
    reg_file.write_text(json.dumps(ten_projects), encoding="utf-8")
    reg = load_registry(reg_file)

    # 10 KB prompt
    base_text = "The quick brown fox jumps over the lazy dog in software testing workspace. "
    prompt_10kb = (base_text * 140) + "perf-proj-3 needs urgent attention and review."
    assert len(prompt_10kb) >= 10000

    # Warmup
    resolve(prompt_10kb, registry=reg)

    start = time.perf_counter()
    res = resolve(prompt_10kb, registry=reg)
    elapsed = time.perf_counter() - start

    assert "perf-proj-3" in res.ids
    # Performance bound from specification: < 50 ms (0.050s)
    assert elapsed < 0.050, f"Resolution took {elapsed*1000:.2f} ms, expected < 50 ms"


from project_resolver import canonical_project, normalize_name


def test_canonical_project_id_case_insensitive(tmp_path: Path):
    root = tmp_path / "synthetic-repos" / "alpha-root"
    out = tmp_path / "synthetic-out" / "alpha-svc"
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "alpha-svc": {
            "root": str(root),
            "out_dir": str(out),
        }
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    assert canonical_project("alpha-svc", registry=reg) == ("alpha-svc", "id", [])
    assert canonical_project("ALPHA-SVC", registry=reg) == ("alpha-svc", "id", [])
    assert canonical_project("Alpha-Svc", registry=reg) == ("alpha-svc", "id", [])
    assert canonical_project("  alpha-svc  ", registry=reg) == ("alpha-svc", "id", [])


def test_canonical_project_alias_spacing_underscores(tmp_path: Path):
    root = tmp_path / "synthetic-repos" / "demo-root"
    out = tmp_path / "synthetic-out" / "demo-svc"
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "demo-svc": {
            "root": str(root),
            "out_dir": str(out),
            "aliases": ["demo site", "frontend web"],
        }
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    assert canonical_project("Demo_Site", registry=reg) == ("demo-svc", "alias", [])
    assert canonical_project("demo site", registry=reg) == ("demo-svc", "alias", [])
    assert canonical_project("DEMO__SITE", registry=reg) == ("demo-svc", "alias", [])
    assert canonical_project("demo - site", registry=reg) == ("demo-svc", "alias", [])
    assert canonical_project("Frontend_Web", registry=reg) == ("demo-svc", "alias", [])


def test_canonical_project_unique_legacy_name(tmp_path: Path):
    root = tmp_path / "synthetic-repos" / "migrated-root"
    out = tmp_path / "synthetic-out" / "migrated-svc"
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "migrated-svc": {
            "root": str(root),
            "out_dir": str(out),
            "legacy_names": ["old-alpha-svc", "legacy service"],
        }
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    assert canonical_project("old-alpha-svc", registry=reg) == ("migrated-svc", "legacy", [])
    assert canonical_project("OLD_ALPHA_SVC", registry=reg) == ("migrated-svc", "legacy", [])
    assert canonical_project("old alpha svc", registry=reg) == ("migrated-svc", "legacy", [])
    assert canonical_project("legacy_service", registry=reg) == ("migrated-svc", "legacy", [])


def test_canonical_project_legacy_name_shared_ambiguous(tmp_path: Path):
    root_a = tmp_path / "synthetic-repos" / "alpha-root"
    out_a = tmp_path / "synthetic-out" / "alpha-svc"
    root_b = tmp_path / "synthetic-repos" / "beta-root"
    out_b = tmp_path / "synthetic-out" / "beta-svc"
    root_a.mkdir(parents=True, exist_ok=True)
    out_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    out_b.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "alpha-svc": {
            "root": str(root_a),
            "out_dir": str(out_a),
            "legacy_names": ["shared-legacy-tool"],
        },
        "beta-svc": {
            "root": str(root_b),
            "out_dir": str(out_b),
            "legacy_names": ["shared_legacy_tool"],
        },
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    expected_ambig = (None, "ambiguous", ["alpha-svc", "beta-svc"])
    assert canonical_project("shared-legacy-tool", registry=reg) == expected_ambig
    assert canonical_project("SHARED_LEGACY_TOOL", registry=reg) == expected_ambig
    assert canonical_project("shared legacy tool", registry=reg) == expected_ambig


def test_canonical_project_path_inside_root_slash_styles_case(tmp_path: Path):
    root = tmp_path / "synthetic-repos" / "alpha-root"
    out = tmp_path / "synthetic-out" / "alpha-svc"
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "alpha-svc": {
            "root": str(root),
            "out_dir": str(out),
        }
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    root_str = str(root)
    # Forward slash style
    fwd_path = root_str.replace("\\", "/") + "/src/main.py"
    assert canonical_project(fwd_path, registry=reg) == ("alpha-svc", "path", [])

    # Backslash style
    bwd_path = root_str.replace("/", "\\") + "\\src\\main.py"
    assert canonical_project(bwd_path, registry=reg) == ("alpha-svc", "path", [])

    # Different case
    upper_path = root_str.replace("\\", "/").upper() + "/SRC/MAIN.PY"
    assert canonical_project(upper_path, registry=reg) == ("alpha-svc", "path", [])

    # Root itself
    assert canonical_project(root_str, registry=reg) == ("alpha-svc", "path", [])


def test_canonical_project_nested_roots_pick_longest(tmp_path: Path):
    root_parent = tmp_path / "synthetic-repos" / "nested-parent"
    root_child = tmp_path / "synthetic-repos" / "nested-parent" / "child-svc"
    out_parent = tmp_path / "synthetic-out" / "nested-parent"
    out_child = tmp_path / "synthetic-out" / "child-svc"
    root_parent.mkdir(parents=True, exist_ok=True)
    root_child.mkdir(parents=True, exist_ok=True)
    out_parent.mkdir(parents=True, exist_ok=True)
    out_child.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "nested-parent": {
            "root": str(root_parent),
            "out_dir": str(out_parent),
        },
        "child-svc": {
            "root": str(root_child),
            "out_dir": str(out_child),
        },
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    child_file = str(root_child).replace("\\", "/") + "/deep/file.py"
    assert canonical_project(child_file, registry=reg) == ("child-svc", "path", [])

    parent_file = str(root_parent).replace("\\", "/") + "/parent_file.py"
    assert canonical_project(parent_file, registry=reg) == ("nested-parent", "path", [])


def test_canonical_project_workspace_parent_containing_two_roots_ambiguous(tmp_path: Path):
    root_a = tmp_path / "synthetic-repos" / "alpha-root"
    root_b = tmp_path / "synthetic-repos" / "beta-root"
    out_a = tmp_path / "synthetic-out" / "alpha-svc"
    out_b = tmp_path / "synthetic-out" / "beta-svc"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    out_a.mkdir(parents=True, exist_ok=True)
    out_b.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "alpha-svc": {
            "root": str(root_a),
            "out_dir": str(out_a),
        },
        "beta-svc": {
            "root": str(root_b),
            "out_dir": str(out_b),
        },
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    workspace_parent = str(tmp_path / "synthetic-repos")
    assert canonical_project(workspace_parent, registry=reg) == (
        None,
        "ambiguous",
        ["alpha-svc", "beta-svc"],
    )


def test_canonical_project_cwd_rules_mirror_path_rules(tmp_path: Path):
    root_parent = tmp_path / "synthetic-repos" / "nested-parent"
    root_child = tmp_path / "synthetic-repos" / "nested-parent" / "child-svc"
    out_parent = tmp_path / "synthetic-out" / "nested-parent"
    out_child = tmp_path / "synthetic-out" / "child-svc"
    root_parent.mkdir(parents=True, exist_ok=True)
    root_child.mkdir(parents=True, exist_ok=True)
    out_parent.mkdir(parents=True, exist_ok=True)
    out_child.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "nested-parent": {
            "root": str(root_parent),
            "out_dir": str(out_parent),
        },
        "child-svc": {
            "root": str(root_child),
            "out_dir": str(out_child),
        },
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    # 1. cwd inside child root -> child-svc cwd
    deep_child_cwd = root_child / "deep" / "subdir"
    deep_child_cwd.mkdir(parents=True, exist_ok=True)
    assert canonical_project(None, cwd=deep_child_cwd, registry=reg) == ("child-svc", "cwd", [])

    # 2. cwd inside parent root (outside child) -> nested-parent cwd
    parent_cwd = root_parent / "parent_only"
    parent_cwd.mkdir(parents=True, exist_ok=True)
    assert canonical_project(None, cwd=parent_cwd, registry=reg) == ("nested-parent", "cwd", [])

    # 3. cwd empty string name given -> same as None
    assert canonical_project("", cwd=deep_child_cwd, registry=reg) == ("child-svc", "cwd", [])

    # 4. workspace parent containing both -> ambiguous listing both
    workspace_parent = tmp_path / "synthetic-repos"
    assert canonical_project(None, cwd=workspace_parent, registry=reg) == (
        None,
        "ambiguous",
        ["child-svc", "nested-parent"],
    )

    # 5. unknown cwd -> unregistered
    unknown_cwd = tmp_path / "unrelated-dir"
    unknown_cwd.mkdir(parents=True, exist_ok=True)
    assert canonical_project(None, cwd=unknown_cwd, registry=reg) == (None, "unregistered", [])


def test_canonical_project_unknown_name(tmp_path: Path):
    root = tmp_path / "synthetic-repos" / "alpha-root"
    out = tmp_path / "synthetic-out" / "alpha-svc"
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    reg_data = {
        "alpha-svc": {
            "root": str(root),
            "out_dir": str(out),
        }
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    assert canonical_project("unknown-project", registry=reg) == (None, "unregistered", [])
    assert canonical_project("C:/unknown/path/to/something", registry=reg) == (None, "unregistered", [])
    assert canonical_project(None, cwd=None, registry=reg) == (None, "unregistered", [])
    assert canonical_project("", cwd=None, registry=reg) == (None, "unregistered", [])


def test_canonical_project_start_here_and_legacy_names_parsing(tmp_path: Path):
    root = tmp_path / "synthetic-repos" / "alpha-root"
    out = tmp_path / "synthetic-out" / "alpha-svc"
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    long_desc = "x" * 161
    valid_desc = "Short starting point for exploration."
    reg_data = {
        "proj-valid": {
            "root": str(root),
            "out_dir": str(out),
            "start_here": valid_desc,
            "legacy_names": ["old-p1", "legacy-p1"],
        },
        "proj-long-start": {
            "root": str(root),
            "out_dir": str(out),
            "start_here": long_desc,
            "legacy_names": "not-a-list",
        },
        "proj-non-list-legacy": {
            "root": str(root),
            "out_dir": str(out),
            "start_here": None,
            "legacy_names": 12345,
        },
        "proj-mixed-legacy": {
            "root": str(root),
            "out_dir": str(out),
            "legacy_names": ["valid-name", 42, None, "another-valid"],
        },
    }
    reg_file = tmp_path / "reg.json"
    reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
    reg = load_registry(reg_file)

    assert reg["proj-valid"].start_here == valid_desc
    assert reg["proj-valid"].legacy_names == ["old-p1", "legacy-p1"]

    assert reg["proj-long-start"].start_here is None
    assert reg["proj-long-start"].legacy_names == []

    assert reg["proj-non-list-legacy"].start_here is None
    assert reg["proj-non-list-legacy"].legacy_names == []

    assert reg["proj-mixed-legacy"].legacy_names == ["valid-name", "another-valid"]


def test_canonical_project_never_raises_on_garbage(tmp_path: Path):
    # Pass garbage types and malformed values
    res1 = canonical_project(12345, cwd=object(), registry=None)
    assert isinstance(res1, tuple) and len(res1) == 3

    res2 = canonical_project(None, cwd=object(), registry={})
    assert isinstance(res2, tuple) and len(res2) == 3

    res3 = canonical_project(object(), cwd=12345, registry="not-a-dict")
    assert res3 == (None, "error", [])

    res4 = canonical_project([1, 2, 3], cwd={"invalid": True}, registry=None)
    assert isinstance(res4, tuple) and len(res4) == 3

    # normalize_name never raises
    assert normalize_name(None) == ""
    assert normalize_name(12345) == ""
    assert normalize_name(object()) == ""


def test_normalize_name_function():
    assert normalize_name("Demo_Site") == "demo-site"
    assert normalize_name("demo site") == "demo-site"
    assert normalize_name("  Demo__Site   ") == "demo-site"
    assert normalize_name("demo - site") == "demo-site"
    assert normalize_name("alpha---beta") == "alpha-beta"
    assert normalize_name("ALL_CAPS_NAME") == "all-caps-name"
    assert normalize_name("") == ""
