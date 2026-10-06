"""Contract tests for gfy_adapter against graphifyy==0.9.77. Run: venv python -m pytest adapter/test_contract.py"""
import importlib.metadata
import inspect
import json
import os
import shutil
import subprocess
import sys

import pytest

os.environ["GFY_STALE_TTL_S"] = "0"  # default tests expect immediate staleness; TTL tests override via monkeypatch
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gfy_client import Adapter, PY  # noqa: E402

FIX = {
    "billing.py": "import math\n\n\ndef compute_invoice_total(items):\n    return sum(i * 2 for i in items)\n\n\nclass InvoiceLedger:\n    def add_entry(self, e):\n        return compute_invoice_total([e])\n",
    "shipping.py": "from billing import compute_invoice_total\n\n\ndef estimate_shipping_cost(weight):\n    base = compute_invoice_total([weight])\n    return base + 3\n",
    "util.py": "def normalize_label(s):\n    return s.strip().lower()\n",
    "app.py": "from shipping import estimate_shipping_cost\nfrom util import normalize_label\n\n\ndef run_checkout(w):\n    return normalize_label('X'), estimate_shipping_cost(w)\n",
}
PID = "fix"


def listing(root):
    out = []
    for d, ds, fs in os.walk(root):
        for n in ds + fs:
            p = os.path.join(d, n)
            out.append((os.path.relpath(p, root), os.path.getsize(p) if os.path.isfile(p) else -1))
    return sorted(out)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("gfy")
    root = base / "repo"
    root.mkdir()
    for n, t in FIX.items():
        (root / n).write_text(t, encoding="utf-8")
    out = base / "out"
    cfg = base / "projects.json"
    cfg.write_text(json.dumps({PID: {"root": str(root), "out_dir": str(out)}}), encoding="utf-8")
    before = listing(root)
    a = Adapter(projects=str(cfg))
    yield {"a": a, "root": root, "out": out, "before": before, "cfg": cfg}
    a.close()


def test_version_pin():
    assert importlib.metadata.version("graphifyy") == "0.9.77"


def test_graphify_internals_signatures():
    from graphify import serve as S
    P = lambda f: list(inspect.signature(f).parameters)
    assert P(S._load_graph) == ["graph_path"]
    assert P(S._query_terms) == ["question"]
    assert P(S._score_query) == ["G", "terms", "collect_per_term_seeds"]
    assert P(S._pick_seeds)[:2] == ["scored", "max_k"] and "best_seed_by_term" in P(S._pick_seeds)
    assert P(S._traversal_view) == ["G"]
    assert P(S._bfs) == ["G", "start_nodes", "depth"]
    assert P(S._find_node) == ["G", "label"]
    assert P(S._search_tokens) == ["text"]
    assert isinstance(S._RELATIONAL_INTENT_TERMS, frozenset)
    assert hasattr(S._score_query, "__call__") and S._QueryScores._fields == ("ranked", "best_seed_by_term")


def test_refresh_and_publish(env):
    a, out = env["a"], env["out"]
    r, raw, _ = a.call(op="refresh", project=PID)
    assert r["ok"], r
    assert r["nodes"] > 5 and r["stale"] is False
    ptr = json.loads((out / "published" / "current.json").read_text())
    assert set(ptr) >= {"snapshot", "sha", "built_at", "fingerprint"}
    assert (out / "published" / f"graph-{ptr['sha'][:12]}.json").exists()
    assert ptr["snapshot"] == r["snapshot"]


def test_locate_known_symbol(env):
    r, raw, _ = env["a"].call(op="locate", project=PID, query="compute_invoice_total")
    assert r["ok"]
    hit = [l for l in r["locators"] if l["symbol"] == "compute_invoice_total"]
    assert hit and hit[0]["file"] == "billing.py" and hit[0]["line"] == 4
    assert r["confidence"] == "high"


