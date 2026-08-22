# Agent Switchboard

**Use Claude Code, Codex, Gemini, Antigravity, and VS Code together — without copy-pasting context between them.**

Agent Switchboard is a **local MCP bridge** that lets your AI coding agents hand tasks to each other, review each other's work, run multi-round debates, and share compact project context — a local nervous system where the tools stay separate but coordinate through one local broker.

**No API keys. No cloud broker. No extra billing.** It uses the local CLIs, IDE bridge routes, and subscriptions you already have where possible.

> **⭐ If this saves your agent workflow, please star the repo so others can find it!**

## Why this exists

Modern AI coding workflows are fragmented. You might use **Codex** or **Claude Code** as the reasoning brain, **Gemini Flash** for bounded workhorse tasks, and **Antigravity / VS Code** for workspace context — but normally they can't talk to each other, so you end up manually copying plans, files, errors, and context from one assistant to another. That's fine for small tasks; it gets messy fast on real projects. Agent Switchboard gives those agents a shared local coordination layer so they cooperate instead of working blind to each other.

## Fast Version

- **Ask one assistant to use another** - from Codex, ask the Claude frontier to audit; from Claude, ask the Codex frontier to reason; send bounded implementation, search, and preparation work to Gemini Flash/Antigravity.
- **See across chats** - pull a *compact snapshot* of what another agent's session knows; **Codex and Claude Code are read on demand from disk**, no copy-paste.
- **Run cross-model debate** - Codex vs Claude for N rounds, then synthesize a verdict.
- **Keep the selected model as the brain, route labour cheaply** - global Codex/Claude/Gemini rules, reader/workhorse roles, and a completion audit are installed and refreshed by the same exe.
- **Use Gemini Flash as an external workhorse** - Codex, Claude, and Gemini brains can proactively route bounded cheap labour through the Switchboard MCP `route_agent_task` tool, without treating Flash as a native child or authoritative brain.
- **Token compaction is built in** - compressed handoffs, compact context packs, work memory, and retrievable originals instead of dumping entire transcripts.
- **Keep it local** - SQLite state under `~/.agent-broker`; no private chat scraping, no cloud broker.
- **Use subscriptions you already pay for** - no required API keys or metered orchestration service.
- **Know the truth** - `doctor` reports which routes are full, partial, or app-only on your machine.

Everything user-facing — the `agent-switchboard.exe` binary, the command, and the MCP server key — is `agent-switchboard`. Internally, local state stays in `~/.agent-broker` and the Python entrypoint is `agent_broker_mcp.py`.

