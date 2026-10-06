"""gfy_adapter - thin graphify locator adapter (pilot, outside Switchboard).

Long-lived stdio server: one JSON object per line in, one per line out.
Run with the pilot venv python:  python gfy_adapter.py [--projects PATH]
See ADAPTER_README.md for the protocol and the graphify internals relied upon.
"""
from __future__ import annotations

import ast
import bisect
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- private UTF-8 protocol channel
_PROTO = None


def _isolate_stdio() -> None:
    """Make the protocol channel private and UTF-8 (Windows pipes default to cp1252).
    Dup fd 1 into a dedicated stream, then point fd 1 / sys.stdout at stderr so stray prints
    from graphify or any library cannot corrupt the protocol."""
    global _PROTO
    _PROTO = open(os.dup(1), "w", encoding="utf-8", newline="\n", buffering=1, closefd=True)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    _isolate_stdio()

PINNED_VERSION = "0.9.77"
HERE = Path(__file__).resolve().parent
DEFAULT_MAX_CHARS = 1500
LOCK_STALE_S = 900
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY")


# ---------------------------------------------------------------- version pin
def assert_version() -> str:
    v = importlib.metadata.version("graphifyy")  # graphify has no __version__ attribute
    if v != PINNED_VERSION:
        raise SystemExit(f"gfy_adapter: graphifyy=={v}, expected {PINNED_VERSION}")
    return v


VERSION = assert_version()

# ---------------------------------------------------------------- network guard
_GUARDED = {"socket.connect", "socket.getaddrinfo", "urllib.Request"}


def _net_guard(event, args):
    if event in _GUARDED:
        raise RuntimeError(f"gfy_adapter network guard blocked {event}")


def install_net_guard() -> None:
    if not getattr(sys, "_gfy_guard", False):
        sys._gfy_guard = True
        sys.addaudithook(_net_guard)


def net_guard_selfcheck() -> bool:
    """True if a connect attempt is blocked by the guard (never reaches the network)."""
    try:
        s = socket.socket()
        try:
            s.connect(("127.0.0.1", 9))
        finally:
            s.close()
    except RuntimeError as e:
        return "network guard" in str(e)
    except Exception:
        return False
    return False


install_net_guard()

# ---------------------------------------------------------------- graphify internals
from graphify import serve as S  # noqa: E402  (after guard so import-time network is blocked too)
from graphify import detect as S_detect  # noqa: E402

GUARD_DIR = str(HERE / "guard")


def child_env(extra=None) -> dict:
    """Environment for the graphify refresh subprocess: keys removed, proxy backstop, and the guard
    sitecustomize FIRST on PYTHONPATH so every child Python (incl. multiprocessing workers) blocks network.
    Non-Python children (git, cmd) are not covered."""
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = GUARD_DIR + (os.pathsep + pp if pp else "")
    env.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"})
    env.update(extra or {})
    return env


