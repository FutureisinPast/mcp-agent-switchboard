# Agent Switchboard v1.0.4

**Antigravity bridge claim isolation.**

This is a focused safety follow-up to v1.0.3. It fixes a bridge-routing edge case where a live Antigravity window could claim stale or cross-project broker work and wake the visible Gemini/Claude panel while you were doing something unrelated.

> **If this saves your agent workflow, please star the repo so others can find it.**

---

## Highlights

### 1. Antigravity claims are workspace-scoped

The bridge now passes the current workspace root when it asks the broker for queued Antigravity work. The broker only returns requests whose project/root matches that workspace.

This prevents an unrelated Antigravity window from picking up a queued task created by another project or session.

### 2. Stale queued work is ignored by default

Auto-claiming now has a 10-minute freshness window by default:

- `antigravityClaimMaxAgeMs` for Antigravity in-app requests.
- `snapshotClaimMaxAgeMs` for active context snapshot requests.

Set either value to `0` only if you intentionally want old queued work to be auto-claimed later.

### 3. Snapshot polling uses the same guardrails

Context snapshot claims are now filtered by project/root and freshness too, so a live bridge host cannot route another project’s snapshot prompt into the visible Antigravity panel.

---

## What's in this release

- **`agent-switchboard.exe`** - the self-contained installer **and** MCP server (recommended download).
- **`antigravity-agent-broker-bridge-1.0.1.vsix`** *(optional)* - the bridge extension with scoped/fresh claim behavior.
- **Source code** *(auto-attached by GitHub)*.

## Upgrading

Run the new `agent-switchboard.exe` and choose **Install** (it backs up and re-registers as before), then **reload Antigravity / VS Code** so the updated bridge extension is active. No data migration; existing `~/.agent-broker` state is preserved.

---

## Compatibility & notes

- Broker version is **1.0.4**.
- Bridge extension version is **1.0.1**.
- Existing stale rows are not auto-replayed by this version. Use `bridge reap 24` or `bridge cancel <request_id>` to clean old pending work explicitly.

## License

PolyForm Noncommercial 1.0.0 - noncommercial use allowed with the required notice; commercial use needs a separate written license from FutureisinPast / ChartTrades. See [LICENSE](LICENSE).

If this helped you, please star the repo so others can find it.
