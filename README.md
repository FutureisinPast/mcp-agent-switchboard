# Agent Broker

Working in Codex and want a second opinion from Claude Opus? Stuck in Claude Code and want Gemini to write the implementation plan? Want your AI coding assistants to debate each other — **without paying for API keys**? This tool routes prompts between the AI assistants you already have installed (Codex, Claude Code, Antigravity, Gemini), using their existing **subscriptions**. No API keys, no extra billing, no cloud — everything runs locally over a small MCP server.

> Built for [Antigravity](https://antigravity.google) and VS Code users. Antigravity is a VS Code fork, so the same bridge extension installs in both.

> **Honest scope:** only **Antigravity** has a true programmatic in-app send *and* a structured reply back to the broker. Claude/Codex are reached through a CLI round-trip or an auto-opened inbox file — see [Delivery, honestly](#delivery-honestly). This is a power-user tool for people who already run these assistants; it drives logged-in subscription UIs, so read [Terms & risk](#terms--risk) first.

---

## Requirements

Two supported install paths — pick one:

- **Self-contained `agent-broker.exe`** (no Python needed). One file from the [Releases](../../releases) page does everything: installs the MCP server into every assistant, installs the bridge extension (the VSIX is **embedded**), and runs the MCP server itself (`agent-broker.exe serve`). Has a built-in uninstall.
- **Python 3.10+** (run from source). The broker is one dependency-free Python file; agents launch it as `python agent_broker_mcp.py`.

Other notes:
- Windows 10/11 for the installer, bridge auto-select, and shortcut patching (the broker itself is cross-platform; the installer/CDP layer is Windows-first today).
- **Node.js on PATH** is needed only for the CDP helpers (Antigravity model auto-select, Codex/Claude webview submit).
- Optional: `pip install tiktoken` for exact token accounting (a `chars/4` estimate is used if it's absent; the exe bundles it).

---

## Quick Start (Windows)

1. **Close Antigravity and VS Code.** The installer refuses to run while either IDE is open, so extensions and debug flags can't be left half-updated.
2. **Install** one of two ways:

   **A — Self-contained exe (no Python):** download `agent-broker.exe` from the Releases page and run it. Pick **Install** from the menu (or `agent-broker.exe install`).

   **B — From source (Python 3.10+):**

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-agent-broker.ps1
   ```

   Either way the installer detects which assistants you have (Codex, Claude Code, Antigravity, VS Code), **registers the MCP server with each**, installs the bridge extension (VSIX embedded in the exe; auto-built/located from source), writes config, and (optionally) patches Antigravity shortcuts to start with a debug port. Every config it edits is backed up first.
3. **Open Antigravity / VS Code again** so the `Agent Broker Bridge` extension activates.
4. **Try it.** In any registered assistant: *"Use Agent Broker to ask Claude Opus to audit this function."*

**Uninstall / rollback (both paths):** run `agent-broker.exe uninstall` (or `python setup.py uninstall`), or pick **Uninstall** from the menu. It reverses MCP registration in all four hosts, **removes the bridge extension**, and removes the installed broker exe. Add `--remove-data` to also delete `~/.agent-broker`. The broker uses whatever subscriptions your assistants are already logged into.

---

## What It Does

| Goal | ✅ |
|---|---|
| Let Codex, Claude Code, and Antigravity consult each other | ✅ |
| Use existing **subscriptions** — no API keys, no extra billing | ✅ |
| Keep all shared state **local** (SQLite), never scrape private chat history | ✅ |
| **Compress** handoffs so cross-agent calls don't burn context | ✅ lossy summarization with a locally-stored, retrievable original (Headroom-style *retrieval*, not a reversible codec) |
| Keep a short per-topic **work memory** so the next model sees what changed, where, why, checks, risks, and next step | ✅ |
| Route to the **extension** by default, the **app/CLI** only when asked | ✅ |
| Fall back to the app automatically when an extension isn't installed | ✅ (positive detection; see caveats in the docs) |
| Send a prompt straight into Antigravity's chat panel | ✅ (`antigravity.sendPromptToAgentPanel`) |
| Pick the Antigravity model automatically | 🧪 Experimental, **off by default** (CDP UI automation) |

---

## How It Works

The broker is a dependency-free Python MCP server. Each assistant talks to it over stdio JSON-RPC; the broker keeps shared state in one local SQLite file (WAL mode, so multiple hosts can poll it concurrently) and routes work to the right place.

**Routing priority:**

1. **Surface** — default is the in-IDE **extension**; the standalone **app/CLI** is used only when you say so ("app", "CLI", "headless") or set `surface: app`. If the target extension is detected as absent, the broker falls back to the app.
2. **Model** — vague requests ("ask Opus") resolve against a per-topic default; explicit requests ("claude opus") become the new default. Sending to **Antigravity always requires naming a model** (it hosts a separate, subscription-backed Claude/Gemini). Versioned names that the CLI can't pin (e.g. "opus 4.8") still resolve to the CLI's `opus` alias but now carry a **`note`** warning that the running version may differ.
3. **Token budget** — every routed task carries a task contract (`implementation_plan`, `co_audit`, `debate`, `review`, …) with a word budget, and a compressed context pack instead of raw history. If a caller inlines a bloated `prompt` (over a soft token limit), the broker stashes the full text as a retrievable `context_ref` and returns a `prompt_notice` nudging it to send a short instruction + ref next time — so token discipline is enforced by the system, not left to each agent.

---

## Delivery, honestly

There is a real difference between **delivered** (a file/prompt reached the surface), **auto-opened** (the bridge opened it for you), **submitted** (it was actually sent into a chat), and **completed back to broker state** (a model-tagged reply landed in the broker). Only Antigravity does the full structured round-trip.

| Target | Mechanism | How far it gets |
|---|---|---|
| **Antigravity** (in-app Gemini/Claude) | `antigravity.sendPromptToAgentPanel` (+ optional CDP model select) | delivered → submitted → **completed back to broker** (`complete_antigravity_request`) — the only structured round-trip |
| **Claude extension** | `claude-inbox` markdown, **auto-opened** + best-effort CDP auto-submit | delivered → auto-opened → (often) submitted. Replies are written to `claude-responses/`; there is **no** symmetric `complete` API |
| **Claude CLI** | `claude -p` headless (prompt via stdin) | **completed** — full headless round-trip |
| **Codex extension** | `codex-inbox` markdown, **auto-opened** | delivered → auto-opened → (with `respond_to_request`) **completed back to broker** |
| **Codex CLI** | `codex exec` headless | **completed** — full headless round-trip |
| **Gemini** | `gemini` CLI (`-m <model>` honored) or `GEMINI_API_KEY` | **completed** via CLI; the API path is an off-by-default escape hatch |

> **Answer return-path:** any surface without a native completion API (Codex/Claude extensions) closes the loop by calling **`respond_to_request(request_id, response)`** — the broker records the answer + timing on the request and refreshes a per-topic **`ledger.md`** (`get_request_ledger`). So you no longer have to copy-paste a reply out of the chat panel.

> **Model enforcement, honestly:** the broker can only *switch the answering model programmatically* on **Antigravity** (CDP UI automation, best-effort) and the **CLIs** (`--model`/`-m` flag). It **cannot** drive the Codex- or Claude-*extension* model picker. So when a specific model is requested on those surfaces, the broker prepends a **strict guard** to the prompt — *"you must be `<model>`; state your model; if you're not, STOP and ask the user to switch"* — and the bridge pops a **notification** to select that model. This way a lesser/default model never silently answers in the requested model's place. A model named only in the prompt ("get Opus's opinion") is detected conservatively and treated as the requested model **for that one request** (it does not overwrite the topic default).

> The broker is **target-driven**: it routes by *where the work should go*, not by *who sent it*. A "from → to" matrix would imply source-awareness the router doesn't have.

---

## Changelog

### v0.5.0 (model enforcement + one-file install)
- **Strict model guard on non-switchable surfaces.** When a specific model is requested for the Codex/Claude *extension* (or app) — surfaces the broker can't switch — the delivered prompt now leads with a self-check: state your model; if you're not the requested one, **STOP and tell the user to switch**. The bridge also shows a "select `<model>`" notification. A lesser/default model can no longer silently answer in the requested model's place. Codex requests carry `target_model` + `strict_model`.
- **Conservative prompt-model detection.** "Get Opus's opinion" with no explicit model arg resolves to Opus (so the topic's Sonnet default doesn't win), as a one-off that doesn't rewrite the stored default. Tightly anchored so ordinary prose ("the *user*…", "*budget*…", "magnum *opus*") never misfires.
- **Self-contained `agent-broker.exe`.** One dual-mode binary (PyInstaller) that installs everything (the bridge **VSIX is embedded**) and runs the MCP server via `agent-broker.exe serve` — no Python required. Both the exe and `python setup.py` expose a built-in **uninstall** that now also **removes the bridge extension** and the installed exe.
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
# -> dist/agent-broker.exe   (embeds the VSIX; dual-mode install + `serve`)
```

Needs Node.js (for `vsce`) and Python (PyInstaller is installed automatically if missing). Upload `dist/agent-broker.exe` to the GitHub Release.

Register it with an MCP client by pointing the client's MCP config at:

```json
{
  "command": "python",
  "args": ["C:\\Users\\<you>\\.agent-broker\\agent_broker_mcp.py"]
}
```

**MCP tools exposed (30):**

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
get_chat_bootstrap
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
A: Either works. The **self-contained `agent-broker.exe`** from Releases needs no Python — it installs everything (the bridge VSIX is embedded) and is itself the MCP server (`agent-broker.exe serve`). Or run from source with Python 3.10+. Both have a built-in uninstall.

**Q: I asked Codex/Claude for a specific model — does it switch automatically?**
A: On Antigravity (CDP) and the CLIs (`--model`/`-m`), yes. The broker **cannot** switch the Codex/Claude *extension* pickers, so instead it tells the receiving agent to **state its model and STOP if it isn't the requested one**, and the bridge notifies you to select it — so a lesser/default model never silently answers. A model named only in the prompt ("get Opus's opinion") is detected as a one-off and doesn't change your topic default.

**Q: Mac / Linux?**
A: The broker is plain Python and cross-platform; the installer, bridge model-selection, and shortcut patching are Windows-first today. Contributions welcome.

---

## License

MIT.

---

**If this saved you an API bill, star the repo** ⭐ — it helps other Antigravity and VS Code users find it.