def test_schema_and_budget(env):
    for q, mc in (("compute_invoice_total", None), ("checkout shipping cost", 600), ("zzzz nothing", 400)):
        kw = {"max_chars": mc} if mc else {}
        r, raw, _ = env["a"].call(op="locate", project=PID, query=q, **kw)
        for k in ("ok", "op", "project", "snapshot", "built_at", "stale", "stale_reason", "chars"):
            assert k in r, (k, r)
        assert len(raw) <= (mc or 1500)
        assert r["chars"] == len(raw)
        assert "not_indexed" in r and "locators" in r and "confidence" in r
        if r["confidence"] == "low":
            assert "fallback_hint" in r
        for l in r["locators"]:
            for k in ("symbol", "kind", "file", "line", "community", "degree", "score"):
                assert k in l
    for op, kw in (("stats", {}), ("expand", {"symbol": "compute_invoice_total()"}),
                   ("path", {"source": "run_checkout()", "target": "compute_invoice_total()"})):
        r, raw, _ = env["a"].call(op=op, project=PID, **kw)
        assert r["ok"] and len(raw) <= 1500, (op, r)


def test_fixture_root_untouched(env):
    assert listing(env["root"]) == env["before"]
    assert not (env["root"] / "graphify-out").exists()


def test_network_guard_blocks_connect(env):
    h, _, _ = env["a"].call(op="health")
    assert h["net_guard"] is True and h["graphify"] == "0.9.77"
    code = ("import sys; sys.path.insert(0, r'%s'); import gfy_adapter, socket\n"
            "s=socket.socket()\n"
            "try:\n    s.connect(('127.0.0.1',9)); print('CONNECTED')\n"
            "except RuntimeError as e: print('BLOCKED', e)\n" % HERE)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "BLOCKED" in p.stdout, p


def test_project_allowlist(env):
    a = env["a"]
    r, _, _ = a.call(op="locate", project="not-listed", query="x")
    assert r["ok"] is False and r["error"] == "project_not_allowed"
    r, _, _ = a.call(op="locate", project=str(env["out"]), query="x")  # a real path that is not a root
    assert r["ok"] is False
    r, _, _ = a.call(op="locate", project=str(env["root"]), query="compute_invoice_total")  # root path maps to id
    assert r["ok"] is True and r["project"] == PID


