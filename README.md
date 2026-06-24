# Agent Switchboard

**Use Claude Code, Codex, Gemini, Antigravity, and VS Code together — without copy-pasting context between them.**

Agent Switchboard is a **local MCP bridge** that lets your AI coding agents hand tasks to each other, review each other's work, run multi-round debates, and share compact project context — a local nervous system where the tools stay separate but coordinate through one local broker.

**No API keys. No cloud broker. No extra billing.** It uses the local CLIs, IDE bridge routes, and subscriptions you already have where possible.

> **⭐ If this saves your agent workflow, please star the repo so others can find it!**

## Why this exists

Modern AI coding workflows are fragmented. You might use **Codex** for implementation, **Claude Code** for review and reasoning, **Gemini** for planning or alternatives, and **Antigravity / VS Code** for workspace context — but normally they can't talk to each other, so you end up manually copying plans, files, errors, and context from one assistant to another. That's fine for small tasks; it gets messy fast on real projects. Agent Switchboard gives those agents a shared local coordination layer so they cooperate instead of working blind to each other.

## Fast Version

- **Ask one assistant to use another** - from Codex, ask Claude Opus to audit; from Claude, ask Codex to implement; send Gemini/Antigravity-hosted models the planning work.
- **See across chats** - pull a *compact snapshot* of what another agent's session knows; **Codex and Claude Code are read on demand from disk**, no copy-paste.
- **Run cross-model debate** - Codex vs Claude for N rounds, then synthesize a verdict.
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
| **Peek at another open chat** — fetch a *compact snapshot* of what another agent's session knows, on request (opt-in, local, never silent scraping) | ✅ active context snapshots; **Codex & Claude Code read on disk** (`request_context_snapshot` → `get_latest_context_snapshot`) |
| **Cross-model debate** — two assistants debate N rounds headless on your subscriptions, then a synthesis judge writes a verdict | ✅ (`agent-switchboard debate`) |
| Route Codex/Claude to the **headless CLI** by default; the **in-app chat** ("in app") or **desktop app** only when asked | ✅ |
| Keep **Gemini** + **Antigravity-hosted** models on in-app automation by default, with the Gemini CLI available when explicitly requested | ✅ |
| Fall back to the in-app extension / app automatically when a CLI isn't installed | ✅ (see caveats in the docs) |
| Send a prompt straight into Antigravity's chat panel + get a structured reply back | ✅ (`antigravity.sendPromptToAgentPanel`, the only full in-app round-trip) |
| Pick the Antigravity model automatically | ✅ **Offered at install** (default on) when Antigravity is detected — patches the launcher to open a local CDP debug port so the broker auto-selects the model in-app; just decline at the prompt to skip |

---

## How It Works

The broker is a dependency-free Python MCP server. Each assistant talks to it over stdio JSON-RPC; the broker keeps shared state in one local SQLite file (WAL mode, so multiple hosts can poll it concurrently) and routes work to the right place.

**Routing priority:**

1. **Surface** — Codex/Claude default to the **headless CLI** (reliable, model-switchable via `-m`, answer returned inline); say **"in app"** (or `surface: extension`) for the IDE chat panel, or "app" for a visible desktop-app handoff. **Gemini** and **Antigravity-hosted** models stay on **in-app automation** by default; only an explicit "gemini cli" uses the standalone Gemini CLI. Antigravity-hosted Claude/Gemini never use a CLI. If a CLI is absent, auto-routing degrades to the extension, then the app.
2. **Model** — vague requests ("ask Opus") resolve against a per-topic default; explicit requests ("claude opus") become the new default. Sending to **Antigravity always requires naming a model** (it hosts a separate, subscription-backed Claude/Gemini). Versioned names that the CLI can't pin (e.g. "opus 4.8") still resolve to the CLI's `opus` alias but now carry a **`note`** warning that the running version may differ.
3. **Token budget** — every routed task carries a task contract (`implementation_plan`, `co_audit`, `debate`, `review`, …) with a word budget, and a compressed context pack instead of raw history. If a caller inlines a bloated `prompt` (over a soft token limit), the broker stashes the full text as a retrievable `context_ref` and returns a `prompt_notice` nudging it to send a short instruction + ref next time — so token discipline is enforced by the system, not left to each agent.

---

## See what another chat knows (active context snapshots)