# ---------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def semantic_id(path: Path) -> str:
    """Hash of a canonical sorted form of nodes (id,label,source_file,source_location) and
    edges (source,target,relation,confidence) - independent of file bytes, key order and run metadata."""
    d = json.loads(path.read_text(encoding="utf-8"))
    nodes = sorted((str(n.get("id")), str(n.get("label")), str(n.get("source_file")), str(n.get("source_location")))
                   for n in d.get("nodes", []))
    edges = sorted((str(e.get("source")), str(e.get("target")), str(e.get("relation")), str(e.get("confidence")))
                   for e in d.get("links", d.get("edges", [])))
    return hashlib.sha256(json.dumps([nodes, edges], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gfy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _inside(child: str, parent: str) -> bool:
    c, p = os.path.normcase(child), os.path.normcase(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def _h16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- project registry
class Project:
    def __init__(self, pid: str, root: str, out_dir: str):
        self.id = pid
        self.root = os.path.realpath(root)
        if not os.path.isabs(out_dir):
            raise ValueError(f"{pid}: out_dir must be absolute")
        self.out_dir = os.path.realpath(out_dir)
        if _inside(self.out_dir, self.root) or _inside(self.root, self.out_dir):
            raise ValueError(f"{pid}: out_dir must be outside root (and not contain it)")
        self.work = Path(self.out_dir) / "work"
        self.published = Path(self.out_dir) / "published"
        self.lock = Path(self.out_dir) / "refresh.lock"
        self._G = None
        self._G_sha = None
        self._L = None          # literal index of the generation being served (None = absent/unavailable)
        self._L_key = None

    # --- pointer / snapshot
    def pointer(self):
        p = self.published / "current.json"
        for _ in range(2):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (json.JSONDecodeError, OSError):
                time.sleep(0.05)
        return None

    served = "current"
    _missing = "no_snapshot"

    def missing_code(self):
        return self._missing

    def _try_load(self, sem: str, file_sha: str):
        """Load graph-<sem12>.json if it exists and matches its recorded file sha, else None."""
        if self._G is not None and self._G_sha == sem:
            return self._G
        gp = self.published / f"graph-{sem[:12]}.json"
        try:
            if not gp.exists() or (file_sha and sha256_file(gp) != file_sha):
                return None
            G = S._load_graph(str(gp))
        except Exception:
            return None
        self._G, self._G_sha = G, sem
        return G

    def _load_lit(self, lit: dict, sem: str):
        """Load + verify a literal artifact named by the generation manifest, else None."""
        try:
            name = str(lit["name"])
            if os.path.basename(name) != name or not name.startswith("literals-"):
                return None
            key = (name, lit.get("file_sha"))
            if self._L is not None and self._L_key == key:
                return self._L
            lp = self.published / name
            if not lp.exists() or sha256_file(lp) != lit.get("file_sha"):
                return None
            d = json.loads(lp.read_text(encoding="utf-8"))
            if d.get("graph_sha") != sem or not isinstance(d.get("literals"), dict):
                return None
        except Exception:
            return None
        self._L, self._L_key = d, key
        return d

    def _try_gen(self, sem: str, file_sha: str, lit):
        """Load one generation = graph + (optional) literal artifact. Returns (G, L, ok).
        A manifest that declares literals whose artifact is missing/corrupt makes the WHOLE generation unusable."""
        G = self._try_load(sem, file_sha)
        if G is None:
            return None, None, False
        if not lit:  # migration: older generation without literals
            return G, None, True
        L = self._load_lit(lit, sem)
        if L is None:
            return None, None, False
        return G, L, True

    def graph(self):
        """Load ONLY a published generation (never work/). Order: current; re-read pointer once; then `prev`.
        A generation is the pair (graph, literals); self._L is set to the served generation's literals or None."""
        self._L = None if self._L_key is None else self._L
        ptr = self.pointer()
        if not ptr:
            self._missing = "no_snapshot"
            return None, None
        G, L, ok = self._try_gen(ptr["sha"], ptr.get("file_sha"), ptr.get("literals"))
        if not ok:  # transient (pointer swapped mid-read?) - re-read the pointer once
            ptr2 = self.pointer()
            if ptr2:
                ptr = ptr2
                G, L, ok = self._try_gen(ptr["sha"], ptr.get("file_sha"), ptr.get("literals"))
        if ok:
            self.served = "current"
            self.lits = L
            return G, ptr
        if ptr.get("prev_sha"):
            G, L, ok = self._try_gen(ptr["prev_sha"], ptr.get("prev_file_sha"), ptr.get("prev_literals"))
            if ok:
                self.served = "prev"
                self.lits = L
                pp = dict(ptr, sha=ptr["prev_sha"], file_sha=ptr.get("prev_file_sha"),
                          built_at=ptr.get("prev_built_at"), literals=ptr.get("prev_literals"))
                return G, pp
        self._missing = "snapshot_unavailable"
        self.lits = None
        return None, None

    lits = None

    def live_hash(self, rel: str):
        """Short sha256 of the live file, or None if unreadable."""
        try:
            with open(os.path.join(self.root, rel), "rb") as f:
                return _h16(f.read())
        except OSError:
            return None

    def code_files_now(self):
        """Code files the project would now contain, as posix relpaths (git when available, else a pruned scan)."""
        from graphify.detect import CODE_EXTENSIONS, _is_noise_dir
        exts = set(CODE_EXTENSIONS)
        out = set()
        if os.path.exists(os.path.join(self.root, ".git")):
            try:
                p = subprocess.run(["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"], cwd=self.root,
                                   capture_output=True, timeout=15)
                if p.returncode == 0:
                    for rel in p.stdout.decode("utf-8", "replace").split("\0"):
                        if rel and os.path.splitext(rel)[1] in exts and os.path.isfile(os.path.join(self.root, rel)):
                            out.add(rel.replace("\\", "/"))
                    return out
            except Exception:
                pass
        for d, dirs, files in os.walk(self.root):
            dirs[:] = [x for x in dirs if not _is_noise_dir(x, Path(d))]
            for n in files:
                if os.path.splitext(n)[1] in exts:
                    out.add(os.path.relpath(os.path.join(d, n), self.root).replace("\\", "/"))
        return out

    def new_files(self, known: set):
        cand = sorted(self.code_files_now() - known)
        if not cand:
            return []
        pred = S_detect.ignored_predicate(Path(self.root))  # graphify's own ignore rules (.gitignore/.graphifyignore/noise dirs)
        return [c for c in cand if not pred(Path(self.root) / c)]

    # --- fingerprint
    def _fp_files(self, ptr):
        p = self.published / f"graph-{ptr['sha'][:12]}.fp.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def compute_fingerprint(self, rels):
        entries = []
        for rel in sorted(rels):
            try:
                st = os.stat(os.path.join(self.root, rel))
                entries.append([rel, st.st_size, st.st_mtime_ns])
            except OSError:
                entries.append([rel, -1, -1])
        head = None
        if os.path.exists(os.path.join(self.root, ".git")):
            try:
                head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True,
                                      text=True, timeout=10).stdout.strip() or None
            except Exception:
                head = None
        return entries, head

    _scache = None
    cache_hits = 0
    cache_computes = 0

    def staleness(self, ptr):
        """TTL-cached wrapper (GFY_STALE_TTL_S, default 2.0, 0 disables). A refresh invalidates it."""
        try:
            ttl = float(os.environ.get("GFY_STALE_TTL_S", "2.0"))
        except ValueError:
            ttl = 2.0
        now = time.monotonic()
        c = self._scache
        if ttl > 0 and c and c[1] == ptr["sha"] and now - c[0] < ttl:
            self.cache_hits += 1
            return c[2]
        res = self._staleness_uncached(ptr)
        self.cache_computes += 1
        self._scache = (now, ptr["sha"], res) if ttl > 0 else None
        return res

    def _staleness_uncached(self, ptr):
        fp = self._fp_files(ptr)
        if not fp:
            return True, "no_fingerprint"
        cur, head = self.compute_fingerprint([e[0] for e in fp["files"]])
        old = {e[0]: (e[1], e[2]) for e in fp["files"]}
        changed = [e[0] for e in cur if old.get(e[0]) != (e[1], e[2])]
        if changed:
            missing = [e[0] for e in cur if e[1] == -1]
            why = f"files_changed:{len(changed)} first={changed[0]}"
            if missing:
                why += f" missing={len(missing)}"
            return True, why
        new = self.new_files(set(old))
        if new:
            return True, f"new_files:{len(new)} first={new[0]}"
        if head != fp.get("git_head"):
            return True, "git_head_changed"
        return False, None


def load_projects(path: Path) -> dict[str, Project]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for pid, v in cfg.items():
        try:
            out[pid] = Project(pid, v["root"], v["out_dir"])
        except Exception as e:  # invalid config entries are refused, not served
            sys.stderr.write(f"gfy_adapter: project {pid} refused: {e}\n")
    return out


# ---------------------------------------------------------------- response building
class Trim(Exception):
    pass


def envelope(op, proj, ptr, stale, why):
    return {"ok": True, "op": op, "project": proj.id if proj else None,
            "snapshot": ptr["sha"][:12] if ptr else None,
            "built_at": ptr.get("built_at") if ptr else None,
            "stale": stale, "stale_reason": why, "served": proj.served if proj else None, "chars": 0, "hits": 0}


def finalize(resp: dict, max_chars: int, trimmers=()) -> str:
    """Serialize; apply trimmers (callables dropping content, returning a label) until <= max_chars."""
    dropped = {}
    for _ in range(200):
        for _ in range(4):  # chars field is self-referential; iterate to a fixed point
            s = jdump(resp)
            if resp["chars"] == len(s):
                break
            resp["chars"] = len(s)
        s = jdump(resp)
        if len(s) <= max_chars:
            if dropped:
                resp["truncated"] = True
                resp["dropped"] = dropped
                for _ in range(4):
                    s = jdump(resp)
                    if resp["chars"] == len(s):
                        break
                    resp["chars"] = len(s)
                s = jdump(resp)
                if len(s) <= max_chars:
                    return s
            else:
                return s
        done = False
        for t in trimmers:
            lab = t()
            if lab:
                dropped[lab] = dropped.get(lab, 0) + 1
                done = True
                break
        if not done:
            break
    # last resort: minimal error-free stub that still fits
    stub = {k: resp.get(k) for k in ("ok", "op", "project", "snapshot", "built_at", "stale", "stale_reason", "hits")}
    stub.update({"truncated": True, "dropped": {"all": 1}, "chars": 0})
    for _ in range(4):
        s = jdump(stub)
        stub["chars"] = len(s)
    return jdump(stub)[:max_chars] if len(jdump(stub)) > max_chars else jdump(stub)


# ---------------------------------------------------------------- locator logic
NOT_INDEXED = "Constants/non-code files are not nodes; a miss is not absence; try find_text."


def _is_test(f: str) -> bool:
    f = (f or "").replace("\\", "/")
    return f.startswith("tests/") or "/tests/" in f or os.path.basename(f).startswith("test_")


def _bare(label: str) -> str:
    return (label or "").strip().lstrip(".").rstrip("()").lower() if label else ""


def _kind(d: dict) -> str:
    lab = d.get("label") or ""
    if d.get("file_type") == "document":
        return "doc"
    if lab.endswith("…"):
        return "docstring"
    if d.get("_callable_class"):
        return "class"
    if lab.endswith("()"):
        return "method" if lab.startswith(".") else "function"
    if lab == os.path.basename(d.get("source_file") or "") or lab.endswith((".py", ".js", ".mjs", ".ps1", ".json")):
        return "file"
    return "symbol"


def _line(d: dict):
    loc = str(d.get("source_location") or "")
    digits = "".join(c for c in loc.split("-")[0] if c.isdigit())
    return int(digits) if digits else None


def _tier(d: dict) -> int:
    """Presentation policy (NOT scoring): code symbols in non-test files first."""
    k = _kind(d)
    if k in ("doc", "docstring", "file"):
        return 2
    return 1 if _is_test(d.get("source_file")) else 0


def locator(G, nid: str, score: float, terms: list[str], with_id=False) -> dict:
    d = G.nodes[nid]
    lab = (d.get("label") or nid)
    toks = set(S._search_tokens(lab))
    mt = [t for t in terms if t in toks or t in lab.lower()][:3]
    out = {"symbol": lab.lstrip(".").rstrip("()") if lab.endswith("()") else lab.lstrip("."),
           "kind": _kind(d), "file": d.get("source_file"), "line": _line(d),
           "community": d.get("community"), "degree": G.degree(nid), "score": round(score, 1)}
    if mt:  # omitted when empty to save chars
        out["matched_terms"] = mt
    if with_id:
        out["id"] = nid
    return out


def short(loc: dict) -> str:
    return f"{loc['symbol']}@{loc['file']}:{loc['line']}"


def op_locate(proj: Project, req: dict) -> str:
    G, ptr = proj.graph()
    if G is None:
        return err("locate", proj, proj.missing_code(), req)
    query = str(req.get("query") or "")
    limit = max(1, min(int(req.get("limit") or 8), 20))
    max_chars = int(req.get("max_chars") or DEFAULT_MAX_CHARS)
    with_id = bool(req.get("include_ids"))
    phrases = quoted_phrases(query)          # quoted phrase -> literal matches first; unquoted queries are untouched
    lit_locs = literal_locators(proj, phrases, min(3, limit)) if phrases else []
    if lit_locs:
        limit = max(1, limit - len(lit_locs))
    # --- graphify's own seeding and scoring (serve.py:_query_graph_text lines 1371-1392)
    terms = S._query_terms(query)
    qs = S._score_query(G, terms, collect_per_term_seeds=True)
    bst = qs.best_seed_by_term
    intent = {t for t in bst if t in S._RELATIONAL_INTENT_TERMS}
    if intent and any(t not in S._RELATIONAL_INTENT_TERMS for t in terms):
        bst = {t: n for t, n in bst.items() if t not in intent}
    seeds = S._pick_seeds(qs.ranked, G=G, best_seed_by_term=bst)
    score = {n: s for s, n in qs.ranked}
    # candidate lists: (A) 1-hop traversal neighbourhood of graphify's seeds, (B) global ranking.
    nbr = set()
    if seeds:
        nbr, _ = S._bfs(S._traversal_view(G), seeds, 1)
    A = sorted([n for n in nbr if n in score], key=lambda n: (-score[n], n))
    B = [n for _, n in qs.ranked]

    def ordered(lst):
        return sorted(lst, key=lambda n: _tier(G.nodes[n]))  # stable: keeps score order within a tier
    A, B = ordered(A), ordered(B)
    merged, seen = [], set()
    for i in range(max(len(A), len(B))):
        for lst in (A, B):
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                merged.append(lst[i])
        if len(merged) >= limit + 16:
            break
    # re-sort the merged list by tier so test/doc/file nodes never outrank code symbols
    merged = sorted(merged, key=lambda n: _tier(G.nodes[n]))
    full_ids = merged[:limit]
    more_ids = merged[limit:limit + 16]
    locs = [locator(G, n, score.get(n, 0.0), terms, with_id) for n in full_ids]
    more = [short(locator(G, n, score.get(n, 0.0), terms)) for n in more_ids]
    rels = []
    if len(full_ids) > 1:
        sub = G.subgraph(full_ids)
        for u, v, e in sub.edges(data=True):
            rels.append({"from": locator(G, u, 0, [])["symbol"], "to": locator(G, v, 0, [])["symbol"],
                         "rel": e.get("relation"), "conf": e.get("confidence")})
            if len(rels) >= 6:
                break
    # --- confidence: exact identifier match on a top candidate (see README, calibrated on pilot B)
    exact = False
    for n in full_ids[:5]:
        b = _bare(G.nodes[n].get("label"))
        if any(len(t) >= 6 and t == b for t in terms):
            exact = True
            break
    stale, why = proj.staleness(ptr)
    resp = envelope("locate", proj, ptr, stale, why)
    if lit_locs:
        locs = lit_locs + locs
        exact = True
    resp.update({"confidence": "high" if exact else "low", "locators": locs, "relations": rels,
                 "more": more, "not_indexed": NOT_INDEXED, "hits": len(locs)})
    if phrases and proj.lits is None:
        resp["literal_index"] = "unavailable"
    if not exact:
        resp["fallback_hint"] = {"grep": sorted(terms, key=len, reverse=True)[:4]}

    def t_more():
        if resp["more"]:
            resp["more"].pop()
            return "more"

    def t_rel():
        if resp["relations"]:
            resp["relations"].pop()
            return "relations"

    def t_loc():
        if len(resp["locators"]) > 1:
            resp["locators"].pop()
            resp["hits"] = len(resp["locators"])
            return "locators"

    def t_mt():  # last: shave matched_terms
        for l in resp["locators"]:
            if l.get("matched_terms"):
                l.pop("matched_terms")
                return "matched_terms"

    return finalize(resp, max_chars, (t_more, t_rel, t_mt, t_loc))


def _resolve(G, req, key_id="node_id", key_sym="symbol"):
    nid = req.get(key_id)
    if nid and nid in G:
        return nid
    sym = req.get(key_sym)
    if sym:
        cands = S._find_node(G, str(sym))
        f = req.get("file")
        if f:
            cands = [c for c in cands if G.nodes[c].get("source_file") == f] or cands
        if cands:
            return max(cands, key=lambda c: G.degree(c))
    return None


def op_expand(proj, req):
    G, ptr = proj.graph()
    if G is None:
        return err("expand", proj, proj.missing_code(), req)
    max_chars = int(req.get("max_chars") or DEFAULT_MAX_CHARS)
    limit = max(1, min(int(req.get("limit") or 10), 40))
    nid = _resolve(G, req)
    if not nid:
        return err("expand", proj, "node_not_found", req)
    items = []
    for _, v, e in G.out_edges(nid, data=True):
        items.append(("out", v, e))
    for u, _, e in G.in_edges(nid, data=True):
        items.append(("in", u, e))
    rank = {"EXTRACTED": 0, "INFERRED": 1}
    items.sort(key=lambda x: (rank.get(x[2].get("confidence"), 2), -G.degree(x[1]), x[1]))
    stale, why = proj.staleness(ptr)
    resp = envelope("expand", proj, ptr, stale, why)
    resp["node"] = locator(G, nid, 0, [])
    resp["neighbors"] = [dict(locator(G, v, 0, []), dir=d, rel=e.get("relation"), conf=e.get("confidence"))
                         for d, v, e in items[:limit]]
    resp["total_neighbors"] = len(items)
    resp["hits"] = len(resp["neighbors"])

    def t_n():
        if resp["neighbors"]:
            resp["neighbors"].pop()
            resp["hits"] = len(resp["neighbors"])
            return "neighbors"
    return finalize(resp, max_chars, (t_n,))


def op_path(proj, req):
    import networkx as nx
    G, ptr = proj.graph()
    if G is None:
        return err("path", proj, proj.missing_code(), req)
    max_chars = int(req.get("max_chars") or DEFAULT_MAX_CHARS)
    hops = max(1, min(int(req.get("max_hops") or 6), 12))
    s = _resolve(G, {"node_id": req.get("source_id"), "symbol": req.get("source"), "file": req.get("source_file")})
    t = _resolve(G, {"node_id": req.get("target_id"), "symbol": req.get("target"), "file": req.get("target_file")})
    if not s or not t:
        return err("path", proj, "endpoint_not_found", req)
    stale, why = proj.staleness(ptr)
    resp = envelope("path", proj, ptr, stale, why)
    try:
        p = nx.shortest_path(G.to_undirected(as_view=True), s, t)
    except nx.NetworkXNoPath:
        p = None
    if p is None or len(p) - 1 > hops:
        resp.update({"found": False, "path": []})
    else:
        resp.update({"found": True, "hops": len(p) - 1, "path": [locator(G, n, 0, []) for n in p]})
    resp["hits"] = len(resp["path"])

    def t_p():
        if len(resp["path"]) > 2:
            resp["path"].pop(len(resp["path"]) // 2)
            resp["hits"] = len(resp["path"])
            return "path_nodes"
    return finalize(resp, max_chars, (t_p,))


def op_stats(proj, req):
    G, ptr = proj.graph()
    if G is None:
        return err("stats", proj, proj.missing_code(), req)
    stale, why = proj.staleness(ptr)
    fp = proj._fp_files(ptr) or {}
    resp = envelope("stats", proj, ptr, stale, why)
    resp.update({"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
                 "communities": len({d.get("community") for _, d in G.nodes(data=True)}),
                 "fingerprint_files": len(fp.get("files", [])), "git_head": fp.get("git_head"),
                 "fingerprint": ptr.get("fingerprint"), "literals": (ptr.get("literals") or {}).get("count"),
                 "stale_cache": {"hits": proj.cache_hits, "computes": proj.cache_computes}})
    return finalize(resp, int(req.get("max_chars") or DEFAULT_MAX_CHARS))


# ---------------------------------------------------------------- literal index (built with the graph, published as one generation)
LIT_MIN = 12          # minimum SOURCE TEXT length (quotes/prefix included) for a literal to be indexed
NOT_INDEXED_LIT = ("comments, strings <12 chars, dynamic strings and non-code files are not indexed; "
                   "a miss is not absence")


def _lit_cfg():
    def i(name, d):
        try:
            return int(os.environ.get(name, d))
        except ValueError:
            return d
    return i("GFY_LIT_MAX_FILE_BYTES", 1_000_000), i("GFY_LIT_MAX_TEXT", 300)


_Q_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def _lang_of(rel: str) -> str:
    ext = os.path.splitext(rel)[1].lower().lstrip(".")
    return "python" if ext == "py" else (ext or "other")


class _LitVisitor(ast.NodeVisitor):
    """Collect static str constants / f-strings with the lexically enclosing qualified symbol."""

    def __init__(self, lines):
        self.lines = lines
        self.stack = []          # [(name, def_line)]
        self.out = []            # (line, end_line, symbol|None, text, sym_line|None)
        self.short = 0

    def _seg(self, node):
        l1, l2, c1, c2 = node.lineno, node.end_lineno, node.col_offset, node.end_col_offset
        if l2 is None or c2 is None or l1 > len(self.lines) or l2 > len(self.lines):
            return None
        if l1 == l2:
            b = self.lines[l1 - 1][c1:c2]
        else:
            b = self.lines[l1 - 1][c1:] + b"".join(self.lines[l1:l2 - 1]) + self.lines[l2 - 1][:c2]
        return b.decode("utf-8", "replace")

    def _add(self, node, text):
        if text is None or len(text) < LIT_MIN:
            self.short += 1
            return
        sym = ".".join(n for n, _ in self.stack) or None
        self.out.append((node.lineno, node.end_lineno if node.end_lineno != node.lineno else None, sym, text,
                         self.stack[-1][1] if self.stack else None))

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self._add(node, self._seg(node))

    def visit_JoinedStr(self, node):
        const = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if len(const) >= 4:
            self._add(node, self._seg(node))
        else:
            self.short += 1
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                self.visit(v.value)

    def _scope(self, node, outer, inner):
        for n in outer:
            self.visit(n)
        self.stack.append((node.name, node.lineno))
        for n in inner:
            self.visit(n)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        a = node.args
        outer = list(node.decorator_list) + list(a.defaults) + [d for d in a.kw_defaults if d is not None]
        if node.returns is not None:
            outer.append(node.returns)
        self._scope(node, outer, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._scope(node, list(node.decorator_list) + list(node.bases) + [k.value for k in node.keywords], node.body)


def extract_py_literals(data: bytes):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    tree = ast.parse(data)
    v = _LitVisitor(data.splitlines(True))
    v.visit(tree)
    return v.out, v.short


def extract_text_literals(data: bytes):
    """Regex candidates for non-Python code: single-line quoted strings; the owner is NEVER inferred."""
    text = data.decode("utf-8", "replace")
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    out, short = [], 0
    for m in _Q_RE.finditer(text):
        t = m.group(0)
        if len(t) < LIT_MIN:
            short += 1
            continue
        out.append((bisect.bisect_right(starts, m.start()), None, None, t, None))
    return out, short


def _manifest_rels(proj, work: Path):
    try:
        keys = list(json.loads((work / "manifest.json").read_text("utf-8")).keys())
    except Exception:
        return []
    out = []
    for k in keys:
        rel = os.path.relpath(k, proj.root) if os.path.isabs(k) else k
        out.append(rel.replace("\\", "/"))
    return out


def hash_sources(proj: Project) -> dict:
    """Short sha256 of every code file the project currently contains (graphify's ignore rules applied)."""
    pred = S_detect.ignored_predicate(Path(proj.root))
    out = {}
    for rel in sorted(proj.code_files_now()):
        if pred(Path(proj.root) / rel):
            continue
        try:
            with open(os.path.join(proj.root, rel), "rb") as f:
                out[rel] = _h16(f.read())
        except OSError:
            out[rel] = None
    return out


def build_literals(proj: Project, graph_path: Path, pre):
    """Extract the literal index from the SAME bytes the graph build saw.
    pre = {rel: hash} taken BEFORE the build (None when no build ran). Returns (artifact_dict, error)."""
    max_bytes, max_text = _lit_cfg()
    rels = _manifest_rels(proj, proj.work)
    try:
        gd = json.loads(graph_path.read_text(encoding="utf-8"))
        node_at = {(n.get("source_file"), _line(n)) for n in gd.get("nodes", [])}
    except Exception:
        node_at = set()
    files, literals, by_lang = {}, {}, {}
    skipped = {"size": 0, "parse": 0, "unreadable": 0}
    excl = {"short": 0, "truncated": 0}
    first = {}
    unverified = 0
    for rel in sorted(set(rels)):
        ap = os.path.join(proj.root, rel)
        try:
            with open(ap, "rb") as f:
                data = f.read()
        except OSError:
            if pre is not None and rel in pre:
                return None, f"source_changed_during_build:{rel}"
            skipped["unreadable"] += 1
            continue
        h = _h16(data)
        first[rel] = h
        if pre is not None:
            if rel in pre:
                if pre[rel] != h:
                    return None, f"source_changed_during_build:{rel}"
            else:
                unverified += 1
        lang = _lang_of(rel)
        ent = {"h": h, "lang": lang}
        files[rel] = ent
        if len(data) > max_bytes:
            skipped["size"] += 1
            ent["skip"] = "size"
            continue
        try:
            if lang == "python":
                lits, short = extract_py_literals(data)
            else:
                lits, short = extract_text_literals(data)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            skipped["parse"] += 1
            ent["skip"] = "parse"
            continue
        excl["short"] += short
        rows = []
        for (ln, eln, sym, text, sym_line) in lits:
            if len(text) > max_text:
                text = text[:max_text]
                excl["truncated"] += 1
            joined = 1 if (sym_line is not None and (rel, sym_line) in node_at) else 0
            rows.append([ln, eln, sym, "ast" if lang == "python" else "unknown", text, joined])
        if rows:
            literals[rel] = rows
            by_lang[lang] = by_lang.get(lang, 0) + len(rows)
        ent["n"] = len(rows)
    # files tracked before the build but absent from the manifest: still verify they did not move
    if pre is not None:
        for rel, h0 in pre.items():
            if rel in first:
                continue
            try:
                with open(os.path.join(proj.root, rel), "rb") as f:
                    h = _h16(f.read())
            except OSError:
                h = None
            if h != h0:
                return None, f"source_changed_during_build:{rel}"
    # final re-hash of every extracted file: the bytes we read must still be the bytes on disk
    for rel, h in first.items():
        try:
            with open(os.path.join(proj.root, rel), "rb") as f:
                if _h16(f.read()) != h:
                    return None, f"source_changed_during_extract:{rel}"
        except OSError:
            return None, f"source_changed_during_extract:{rel}"
    art = {"version": 1, "files": files, "literals": literals, "by_lang": by_lang,
           "count": sum(by_lang.values()), "skipped": skipped, "excluded_literals": excl,
           "unverified_files": unverified,
           "limits": {"max_file_bytes": max_bytes, "max_text": max_text, "min_len": LIT_MIN}}
    return art, None


def op_find_text(proj: Project, req: dict) -> str:
    G, ptr = proj.graph()
    if G is None:
        return err("find_text", proj, proj.missing_code(), req)
    text = req.get("text")
    max_chars = int(req.get("max_chars") or DEFAULT_MAX_CHARS)
    if not isinstance(text, str) or not (3 <= len(text) <= 200):
        return err("find_text", proj, "text_length_3_to_200", req)
    limit = max(1, min(int(req.get("limit") or 5), 10))
    L = proj.lits
    stale, why = proj.staleness(ptr)
    if L is None:
        return err("find_text", proj, "literal_index_unavailable", req)
    found = []
    for rel, rows in L["literals"].items():
        for r in rows:
            if text in r[4]:
                found.append((rel, r))
    total = len(found)
    found.sort(key=lambda x: (x[1][4] != text, x[1][3] != "ast", _is_test(x[0]), x[0], x[1][0]))
    resp = envelope("find_text", proj, ptr, stale, why)
    hc, matches = {}, []
    for rel, r in found[:limit]:
        i = r[4].index(text)
        a = max(0, i - max(0, 100 - len(text)) // 2)
        if rel not in hc:
            hc[rel] = proj.live_hash(rel)
        fm = L["files"].get(rel, {})
        m = {"file": rel, "line": r[0], "end_line": r[1], "symbol": r[2], "owner": r[3],
             "lang": fm.get("lang"), "excerpt": r[4][a:a + 100], "stale": hc[rel] != fm.get("h")}
        if r[5]:
            m["in_graph"] = True
        matches.append(m)
    sk = L.get("skipped", {})
    resp.update({"matches": matches, "omitted": total - len(matches), "hits": len(matches),
                 "coverage": {"files_indexed": len([1 for f in L["files"].values() if "skip" not in f]),
                              "files_skipped": sum(sk.values()), "literal_count": L.get("count", 0),
                              "by_lang": L.get("by_lang", {})},
                 "not_indexed": NOT_INDEXED_LIT})

    def t_m():
        if len(resp["matches"]) > 1:
            resp["matches"].pop()
            resp["omitted"] += 1
            resp["hits"] = len(resp["matches"])
            return "matches"

    def t_cov():
        if resp["coverage"].get("by_lang"):
            resp["coverage"].pop("by_lang")
            return "by_lang"

    def t_exc():
        for m in resp["matches"]:
            if len(m["excerpt"]) > 40:
                m["excerpt"] = m["excerpt"][:len(m["excerpt"]) // 2]
                return "excerpt"

    return finalize(resp, max_chars, (t_cov, t_exc, t_m))


_QUOTED_RE = re.compile(r'(?<!\w)(["\'])(.{3,200}?)\1(?!\w)')


def quoted_phrases(query: str):
    return [m.group(2) for m in _QUOTED_RE.finditer(query or "")]


def literal_locators(proj: Project, phrases, cap: int):
    """Literal matches for quoted phrases as locators (kind 'literal'); [] if the index is unavailable."""
    L = proj.lits
    if L is None:
        return []
    found, seen = [], set()
    for ph in phrases:
        for rel, rows in L["literals"].items():
            for r in rows:
                if ph in r[4] and (rel, r[0]) not in seen:
                    seen.add((rel, r[0]))
                    found.append((ph, rel, r))
    found.sort(key=lambda x: (x[2][4] != x[0], x[2][3] != "ast", _is_test(x[1]), x[1], x[2][0]))
    out = []
    for ph, rel, r in found[:cap]:
        i = r[4].index(ph)
        out.append({"symbol": r[2] or "<module>", "kind": "literal", "file": rel, "line": r[0], "community": None,
                    "degree": 0, "score": 0.0, "owner": r[3], "excerpt": r[4][max(0, i - 20):i + len(ph) + 20][:80]})
    return out


# ---------------------------------------------------------------- context_for (python-only import/conftest/fixture suggestions)
CTX_NOTE = "suggestions only: add them to read_context explicitly; they never widen allowed writes"
CTX_MAX_BYTES = 1_000_000
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def _parse_py(path):
    try:
        with open(path, "rb") as f:
            return ast.parse(f.read())
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


def _bases(root: str, relfile: str):
    """Directories to resolve absolute imports against: the file's dir and its ancestors up to root, then root/src."""
    d = os.path.dirname(os.path.join(root, relfile))
    out = []
    while True:
        out.append(d)
        if os.path.normcase(d) == os.path.normcase(root) or not _inside(d, root):
            break
        d = os.path.dirname(d)
    src = os.path.join(root, "src")
    if os.path.isdir(src):
        out.append(src)
    return out


def _resolve_parts(bases, parts):
    for b in bases:
        p = os.path.join(b, *parts) if parts else b
        if parts and os.path.isfile(p + ".py"):
            return p + ".py"
        if os.path.isfile(os.path.join(p, "__init__.py")):
            return os.path.join(p, "__init__.py")
    return None


def file_imports(root: str, relfile: str, tree):
    """-> (resolved: ordered abs paths, unresolved: [{import, why, line}], external: int)"""
    bases = _bases(root, relfile)
    here = os.path.dirname(os.path.join(root, relfile))
    resolved, unresolved, ext = [], [], 0

    def add(p):
        if p and p not in resolved:
            resolved.append(p)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                p = _resolve_parts(bases, a.name.split("."))
                if p:
                    add(p)
                elif a.name.split(".")[0] not in _STDLIB:
                    ext += 1
        elif isinstance(node, ast.ImportFrom):
            mod = node.module.split(".") if node.module else []
            if node.level:
                base = here
                for _ in range(node.level - 1):
                    base = os.path.dirname(base)
                bs = [base]
            else:
                bs = bases
            mp = _resolve_parts(bs, mod) if mod else None
            if mp:
                add(mp)
            sub = False
            for a in node.names:
                if a.name == "*":
                    continue
                sp = _resolve_parts(bs, mod + [a.name])
                if sp:
                    add(sp)
                    sub = True
            if not mp and not sub:
                if node.level:
                    unresolved.append({"import": ("." * node.level) + (node.module or ""),
                                       "why": "relative_unresolved", "line": node.lineno})
                elif mod and mod[0] not in _STDLIB:
                    ext += 1
        elif isinstance(node, ast.Call):
            f = node.func
            nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if nm in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    p = _resolve_parts(bases, arg.value.split("."))
                    if p:
                        add(p)
                    elif arg.value.split(".")[0] not in _STDLIB:
                        ext += 1
                else:
                    unresolved.append({"import": f"{nm}(<dynamic>)", "why": "dynamic_import", "line": node.lineno})
    return resolved, unresolved, ext


def _fixture_names(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                t = d.func if isinstance(d, ast.Call) else d
                if (isinstance(t, ast.Attribute) and t.attr == "fixture") or (isinstance(t, ast.Name) and t.id == "fixture"):
                    nm = n.name
                    if isinstance(d, ast.Call):
                        for kw in d.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                nm = str(kw.value.value)
                    out.add(nm)
    return out


def _used_fixtures(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
            a = n.args
            for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                if x.arg not in ("self", "cls"):
                    out.add(x.arg)
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "usefixtures":
                    out.update(str(c.value) for c in d.args if isinstance(c, ast.Constant))
    return out


def op_context_for(proj: Project, req: dict) -> str:
    G, ptr = proj.graph()
    if G is None:
        return err("context_for", proj, proj.missing_code(), req)
    max_chars = int(req.get("max_chars") or DEFAULT_MAX_CHARS)
    files = req.get("files")
    if not isinstance(files, list) or not (1 <= len(files) <= 5) or not all(isinstance(f, str) and f for f in files):
        return err("context_for", proj, "files_1_to_5", req)
    limit = max(1, min(int(req.get("limit") or 5), 5))
    root = proj.root
    stale, why = proj.staleness(ptr)
    gen_files = (proj.lits or {}).get("files", {})
    pred = S_detect.ignored_predicate(Path(root))
    unresolved, declared = [], []
    ext_total = 0
    for f in files:
        rel = os.path.normpath(f.replace("\\", "/")).replace("\\", "/")
        full = os.path.join(root, rel)
        if os.path.isabs(f) or rel.startswith("..") or not _inside(os.path.realpath(full), root):
            unresolved.append({"file": f, "why": "outside_project_root"})
        elif not rel.endswith(".py"):
            unresolved.append({"file": rel, "why": "python_only"})
        elif not os.path.isfile(full):
            unresolved.append({"file": rel, "why": "file_not_found"})
        elif rel not in declared:
            declared.append(rel)
    dset = {os.path.normcase(os.path.join(root, d)) for d in declared}

    def ok(path):
        if os.path.normcase(path) in dset or not os.path.isfile(path) or not _inside(os.path.realpath(path), root):
            return False
        try:
            return not pred(Path(path))
        except Exception:
            return True

    cands = {}   # normcase abs path -> (rank, order, reason, from, confidence, path)

    def put(path, rank, order, reason, frm, conf):
        if not ok(path):
            return
        k = os.path.normcase(path)
        if k not in cands or (rank, order) < cands[k][:2]:
            cands[k] = (rank, order, reason, frm, conf, path)

    def conftest_entry(cp, used, order, d, conf_plain):
        names = set()
        if used:
            ct = _parse_py(cp)
            names = (_fixture_names(ct) if ct else set()) & used
        if names:
            put(cp, 1, order, "conftest+fixture:" + ",".join(sorted(names)[:3]), d, "high")
        else:
            put(cp, 1, order, "conftest", d, conf_plain)

    test_files = None
    for order, d in enumerate(declared):
        full = os.path.join(root, d)
        tree = _parse_py(full)
        if tree is None:
            unresolved.append({"file": d, "why": "parse_error"})
            continue
        res, unr, ext = file_imports(root, d, tree)
        ext_total += ext
        for u in unr:
            unresolved.append(dict(u, file=d))
        for p in res:
            put(p, 0, order, "import", d, "high")
        is_t = _is_test(d)
        used = _used_fixtures(tree) if is_t else set()
        for b in _bases(root, d):
            if b == os.path.join(root, "src"):
                continue
            cp = os.path.join(b, "conftest.py")
            if os.path.isfile(cp):
                conftest_entry(cp, used, order, d, "medium")
        rc = os.path.join(root, "tests", "conftest.py")
        if is_t:
            if os.path.isfile(rc):
                conftest_entry(rc, used, order, d, "medium")
        else:
            if test_files is None:
                test_files = sorted(r for r in gen_files if r.endswith(".py") and _is_test(r))
                if not test_files:
                    for dp, dn, fn in os.walk(root):
                        dn[:] = [x for x in dn if not S_detect._is_noise_dir(x, Path(dp))]
                        for n in fn:
                            r = os.path.relpath(os.path.join(dp, n), root).replace("\\", "/")
                            if n.endswith(".py") and _is_test(r):
                                test_files.append(r)
                    test_files.sort()
            stem = os.path.splitext(os.path.basename(d))[0]
            refs = 0
            for tr in test_files:
                tp = os.path.join(root, tr)
                if os.path.normcase(tp) in dset:
                    continue
                try:
                    with open(tp, encoding="utf-8", errors="replace") as fh:
                        if stem not in fh.read():
                            continue
                except OSError:
                    continue
                tt = _parse_py(tp)
                if tt is None:
                    continue
                tres, _, _ = file_imports(root, tr, tt)
                if any(os.path.normcase(x) == os.path.normcase(full) for x in tres):
                    put(tp, 3, order, "test_imports_declared_module", d, "low")
                    refs += 1
                    if refs >= 10:
                        break
            if refs and os.path.isfile(rc):
                put(rc, 1, order, "conftest", d, "low")
    ranked = sorted(cands.values(), key=lambda c: (c[0], c[1], c[5]))
    sugg, omitted, total_bytes = [], 0, 0
    for rank, order, reason, frm, conf, path in ranked:
        rel = os.path.relpath(path, root).replace("\\", "/")
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if len(sugg) >= limit or total_bytes + size > CTX_MAX_BYTES:
            omitted += 1
            continue
        sugg.append({"file": rel, "reason": reason, "from": frm, "confidence": conf, "bytes": size,
                     "stale": (proj.live_hash(rel) != gen_files[rel]["h"]) if rel in gen_files else True})
        total_bytes += size
    resp = envelope("context_for", proj, ptr, stale, why)
    resp.update({"suggestions": sugg, "unresolved": unresolved[:5], "omitted": omitted, "bytes_total": total_bytes,
                 "hits": len(sugg), "note": CTX_NOTE})
    if len(unresolved) > 5:
        resp["unresolved_total"] = len(unresolved)
    if ext_total:
        resp["external_imports"] = ext_total

    def t_s():
        if len(resp["suggestions"]) > 1:
            x = resp["suggestions"].pop()
            resp["omitted"] += 1
            resp["bytes_total"] -= x["bytes"]
            resp["hits"] = len(resp["suggestions"])
            return "suggestions"

    def t_u():
        if resp["unresolved"]:
            resp["unresolved"].pop()
            return "unresolved"
    return finalize(resp, max_chars, (t_u, t_s))


# ---------------------------------------------------------------- refresh
def validate_graph(path: Path, prev_nodes: int | None, force: bool):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"graph.json does not parse: {e}"
    nodes = d.get("nodes")
    links = d.get("links", d.get("edges"))
    if not isinstance(nodes, list) or not isinstance(links, list) or not nodes:
        return None, "missing/empty nodes or links"
    for n in nodes[:200]:
        if not all(k in n for k in ("id", "label", "source_file")):
            return None, "node missing required fields (id,label,source_file)"
    for e in links[:200]:
        if not all(k in e for k in ("source", "target", "relation", "confidence")):
            return None, "edge missing required fields (source,target,relation,confidence)"
    if prev_nodes and not force and len(nodes) < prev_nodes * 0.8:
        return None, f"node count shrank {prev_nodes}->{len(nodes)} (>20%); use force"
    return {"nodes": len(nodes), "edges": len(links)}, None


def _acquire_lock(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            return False
    return False


def op_refresh(proj: Project, req: dict) -> str:
    max_chars = int(req.get("max_chars") or DEFAULT_MAX_CHARS)
    force = bool(req.get("force"))
    ptr0 = proj.pointer()
    stale0, why0 = (proj.staleness(ptr0) if ptr0 else (True, "no_snapshot"))
    if not _acquire_lock(proj.lock):
        resp = envelope("refresh", proj, ptr0, stale0, why0)
        resp.update({"ok": False, "error": "refresh_busy"})
        return finalize(resp, max_chars)
    t0 = time.perf_counter()
    proj._scache = None
    try:
        err_msg = None
        info = None
        want_lits = not req.get("no_literals")
        tm = {}
        pre = None
        if want_lits and not req.get("skip_build"):
            t1 = time.perf_counter()
            pre = hash_sources(proj)
            tm["hash_pre_s"] = round(time.perf_counter() - t1, 2)
        t1 = time.perf_counter()
        if not req.get("skip_build"):
            env = child_env({"GRAPHIFY_OUT": str(proj.work)})
            def build():
                proj.work.mkdir(parents=True, exist_ok=True)
                p = subprocess.run([sys.executable, "-m", "graphify", "extract", proj.root, "--code-only"],
                                   env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=900, cwd=str(proj.out_dir))
                if p.returncode != 0:
                    return f"graphify extract rc={p.returncode}: {(p.stderr or p.stdout)[-200:]}"
                return None
            err_msg = build()
            if err_msg is None:
                try:
                    json.loads((proj.work / "graph.json").read_text(encoding="utf-8"))
                except Exception:
                    err_msg = "work graph unreadable"
            if err_msg:  # a corrupt incremental work dir must not wedge refresh: rebuild once from scratch
                shutil.rmtree(proj.work, ignore_errors=True)
                err_msg = build()
        tm["build_s"] = round(time.perf_counter() - t1, 2)
        gp = proj.work / "graph.json"
        prev_nodes = None
        if ptr0:
            try:
                prev_nodes = json.loads((proj.published / f"graph-{ptr0['sha'][:12]}.json").read_text("utf-8")).get("nodes")
                prev_nodes = len(prev_nodes)
            except Exception:
                prev_nodes = None
        if not err_msg:
            info, verr = validate_graph(gp, prev_nodes, force)
            err_msg = verr
        lit_art = None
        if not err_msg and want_lits:
            t1 = time.perf_counter()
            lit_art, err_msg = build_literals(proj, gp, pre)
            tm["literals_s"] = round(time.perf_counter() - t1, 2)
        if err_msg:
            resp = envelope("refresh", proj, ptr0, stale0, why0)
            resp.update({"ok": False, "error": "refresh_failed", "detail": err_msg[:300],
                         "serving": "last-good" if ptr0 else "nothing"})
            return finalize(resp, max_chars)
        sha = semantic_id(gp)
        proj.published.mkdir(parents=True, exist_ok=True)
        snap = proj.published / f"graph-{sha[:12]}.json"
        changed = not ptr0 or ptr0["sha"] != sha
        if not snap.exists():
            fd, tmp = tempfile.mkstemp(dir=str(proj.published), prefix=".gfy-", suffix=".tmp")
            os.close(fd)
            shutil.copyfile(gp, tmp)
            os.replace(tmp, snap)
        file_sha = sha256_file(snap)
        lit_desc = None
        if lit_art is not None:  # sibling artifact of the snapshot; written BEFORE the pointer swap
            lit_art["graph_sha"] = sha
            raw = jdump(lit_art).encode("utf-8")
            lsha = hashlib.sha256(raw).hexdigest()
            lname = f"literals-{sha[:12]}-{lsha[:8]}.json"
            fd, tmp = tempfile.mkstemp(dir=str(proj.published), prefix=".gfy-", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, proj.published / lname)
            lit_desc = {"name": lname, "file_sha": lsha, "count": lit_art["count"]}
            tm["graph_bytes"] = snap.stat().st_size
            tm["literals_bytes"] = len(raw)
        # fingerprint from the graphify manifest (stat only)
        rels = []
        try:
            rels = list(json.loads((proj.work / "manifest.json").read_text("utf-8")).keys())
        except Exception:
            pass
        entries, head = proj.compute_fingerprint(rels)
        fp_digest = hashlib.sha256(jdump([entries, head]).encode()).hexdigest()[:16]
        atomic_write_json(proj.published / f"graph-{sha[:12]}.fp.json", {"files": entries, "git_head": head})
        built = now_iso()
        newptr = {"snapshot": sha[:12], "sha": sha, "file_sha": file_sha, "built_at": built, "fingerprint": fp_digest}
        if lit_desc:
            newptr["literals"] = lit_desc
        if ptr0 and changed:  # previous generation (graph + its literals) becomes last-good
            newptr.update({"prev": ptr0["sha"][:12], "prev_sha": ptr0["sha"], "prev_file_sha": ptr0.get("file_sha"),
                           "prev_built_at": ptr0.get("built_at")})
            if ptr0.get("literals"):
                newptr["prev_literals"] = ptr0["literals"]
        elif ptr0:
            newptr.update({k: ptr0[k] for k in ("prev", "prev_sha", "prev_file_sha", "prev_built_at", "prev_literals")
                           if k in ptr0})
        atomic_write_json(proj.published / "current.json", newptr)
        # keep current + previous (last-good); prune the rest
        keep = {sha[:12]}
        if newptr.get("prev"):
            keep.add(newptr["prev"])
        for f in proj.published.glob("graph-*"):
            if f.name.split(".")[0][len("graph-"):] not in keep:
                try:
                    f.unlink()
                except OSError:
                    pass
        keep_l = {d["name"] for d in (newptr.get("literals"), newptr.get("prev_literals")) if d}
        for f in proj.published.glob("literals-*"):
            if f.name not in keep_l:
                try:
                    f.unlink()
                except OSError:
                    pass
        proj._scache = None  # a successful refresh invalidates the staleness cache
        ptr = proj.pointer()
        resp = envelope("refresh", proj, ptr, False, None)
        resp.update({"graph_changed": changed, "nodes": info["nodes"], "edges": info["edges"],
                     "literals": lit_art["count"] if lit_art is not None else None,
                     "wall_s": round(time.perf_counter() - t0, 2), "timing": tm})
        return finalize(resp, max_chars)
    except subprocess.TimeoutExpired:
        resp = envelope("refresh", proj, ptr0, stale0, why0)
        resp.update({"ok": False, "error": "refresh_failed", "detail": "timeout"})
        return finalize(resp, max_chars)
    finally:
        try:
            proj.lock.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- dispatch
def err(op, proj, code, req=None) -> str:
    r = {"ok": False, "op": op, "project": proj.id if proj else None, "snapshot": None, "built_at": None,
         "stale": None, "stale_reason": None, "chars": 0, "hits": 0, "error": code}
    return finalize(r, int((req or {}).get("max_chars") or DEFAULT_MAX_CHARS))


def resolve_project(projects: dict[str, Project], ref):
    if not ref:
        return None
    if ref in projects:
        return projects[ref]
    try:
        rp = os.path.realpath(str(ref))
    except Exception:
        return None
    for p in projects.values():
        if os.path.normcase(p.root) == os.path.normcase(rp):
            return p
    return None


def handle(projects, req: dict) -> str:
    op = req.get("op")
    if op == "health":
        info = {}
        for pid, p in projects.items():
            ptr = p.pointer()
            info[pid] = ptr["sha"][:12] if ptr else None
        r = {"ok": True, "op": "health", "project": None, "snapshot": None, "built_at": None, "stale": None,
             "stale_reason": None, "chars": 0, "hits": 0, "graphify": VERSION, "net_guard": net_guard_selfcheck(),
             "projects": info, "roots": {k: p.root for k, p in projects.items()}}
        return finalize(r, int(req.get("max_chars") or DEFAULT_MAX_CHARS))
    proj = resolve_project(projects, req.get("project"))
    if proj is None:
        return err(op, None, "project_not_allowed", req)
    fn = {"locate": op_locate, "expand": op_expand, "path": op_path, "stats": op_stats, "refresh": op_refresh,
          "find_text": op_find_text, "context_for": op_context_for}.get(op)
    if fn is None:
        return err(op, proj, "unknown_op", req)
    return fn(proj, req)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pj = HERE / "projects.json"
    if "--projects" in argv:
        pj = Path(argv[argv.index("--projects") + 1])
    projects = load_projects(pj)
    out = _PROTO if _PROTO is not None else sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = {}
        try:
            req = json.loads(line)
            if isinstance(req, dict) and req.get("id") is not None:  # reserve room for the echoed id
                req["max_chars"] = int(req.get("max_chars") or DEFAULT_MAX_CHARS) - len(jdump(req["id"])) - 6
            resp = handle(projects, req)
        except Exception as e:  # never die on a bad request
            r = {"ok": False, "op": req.get("op") if isinstance(req, dict) else None, "project": None,
                 "snapshot": None, "built_at": None, "stale": None, "stale_reason": None, "chars": 0, "hits": 0,
                 "error": f"{type(e).__name__}: {str(e)[:200]}"}
            resp = finalize(r, DEFAULT_MAX_CHARS)
        try:
            rid = req.get("id") if isinstance(req, dict) else None
            if rid is not None:
                o = json.loads(resp)
                o["id"] = rid
                for _ in range(4):
                    resp = jdump(o)
                    if o.get("chars") == len(resp):
                        break
                    o["chars"] = len(resp)
        except Exception:
            pass
        out.write(resp + "\n")
        out.flush()


if __name__ == "__main__":
    main()
