"""Live regression test: Switchboard is mapped by its own code graph (graphify via the code_graph bridge).

Gated: skips with an explicit reason unless the code graph is installed, project `agent-broker` is registered
and its root is THIS checkout. Uses only code_graph_bridge + stdlib. Writes nothing inside the repo.
Add a feature -> add one question to tests/code_graph_questions.json (see its "_how_to_extend").
"""
import ast
import collections
import json
import os
import sys
import unittest  # noqa: F401  (kept for parity with sibling tests)

import pytest

REPO = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
import code_graph_bridge  # noqa: E402

PROJECT = "agent-broker"
QUESTIONS_PATH = os.path.join(REPO, "tests", "code_graph_questions.json")


def _gate():
    if os.environ.get("AGENT_BROKER_SKIP_CODE_GRAPH_LIVE") == "1":
        pytest.skip("AGENT_BROKER_SKIP_CODE_GRAPH_LIVE=1", allow_module_level=True)
    h = code_graph_bridge.call({"op": "health"})
    if h.get("error") == "code_graph_not_installed":
        pytest.skip("code graph not installed (code_graph_not_installed)", allow_module_level=True)
    if not h.get("ok"):
        pytest.skip(f"code graph health failed: {h.get('error')}", allow_module_level=True)
    if PROJECT not in (h.get("projects") or {}):
        pytest.skip(f"project {PROJECT!r} is not registered in the code graph", allow_module_level=True)
    root = (h.get("roots") or {}).get(PROJECT)
    if not root:
        pytest.skip("code graph health does not report project roots (adapter too old)", allow_module_level=True)
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(REPO):
        pytest.skip(f"registered root {root!r} is not this checkout {REPO!r} (e.g. a staging copy)", allow_module_level=True)


_gate()


@pytest.fixture(scope="module", autouse=True)
def graph():
    r = code_graph_bridge.call({"op": "refresh", "project": PROJECT})
    assert r.get("ok"), f"code graph refresh failed: {r}"
    yield r
    code_graph_bridge.reset_bridge()


def _questions():
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)["questions"]