def test_out_dir_must_be_outside_root(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = tmp_path / "p.json"
    cfg.write_text(json.dumps({"bad": {"root": str(root), "out_dir": str(root / "out")},
                               "rel": {"root": str(root), "out_dir": "relative/out"}}))
    a = Adapter(projects=str(cfg))
    try:
        r, _, _ = a.call(op="locate", project="bad", query="x")
        assert r["ok"] is False and r["error"] == "project_not_allowed"
        r, _, _ = a.call(op="locate", project="rel", query="x")
        assert r["ok"] is False
    finally:
        a.close()


def test_corrupt_work_graph_keeps_last_good(env):
    a, out = env["a"], env["out"]
    good = a.call(op="stats", project=PID)[0]
    (out / "work" / "graph.json").write_text("{ not json", encoding="utf-8")
    r, _, _ = a.call(op="refresh", project=PID, skip_build=True)
    assert r["ok"] is False and r["error"] == "refresh_failed"
    after = a.call(op="locate", project=PID, query="compute_invoice_total")[0]
    assert after["ok"] and after["snapshot"] == good["snapshot"]
    # restore a valid work graph for later tests
    r, _, _ = a.call(op="refresh", project=PID)
    assert r["ok"]


def test_stale_flips_after_touch(env):
    a, root = env["a"], env["root"]
    assert a.call(op="stats", project=PID)[0]["stale"] is False
    with open(root / "util.py", "a", encoding="utf-8") as f:
        f.write("# touched\n")
    s = a.call(op="stats", project=PID)[0]
    assert s["stale"] is True and "util.py" in s["stale_reason"]
    r = a.call(op="refresh", project=PID)[0]
    assert r["ok"] and r["stale"] is False
    assert a.call(op="stats", project=PID)[0]["stale"] is False


# ---------------------------------------------------------------- hardening tests
def make_project(tmp_path, git=False, name="p"):
    root = tmp_path / f"{name}_repo"
    root.mkdir()
    for n, t in FIX.items():
        (root / n).write_text(t, encoding="utf-8")
    out = tmp_path / f"{name}_out"
    cfg = tmp_path / f"{name}.json"
    cfg.write_text(json.dumps({PID: {"root": str(root), "out_dir": str(out)}}), encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root, out, str(cfg)


def test_hardening_a_child_network_guard(tmp_path):
    import gfy_adapter
    env = gfy_adapter.child_env()
    assert env["PYTHONPATH"].split(os.pathsep)[0] == gfy_adapter.GUARD_DIR
    code = ("import socket\ns=socket.socket()\n"
            "try:\n    s.connect(('127.0.0.1',9)); print('CONNECTED')\n"
            "except RuntimeError as e: print('BLOCKED', e)\n")
    p = subprocess.run([PY, "-c", code], env=env, capture_output=True, text=True)
    assert "BLOCKED" in p.stdout, p
    # multiprocessing worker (spawn) inherits the guard through PYTHONPATH
    mp = tmp_path / "mp_probe.py"
    mp.write_text("import multiprocessing as m, socket\n"
                  "def w():\n"
                  "    s=socket.socket()\n"
                  "    try:\n        s.connect(('127.0.0.1',9)); return 'CONNECTED'\n"
                  "    except RuntimeError as e: return 'BLOCKED'\n"
                  "if __name__=='__main__':\n"
                  "    with m.Pool(1) as p: print(p.apply(w))\n")
    p = subprocess.run([PY, str(mp)], env=env, capture_output=True, text=True, cwd=str(tmp_path))
    assert "BLOCKED" in p.stdout, p
    # the refresh itself still works under the guard
    root, out, cfg = make_project(tmp_path, name="g")
    a = Adapter(projects=cfg)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
    finally:
        a.close()


def test_hardening_b_reader_falls_back_to_prev(tmp_path):
    root, out, cfg = make_project(tmp_path, name="b")
    a = Adapter(projects=cfg)
    try:
        r1 = a.call(op="refresh", project=PID)[0]
        assert r1["ok"]
        (root / "util.py").write_text(FIX["util.py"] + "\n\ndef brand_new_helper():\n    return 1\n", encoding="utf-8")
        r2 = a.call(op="refresh", project=PID)[0]
        assert r2["ok"] and r2["snapshot"] != r1["snapshot"]
    finally:
        a.close()
    ptr = json.loads((out / "published" / "current.json").read_text())
    assert ptr["prev"] == r1["snapshot"]
    cur = out / "published" / f"graph-{ptr['snapshot']}.json"
    cur.write_text("corrupt", encoding="utf-8")          # fails the sha check
    a = Adapter(projects=cfg)                            # fresh process: nothing cached
    try:
        r = a.call(op="locate", project=PID, query="compute_invoice_total")[0]
        assert r["ok"] and r["served"] == "prev" and r["snapshot"] == r1["snapshot"]
        prev = out / "published" / f"graph-{r1['snapshot']}.json"
        prev.unlink()
        a2 = Adapter(projects=cfg)
        try:
            r = a2.call(op="locate", project=PID, query="compute_invoice_total")[0]
            assert r["ok"] is False and r["error"] == "snapshot_unavailable"
        finally:
            a2.close()
    finally:
        a.close()


@pytest.mark.parametrize("git", [False, True])
def test_hardening_c_new_file_flips_stale(tmp_path, git):
    root, out, cfg = make_project(tmp_path, git=git, name=f"c{int(git)}")
    a = Adapter(projects=cfg)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
        assert a.call(op="stats", project=PID)[0]["stale"] is False
        (root / "newmod.py").write_text("def added_later():\n    return 1\n", encoding="utf-8")
        s = a.call(op="stats", project=PID)[0]
        assert s["stale"] is True and "newmod.py" in s["stale_reason"] and "new_files" in s["stale_reason"], s
        (root / "notes.txt").write_text("x")  # non-code files must not matter
        assert a.call(op="refresh", project=PID)[0]["stale"] is False
        assert a.call(op="stats", project=PID)[0]["stale"] is False
    finally:
        a.close()


def test_hardening_d_semantic_snapshot_id(tmp_path):
    ids = []
    for name in ("d1", "d2"):
        root, out, cfg = make_project(tmp_path, name=name)
        a = Adapter(projects=cfg)
        try:
            r = a.call(op="refresh", project=PID)[0]
            assert r["ok"]
            ids.append(r["snapshot"])
        finally:
            a.close()
    assert ids[0] == ids[1]
    (root / "util.py").write_text(FIX["util.py"].replace("normalize_label", "normalize_title"), encoding="utf-8")
    a = Adapter(projects=cfg)
    try:
        r = a.call(op="refresh", project=PID)[0]
        assert r["ok"] and r["snapshot"] != ids[1] and r["graph_changed"] is True
    finally:
        a.close()

def test_staleness_ttl_cache(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("GFY_STALE_TTL_S", "1.0")
    root, out, cfg = make_project(tmp_path, name="ttl")
    a = Adapter(projects=cfg)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
        s1 = a.call(op="stats", project=PID)[0]
        c1 = s1["stale_cache"]
        s2 = a.call(op="stats", project=PID)[0]
        c2 = s2["stale_cache"]
        assert c2["hits"] == c1["hits"] + 1 and c2["computes"] == c1["computes"], (c1, c2)   # hit within TTL
        with open(root / "util.py", "a", encoding="utf-8") as f:
            f.write("# touched\n")
        s3 = a.call(op="stats", project=PID)[0]
        assert s3["stale"] is False and s3["stale_cache"]["hits"] == c2["hits"] + 1          # still cached inside TTL
        time.sleep(1.2)
        s4 = a.call(op="stats", project=PID)[0]
        assert s4["stale"] is True and s4["stale_cache"]["computes"] == c2["computes"] + 1    # flips once TTL expired
        # refresh invalidates: next stats must recompute (not hit) and report fresh
        assert a.call(op="refresh", project=PID)[0]["ok"]
        s5 = a.call(op="stats", project=PID)[0]
        assert s5["stale"] is False and s5["stale_cache"]["computes"] > s4["stale_cache"]["computes"]
    finally:
        a.close()


def test_staleness_ttl_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("GFY_STALE_TTL_S", "0")
    root, out, cfg = make_project(tmp_path, name="ttl0")
    a = Adapter(projects=cfg)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
        c1 = a.call(op="stats", project=PID)[0]["stale_cache"]
        c2 = a.call(op="stats", project=PID)[0]["stale_cache"]
        assert c2["hits"] == 0 and c2["computes"] == c1["computes"] + 1
    finally:
        a.close()

UNI_SRC = ('def arrow_mapper(x):\n    """Maps input → output … with a long label tail for truncation."""\n    return x\n')


def _raw_env():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONHASHSEED"] = "0"
    return env


def _raw_run(cmd, cfg, reqs, env):
    stdin = "".join(json.dumps(r) + "\n" for r in reqs).encode("utf-8")
    return subprocess.run(cmd + ["--projects", cfg], input=stdin, capture_output=True, env=env, timeout=120)


def _unicode_project(tmp_path):
    root, out, cfg = make_project(tmp_path, name="u")
    (root / "uni.py").write_text(UNI_SRC, encoding="utf-8")
    a = Adapter(projects=cfg)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
    finally:
        a.close()
    return cfg


def test_protocol_stdout_is_utf8_without_env(tmp_path):
    """Windows pipes default to cp1252; the protocol channel must still be UTF-8 bytes."""
    cfg = _unicode_project(tmp_path)
    env = _raw_env()
    assert "PYTHONIOENCODING" not in env and "PYTHONUTF8" not in env
    p = _raw_run([PY, "-B", os.path.join(HERE, "gfy_adapter.py")], cfg,
                 [{"id": 1, "op": "locate", "project": PID, "query": "maps input output", "limit": 8, "max_chars": 6000}], env)
    raw = p.stdout
    text = raw.decode("utf-8")  # must not raise
    line = text.splitlines()[0]
    obj = json.loads(line)
    assert obj["ok"] and obj["id"] == 1
    assert "…" in line or "→" in line, "fixture must exercise non-ASCII output"
    assert b"\r" not in raw


DRIVER_SRC = r'''
import sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
marker = "def handle(projects, req: dict) -> str:\n"
assert marker in src
hook = "    print('STRAY_PRINT')\n    import os as _o; _o.write(1, b'STRAY_FD\\n')\n"
src = src.replace(marker, marker + hook, 1)
sys.argv = [path] + sys.argv[2:]
exec(compile(src, path, "exec"), {"__name__": "__main__", "__file__": path})
'''


def test_stray_print_goes_to_stderr_not_protocol(tmp_path):
    """Driver execs the adapter source as __main__ with handle() patched to print to sys.stdout and write to fd 1."""
    cfg = _unicode_project(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER_SRC, encoding="utf-8")
    p = _raw_run([PY, "-B", str(driver), os.path.join(HERE, "gfy_adapter.py")], cfg,
                 [{"id": 7, "op": "health"}], _raw_env())
    out = p.stdout.decode("utf-8")
    assert "STRAY" not in out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["id"] == 7, p.stderr.decode("utf-8", "replace")[-600:]
    err = p.stderr.decode("utf-8", "replace")
    assert "STRAY_PRINT" in err and "STRAY_FD" in err


# ---------------------------------------------------------------- WP-CGN-1: literal index, find_text, context_for
LIT_FILES = {
    "svc.py": (
        'MODULE_MSG = "module level literal text here"\n\n\n'
        "class Checker:\n"
        "    def validate(self, changed):\n"
        "        return f\"out-of-scope file reported: {changed['path']}\"\n\n"
        "    def outer(self):\n"
        "        def inner():\n"
        '            return "inner function literal message"\n'
        "        return inner\n\n\n"
        "def top_func():\n"
        '    return "top level function error string"\n'
    ),
    "scripts/run.ps1": 'Write-Host "powershell literal message here"\nfunction Get-Thing { return 1 }\n',
    "tool.js": "const m = 'javascript literal message text';\nfunction doThing() { return m; }\n",
    "lib.py": "def helper_value():\n    return 41\n",
    "app2.py": ("import importlib\nimport lib\nimport os\n\n\n"
                "def load(name):\n    return importlib.import_module(name), lib.helper_value()\n"),
    "tests/conftest.py": ("import pytest\n\n\n@pytest.fixture\ndef sample_cfg():\n    return {}\n\n\n"
                          "@pytest.fixture\ndef unused_cfg():\n    return 1\n"),
    "tests/sub/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef deep_cfg():\n    return 2\n",
    "tests/sub/test_app2.py": ("import app2\n\n\ndef test_load(sample_cfg, deep_cfg):\n"
                               "    assert app2.lib.helper_value() == 41\n"),
}


def make_lit_project(tmp_path, name="L"):
    root = tmp_path / f"{name}_repo"
    for rel, t in LIT_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t, encoding="utf-8")
    out = tmp_path / f"{name}_out"
    cfg = tmp_path / f"{name}.json"
    cfg.write_text(json.dumps({PID: {"root": str(root), "out_dir": str(out)}}), encoding="utf-8")
    return root, out, str(cfg)


@pytest.fixture(scope="module")
def lit(tmp_path_factory):
    root, out, cfg = make_lit_project(tmp_path_factory.mktemp("lit"))
    a = Adapter(projects=cfg)
    r = a.call(op="refresh", project=PID)[0]
    assert r["ok"], r
    yield {"a": a, "root": root, "out": out, "cfg": cfg, "refresh": r}
    a.close()


def test_literal_refresh_publishes_one_generation(lit):
    ptr = json.loads((lit["out"] / "published" / "current.json").read_text())
    d = ptr["literals"]
    assert (lit["out"] / "published" / d["name"]).exists() and d["count"] > 3
    assert lit["refresh"]["timing"]["literals_s"] >= 0 and lit["refresh"]["literals"] == d["count"]


def test_fstring_matches_by_constant_part(lit):
    r, raw, _ = lit["a"].find_text(PID, "out-of-scope file reported")
    assert r["ok"] and r["hits"] == 1 and len(raw) <= 1500
    m = r["matches"][0]
    assert m["file"] == "svc.py" and m["symbol"] == "Checker.validate" and m["owner"] == "ast"
    assert m["lang"] == "python" and m["stale"] is False and m["line"] == 6
    assert "not_indexed" in r and r["coverage"]["literal_count"] > 3 and r["omitted"] == 0
    # match is against the SOURCE text as written (braces/quotes included), case-sensitive
    r2 = lit["a"].find_text(PID, "reported: {changed['path']}")[0]
    assert r2["ok"] and r2["hits"] == 1
    assert lit["a"].find_text(PID, "OUT-OF-SCOPE file")[0]["hits"] == 0


def test_nested_qualified_owner_and_module_level(lit):
    a = lit["a"]
    m = a.find_text(PID, "inner function literal")[0]["matches"][0]
    assert m["symbol"] == "Checker.outer.inner" and m["owner"] == "ast"
    m = a.find_text(PID, "top level function error")[0]["matches"][0]
    assert m["symbol"] == "top_func" and m.get("in_graph") is True
    m = a.find_text(PID, "module level literal")[0]["matches"][0]
    assert m["symbol"] is None and m["owner"] == "ast" and m["line"] == 1


def test_non_python_literals_are_textual_candidates(lit):
    a = lit["a"]
    for text, lang in (("powershell literal message", "ps1"), ("javascript literal message", "js")):
        r = a.find_text(PID, text)[0]
        assert r["ok"] and r["hits"] == 1, (text, r)
        m = r["matches"][0]
        assert m["owner"] == "unknown" and m["symbol"] is None and m["lang"] == lang


def test_find_text_arg_validation_and_limits(lit):
    a = lit["a"]
    assert a.find_text(PID, "ab")[0]["error"] == "text_length_3_to_200"
    assert a.find_text(PID, "x" * 201)[0]["error"] == "text_length_3_to_200"
    r = a.find_text(PID, "literal", limit=1)[0]
    assert r["ok"] and r["hits"] == 1 and r["omitted"] >= 1


def test_stale_flag_flips_after_edit(tmp_path):
    root, out, cfg = make_lit_project(tmp_path, "st")
    a = Adapter(projects=cfg)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
        assert a.find_text(PID, "top level function")[0]["matches"][0]["stale"] is False
        with open(root / "svc.py", "a", encoding="utf-8") as f:
            f.write("# edited\n")
        r = a.find_text(PID, "top level function")[0]
        assert r["matches"][0]["stale"] is True and r["stale"] is True
        assert a.call(op="refresh", project=PID)[0]["ok"]
        assert a.find_text(PID, "top level function")[0]["matches"][0]["stale"] is False
    finally:
        a.close()


def _inproc_project(cfg):
    import pathlib
    import gfy_adapter
    return gfy_adapter, gfy_adapter.load_projects(pathlib.Path(cfg))[PID]


def test_source_change_during_build_is_rejected_keeps_last_good(tmp_path, monkeypatch):
    root, out, cfg = make_lit_project(tmp_path, "mid")
    ga, proj = _inproc_project(cfg)
    r1 = json.loads(ga.op_refresh(proj, {}))
    assert r1["ok"], r1
    good = (out / "published" / "current.json").read_text()
    real_run = ga.subprocess.run

    def run_and_mutate(cmd, *a, **kw):
        res = real_run(cmd, *a, **kw)
        if isinstance(cmd, list) and "graphify" in cmd:
            (root / "svc.py").write_text(LIT_FILES["svc.py"] + "\n\ndef sneaky():\n    return 'edited during the build'\n",
                                         encoding="utf-8")
        return res
    monkeypatch.setattr(ga.subprocess, "run", run_and_mutate)
    r2 = json.loads(ga.op_refresh(proj, {}))
    assert r2["ok"] is False and r2["error"] == "refresh_failed" and "source_changed_during_build" in r2["detail"], r2
    assert r2["serving"] == "last-good"
    assert (out / "published" / "current.json").read_text() == good
    q = json.loads(ga.handle({PID: proj}, {"op": "find_text", "project": PID, "text": "top level function"}))
    assert q["ok"] and q["hits"] == 1


def test_graph_and_literals_stay_one_generation(tmp_path):
    root, out, cfg = make_lit_project(tmp_path, "gen")
    a = Adapter(projects=cfg)
    try:
        r1 = a.call(op="refresh", project=PID)[0]
        (root / "svc.py").write_text(LIT_FILES["svc.py"].replace("top level function error string", "brand new top level text")
                                     + "\n\ndef added_fn():\n    return 1\n", encoding="utf-8")
        r2 = a.call(op="refresh", project=PID)[0]
        assert r1["ok"] and r2["ok"] and r1["snapshot"] != r2["snapshot"]
    finally:
        a.close()
    pub = out / "published"
    ptr = json.loads((pub / "current.json").read_text())
    assert ptr["prev_literals"]["name"] != ptr["literals"]["name"]
    # simulate a partial publish: the current literal artifact is gone -> whole generation falls back to prev
    (pub / ptr["literals"]["name"]).unlink()
    a = Adapter(projects=cfg)
    try:
        r = a.find_text(PID, "top level function error")[0]
        assert r["ok"] and r["served"] == "prev" and r["snapshot"] == r1["snapshot"] and r["hits"] == 1
        assert a.find_text(PID, "brand new top level")[0]["hits"] == 0   # never mix new literals with the old graph
        loc = a.call(op="locate", project=PID, query="added_fn")[0]
        assert loc["served"] == "prev" and loc["snapshot"] == r1["snapshot"]
    finally:
        a.close()


def test_old_generation_without_literals_still_serves(tmp_path):
    root, out, cfg = make_lit_project(tmp_path, "old")
    a = Adapter(projects=cfg)
    try:
        r = a.call(op="refresh", project=PID, no_literals=True)[0]
        assert r["ok"] and r["literals"] is None
        f = a.find_text(PID, "top level function")[0]
        assert f["ok"] is False and f["error"] == "literal_index_unavailable" and f["hits"] == 0
        assert a.call(op="locate", project=PID, query="top_func")[0]["ok"]
        assert a.call(op="stats", project=PID)[0]["ok"]
        assert a.call(op="expand", project=PID, symbol="top_func()")[0]["ok"]
        q = a.call(op="locate", project=PID, query='"top level function"')[0]   # quoted + no index: still answers
        assert q["ok"] and q["literal_index"] == "unavailable"
        # a refresh with literals upgrades the same pointer in place
        assert a.call(op="refresh", project=PID)[0]["literals"] > 0
        assert a.find_text(PID, "top level function")[0]["ok"]
    finally:
        a.close()


def _strip(r):
    r = dict(r)
    for k in ("chars", "hits", "stale_reason", "id", "literal_index", "built_at"):
        r.pop(k, None)
    return r


def test_quoted_locate_priority_and_unquoted_unchanged(tmp_path):
    ra, oa, ca = make_lit_project(tmp_path, "qa")
    rb, ob, cb = make_lit_project(tmp_path, "qb")
    a, b = Adapter(projects=ca), Adapter(projects=cb)
    try:
        assert a.call(op="refresh", project=PID)[0]["ok"]
        assert b.call(op="refresh", project=PID, no_literals=True)[0]["ok"]
        for q in ("top_func", "checker validate changed", "inner function literal message", "helper value lib"):
            ra_, rb_ = a.call(op="locate", project=PID, query=q)[0], b.call(op="locate", project=PID, query=q)[0]
            assert _strip(ra_) == _strip(rb_), q          # unquoted ranking identical with/without the index
            assert all(l["kind"] != "literal" for l in ra_["locators"])
        r = a.call(op="locate", project=PID, query='where is "out-of-scope file reported" raised')[0]
        first = r["locators"][0]
        assert first["kind"] == "literal" and first["file"] == "svc.py" and first["symbol"] == "Checker.validate"
        assert r["hits"] == len(r["locators"]) and len(r["locators"]) > 1   # symbol ranking still follows
        assert r["locators"][1]["kind"] != "literal"
    finally:
        a.close()
        b.close()


def test_context_for_test_file(lit):
    r, raw, _ = lit["a"].context_for(PID, ["tests/sub/test_app2.py"])
    assert r["ok"] and len(raw) <= 1500 and "suggestions only" in r["note"]
    by = {s["file"]: s for s in r["suggestions"]}
    assert "tests/sub/test_app2.py" not in by                        # declared file excluded
    assert by["app2.py"]["reason"] == "import" and by["app2.py"]["confidence"] == "high"
    assert by["tests/sub/conftest.py"]["reason"].startswith("conftest+fixture:") and "deep_cfg" in by["tests/sub/conftest.py"]["reason"]
    assert "sample_cfg" in by["tests/conftest.py"]["reason"] and "unused_cfg" not in by["tests/conftest.py"]["reason"]
    assert r["hits"] == len(r["suggestions"]) and r["bytes_total"] == sum(s["bytes"] for s in r["suggestions"])
    assert all(set(s) == {"file", "reason", "from", "confidence", "bytes", "stale"} for s in r["suggestions"])
    ranks = [s["file"] for s in r["suggestions"]]
    assert ranks.index("app2.py") < ranks.index("tests/sub/conftest.py")   # import before conftest


def test_context_for_source_file_dynamic_and_referencing_test(lit):
    r = lit["a"].context_for(PID, ["app2.py"])[0]
    assert r["ok"]
    by = {s["file"]: s for s in r["suggestions"]}
    assert by["lib.py"]["reason"] == "import" and "app2.py" not in by
    assert any(u["why"] == "dynamic_import" for u in r["unresolved"])
    t = by["tests/sub/test_app2.py"]
    assert t["confidence"] == "low" and r["suggestions"][-1]["file"] == t["file"]   # referencing test: low, last


def test_context_for_cap_limits_and_errors(lit):
    a = lit["a"]
    r = a.context_for(PID, ["tests/sub/test_app2.py"], limit=1)[0]
    assert r["hits"] == 1 and r["omitted"] >= 1
    r = a.context_for(PID, ["../escape.py", "svc.ps1", "missing.py"])[0]
    assert r["ok"] and r["hits"] == 0 and {u["why"] for u in r["unresolved"]} == {"outside_project_root", "python_only", "file_not_found"}
    assert a.context_for(PID, [])[0]["error"] == "files_1_to_5"
    assert a.context_for(PID, [f"f{i}.py" for i in range(6)])[0]["error"] == "files_1_to_5"


def test_context_for_bytes_cap(lit, monkeypatch):
    import gfy_adapter
    monkeypatch.setattr(gfy_adapter, "CTX_MAX_BYTES", 100)
    ga, proj = _inproc_project(lit["cfg"])
    r = json.loads(ga.handle({PID: proj}, {"op": "context_for", "project": PID, "files": ["tests/sub/test_app2.py"]}))
    assert r["bytes_total"] <= 100 and r["omitted"] >= 1


def test_every_response_within_budget_and_has_hits(lit):
    a = lit["a"]
    calls = [dict(op="find_text", text="literal"), dict(op="find_text", text="literal", limit=10, max_chars=500),
             dict(op="context_for", files=["tests/sub/test_app2.py"], max_chars=400),
             dict(op="context_for", files=["app2.py", "svc.py"], max_chars=600),
             dict(op="locate", query='"top level function error" top_func', max_chars=600),
             dict(op="locate", query="top_func"), dict(op="expand", symbol="top_func()"),
             dict(op="path", source="load()", target="helper_value()"), dict(op="stats"),
             dict(op="find_text", text="zz"), dict(op="find_text", text="no such literal anywhere")]
    for c in calls:
        mc = c.get("max_chars", 1500)
        r, raw, _ = a.call(project=PID, **c)
        assert len(raw) <= mc, (c, len(raw))
        assert isinstance(r["hits"], int) and r["chars"] == len(raw), (c, r)
    h = a.call(op="health")[0]
    assert h["hits"] == 0
