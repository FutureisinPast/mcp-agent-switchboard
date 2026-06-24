# Agent Switchboard v1.0.3

**Claude/MCP context budget reduction.**

This is a focused follow-up to v1.0.2. It keeps the same cross-agent routing behavior, but cuts down the passive MCP context Claude carries when Agent Switchboard is enabled.

> **If this saves your agent workflow, please star the repo so others can find it.**

---

## Highlights

### 1. Claude gets a lite MCP catalog by default

When the MCP client identifies as Claude, `tools/list` now returns 12 compact user-facing tools instead of the full 36-tool bridge/internal catalog. The full catalog is still available with:

```powershell
$env:AGENT_BROKER_TOOL_PROFILE = "full"
```

or by setting `"mcp_tool_profile": "full"` in `~/.agent-broker/config.json`.

### 2. Tool results are summary-first

- MCP JSON tool results are compact by default.
- `get_consultation_history` now returns bounded summaries unless `include_raw=true`.
- Long `consult_*` / routed CLI answers return a bounded `response` plus `response_ref`; retrieve exact details with `retrieve_shared_context(response_ref, query)`.

### 3. Smaller default context reads

- `get_context_pack` default: 2,400 tokens.
- `get_work_memory` default: 5 entries / about 2,600 chars.
- `request_context_snapshot` default: 4 recent turns / about 300 tokens.
- `get_latest_context_snapshot` caps returned content unless `max_tokens` is raised.

## What's in this release

- **`agent-switchboard.exe`** - the self-contained installer **and** MCP server (recommended download).
- **`antigravity-agent-broker-bridge-1.0.0.vsix`** *(optional)* - the bridge extension (unchanged from v1.0.0; still embedded in the exe).
- **Source code** *(auto-attached by GitHub)*.

## Upgrading

Run the new `agent-switchboard.exe` and choose **Install** (it backs up and re-registers as before), then **restart your MCP server / reload your IDE** so the updated broker and tool catalog are live. No data migration; existing `~/.agent-broker` state is preserved.

---

## Compatibility & notes

- Bridge extension remains **1.0.0**; this is a broker-only release.
- Raw data is still available, but now requires explicit raw flags or retrieval refs.
- Set `AGENT_BROKER_COMPACT_JSON_RESULTS=false` only if you prefer pretty-printed MCP JSON over lower context usage.

## License

PolyForm Noncommercial 1.0.0 - noncommercial use allowed with the required notice; commercial use needs a separate written license from FutureisinPast / ChartTrades. See [LICENSE](LICENSE).

If this helped you, please star the repo so others can find it.
