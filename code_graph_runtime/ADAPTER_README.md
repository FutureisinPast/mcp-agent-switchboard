# gfy_adapter (pilot, outside Switchboard)

Long-lived stdio server around graphifyy==0.9.77. Run: `venv\Scripts\python.exe adapter\gfy_adapter.py [--projects PATH]`. One JSON object per line in, one per line out. Optional `id` is echoed. Bad input never kills the process.

## Request / response
Request: `{"op":..., "project": <id or root path>, "id":..., "max_chars": 1500, ...op args}`.
Every response: `ok, op, project, snapshot (sha12), built_at, stale, stale_reason, chars` (+ `error` on failure). Every serialized response (including the echoed id) is <= max_chars (default 1500). If trimmed: `truncated:true` and `dropped:{what:count}`.

| op | args | returns |
|---|---|---|
| health | - | graphify version, `net_guard` (a real connect attempt is blocked), projects -> snapshot ids |
| locate | query, limit=8 (max 20), max_chars, include_ids | `confidence` high/low, `locators[]` {symbol, kind, file (relative), line, community, degree, score, matched_terms?}, `relations[]` (<=6, among returned nodes, with `conf` EXTRACTED/INFERRED), `more[]` ("symbol@file:line" for next ranks, dropped first when trimming), `not_indexed` (always), `fallback_hint.grep` when low |
| expand | node_id or symbol(+file), limit=10 | node + neighbors with dir, rel, conf (EXTRACTED first) |
| path | source/target (or source_id/target_id), max_hops=6 | shortest undirected path as locators |
| stats | - | nodes, edges, communities, fingerprint info, stale |
| refresh | force, skip_build | see below |

## Guarantees
- Version pin: exits at startup unless `importlib.metadata.version("graphifyy") == "0.9.77"` (graphify has no `__version__`).
- Network guard: `sys.addaudithook` raises on socket.connect, socket.getaddrinfo, urllib.Request in the adapter process. The refresh subprocess is a separate process: API keys removed, proxies -> 127.0.0.1:9, `extract --code-only` needs no network (pilot A audit: 0 network events).
- Allowlist: `projects.json` id -> {root, out_dir}; request project must be a listed id or equal to a listed root's realpath; else `project_not_allowed`. out_dir must be absolute and neither inside nor containing root, else the project is refused at load.
- Readers load only `<out_dir>\published\graph-<sha12>.json` named by `published\current.json` ({snapshot, sha, built_at, fingerprint, prev}); sha256 verified on load. `work\` is never read by queries.
- Refresh: lock file `refresh.lock` (stale after 900 s; concurrent call -> `refresh_busy`); `python -m graphify extract <root> --code-only` with GRAPHIFY_OUT=<out_dir>\work, PYTHONHASHSEED=0. If the build or the work graph is unreadable it wipes `work\` and rebuilds once. Validation: parses, nodes need id/label/source_file, edges need source/target/relation/confidence, node count may not shrink >20% unless `force`. Publish: temp+copy -> replace snapshot, write `graph-<sha>.fp.json`, then atomic replace of `current.json`. Previous snapshot kept as last-good; others pruned. Failure -> `ok:false, error:refresh_failed`, pointer unchanged, still serving last-good. `skip_build:true` publishes whatever is in work\ (recovery/testing).
- Stale: stat-only fingerprint (relpath,size,mtime_ns) over the files in graphify's `work\manifest.json`, plus `git rev-parse HEAD` if `<root>\.git` exists, compared with the values stored at publish. Limitation: brand-new files not yet in the manifest do not flip stale; deleted files do.
- Nothing is written inside the project root (graphify output goes to out_dir via GRAPHIFY_OUT).

## Ranking policy (locate)
Scoring and seeding are graphify's own (imports below); the adapter adds only presentation policy: candidates = interleave of (A) the 1-hop neighbourhood of graphify's picked seeds ordered by graphify score and (B) graphify's global ranking; then a stable partition puts code symbols in non-test files first, test symbols next, then docs/docstrings/file nodes. This policy was chosen after looking at the 13 pilot questions, so the eval is not a held-out test.
Confidence: `high` iff a query term (len >= 6) equals the bare label of one of the top 5 locators (an exact-identifier match, graphify's exact tier). Raw score did not separate HIT from PARTIAL in the pilot data (top non-test scores: HIT 11.7-232, PARTIAL 8.5-69, MISS 9.7), so no score threshold is used.

## graphify internals depended on (site-packages\graphify\serve.py, 0.9.77)
- `_load_graph(graph_path)` :44 - JSON to networkx (directed)
- `_query_terms(question)` :293 - term extraction/stopwords
- `_score_query(G, terms, *, collect_per_term_seeds)` :554 returning `_QueryScores(ranked, best_seed_by_term)` :527 - the scorer `query_graph` uses
- `_pick_seeds(scored, ..., G=, best_seed_by_term=)` :761 - seed selection
- `_RELATIONAL_INTENT_TERMS` :863 - same intent-term filter as `_query_graph_text` :1386-1392 (4 lines replicated, signature-tested)
- `_traversal_view(G)` :1318, `_bfs(G, start_nodes, depth)` :1029 - neighbourhood
- `_find_node(G, label)` :1600 - symbol resolution for expand/path
- `_search_tokens(text)` :203 - matched_terms
- `importlib.metadata` for the version; CLI `graphify extract <root> --code-only` (cli.py:3351, :3748-3757) for builds; `update`, `watch`, `hook`, `install`, `global` are never invoked.
Tests (`test_contract.py`) assert these names and signatures; any upgrade must re-run them.

## Known limits
Module-level constants and non-code files are not nodes (so `MANIFEST_MAX_*`, `CLAUDE_LITE_TOOL_NAMES` are found only by file proximity). Paraphrases without symbol words usually fail; `confidence:low` + `fallback_hint` is the signal to grep. Line numbers are start lines only (graphify stores `source_location` as `L<n>`).

## Hardening (WP-CG1, code-graph install)
- Child guard: `guard\sitecustomize.py` first on PYTHONPATH for the refresh subprocess (`child_env()`); raises on socket.connect/getaddrinfo/urllib.Request in every child Python incl. multiprocessing workers. Non-Python children (git, cmd) are NOT covered. Test: test_hardening_a_child_network_guard.
- Reader retry: pointer re-read once, then fall back to `prev` (pointer holds prev_sha/prev_file_sha); responses carry `served: current|prev`; both bad -> `snapshot_unavailable`. Test: test_hardening_b_reader_falls_back_to_prev.
- New-file staleness: `git ls-files -z -c -o --exclude-standard` (if <root>\.git) else a pruned os.walk, filtered by `graphify.detect.CODE_EXTENSIONS`, minus graphify's `ignored_predicate(root)`, minus manifest files -> `stale_reason: new_files:N first=<path>`. Depends on detect.py CODE_EXTENSIONS :47, _is_noise_dir :1105, ignored_predicate :1761. Test: test_hardening_c_new_file_flips_stale (git and non-git). Cost on agent-broker: a stats call (includes this check and git rev-parse HEAD) took ~170 ms; each locate call pays the same ~110 ms staleness cost.
- Semantic snapshot id: sha256 of sorted nodes (id,label,source_file,source_location) + edges (source,target,relation,confidence); snapshot = first 12 hex; pointer also stores `file_sha` for integrity. Test: test_hardening_d_semantic_snapshot_id.
- Layout: venv\, gfy_adapter.py, gfy_client.py, projects.json, questions.json, eval_locator.py, coverage_probe.py, test_contract.py, guard\, graphs\<project>\{work,published}.