def _defined_names(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _locate(query, **kw):
    r = code_graph_bridge.call({"op": "locate", "project": PROJECT, "query": query, **kw})
    assert r.get("ok"), f"locate failed for {query!r}: {r}"
    return r


def _score(resp, q):
    syms = {q["symbol"], *q.get("alt_symbols", [])}
    locs = resp.get("locators", [])
    if any(l.get("file") == q["file"] and l.get("symbol") in syms for l in locs):
        return "HIT"
    if any(l.get("file") == q["file"] for l in locs) or any(f"@{q['file']}:" in m for m in resp.get("more", [])):
        return "PARTIAL"
    return "MISS"


def test_ground_truth_symbols_exist():
    bad = []
    for q in _questions():
        path = os.path.join(REPO, q["file"])
        if not os.path.isfile(path):
            bad.append(f"{q['id']}: file {q['file']} no longer exists")
            continue
        names = _defined_names(path)
        if not ({q["symbol"], *q.get("alt_symbols", [])} & names):
            bad.append(f"{q['id']}: none of {[q['symbol'], *q.get('alt_symbols', [])]} is defined in {q['file']}")
    assert not bad, "Ground truth is stale - update tests/code_graph_questions.json:\n" + "\n".join(bad)


def test_question_bar():
    qs = _questions()
    rows, over = [], []
    for q in qs:
        r = _locate(q["question"])
        if r["chars"] > 1500 or len(json.dumps(r, ensure_ascii=False, separators=(",", ":"))) > 1500:
            over.append(q["id"])
        rows.append((q, _score(r, q), r.get("confidence")))
    for q, s, c in rows:
        print(f"{q['id']:4}{q['kind']:11}{s:8}conf={c}{'  must_hit' if q.get('must_hit') else ''}")
    lex = [x for x in rows if x[0]["kind"] == "lexical"]
    hit = sum(1 for _, s, _ in lex if s == "HIT")
    hp = sum(1 for _, s, _ in lex if s in ("HIT", "PARTIAL"))
    print(f"lexical HIT {hit}/{len(lex)} HIT+PARTIAL {hp}/{len(lex)}; paraphrase (not gated): "
          f"{[(q['id'], s) for q, s, _ in rows if q['kind'] != 'lexical']}")
    missed = [q["id"] for q, s, _ in rows if q.get("must_hit") and s != "HIT"]
    assert not missed, f"must_hit questions no longer HIT: {missed}"
    assert hit / len(lex) >= 0.60, f"lexical HIT rate {hit}/{len(lex)} < 60%"
    assert hp / len(lex) >= 0.90, f"lexical HIT+PARTIAL {hp}/{len(lex)} < 90%"
    assert not over, f"responses over 1500 chars: {over}"


SKIP_DIRS = {".git", "__pycache__", "tests", "build", "dist", ".pytest_cache", ".agent-broker", "node_modules", "venv", ".venv"}


def _symbols():
    items = []
    for d, dirs, files in os.walk(REPO):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for n in files:
            if not n.endswith(".py") or n.startswith("test_"):
                continue
            rel = os.path.relpath(os.path.join(d, n), REPO).replace("\\", "/")
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    items.append((node.name, rel))
                elif isinstance(node, ast.ClassDef):
                    items.append((node.name, rel))
                    items += [(s.name, rel) for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return items


def test_symbol_coverage():
    items = _symbols()
    counts = collections.Counter(n for n, _ in items)
    dup = sorted(n for n, c in counts.items() if c >= 2)
    todo = [(n, f) for n, f in items if counts[n] == 1]
    print(f"symbols {len(items)}; excluded {len(dup)} names defined in 2+ places: {dup}")
    misses = []
    for name, rel in todo:
        r = _locate(name, limit=8)
        if not any(l.get("symbol") == name and l.get("file") == rel for l in r.get("locators", [])):
            misses.append(f"{name}@{rel}")
    cov = 100.0 * (len(todo) - len(misses)) / max(1, len(todo))
    print(f"coverage {cov:.2f}% ({len(todo) - len(misses)}/{len(todo)}); misses: {misses}")
    assert cov >= 99.0, f"code graph symbol coverage {cov:.2f}% < 99%; misses: {misses}"


def _literal_questions():
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        return json.load(f).get("literal_questions", [])


def _string_literals_in_symbol(path, symbol):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            out += [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return out


def test_literal_ground_truth_exists():
    qs = _literal_questions()
    assert qs, "tests/code_graph_questions.json has no literal_questions"
    bad = []
    for q in qs:
        path = os.path.join(REPO, q["file"])
        if not os.path.isfile(path):
            bad.append(f"{q['id']}: file {q['file']} no longer exists")
        elif not any(q["text"] in lit for lit in _string_literals_in_symbol(path, q["symbol"])):
            bad.append(f"{q['id']}: {q['text']!r} is not inside a string literal in {q['symbol']} of {q['file']}")
    assert not bad, "Literal ground truth is stale - update literal_questions in tests/code_graph_questions.json:\n" + "\n".join(bad)


def test_find_text_known_messages():
    bad = []
    for q in _literal_questions():
        r = code_graph_bridge.call({"op": "find_text", "project": PROJECT, "text": q["text"]})
        assert r.get("ok"), f"find_text failed for {q['id']}: {r}"
        assert isinstance(r.get("hits"), int), f"{q['id']}: response lacks integer hits"
        size = len(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
        if r.get("chars", 0) > 1500 or size > 1500:
            bad.append(f"{q['id']}: response {size} chars (reported {r.get('chars')}) > 1500")
        if not any(m.get("file") == q["file"] and m.get("symbol") == q["symbol"] and m.get("owner") == "ast"
                   for m in r.get("matches", [])):
            bad.append(f"{q['id']}: no ast-owned match for {q['symbol']}@{q['file']}")
    assert not bad, "find_text regression:\n" + "\n".join(bad)


def _local_imports(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {m + ".py" for m in mods if os.path.isfile(os.path.join(REPO, m + ".py"))}


def test_context_for_imports():
    declared = "routing_gate.py"
    real = _local_imports(os.path.join(REPO, declared))
    assert real, "routing_gate.py has no local imports any more - pick another declared file"
    r = code_graph_bridge.call({"op": "context_for", "project": PROJECT, "files": [declared]})
    assert r.get("ok"), f"context_for failed: {r}"
    sug = r.get("suggestions", [])
    assert len(sug) <= 5, f"more than 5 suggestions: {len(sug)}"
    assert all(s.get("file") != declared for s in sug), "suggestions include the declared file"
    assert any(s.get("file") in real and s.get("reason") == "import" for s in sug), (
        f"no real local import of {declared} ({sorted(real)}) suggested with reason import: {sug}")
    assert isinstance(r.get("hits"), int)


def test_bridge_adapter_comes_from_release_marker():
    home, _py, adapter, _pj = code_graph_bridge.get_bridge()._resolve_paths()
    marker = home / "runtime.json"
    if not marker.exists():
        pytest.skip("no runtime.json marker (legacy install)")
    release = json.loads(marker.read_text(encoding="utf-8"))["release"]
    assert adapter == home / "releases" / release / "gfy_adapter.py"
