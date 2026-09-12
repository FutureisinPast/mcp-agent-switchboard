# Agent Switchboard v1.0.47

## Highlights

- Corrects v1.0.46's Codex hook activation assumption. Codex automatically discovers `~/.codex/hooks.json`; its `config.toml` `hooks` value is a table for inline hooks and trust state, not a scalar path. Switchboard no longer writes the incompatible scalar and preserves existing `[hooks.state]` entries.
- Adds an authoritative, bounded, read-only Codex `hooks/list` diagnostic. `doctor` reports discovery, enabled state, and trust state for all six required Switchboard handlers.
- Reports new or changed handlers as `pending_user_review` and directs the user to Codex `/hooks`, the supported review/approval flow. The installer does not manufacture Codex's private trust hashes or enable a broad trust bypass.
- Retains all v1.0.46 routing fixes: capability tier outranks provider priority, Astra stays above Sol, explicit session identity prevents unrelated audit fallback, read-only important decisions are completion-gated, async consultation outcomes reconcile durably, and Flash receipts expose truthful terminal progress.
- Keeps symbolic `gemini flash` future-proof: the newest advertised stable numeric Flash High is selected and runtime-attested for every dispatch, including 3.8 today and future 3.9/4 releases.

## Compatibility and verification

- Existing MCP tool names and signatures remain compatible. The Codex health fields are additive.
- Existing user and plugin hook trust state is preserved. New Switchboard hooks still require user review through `/hooks`, by Codex design.
- Full source verification completed with `388 passed, 1 skipped, 108 subtests passed`. The packaged executable reports `Agent Switchboard 1.0.47`, its deterministic enforcement harness passes all 20 checks, and packaged `doctor` authoritatively reports all six local Switchboard hooks as discovered/enabled but pending user review.
- EXE SHA-256: `90AEE598AC2FE3CA47DA9DB240EB019D94C8ACDC807D933DE4B0FB1D70D1E0C3`. VSIX SHA-256: `87B8FED9E37EA96E0074D4AF2D713F1C265ECD385BC2434B1C2C19E6C96BDA74`.
