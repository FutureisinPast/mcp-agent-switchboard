# Agent Broker Bridge

This Antigravity extension polls the local Agent Broker for queued Antigravity requests and sends them into Antigravity's in-app agent panel using:

```text
antigravity.sendPromptToAgentPanel
```

The selected model in Antigravity handles the request. For strict model requests, the bridge first tries to select the requested Antigravity model automatically. The preferred path uses Antigravity's Chrome DevTools Protocol debug port, then falls back to Antigravity command/keyboard selection.

To enable the stronger model-selection path, close Antigravity and launch it with:

```powershell
antigravity --remote-debugging-address=127.0.0.1 --remote-debugging-port=9000
```

The prompt asks the model to call `complete_antigravity_request` through MCP, or write a fallback response file under the current project:

```text
<project>\.agent-broker\antigravity-responses
```

Context packs may include `context_ref=ctx_...` markers. These are compressed shared-context references stored by the broker. The model should call `retrieve_shared_context(ref, query)` only when it needs exact original details.

Use `Agent Broker Bridge: Start Compressed Chat` to start a fresh Antigravity conversation from a compact broker context pack. In non-Antigravity VS Code-compatible hosts, the command opens a bootstrap markdown file for pasting into Codex or Claude.

Model routing tools:

- `list_agent_models(agent, project, topic)` lists detected choices.
- `resolve_model_request(...)` resolves concrete requests or returns `needs_model_selection`.
- `set_model_default(...)` remembers the selected model for the project/topic.

Codex callback behavior:

- Queued Codex inbox requests are written to `~\.agent-broker\codex-inbox`.
- By default the bridge tries to open the Codex sidebar, then opens the inbox markdown automatically.
- If the host does not expose a Codex sidebar command, the markdown file still opens as the reliable handoff surface.

Uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.agent-broker\uninstall-agent-broker.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.agent-broker\uninstall-agent-broker.ps1
```

The default uninstall removes the bridge extension from Antigravity and VS Code when their CLIs are available, removes Agent Broker MCP config entries, and strips the remote-debugging flags from Antigravity/VS Code shortcuts. It keeps `~\.agent-broker` data unless `-RemoveData` is passed.