> Built for [Antigravity](https://antigravity.google) and VS Code users. Antigravity is a VS Code fork, so the same bridge extension installs in both.

> **Honest scope:** only **Antigravity** has a true programmatic in-app send *and* a structured reply back to the broker. Claude/Codex are reached through a CLI round-trip or an auto-opened inbox file - see [Delivery, honestly](#delivery-honestly). This is a power-user tool for people who already run these assistants; it drives logged-in subscription UIs, so read [Terms & risk](#terms--risk) first.

---

## Requirements

Two supported install paths — pick one:

- **Self-contained `agent-switchboard.exe`** (no Python needed). One file from the [Releases](../../releases) page does everything: installs the MCP server into every assistant, installs the bridge extension (the VSIX is **embedded**), and runs the MCP server itself (`agent-switchboard.exe serve`). Has a built-in uninstall.
- **Python 3.10+** (run from source). The broker is one dependency-free Python file; agents launch it as `python agent_broker_mcp.py`.

Other notes:
- Windows 10/11 for the installer, bridge auto-select, and shortcut patching (the broker itself is cross-platform; the installer/CDP layer is Windows-first today).
- **Antigravity CLI (`agy`)** is required for the default headless Antigravity route. Install it from the [official Antigravity CLI docs](https://antigravity.google/docs/cli/installation); without it, Switchboard falls back to the existing in-app bridge/inbox.
- **Node.js on PATH** is needed only for the CDP helpers (Antigravity model auto-select, Codex/Claude webview submit).
- Optional: `pip install tiktoken` for exact token accounting (a `chars/4` estimate is used if it's absent; the exe bundles it).

---

## Quick Start (Windows)

1. **Close Antigravity and VS Code.** The installer refuses to run while either IDE is open, so extensions and debug flags can't be left half-updated.
2. **Install** one of two ways:

   **A — Self-contained exe (no Python):** download `agent-switchboard.exe` from the Releases page and run it. Pick **Install** from the menu (or `agent-switchboard.exe install`).

   **B — From source (Python 3.10+):**

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-agent-broker.ps1
   ```

   Either way the installer detects which assistants you have (Codex, Claude Code, Antigravity, VS Code), **registers the MCP server with each**, installs the bridge extension (VSIX embedded in the exe; auto-built/located from source), and writes config. If **Antigravity is installed**, it then **offers (default Yes) to enable automated in-app model selection** — press Enter to accept (it patches the Antigravity launcher to open a local debug port) or decline to skip. Every config it edits is backed up first.
3. **Open Antigravity / VS Code again** so the `Agent Switchboard Bridge` extension activates.
4. **Try it.** In any registered assistant: *"Use Agent Switchboard to ask Claude Opus to audit this function."*
5. **Check what actually works on your machine:** run `agent-switchboard.exe doctor` (or `python agent_broker_mcp.py bridge doctor`). It's read-only and tells you, per assistant, whether a CLI/extension is present, which delivery route you'll get, and whether a headless debate can run — see [Diagnostics: `doctor`](#diagnostics-doctor).

**Uninstall / rollback (both paths):** run `agent-switchboard.exe uninstall` (or `python setup.py uninstall`), or pick **Uninstall** from the menu. It reverses MCP registration in all four hosts, **removes the bridge extension**, and removes the installed broker exe. Add `--remove-data` to also delete `~/.agent-broker`. The broker uses whatever subscriptions your assistants are already logged into.

---

## What It Does

| Goal | ✅ |
|---|---|
| Let Codex, Claude Code, and Antigravity consult each other | ✅ |
| Use existing **subscriptions** — no API keys, no extra billing | ✅ |
| Keep all shared state **local** (SQLite), never scrape private chat history | ✅ |
| **Token compaction** so cross-agent calls don't burn context | ✅ compressed handoffs + compact context packs with a locally-stored, retrievable original (Headroom-style *retrieval*, not a reversible codec) |
| Keep a short per-topic **work memory** so the next model sees what changed, where, why, checks, risks, and next step | ✅ |
| **Peek at another open chat** — fetch a *compact snapshot* of what another agent's session knows, on request (opt-in, local, never silent scraping) | ✅ active context snapshots; **Codex & Claude Code read on disk**, with **Antigravity local task/log/activity fallback** (`request_context_snapshot` → `get_latest_context_snapshot`) |
| **Cross-model debate** — two assistants debate N rounds headless on your subscriptions, then a synthesis judge writes a verdict | ✅ (`agent-switchboard debate`) |
| Route Codex, Claude, and Antigravity to their **headless CLIs** by default; use the **in-app chat/inbox** only when asked or as fallback | ✅ |
| Select Antigravity CLI models naturally (`gemini flash` for the moving workhorse, or an exact version pin) and discover future models from live `agy models` output | ✅ |
| Let Codex/Claude proactively use newest live Flash High as a cheap external workhorse, with native fallback and independent-package concurrency rules | ✅ |
| Fall back to the in-app extension / app automatically when a CLI isn't installed | ✅ (see caveats in the docs) |
| Send a prompt straight into Antigravity's chat panel + get a structured reply back | ✅ (`antigravity.sendPromptToAgentPanel`, the only full in-app round-trip) |
| Pick the Antigravity model automatically | ✅ **Offered at install** (default on) when Antigravity is detected — patches the launcher to open a local CDP debug port so the broker auto-selects the model in-app; just decline at the prompt to skip |

---

## How It Works

The broker is a dependency-free Python MCP server. Each assistant talks to it over stdio JSON-RPC; the broker keeps shared state in one local SQLite file (WAL mode, so multiple hosts can poll it concurrently) and routes work to the right place.

**Routing priority:**

1. **Surface** — Codex, Claude, and Antigravity default to the **headless CLI** (reliable, model-switchable, answer returned inline). For Antigravity, that means the standalone `agy` executable, not the IDE's `antigravity chat` launcher. Say **"in app"** / **"inbox"**, pass `surface: extension` / `surface: inbox`, or set `use_inbox: true` to force the bridge panel. If `agy` is absent, an automatic Antigravity route falls back to that bridge/inbox.
2. **Model** — vague Codex requests resolve from live `codex debug models` metadata; Claude frontier consults use the moving `fable` alias and fall back to the moving `opus` alias only on an explicit availability/entitlement error. Bare Antigravity and **"Gemini Flash"** select the newest exact stable `gemini-<numeric>-flash-high` slug advertised by live `agy models`, using numeric version order; the bundled static slug is offline fallback only. Explicit version pins remain exact, and preview/nonconforming SKUs are never auto-promoted. Gemini Flash is a useful non-authoritative workhorse-level adviser: a higher version does not promote it above Sol/Fable or make it automatically authoritative.
3. **External workhorse lane** — Codex, Claude, and Gemini brains should proactively consider newest live Flash High through Agent Switchboard for bounded search/read/extraction/summaries/drafting, low-risk implementation/tests from an approved plan, and independent parallel packages. Flash is not a native child agent. Each call carries exactly one package and schema-enforced output; implementation requires a package id, a 1-5-file allowlist, and explicit acceptance criteria. Flash cannot receive danger-full-access or live production work. If `agy`/Flash is missing, quota-limited, times out, mismatches, returns malformed/contradictory output, or fails, use the host's native cheap reader/workhorse and record the fallback. The brain independently verifies cited lines, the actual diff, and checks before accepting or sending another package.
4. **Token budget** — every routed task carries a task contract (`implementation_plan`, `co_audit`, `debate`, `review`, …) with a word budget, and a compressed context pack instead of raw history. If a caller inlines a bloated `prompt` (over a soft token limit), the broker stashes the full text as a retrievable `context_ref` and returns a `prompt_notice` nudging it to send a short instruction + ref next time — so token discipline is enforced by the system, not left to each agent.

Use the Switchboard MCP tool `route_agent_task` with `target_agent: "antigravity"`, `surface: "cli"`, `target_model: "gemini flash"`, and `effort: "high"`. The sender brain must not run `agy` directly; `surface: "cli"` tells Switchboard to invoke its internal CLI backend. Set `mode: "plan"` for read-only work. For implementation, set `mode: "accept-edits"` and include `work_package_id`, 1-5 exact `allowed_files`, and `acceptance_criteria`; Switchboard rejects an incomplete or whole-plan handoff.

---

## See what another chat knows (active context snapshots)

Working in one assistant but need the *current* state another open chat is holding? Ask for a **context snapshot** — a COMPACT continuation state (objective, plan, touched files, checks, risks, next step), **not** a full transcript, and **never** silent scraping (it's opt-in and local).

- **`request_context_snapshot(project, topic, target_agent)`** asks the best available open surface for that compact state.
- **Codex & Claude Code fast path (on disk):** the broker reads the live session transcript on disk — **Codex** from `~/.codex`, **Claude Code** from `~/.claude/projects` — redacted + truncated and **scoped to the session whose `cwd` matches the project** (no cross-project leak), returning **immediately** with no agent cooperation or CDP needed. The two CLIs are symmetric.
- **Antigravity fallback:** when no live bridge snapshot is available, the broker can read local Antigravity task/log/activity files (`~/.gemini/antigravity-ide/brain` plus workspace/file history) and return a bounded, clearly low/medium-confidence continuation snapshot. This is not guaranteed to be the visible chat transcript, but it prevents dead-end "cannot find context" failures.
- **Other surfaces:** the request is queued for a capable bridge host (`claim_context_snapshot_request` / `complete_context_snapshot_request`, race-safe + idempotent, with a stale-claim reaper), or picked up from a `.agent-broker/context-snapshots/` fallback file. If **no live surface is heartbeating** and no local fallback exists, the request reports `no_live_surface` (with guidance) instead of queuing forever with no claimer.
- **Honest limit:** a surface feeds the nerve system only if it's readable on disk (Codex/Claude Code), a live heartbeating bridge (Antigravity/VS Code), **or** it proactively records. A disconnected helper — e.g. the **Claude desktop app** (Electron + server-side history, not on disk) — can be *registered to push* context, but cannot be read on demand. `doctor` shows exactly which surfaces can contribute, so blind spots are visible, not surprising.
- Read it back with **`get_latest_context_snapshot`** — it also folds into `get_context_pack` ("Latest Context Snapshots") and `get_topic_status`, so the next model picks it up automatically. Live-host routing uses `record_surface_heartbeat` / `list_live_surfaces`.

This is the cross-chat "peek" layer: agents and IDEs can see what another agent's session currently knows and fetch it on request, without copy-pasting transcripts.

---

## Delivery, honestly

There is a real difference between **delivered** (a file/prompt reached the surface), **auto-opened** (the bridge opened it for you), **submitted** (it was actually sent into a chat), and **completed back to broker state** (a model-tagged reply landed in the broker). Antigravity is still the only true structured in-app round-trip; queued Codex requests also get a broker-owned CLI worker when the Codex CLI is available.

| Target | Mechanism | How far it gets |
|---|---|---|
| **Antigravity CLI** (default) | `agy --print <prompt> --model … --effort … --output-format json --json-schema …` (direct argument-array invocation, no shell) | **reported, pending brain verification** — schema-enforced headless round-trip; `plan` is sandboxed, while bounded implementation uses `accept-edits` |
| **Antigravity** (in-app Gemini/Claude) | `antigravity.sendPromptToAgentPanel` (+ optional CDP model select) | delivered → submitted → **completed back to broker** (`complete_antigravity_request`) — the only structured round-trip |
| **Claude extension** | `claude-inbox` markdown, **auto-opened** + best-effort CDP auto-submit | delivered → auto-opened → (often) submitted → **recorded back to broker**: the request now has a durable `claude_requests` row, so a reply via `respond_to_request` lands on it, or a file written to `claude-responses/` is ingested by `bridge claude-responses` |
| **Claude CLI** | `claude -p` headless (prompt via stdin) | **completed** — full headless round-trip |
| **Codex extension / inbox** | `codex-inbox` markdown, **auto-opened**, plus bounded Codex CLI worker when available | delivered → auto-opened for visibility; the worker records **completed/failed** back to broker state so polling does not hang forever. Extension-only/no-CLI installs remain manual via `respond_to_request` |
| **Codex CLI** | `codex exec` headless | **completed** — full headless round-trip |
| **Gemini** | `gemini` CLI (`-m <model>` honored) or `GEMINI_API_KEY` | **completed** via CLI; the API path is an off-by-default escape hatch |

> **Answer return-path:** any surface without a native completion API closes the loop by calling **`respond_to_request(request_id, response)`** — the broker records the answer + timing on the request and refreshes a per-topic **`ledger.md`** (`get_request_ledger`). Codex inbox requests now also start a bounded CLI worker by default, so `request_result` returns an answer or a terminal error instead of staying `delivered` forever.

> **Model enforcement, honestly:** the broker can only *switch the answering model programmatically* on **Antigravity** (CDP UI automation, best-effort) and the **CLIs** (`--model`/`-m` flag). Codex/Claude CLI responses are attested from runtime metadata and fail closed when metadata is missing or mismatched. `agy` receives the exact dynamically resolved Flash slug and returns a conversation id, but currently does not expose independent actual-model attestation, so the result labels the requested route without claiming stronger proof. Answer prose and usage summaries are never accepted as model proof. The broker **cannot** drive the Codex- or Claude-*extension* model picker, so those surfaces still receive a strict guard plus a notification to select the requested model.

> **Model + effort on the CLIs:** model and reasoning effort are **separate inputs**, never folded together. Pass **`effort`** and the broker sets the CLI's own effort flag. A bare family request defaults to the live Codex frontier at `max`, Claude's moving `fable` alias at `max` (then `opus` only when unavailable), or Antigravity's dynamically selected latest stable Gemini Flash High workhorse at `high`. Explicit `cheap_read` and `balanced` policies select dynamically discovered Codex reader/workhorse models or Claude `haiku`/`sonnet`; prompt keywords never guess. Flash remains a non-authoritative external workhorse regardless of version. If Claude's frontier is unavailable, Flash supplies a degraded advisory second opinion; for routine bounded labour, both Codex and Claude may proactively use it with the failure/concurrency safeguards above. The selected main-session model is never rewritten. Flash permission bypass is prohibited; production deployment remains with the brain.

> The broker is **target-driven** when a target is named. If a Codex/Claude caller leaves the target completely empty, Switchboard uses the caller only as a fallback: Codex defaults to Claude, and Claude defaults to Codex. A named target or prompt phrase like "consult with Claude" still wins.

---

## Diagnostics: `doctor`

Because "what works" depends on **what you have installed**, the broker ships a
read-only `doctor` that probes this machine and tells you the truth — no state is
changed.

```powershell
agent-switchboard.exe doctor          # rendered report
agent-switchboard.exe doctor --json   # machine-readable
# from source:  python agent_broker_mcp.py bridge doctor
```

For each assistant it reports: whether the **CLI** is found (and a live
`--version` smoke test), whether the **extension** is installed, the **CDP port**,
the **delivery route** you'll actually get, the **reply path**, and whether a
**headless debate** can run. It also prints a **nerve-system** view — which
surfaces can feed `request_context_snapshot` (on-disk fast-path vs live bridge vs
push-only), so a blind spot like a disconnected desktop app is visible. It flags
broker/bridge version drift and prints actionable next steps.

**What each install combination gets you** (this is what `doctor` checks):

| You have… | Codex / Claude result |
|---|---|
| **CLI on PATH** | full headless round-trip (best); answer returns inline |
| **Extension only, no CLI** | the broker still *delivers* into the extension (auto-opened inbox + best-effort CDP auto-submit), but it's **semi-manual** and not a silent headless round-trip. `doctor` reports this as `partial` / `delivery-only` |
| **Desktop app only** | clipboard hand-off only — no programmatic return path |
| **Neither** | `doctor` tells you exactly what to install |

> **Headless debate** (running both sides automatically) needs **both** the Codex
> **and** Claude CLIs present — `doctor` reports `headless autonomous debate
> runnable: YES/no` before you try. Extension-only setups can still get a one-shot
> second opinion, just not an autonomous multi-round run.

---

## Changelog

### v1.0.39 (a worker's finished work is no longer thrown away)
- **Work that succeeded was being reported as a failure and discarded.** The worker CLI exits with an error process status whenever any single tool call inside it fails, even when the task itself completed correctly. Validation treated that exit status as authoritative and rejected the whole dispatch, discarding a correct, fully-formed result along with it. The structured payload the worker returns is now the primary signal: a process-status mismatch alone is recorded as a caveat and the result is accepted with caveats, while a missing or unparseable payload, a schema violation, a scope violation, or a safety-rule failure still rejects exactly as before. Every result now carries a disposition (accepted, accepted with caveats, or rejected) and the list of caveats behind it.
- **A rejected dispatch now keeps its artifacts for inspection instead of losing them.** When a rejection follows real worker execution, its stdout, stderr, parsed payload, and a manifest with hashes and sizes are written to a quarantine location that is pruned by age, count, and size, and the rejection receipt names that location and states plainly that the quarantined artifacts were not accepted and not applied. Rejections that happen before a worker ever runs are unchanged. Error messages for a missing structured payload now report what the backend actually returned instead of a generic complaint.

### v1.0.38 (quiet disconnects + a steadier worker lane)
- **A peer hanging up is no longer logged as an error.** When the other side of the connection closes without warning, the server now recognizes that as a normal disconnect (including the platform-specific way Windows reports it) and logs a single quiet line instead of a full error trace; a genuine write failure still gets the full trace. Request ids in these log lines are now length-capped so an oversized or malformed id can't blow up the log.
- **Fixed a bug where the worker lane could go dark for no real reason.** The file that tracks model availability was being updated by many processes at once with no coordination, so failure counts could be lost or double-counted — occasionally tripping the circuit breaker that disables the worker lane even while model discovery was working fine. Updates to that file are now serialized and written atomically, so the file can never be left half-written, and an update that can't get a lock in time is skipped and logged rather than blocking a dispatch.

### v1.0.37 (stdio transport reliability)
- **Responses are now serialized and written under a single guarded path.** Every reply is serialized inside the request guard and emitted through one writer that encodes, writes, and flushes under a lock, logging the request id, byte count, and a hash digest. Previously a response that failed to serialize could take the server down after the work had already completed, leaving the caller to wait out its own timeout with nothing returned.
- **Malformed frames no longer end the session.** Frames are read as raw bytes and decoded as strict UTF-8 one at a time, so a malformed frame is reported as a parse error instead of dropping the connection. UTF-8 request bodies are no longer mangled by the host's locale codec.
- **Failures now answer with the request's own id instead of null**, so a caller can match an error reply to the call it made. Host registrations set `PYTHONUTF8=1`, and new end-to-end tests exercise the server as a real subprocess.

### v1.0.36 (model resolution order)
- Model resolution now consults the observed model catalog before probing the network, avoiding an unnecessary network round trip when the answer is already known locally.

### v1.0.35 (manifest-scoped packages + safer staging)
- **The package manifest is now the scope, not the containing directory.** A dispatch no longer walks the whole project tree looking for files to read, which previously caused every read package to be refused on large workspaces.
- **The Flash worker gets real tool access, confined to a disposable copy.** Work happens against an isolated copy of the relevant files rather than the live tree.
- **Staged write-back no longer destroys a concurrent edit.** Writing results back checks for a conflicting change instead of blindly overwriting.
- **Consult children no longer inherit the whole environment.** Child processes get an allowlisted environment instead of a full copy of the parent's.

### v1.0.34 (Flash-first workhorse lane + gate repair)
- **Stale MCP registrations are detected and repaired.** `refresh`/`hierarchy` now re-points any host still registered to an older broker binary at the canonical `agent-switchboard.exe`, and `status` prints a per-host registration health table with the version each registered command actually reports. This closes a silent failure mode in which the hooks ran the current build while every host's MCP *server* stayed on an old one — so schema, envelope, and validation changes shipped but never took effect. A running host keeps serving the process it launched, so a window reload is still required after a repair.
- **Flash is now the default workhorse lane, with objective exceptions.** The managed hierarchy, the runtime routing rules, and the `route_agent_task` description all lead with the Flash package call instead of "native first". Choosing a native role for an eligible package now calls for a stated `flash_skip` reason (`host-tools:`, `unshared-state:`, `flash-failed:`, `flash-unavailable:`, `atomic-oversize:`). Every prior Flash prohibition is unchanged.
- **A completed Flash dispatch relieves the labour gate; a failed one does not.** `route_agent_task` returns a server-issued, ledger-backed receipt (`broker:<uuid>`), the resolved and backend-attested model, elapsed time, and an outcome. Only `completed_verified` grants credit, the receipt is verified against the ledger and is single-use, and credit for a blocked, rejected, failed, or unavailable dispatch is refused — so relief cannot be farmed by firing a route known to fail.
- **The gate actually engages.** The direct-labour allowance drops from ten to four (it previously exempted most real turns), hook matchers changed from a static tool list to `.*` after `PowerShell` — this platform's primary shell — was found missing from it and bypassing labour counting, mutation tracking, and the direct-`agy` deny. Tool-name classification now canonicalizes the MCP server segment, so `mcp__agent-switchboard__*` and `mcp__agent_switchboard__*` are treated alike. The direct-`agy` deny no longer depends on a host field being present.
- **Resolved-model mismatch and out-of-scope writes are rejected.** Switchboard compares the model the backend attests against the one requested, and hashes the package root before and after an implementation package to check the worker's file-change claims against the filesystem. An out-of-scope change is reported and refused; nothing is auto-reverted, because a file outside the allowlist may be the user's.
- **New `run_evidence_probe` tool** gives the read-only reader lane the measurements it could not make without a shell — file hashes, real encoding/BOM/line endings, git state, file stat, bounded literal grep, filtered process list. The catalog is fixed, with no pass-through command probe; secret-material filenames and symlink/reparse paths are refused, results are capped, and process listings never include command lines.
- **New verification commands.** `agent-switchboard.exe gate-harness` proves enforcement deterministically (including the negative control a cooperative live session may never produce); `canary flash` runs one real read-only package end to end and prints its receipt and attested model; `routing-report` summarizes a session's lanes. Every MCP result now carries `_meta.switchboard` with the version, a build fingerprint of the running image, and the server pid, so "is this window on the new build?" is answerable from any response.

### v1.0.33 (managed Gemini hierarchy + MCP-only sender boundary)
- Install/repair now manages the same checksum-protected hierarchy in `~/.gemini/GEMINI.md`. It narrowly replaces the known obsolete global Pine-v6 persona, while preserving unrelated user-authored Gemini content and refusing tampered managed blocks.
- Sender brains must enter cross-vendor and Flash labour through Switchboard MCP `route_agent_task`. "Through CLI" means `surface: "cli"` on that tool; only Switchboard may start `agy`, and the installed Codex/Claude pre-tool hooks deny sender-side direct invocation.
- A Switchboard-launched Flash session is explicitly the non-authoritative worker for one schema-enforced package, never the brain/router and never an autonomous whole-plan executor.

### v1.0.32 (fail-closed Gemini Flash work packages)
- Every Antigravity Flash CLI call now uses `--output-format json --json-schema ...`. Switchboard validates the structured result locally and rejects missing fields, malformed output, contradictory completion, out-of-scope file changes, and unsupported claims that a defect is intentional/by design.
- Flash implementation is limited to one package per call and requires `work_package_id`, 1-5 exact `allowed_files`, and explicit `acceptance_criteria`. Whole-plan execution, continuation to another package, `danger-full-access`, production SSH, live credentials, destructive operations, migrations, and live deployment are prohibited.
- A valid Flash result returns `brain_verification: pending` and `accepted: false`. The Codex or Claude sender must independently inspect cited primary lines, the actual diff, and check output before accepting the package or dispatching the next one; ambiguity and failure fall back to the native reader/workhorse.

### v1.0.31 (dynamic Gemini Flash external workhorse routing)
- Bare Antigravity and `gemini flash` requests now select the newest exact stable `gemini-<numeric>-flash-high` slug advertised by live `agy models`, including its current tabular output. Numeric version ordering handles future releases automatically, while preview/nonconforming models are excluded, the bundled 3.6 slug remains an offline fallback, and explicit version pins remain exact.
- Gemini Flash High is classified as a proactive external workhorse for bounded search, reading, extraction, summaries, drafting, and approved low-risk implementation/tests. It remains non-authoritative regardless of version and cannot replace the Sol/Fable frontier brain; when Claude's Fable-to-Opus chain is unavailable, Flash is only a degraded advisory second opinion and the Codex brain retains judgment.
- Missing, quota-limited, timed-out, mismatched, or failed `agy`/Flash work falls back to the host's native cheap reader/workhorse and records the fallback. Flash and native workers may run concurrently only on independent packages: reads may be parallel, while writes remain serial unless demonstrably isolated, with the brain reviewing evidence and actual diffs.

### v1.0.30 (Windows-safe Claude hook execution)
- Claude Code routing hooks now use executable-plus-argument-array form, preventing `/usr/bin/bash` from stripping backslashes out of Windows executable paths. Install/repair migrates legacy string-form Switchboard hooks without changing other user hooks; Codex hooks keep their existing command-string format.
- Completion now requires every Claude-managed background Bash, PowerShell, or Monitor job started by a package to reach a terminal result or be stopped. Launching or detaching background work does not count as verification.

### v1.0.29 (strict native pre-labour enforcement)
- The installed `PreToolUse` gate now atomically allows one bounded block of direct brain labour, then denies the next eligible read, search, evidence, test, documentation, or mechanical call until a same-vendor native reader/workhorse starts or a package-specific brain override is registered.
- Native-agent relief is bounded rather than permanent: each cheap-role start opens only the next block, while completed planning work cannot disable later implementation enforcement. Switchboard consultation controls remain available, but ordinary research MCP calls count as evidence labour.
- Direct-labour counts are captured before tool execution, resist parallel-call and retry bypasses, and set a floor that the final routing audit cannot under-report. Managed native roles are also instructed to cap their return at 8,000 characters so cheap-agent transcripts do not flood the brain context.

### v1.0.28 (bounded context ingress and complete routing audits)
- Oversized MCP verification payloads are quarantined outside the brain context by the installed `PostToolUse` hook. The brain receives a compact evidence reference and must request an explicit field projection or output cap instead of ingesting the raw response.
- Decision premises are explicit: readers locate candidate evidence and distinguish fact from interpretation; the brain adjudicates only the minimum primary evidence whose truth could change a decision.
- Completion audits now cover unplanned as well as planned work and include a direct-brain labour census for reads, searches, evidence queries, tests, documentation, and other routine work.
- `agent-switchboard.exe --version` (plus `version` and `-v`) reports the packaged release, so an installed binary can be verified directly.

### v1.0.27 (native-first labour routing)
- Same-vendor labour now uses native subagents first: Codex `explorer`/Luna-low and `worker`/Terra-medium, or Claude `Explore`/Haiku and `economy-worker`/Sonnet-medium. That same-vendor restriction remains; the current policy also permits the distinct external Antigravity Flash workhorse lane documented above.
- Plans now carry a portable semantic lane plus execution mechanism and executor-resolved exact model/effort. A Claude-authored Sonnet/Haiku package is re-resolved to Codex's current native worker/reader when Codex executes it, and vice versa.
- The completion gate records host-issued `SubagentStart`/`SubagentStop` ids and accepts mixed `native:<agent-id>` and `broker:<uuid>` receipts. Bare brain overrides no longer bypass the whole audit; retained work uses a package-specific `override: brain - <WP-ID>: <specific reason>`.
- A one-shot native-first checkpoint fires after ten mutating operations without a completed cheap native agent. Dirty-worktree or deployment ownership no longer excuses read-only, test, evidence, documentation, or isolated mechanical labour.
- Dynamic Codex role selection excludes the frontier brain from cheaper roles when alternatives exist. A transient catalog failure keeps the last-known managed native roles instead of installing stale hard-coded model ids.

### v1.0.26 (future-proof brain/labour hierarchy)
- The install/repair flow now owns checksum-marked global Codex and Claude hierarchy blocks, cheap reader/workhorse role files, and merge-safe prompt/tool/stop hooks. It preserves existing hooks and main model/effort settings; the same refresh runs whenever the installed MCP server starts.
- Codex brain/worker/reader roles are selected from live `codex debug models` priority/visibility/description metadata. Claude uses moving family aliases: Fable/max for the peer brain, Opus/max only on an explicit Fable availability failure, Sonnet/medium for workhorse implementation, and Haiku for read-only labour.
- Queued Claude jobs now preserve their requested permission mode instead of hardcoding `plan`, so approved routine implementation can execute on the workhorse. Direct and async results report requested, attempted, and runtime-attested actual models.
- A bounded completion gate observes mutating tool use and requests a broker-verified routing audit (or an explicit brain override) before an implementation can claim completion. It fails open when the broker ledger is unavailable and blocks at most once per turn.

### v1.0.25 (exact Haiku pin + tighter delegation contracts + codex discovery order)
- `CLAUDE_CHEAP_MODEL` now pins the exact `claude-haiku-4-5-20251001` model id instead of the floating `haiku` alias; the static Claude catalog entry was updated to match while keeping all existing Haiku aliases (`haiku`, `claude haiku`, `haiku 4.5`) resolvable.
- Implementation-plan and implementation task contracts, and the cost-aware routing rules, now require each work package to state `Route | exact model/effort | deliverable | verification | escalation`, require workers to record an `override: brain - <reason>` line when deviating from the assigned route, reclassify risk/difficulty at each work-package boundary, return the first ambiguity or failed fix to the brain before delegating the deterministic remainder, default to parallel reads / serial writes, and require the final routing audit to cross-check the broker's actual-model ledger rather than a worker's self-report.
- `discover_codex` (broker) and `setup.py`'s config writer/repair now resolve Codex CLI path in the same order: a valid configured `codex_path`, then a valid `CODEX_CLI_PATH` marker from `~/.codex/config.toml`, then `PATH`.
- Direct and asynchronous Codex/Claude CLI calls now record the runtime-reported model (and Codex effort), label missing evidence `unverified`, and fail closed on a requested-model mismatch. Claude trusts only the main assistant event; Codex trusts the persisted `turn_context` tied to the emitted thread id.
- Codex request rows now preserve `read-only`, `workspace-write`, or `danger-full-access` through the detached worker instead of silently forcing every worker to read-only. Native Windows sandbox failures still escalate to the brain; the broker never weakens the requested sandbox automatically.

### v1.0.24 (cost-aware frontier brain + worker routing)
- Bare serious consultations now use the current frontier brain at maximum effort: Codex Sol/max and Claude Fable/max.
- Explicit `model_policy="cheap_read"` routes read/extract/summarize labor to Luna/low or Haiku (without an unsupported Haiku effort flag).
- Explicit `model_policy="balanced"` routes bounded implementation/testing from an approved plan to Terra/medium or Sonnet/medium. Prompt keywords never silently downshift a serious request.
- The routing guide now documents both families and the evidence/escalation contract remains in the shared ground rules.

### v1.0.23 (cross-agent output discipline)
- Shared task contracts now lead with the result, describe failures concretely, avoid invented estimates, and prefer plain language.
- Review/audit/bug-hunt contracts report every substantiated in-scope finding, keep unrelated observations separate, and identify residual verification gaps when no finding is confirmed.

### v1.0.22 (Antigravity CLI-first routing)
- **Antigravity now defaults to the standalone `agy` CLI**, matching Codex and Claude's CLI-first behavior. Calls return stdout directly; if `agy` is missing, automatic routing falls back to the existing in-app bridge/inbox.
- **Explicit surface intent always wins.** `surface="extension"` / `"inbox"` or `use_inbox=true` forces the in-app bridge; `surface="cli"` requires the headless CLI.
- **Model selection uses stable, live CLI slugs.** `"flash high 3.6"` resolves to `gemini-3.6-flash-high`, and `list_agent_models` merges `agy models` output so newly released models become available without hardcoding another broker release.
- **Execution mode follows task intent.** Consult/review defaults to sandboxed `plan`; `task_kind="implementation"` defaults to `accept-edits`; bypassing permission prompts remains an explicit `danger-full-access` choice.
- Added `consult_antigravity`, `antigravity_cli_path`, doctor reporting for `agy`, installer detection, and CLI/inbox fallback guidance.

### v1.0.21 (limits are advisory — stop force-shrinking data between sessions)
- **Inline consult responses: default 5k → 20k chars, hard ceiling 40k → 200k** (`AGENT_BROKER_CONSULT_RESPONSE_CHARS` / `_MAX`). Full responses were always preserved (history + request row + `response_ref`), but the small inline cap force-shrank what the calling session actually saw.
- **Truncation no longer mangles structure.** The old path collapsed all newlines/indentation (destroying code blocks and diffs). The rare over-ceiling cut is now a clean tail-cut at a line boundary with an explicit `[... truncated inline; FULL response preserved — see response_ref]` marker.
- **Word budgets are now explicitly ADVISORY** in the task contract, ground-rules file, and the prompt-size notice: aim lean, avoid *redundant* content (re-pasted files the receiver can read itself), but **never omit unique data** needed for a correct/complete answer. The prompt notice now states the prompt was delivered in full.
- Audited the full transfer path: prompts (MCP → DB → stdin → CLI) and responses (CLI pipe → DB → `request_result`) move **untruncated**; only display excerpts (history, event log) are shortened.

### v1.0.20 (no stray Claude tabs — worker requests skip the UI inbox)
- **Worker-handled Claude requests no longer open a new Claude tab.** v1.0.19 wrote the inbox `.md` *and* started the CLI worker, so the bridge also delivered the prompt into the IDE — a stray tab popped up while the worker answered headless. Inbox files are now written **only when no CLI worker took the request** (UI fallback path), and the worker deletes any leftover inbox copies when it finalizes (covers rows queued by older servers).

### v1.0.19 (Claude/Fable requests get the CLI worker too)
- **Queued Claude consults no longer sit "queued" forever.** A Claude inbox request (e.g. Opus consulting Fable) used to depend entirely on an interactive session or the bridge picking the inbox file up — in a headless environment nothing ever did. Queueing now also starts a detached **Claude CLI worker** (`claude -p --model fable/opus/sonnet/haiku --effort …`, same machinery as the Codex worker: atomic claim, 1800s cap, rowcount-gated side effects, no console window) that records the answer; collect it with `request_result(request_id, wait_seconds=180)`. The inbox file stays as the UI fallback. Disable via `AGENT_BROKER_CLAUDE_QUEUE_AUTORUN=0`.
- **Targets that aren't CLI-runnable** (e.g. Antigravity panel models) keep the UI delivery path, and now **expire with a clear error** after ~35 min instead of hanging forever; stale-expiry covers `claude_requests` like it covers `codex_requests`.
- Schema: `claude_requests` gained `effort`, `cli_model`, `worker_pid`, `worker_started_at`, `worker_completed_at` (auto-migrated).

### v1.0.18 (max-effort headroom + honest wait expectations)
- **Worker cap raised 900s → 1800s.** A max-effort Sol consult on a real design prompt commonly runs 5-15 minutes (live-measured: 8m21s); the old cap risked killing legitimate long runs. Override via `AGENT_BROKER_CODEX_ASYNC_TIMEOUT_SECONDS`.
- **Pending/running responses now state the real ETA.** The pending payload carries `typical_wait: "5-15 minutes at max/xhigh effort"` and `retry_after_seconds: 120` (was a hammer-inducing 20), and `request_result` reports `elapsed_seconds` plus a "this is normal, not a hang" note for max/xhigh — so callers stop reading a 8-minute run as stuck.

### v1.0.17 (consult is always Sol/max — no silent downgrade)
- **Removed the prompt-keyword "cheap read" guesser.** It was silently routing real consults to `gpt-5.6-luna` at low effort whenever the prompt mentioned reading/lines/deleting — producing hedged, untrustworthy answers. Luna now runs **only** when the caller explicitly sets `model_policy='cheap_read'` or names a Luna model.
- **A serious consult on Sol is forced to `max`.** Even if the caller passes `high`/`medium`, a consult/plan/audit/review/debate is clamped up to `max` (unless it explicitly opted into a cheaper tier). Safe now that max routes async instead of hanging — so the earlier hang fix no longer costs you effort.

### v1.0.16 (no more stray cmd windows)
- **Consults no longer pop an empty `cmd.exe` window on Windows.** The detached async worker runs without a console of its own, so the Codex/Claude CLI (and git/powershell helpers) it spawned were getting a fresh console window that lingered on screen. Every child process now spawns with `CREATE_NO_WINDOW`, so all consultation work happens silently in the background.

### v1.0.15 (highest-effort default + effort-based async routing)
- **Consults/plans default to `gpt-5.6-sol` at `max` again** (v1.0.14 had dropped this to `high`). Quality is the default; latency is handled by routing, not by lowering effort.
- **`max`/`xhigh` consults route async up front.** Instead of blocking the 240s sync window and then reporting pending, an effort that doesn't fit the window returns a pending `request_id` immediately while the detached worker finishes it — collect with `request_result(request_id, wait_seconds=180)`. Efforts that fit (`high`/`medium`/`low`) still return **inline**.
- **Reading/labour stays on Luna.** `model_policy='cheap_read'` (and cheap/reader-shaped requests) run `gpt-5.6-luna` at `low` and return inline.
- **Defaults only — the caller always overrides.** An explicit `effort`, `target_model`, or `model_policy` wins: request Luna for a consult, or Sol/max for anything, as the task needs.

### v1.0.14 (Codex consult no longer hangs / times out)
- **Direct `consult_codex` no longer times out and discards the work.** A consult now runs through the same ledger+worker path as queued requests: it returns the answer inline when it finishes inside the sync window, otherwise it returns a `status: "pending"` payload with a `request_id` — the detached worker keeps running to its own cap and records the answer, so nothing is lost. Collect a pending answer with `request_result(request_id, wait_seconds=120)`.
- **Consults default to `high` effort, not `max`.** At `max`, `gpt-5.6-sol` routinely overran the 240s sync window and the timeout threw the work away. `high` finishes inline for typical consults; the sync path no longer clamps serious consults *up* to `max` (the async routing paths still do). Pass `effort: "max"` explicitly when you want it — that request just returns a pending id.
- **`request_result` / `request_status` gained `wait_seconds` long-poll.** One call blocks (bounded to the MCP window) until the request reaches a terminal state, the reliable way for a turn-based caller to collect a pending consult.
- **Worker hardening.** Atomic single-writer claim (no duplicate workers on a simultaneous start); post-completion history/events are skipped when a worker loses the finalize race; lone UTF-16 surrogates in CLI output are scrubbed before the DB write (previously crashed `store_consultation` *after* a successful consult, discarding the answer).

### v1.0.13 (Codex inbox async worker)
- **Claude -> Codex inbox requests no longer stay `delivered` forever.** Queued Codex requests now start a bounded headless Codex CLI worker that writes the answer/error back to the same request row.
- **Old stuck Codex inbox requests now fail cleanly when polled.** `request_status` / `request_result` turns stale pre-fix Codex rows into terminal errors with a requeue note.
- **Async Codex requests preserve model policy.** Task kind, token budget, target model, and effort are stored on the queued request, so serious Sol consults still run at `max`.

### v1.0.12 (Codex consult effort guard)
- **Serious Codex Sol consults no longer silently run at medium.** Accidental lower efforts on `gpt-5.6-sol` consult/audit/review/debate routes are upgraded to `max`.
- **Deliberate downshifts are still allowed.** Use `model_policy: "balanced"`, `"efficient"`, or `"lower_effort"` when medium/lower effort is intentionally enough, or `model_policy: "cheap_read"` for Luna reader/sample-prep work.

### v1.0.11 (Codex 5.6 routing guide)
- **Codex defaults now target GPT-5.6 Sol at max reasoning.** Bare `codex`/`gpt` consults, audits, reviews, debates, and co-op routes resolve to `gpt-5.6-sol` with `max` effort.
- **Cheap reader/sample-prep requests now downshift automatically.** Explicit cheap/fast reading, extraction, summarizing, drafting, or sample-prep requests can use `model_policy: "cheap_read"` and resolve to `gpt-5.6-luna` with `low` effort.
- **Agents can ask the broker which model policy to use.** New `get_model_routing_guide` returns the policy, examples, and available model catalog so Claude/Codex do not have to rediscover the rules every session.

### v1.0.10 (consultation response redaction fix)
- **Successful local consultation answers are no longer line-redacted before return or storage.** Long Fable/Claude/Codex/Gemini responses now keep security-audit wording intact in both the inline excerpt and `retrieve_shared_context(response_ref, query)`.
- **Generic shared context redaction remains enabled by default.** User-provided context, prompts, logs, and error responses still use the existing safety redaction path.
- **Retrieved shared context now flags stored redaction placeholders.** Older refs that already contain `[redacted possible secret line]` report `contains_redaction_placeholders` so callers know those lines were removed before storage.

### v1.0.9 (Fable backend routing fix)
- **Fable max no longer gets caught by the Opus async-inbox rule.** The automatic async queue is now limited to `opus` at max effort; `fable`, `sonnet`, and other Claude aliases stay on the direct Claude CLI backend unless async is explicitly requested.
- **`route_agent_task` now honors the requested Claude effort.** A routed request with `effort: low` now resolves and runs as low instead of silently falling back to Claude's max-effort default.
- **Stale Fable inbox requests can be safely cancelled.** The failed queued Fable requests from the regression are terminal once cancelled and will not be re-delivered by the bridge.

### v1.0.8 (async Opus Max consults)
- **Heavy Codex -> Claude Opus/max consults no longer block inside the MCP timeout.** Reviews, audits, debates, bug hunts, implementations, and other large max-effort Claude requests from Codex now queue through the Claude inbox and return a request id immediately.
- **Opus/max quality is preserved instead of downgraded.** The queued Claude request carries a strict model guard plus an explicit effort guard, so the receiver is told to use Claude Opus at max effort or report a mismatch instead of silently answering with a lesser/default model.
- **Codex can track the queued Claude answer.** `queue_claude_request`, `get_claude_requests`, `request_status`, and `request_result` are now exposed as MCP tools, so a caller can queue a long Opus pass, keep working, then retrieve the recorded answer.
- **Claude inbox prompts now include the return path in the injected body.** Claude sees the exact `respond_to_request(...)` instruction, plus the `.agent-broker/claude-responses/<request-id>.md` fallback, after the bridge strips the metadata header.

### v1.0.7 (direct consult timeout hardening)
- **Direct Claude/Codex consults now finish before Codex's MCP tool-call timeout.** Synchronous CLI consults are capped below the client timeout, so Codex gets a controlled broker response instead of a red `timed out awaiting tools/call` failure.
- **Codex -> Claude CLI consults are isolated from Claude extension state.** Switchboard starts Claude consults with safe mode, an empty MCP config, no Chrome bridge, and no session persistence so a Claude extension task using Switchboard does not bleed into a Codex extension consult.
- **Claude consults now use stream-json parsing.** If Claude starts answering but does not finish before the safe timeout, the broker can return any partial answer it received instead of losing everything.
- **Tool descriptions now warn that direct consults are bounded.** Full-site reviews should be split into batches or routed asynchronously; a single synchronous MCP tool call is not a safe place for a many-minute Opus pass.

### v1.0.6 (Codex/Claude peer-consult fallback)
- **Codex can now ask for a peer consult without naming every routing field.** If a Codex-origin Switchboard call leaves `target_agent` and `target_model` empty, the broker now defaults to Claude Code CLI with the flagship Claude model (`opus`, max effort) instead of falling into Antigravity model selection.
- **Claude gets the symmetric fallback.** Ambiguous Claude-origin consult/co-op/debate requests now default to Codex CLI with the flagship Codex model (`gpt-5.5`, xhigh effort).
- **Prompt wording still wins.** Natural phrases such as "consult with Claude" or "ask Codex for a second opinion" are detected before the peer fallback, and explicit structured targets continue to take priority.

### v1.0.5 (Antigravity local context + Claude tool permissions)
- **Antigravity context pickup no longer dead-ends when no live bridge answers.** `request_context_snapshot(target_agent="antigravity")` now falls back to bounded local Antigravity task/log/activity state (`~/.gemini/antigravity-ide/brain`), workspace state, file history, and recent project file mtimes. It clearly labels the result as low/medium-confidence instead of pretending it is a guaranteed visible-chat transcript.
- **Claude's default MCP catalog now includes the tools Switchboard asks it to use.** The lite profile exposes Codex queue/status, work-memory recording, context-event recording, and request-ledger tools, so Claude no longer gets instructions to call hidden tools such as `record_work_memory`.
- **Docs now describe the real Antigravity fallback and Claude tool profile.** README context-snapshot and tool-profile sections now match the shipped behavior.

### v1.0.4 (Antigravity bridge claim isolation)
- **Antigravity bridge claims are now workspace-scoped and fresh-only by default.** The bridge passes its current workspace root when claiming queued Antigravity work, and ignores queued work older than 10 minutes unless configured otherwise. This prevents an unrelated Antigravity window/chat from waking up for stale or cross-project broker tasks.
- **Context snapshot claims use the same isolation.** Live bridge hosts now claim snapshot requests only for the active workspace and within the freshness window, so snapshot polling cannot route another project’s request into the visible Antigravity panel.
- **Antigravity broker handoffs are one-shot by default.** The bridge prompt now tells the in-app agent not to create scheduled tasks, background timers, wait loops, or delayed follow-up chat turns after a broker request is delivered. If a deploy/test/tool is still pending, the agent should report current status, complete the broker request, and stop.
- **Bridge settings added:** `claimCurrentWorkspaceOnly`, `antigravityClaimMaxAgeMs`, `snapshotClaimMaxAgeMs`, and `preventAntigravityBackgroundTimers`. Bridge extension version is now `1.0.1`.

### v1.0.3 (Claude/MCP context budget reduction)
- **Claude gets a lite MCP catalog by default.** When the MCP client identifies as Claude, `tools/list` now returns 18 compact user-facing tools instead of the full bridge/internal catalog. It includes Codex routing, request return, and work-memory recording so broker instructions never ask Claude to call hidden tools. Set `AGENT_BROKER_TOOL_PROFILE=full` or `mcp_tool_profile: "full"` if a client needs every internal bridge tool.
- **Tool results are summary-first.** MCP JSON results are compact by default, `get_consultation_history` now returns bounded summaries unless `include_raw=true`, and long consult responses return an excerpt plus `response_ref` for explicit retrieval.
- **Smaller default context reads.** Default context packs are 2.4k tokens, work memory is 5 entries / ~2.6k chars, and snapshot fast paths read 4 turns / ~300 tokens unless a caller asks for more.

### v1.0.2 (straightforward CLI model + reasoning-effort selection; smallest-sufficient build rung)
- **Pick the model and reasoning effort the obvious way.** `consult_codex` / `consult_claude` / `route_agent_task` now take a first-class **`effort`** field (`minimal|low|medium|high|xhigh`, plus phrases — *"extra high" → xhigh*, *"ultra"/"max" → family top*) that is passed to the CLI as **its own flag** (Codex `-c model_reasoning_effort=`, Claude `--effort`) and **never** smuggled into the model name. A bare family request — **"codex"**, **"claude"** — now resolves to the **flagship model at the highest available effort** (Codex `gpt-5.5`/`xhigh`, Claude `opus`/`max`) instead of stalling on a model-selection prompt; a specific model is honored verbatim (**"sonnet 4.6 for implementation"**, **"gpt-5.4-mini"**). Effort phrases are split out of the model text before matching, so a request like *"5.5 extra high"* resolves cleanly to model `gpt-5.5` + effort `xhigh` — fixing a class of failures where the effort phrase produced an invalid `--model "gpt-5.5-codex xhigh"` (rejected by Codex). Per-request auto-pinning of a topic default is now **opt-in** (`remember_model`). New shared helper `resolve_cli_model_and_effort()`; **Fable** added to the Claude catalog. *(Tagged `v1.0.1` in source; first shipped as a binary in v1.0.2.)*
- **Smallest-sufficient-implementation rung in the build contracts.** The `implementation` and `implementation_plan` task contracts now tell the receiving agent to prefer the **standard library / a native platform feature / an already-installed dependency over new code or new dependencies** — explicitly **without** dropping required validation, error handling, security checks, or tests, and without disputing an approved plan (stop and report instead). Scoped to code-writing task kinds only; `consult`/`co_audit`/`debate`/`review` are unchanged, so second-opinion reasoning quality is untouched.
- **Installer: more reliable Claude desktop detection.** Recognizes the Microsoft Store / MSIX "Cowork" build (registered AppX package) in addition to the `%APPDATA%/Claude` data dir and the legacy standalone installer, so Store users aren't false-negatived.

### v1.0.0 (diagnostics + Claude reply path + CLI-default routing + debate + Claude Code nerve-system)
- **Claude Code joins the nerve system (on-disk fast path).** `request_context_snapshot` now reads live **Claude Code** sessions on disk (`~/.claude/projects`, scoped to the session whose `cwd` matches the project) and completes **immediately** — symmetric with the existing Codex `~/.codex` reader, so the most common "recent chat" surface is finally peekable without any agent cooperation. When **no surface is heartbeating** it returns `no_live_surface` (with guidance) instead of queuing forever; `doctor` gained a **nerve-system** report of which surfaces can contribute (on-disk vs live bridge vs push-only); and the installer now also registers the **Claude desktop app** (push-only — it stores chat in Electron/server-side and can't be read on disk, surfaced honestly in `doctor`). Installer hardening: Antigravity debug-port helper scripts are copied to a **durable** `~/.agent-broker` path (the frozen-exe build previously baked a PyInstaller temp path into the launcher shortcut, breaking Antigravity launch after install), uninstall **restores the patched launcher shortcuts** so the opt-in is fully reversible, and the setup menu leads with **Install** (Status moved last).
- **Headless CLI is now the default route for Codex/Claude.** `route_agent_task` sends Codex/Claude work to the headless CLI by default (reliable, model-switchable via `-m`, answer returned inline). Say **"in app"** / `surface=extension` for the in-app IDE chat panel, or `surface=app` for a visible desktop-app handoff — both honored. **Exceptions:** **Gemini** defaults to Antigravity in-app automation unless you explicitly request `surface=cli`; and **Antigravity-hosted models** (e.g. Antigravity's Opus/Gemini) **always** use Antigravity automation, never a CLI. If the CLI is missing, auto-routing degrades to the in-app extension, then the app handoff.
- **Antigravity automation is a true round-trip (verified).** From any driver (e.g. the Claude app) you can route to a *named* Antigravity model — the bridge **auto-selects that model** (switching away from whatever was active) over CDP, sends the prompt into the live Antigravity agent panel, and the structured reply returns to the broker (`complete_antigravity_request`). Confirmed working end-to-end: "send to Antigravity Gemini 3.5 (High) and reply" auto-switched the model and returned the answer. This remains the **only** surface with a fully programmatic in-app send *and* structured reply.
- **`doctor` — read-only capability report.** `agent-switchboard.exe doctor` (or `bridge doctor [--json]`) probes this machine per assistant: CLI present + live `--version` smoke test, extension installed, CDP port, the delivery route you'll actually get, the reply path, and whether a headless debate can run. Flags broker/bridge version drift and prints next steps. **No new MCP tool** (CLI-only — keeps the 36-tool context budget unchanged). Also probes for a CLI binary bundled inside an installed extension as a *detected-and-smoke-tested* fallback, never an assumed one.
- **Claude-extension replies are now first-class.** Added a durable `claude_requests` table (mirrors `codex_requests`): `queue_claude_request` records a row, `respond_to_request` and `ledger.md` now recognize Claude requests, and a new `bridge claude-responses [project]` verb ingests answer files written under `.agent-broker/claude-responses/` (idempotent; archives to `processed/`). Previously a Claude-extension reply had no row to attach to. Still no MCP tool added (36 unchanged).
- **Internal request-lifecycle adapter.** One canonical state map + `is_terminal_state()` so terminal-state logic lives in a single place for new code (reply ingestion, `doctor`, `status`/`result`/`cancel`). Existing per-table status values are unchanged on the wire — no migration.
- **Request inspection + maintenance verbs.** `bridge status <id>` and `result <id>` (read-only, normalized to the canonical lifecycle across codex/antigravity/claude requests), `cancel <id> [reason]` (terminal-guarded, idempotent), and `reap [max_age_hours]` (marks abandoned non-terminal requests `expired` — never re-queues, so no double-delivery; never touches terminal or `awaiting_model_selection` rows). CLI-only; 36 MCP tools unchanged.
- **Cross-model debate engine** (`bridge debate <project> <topic> "<proposition>" [rounds] [sideA[:model[:effort]]] [sideB[:model[:effort]]]`). Two assistants debate **headless on your subscriptions** (no API key) for N rounds, then a synthesis judge writes a verdict; the transcript + verdict are saved under `.agent-broker/debates/`. Each debater keeps real memory across rounds via its CLI's own resume primitive (`codex exec resume`, `claude -p --resume`) — **no daemon, no app-server, no network port**; every turn is a clean bounded subprocess. Defaults: Codex latest + `xhigh` reasoning vs Claude `opus` + `xhigh`, synthesis at `high`; the transcript labels each side as e.g. `codex/latest (xhigh)` so you always see which model+effort argued. Token discipline (no file/command exploration, ~500-word cap, only the opponent's last message per turn) keeps cost down without lowering reasoning. CLI-only; 36 MCP tools unchanged.

### v0.6.0 (active context snapshots)
- **Peek at what another open chat knows.** New `request_context_snapshot` asks the best available surface for a COMPACT continuation state (objective, plan, files, checks, risks, next step) - not a full transcript. Read it back with `get_latest_context_snapshot`; it also lands in `get_context_pack` under "Latest Context Snapshots" and in `get_topic_status`. Opt-in and local - no silent chat scraping.
- **Codex fast path:** for Codex the broker reads the live `~/.codex` session transcript on disk (redacted + truncated) and returns immediately - no agent cooperation or CDP needed. Strictly scoped to the session whose `cwd` matches the project (no cross-project leak).
- **Cooperative delivery for other surfaces:** `claim_context_snapshot_request` (capability-gated, stale-claim reaper), `complete_context_snapshot_request` (race-safe, idempotent), `snapshot-release` for undeliverable claims, plus `record_surface_heartbeat`/`list_live_surfaces` so the bridge can route to a live host. Bridge polls snapshots first and scans a `.agent-broker/context-snapshots/` fallback dir. 36 MCP tools.

### v0.5.0 (model enforcement + one-file install)
- **Strict model guard on non-switchable surfaces.** When a specific model is requested for the Codex/Claude *extension* (or app) — surfaces the broker can't switch — the delivered prompt now leads with a self-check: state your model; if you're not the requested one, **STOP and tell the user to switch**. The bridge also shows a "select `<model>`" notification. A lesser/default model can no longer silently answer in the requested model's place. Codex requests carry `target_model` + `strict_model`.
- **Conservative prompt-model detection.** "Get Opus's opinion" with no explicit model arg resolves to Opus (so the topic's Sonnet default doesn't win), as a one-off that doesn't rewrite the stored default. Tightly anchored so ordinary prose ("the *user*…", "*budget*…", "magnum *opus*") never misfires.
- **Self-contained `agent-switchboard.exe`.** One dual-mode binary (PyInstaller) that installs everything (the bridge **VSIX is embedded**) and runs the MCP server via `agent-switchboard.exe serve` — no Python required. Both the exe and `python setup.py` expose a built-in **uninstall** that now also **removes the bridge extension** and the installed exe.
- **Installer fixes:** `latest_vsix()` is recursive + version-aware (a fresh clone could previously ship no usable VSIX); frozen self-install uses an atomic replace and **aborts** instead of silently keeping a stale exe.

### v0.4.22 (request ledger + answer return-path)
- **`respond_to_request`** (new): any receiving agent returns its answer to the broker, which records the response + timing + responder on the queued request — the symmetric reply Codex/Claude extensions lacked. No more copy-pasting from the chat panel.
- **`get_request_ledger`** (new): a per-topic, human-readable `ledger.md` (request → answer → timing) generated from SQLite (broker is the single writer; SQLite stays the source of truth). Auto-refreshes on queue/complete/respond.
- Task contracts now tell the receiver to **return via `respond_to_request`** with the Request ID. 30 MCP tools.

### v0.4.21 (review follow-ups)
- Bridge `hasAntigravitySendCommand` caches **positive only** (re-checks negatives on a TTL) so a late-registering Antigravity command isn't refused until reload; `complete_antigravity_request` race branch returns the **actual** terminal status; removed dead bridge callback code.
- **Compact task contract:** the per-message ground-rules block is no longer re-pasted into chat — the full rules live once in `AGENT_GROUND_RULES.md` and the message references it (~183→72 tokens/message). Plus a token-economy guard that flags oversized handoff prompts (`prompt_notice`).

### v0.4.20 (audit-hardening)
- **Stop stranding Antigravity requests:** the bridge only claims them in a host that actually exposes `antigravity.sendPromptToAgentPanel`, and wraps the send in try/catch → requeue.
- **No double/stale completion:** `complete_antigravity_request` is now idempotent (status guard + rowcount → `already_completed`), the Codex callback is single-sourced through the broker, and the bridge archives the fallback response file after completing so a stale file can't re-complete a requeued request.
- **Correctness:** `consult_gemini` now passes `-m <model>` on the CLI path (was silently running the CLI default); SQLite uses WAL + a 30s busy timeout; env-int parsing can't crash the server on import.
- **Security default:** CDP model auto-selection (`useCdpModelSelection`) now ships **off**; the unauthenticated debug port is opt-in only.
- **Honesty:** versioned model aliases carry a version-collapse `note`; MCP `serverInfo` reports the real version; docs reconciled to code (28 MCP tools; bridge 0.4.20).

### v0.6 (work memory)
- **Topic Work Memory**: context packs and compacted handoffs include a short continuation log before broad history (`record_work_memory` / `get_work_memory`).

### v0.5 (router + surface selector)
- **Surface routing** (`extension` default · `app` when named) with app fallback; fixed model misrouting; Antigravity "which model?" gate; Claude inbox route; Claude CLI hardened (stdin).

### v0.4 (routing / token discipline)
- `route_agent_task` with task kinds, model aliases, strict-model handling, and token budgets.

### v0.3 (context efficiency)
- Reversible-*retrieval* context compression (`store/retrieve_shared_context`), per-topic context packs, and new-chat bootstrap.

### v0.1–0.2 (foundation)
- MCP broker with shared SQLite state; Antigravity bridge using `antigravity.sendPromptToAgentPanel`; Codex inbox + callbacks.

---

## Advanced: Run from Source

The broker is a single dependency-free Python file (Python 3.10+):

```bash
python agent_broker_mcp.py            # start the MCP stdio server
python agent_broker_mcp.py bridge ... # CLI helpers used by the bridge extension
```

**Build the release artifacts** (the bridge VSIX + the self-contained exe):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-release.ps1
# -> extensions/antigravity-agent-broker-bridge/antigravity-agent-broker-bridge-<ver>.vsix
# -> dist/agent-switchboard.exe   (embeds the VSIX; dual-mode install + `serve`)
```

Needs Node.js (for `vsce`) and Python (PyInstaller is installed automatically if missing). Upload `dist/agent-switchboard.exe` to the GitHub Release.

Register it with an MCP client by pointing the client's MCP config at:

```json
{
  "command": "python",
  "args": ["C:\\Users\\<you>\\.agent-broker\\agent_broker_mcp.py"]
}
```

**MCP tools exposed:**

- Full profile: 36 tools.
- Claude/default lite profile: 18 compact user-facing tools (`consult_codex`, `route_agent_task`, Codex queue/status, model listing, compact history/memory/context/snapshot reads, memory/event recording, retrieval, live-surface status, request ledger, and `respond_to_request`).
- Override with `AGENT_BROKER_TOOL_PROFILE=full|public|lite|compact` or `mcp_tool_profile` in `~/.agent-broker/config.json`.

Full profile:

```text
register_project, route_agent_task, resolve_model_request, list_agent_models,
set_model_default, get_model_defaults,
consult_codex, consult_claude, consult_gemini, get_consultation_history,
queue_antigravity_request, claim_antigravity_request, complete_antigravity_request,
get_antigravity_requests, queue_codex_request, get_codex_requests,
record_agent_event, get_topic_timeline, get_topic_status,
respond_to_request, get_request_ledger,
get_work_memory, record_work_memory,
get_context_pack, record_context_event, compact_topic,
store_shared_context, retrieve_shared_context, get_shared_context_stats,
get_chat_bootstrap,
request_context_snapshot, claim_context_snapshot_request, complete_context_snapshot_request,
get_latest_context_snapshot, list_live_surfaces, record_surface_heartbeat
```

**Antigravity model auto-selection (experimental, off by default)** requires launching Antigravity with a debug port so the bridge can drive the model picker over Chrome DevTools Protocol, then enabling `agentBrokerBridge.useCdpModelSelection`:

```powershell
antigravity --remote-debugging-address=127.0.0.1 --remote-debugging-port=9000
```

Without it, the bridge uses whatever model is currently selected and asks you to pick the target model first.

---

## Terms & risk

- ⚠️ **Subscription automation, not API.** The broker drives the assistants you're already logged into — including, optionally, keystroke/CDP UI automation. Automating prompts against a logged-in subscription UI may violate a provider's terms and carries account risk. Review your providers' terms before using it, and keep automation opt-in.
- ⚠️ **No chat-history scraping.** The broker only uses authenticated IDE surfaces and shared state you create. It does not read private conversation databases.
- ⚠️ **Local debug port is unauthenticated.** CDP model auto-selection opens an unauthenticated DevTools port on `127.0.0.1:9000` (`9010` for VS Code). It ships **off** (`useCdpModelSelection: false`); only enable it when you've deliberately launched the IDE with the debug flag, and close the port when you're done.
- ⚠️ **The bridge can open files and drive UI.** It polls a local queue and can open inbox files / send prompts into the active panel / (optionally) press Enter. Read the extension source before installing.
- ⚠️ **Your data stays yours.** Everything lives under `%USERPROFILE%\.agent-broker`. The uninstaller keeps it unless you pass `-RemoveData`.

---

## FAQ

**Q: Do I need an API key?**
A: No. It uses the subscriptions your installed assistants are logged into. (A `GEMINI_API_KEY` path exists only as an off-by-default escape hatch when no Gemini CLI is present.)

**Q: Can it force Antigravity to use a specific model?**
A: Not reliably. Antigravity exposes no stable "set model" API. The experimental CDP path clicks the picker for you (needs the debug port and `useCdpModelSelection: true`); otherwise you select the model and the broker confirms which one answered.

**Q: I asked for "Opus 4.8" but it ran something else?**
A: The Claude CLI `opus` alias runs whichever Opus the installed CLI maps it to — there's no `opus 4.8` CLI alias. The broker still resolves it but attaches a `note` warning that the running version may differ. Confirm the running model if the exact version matters.

**Q: Does the Claude extension get prompts automatically like Antigravity?**
A: Closer than it used to. The bridge **auto-opens** the Claude inbox file and best-effort auto-submits it, and Claude can write a reply under `claude-responses/`. But there's no symmetric send/complete API, so it's not the structured round-trip Antigravity has. The Claude CLI route is fully headless.

**Q: Is Gemini supported?**
A: Through Antigravity's in-app Gemini, yes. A standalone `gemini` CLI is also honored (the requested model is passed with `-m`). It is optional and not bundled.

**Q: Do I need Python, or can I just run the `.exe`?**
A: Either works. The **self-contained `agent-switchboard.exe`** from Releases needs no Python — it installs everything (the bridge VSIX is embedded) and is itself the MCP server (`agent-switchboard.exe serve`). Or run from source with Python 3.10+. Both have a built-in uninstall.

**Q: I asked Codex/Claude for a specific model — does it switch automatically?**
A: On Antigravity (CDP) and the CLIs (`--model`/`-m`), yes. The broker **cannot** switch the Codex/Claude *extension* pickers, so instead it tells the receiving agent to **state its model and STOP if it isn't the requested one**, and the bridge notifies you to select it — so a lesser/default model never silently answers. A model named only in the prompt ("get Opus's opinion") is detected as a one-off and doesn't change your topic default.

**Q: Mac / Linux?**
A: The broker is plain Python and cross-platform; the installer, bridge model-selection, and shortcut patching are Windows-first today. Contributions welcome.

---

## License

PolyForm Noncommercial 1.0.0. Noncommercial use is allowed with the required copyright notice. Commercial use requires a separate written license from FutureisinPast / ChartTrades (https://chartrades.com/). See [LICENSE](LICENSE).

---

**⭐ If this saves your agent workflow, please star the repo so others can find it!**
