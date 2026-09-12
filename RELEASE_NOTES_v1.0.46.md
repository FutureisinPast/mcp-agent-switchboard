# Agent Switchboard v1.0.46

> Superseded by v1.0.47. Do not use v1.0.46 for Codex hook setup: Codex auto-discovers `~/.codex/hooks.json`, while `hooks` in `config.toml` is a table, not the scalar path assumed by this release. v1.0.47 removes that incompatible writer and adds authoritative trust diagnosis.

## Highlights

- Activates the generated Codex hook file through the required top-level `hooks` setting in `~/.codex/config.toml`. Installation preserves existing TOML structure and feature flags, while refusing to overwrite a different user-selected hook file.
- Extends `doctor` with structural hook checks, exact current-session runtime evidence, the resolved Switchboard executable, and the deterministic gate-harness command. Missing activation now degrades top-level health instead of looking installed.
- Makes capability tier authoritative in dynamic role resolution. Astra remains the Codex flagship over Sol regardless of provider ordering; an explicitly frontier-classified future model can supersede it.
- Keeps symbolic `gemini flash` future-proof: every dispatch refreshes bounded live discovery and selects the newest stable numeric Flash High (3.8 today, then 3.9, 4, and later releases when advertised and runtime-attested).
- Makes routing evidence session-safe. Reports use an explicit current session, `--last` is required for historical fallback, important read-only decision work is completion-gated, and native, Flash, handoff, and flagship events share one non-duplicated view.
- Reconciles asynchronous flagship children into idempotent terminal events linked to the immutable parent decision. Access failures such as organization-level HTTP 403 are latched for the session and returned with an actionable handoff notice.
- Adds truthful terminal progress metadata for Flash execution and consistent schema guidance for research locators and attestation fields.

## Compatibility and verification

- Existing MCP tool names and public signatures remain compatible; new decision, progress, doctor, and schema fields are additive.
- Historical events that predate explicit session identity remain untouched rather than being guessed into the current session.
- Full source verification completed with `387 passed, 1 skipped, 106 subtests passed`. The packaged executable reports `Agent Switchboard 1.0.46`, and its deterministic enforcement harness passes all 20 checks, including the important read-only decision gate.
- EXE SHA-256: `09ABD9B1BA162D1B4D663CC9FB51C501725A64CA273ACFE8A7D087E8BE27E68B`. VSIX SHA-256: `3B5516D3EDF29C7BBD3805F0B5089986EA6681421356B08C2A4E37C6867C4E1C`.