Working in one assistant but need the *current* state another open chat is holding? Ask for a **context snapshot** — a COMPACT continuation state (objective, plan, touched files, checks, risks, next step), **not** a full transcript, and **never** silent scraping (it's opt-in and local).

- **`request_context_snapshot(project, topic, target_agent)`** asks the best available open surface for that compact state.
- **Codex & Claude Code fast path (on disk):** the broker reads the live session transcript on disk — **Codex** from `~/.codex`, **Claude Code** from `~/.claude/projects` — redacted + truncated and **scoped to the session whose `cwd` matches the project** (no cross-project leak), returning **immediately** with no agent cooperation or CDP needed. The two CLIs are symmetric.
- **Other surfaces:** the request is queued for a capable bridge host (`claim_context_snapshot_request` / `complete_context_snapshot_request`, race-safe + idempotent, with a stale-claim reaper), or picked up from a `.agent-broker/context-snapshots/` fallback file. If **no live surface is heartbeating**, the request reports `no_live_surface` (with guidance) instead of queuing forever with no claimer.
- **Honest limit:** a surface feeds the nerve system only if it's readable on disk (Codex/Claude Code), a live heartbeating bridge (Antigravity/VS Code), **or** it proactively records. A disconnected helper — e.g. the **Claude desktop app** (Electron + server-side history, not on disk) — can be *registered to push* context, but cannot be read on demand. `doctor` shows exactly which surfaces can contribute, so blind spots are visible, not surprising.
- Read it back with **`get_latest_context_snapshot`** — it also folds into `get_context_pack` ("Latest Context Snapshots") and `get_topic_status`, so the next model picks it up automatically. Live-host routing uses `record_surface_heartbeat` / `list_live_surfaces`.

This is the cross-chat "peek" layer: agents and IDEs can see what another agent's session currently knows and fetch it on request, without copy-pasting transcripts.

---

## Delivery, honestly

There is a real difference between **delivered** (a file/prompt reached the surface), **auto-opened** (the bridge opened it for you), **submitted** (it was actually sent into a chat), and **completed back to broker state** (a model-tagged reply landed in the broker). Only Antigravity does the full structured round-trip.

| Target | Mechanism | How far it gets |
|---|---|---|
| **Antigravity** (in-app Gemini/Claude) | `antigravity.sendPromptToAgentPanel` (+ optional CDP model select) | delivered → submitted → **completed back to broker** (`complete_antigravity_request`) — the only structured round-trip |
| **Claude extension** | `claude-inbox` markdown, **auto-opened** + best-effort CDP auto-submit | delivered → auto-opened → (often) submitted → **recorded back to broker**: the request now has a durable `claude_requests` row, so a reply via `respond_to_request` lands on it, or a file written to `claude-responses/` is ingested by `bridge claude-responses` |
| **Claude CLI** | `claude -p` headless (prompt via stdin) | **completed** — full headless round-trip |
| **Codex extension** | `codex-inbox` markdown, **auto-opened** | delivered → auto-opened → (with `respond_to_request`) **completed back to broker** |
| **Codex CLI** | `codex exec` headless | **completed** — full headless round-trip |
| **Gemini** | `gemini` CLI (`-m <model>` honored) or `GEMINI_API_KEY` | **completed** via CLI; the API path is an off-by-default escape hatch |

> **Answer return-path:** any surface without a native completion API (Codex/Claude extensions) closes the loop by calling **`respond_to_request(request_id, response)`** — the broker records the answer + timing on the request and refreshes a per-topic **`ledger.md`** (`get_request_ledger`). So you no longer have to copy-paste a reply out of the chat panel.

> **Model enforcement, honestly:** the broker can only *switch the answering model programmatically* on **Antigravity** (CDP UI automation, best-effort) and the **CLIs** (`--model`/`-m` flag). It **cannot** drive the Codex- or Claude-*extension* model picker. So when a specific model is requested on those surfaces, the broker prepends a **strict guard** to the prompt — *"you must be `<model>`; state your model; if you're not, STOP and ask the user to switch"* — and the bridge pops a **notification** to select that model. This way a lesser/default model never silently answers in the requested model's place. A model named only in the prompt ("get Opus's opinion") is detected conservatively and treated as the requested model **for that one request** (it does not overwrite the topic default).

> **Model + effort on the CLIs:** model and reasoning effort are **separate inputs**, never folded together. Pass **`effort`** (`minimal|low|medium|high|xhigh`; *"extra high" → xhigh*, *"ultra"/"max" → family top*) and the broker sets it as its own CLI flag (Codex `-c model_reasoning_effort=`, Claude `--effort`) — it is **never** appended to the model name. A bare family request (**"codex"**, **"claude"**) defaults to the **flagship model at the highest available effort** (Codex `gpt-5.5`/`xhigh`, Claude `opus`/`max`); a specific model is honored verbatim (*"sonnet 4.6 for implementation"*, *"gpt-5.4-mini"*). Effort phrases embedded in a model request (*"5.5 extra high"*) are split off before matching, so they resolve to model `gpt-5.5` + effort `xhigh` rather than an invalid model string.

> The broker is **target-driven**: it routes by *where the work should go*, not by *who sent it*. A "from → to" matrix would imply source-awareness the router doesn't have.

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

### v1.0.4 (Antigravity bridge claim isolation)
- **Antigravity bridge claims are now workspace-scoped and fresh-only by default.** The bridge passes its current workspace root when claiming queued Antigravity work, and ignores queued work older than 10 minutes unless configured otherwise. This prevents an unrelated Antigravity window/chat from waking up for stale or cross-project broker tasks.
- **Context snapshot claims use the same isolation.** Live bridge hosts now claim snapshot requests only for the active workspace and within the freshness window, so snapshot polling cannot route another project’s request into the visible Antigravity panel.
- **Antigravity broker handoffs are one-shot by default.** The bridge prompt now tells the in-app agent not to create scheduled tasks, background timers, wait loops, or delayed follow-up chat turns after a broker request is delivered. If a deploy/test/tool is still pending, the agent should report current status, complete the broker request, and stop.
- **Bridge settings added:** `claimCurrentWorkspaceOnly`, `antigravityClaimMaxAgeMs`, `snapshotClaimMaxAgeMs`, and `preventAntigravityBackgroundTimers`. Bridge extension version is now `1.0.1`.

### v1.0.3 (Claude/MCP context budget reduction)
- **Claude gets a lite MCP catalog by default.** When the MCP client identifies as Claude, `tools/list` now returns 12 compact user-facing tools instead of the full 36-tool bridge/internal catalog. Set `AGENT_BROKER_TOOL_PROFILE=full` or `mcp_tool_profile: "full"` if a client needs every internal bridge tool.
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
- Claude/default lite profile: 12 compact user-facing tools (`consult_codex`, `route_agent_task`, model listing, compact history/memory/context/snapshot reads, retrieval, live-surface status, and `respond_to_request`).
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
