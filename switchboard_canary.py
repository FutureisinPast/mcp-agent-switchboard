#!/usr/bin/env python3
"""Acceptance probes for the routing hierarchy: `canary flash` and `gate-harness`.

Two different questions, deliberately answered by two different tools:

  canary flash   Does a real Flash dispatch work end to end, through the same code
                 path an agent uses, and come back with a ledger receipt and a
                 backend-attested model? Needs `agy` and network.

  gate-harness   Does the routing gate actually enforce, deterministically, with no
                 model in the loop? Drives the hook functions directly with synthetic
                 payloads and asserts the decision after each call. This is the
                 enforcement proof; a live session can only ever show PREFERENCE,
                 because a cooperative model may simply never make the fifth direct
                 call the negative control depends on.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any


# --- canary ----------------------------------------------------------------
def canary_flash(argv: list[str]) -> int:
    """Dispatch one harmless read-only package through the real router."""
    import agent_broker_mcp as broker

    prompt = "Reply with the single word OK. Do not read any file."
    for i, token in enumerate(argv):
        if token == "--prompt" and i + 1 < len(argv):
            prompt = argv[i + 1]
    package_id = f"WP-CANARY-{uuid.uuid4().hex[:8]}"
    args: dict[str, Any] = {
        "target_agent": "antigravity",
        "surface": "cli",
        "target_model": "gemini flash",
        "effort": "high",
        "mode": "plan",
        "task_kind": "quick_check",
        "work_package_id": package_id,
        "prompt": prompt,
        "max_response_chars": 4000,
    }
    print(f"canary: dispatching {package_id} through route_agent_task ...")
    try:
        result = broker.route_agent_task(args)
    except Exception as exc:  # noqa: BLE001
        print(f"canary: FAILED to dispatch: {type(exc).__name__}: {exc}")
        return 1
    if not isinstance(result, dict):
        print(f"canary: FAILED, router returned {type(result).__name__}")
        return 1

    fields = {
        "route": result.get("route"),
        "surface": result.get("surface"),
        "status": result.get("status"),
        "outcome": result.get("outcome"),
        "receipt": result.get("receipt"),
        "work_package_id": result.get("work_package_id"),
        "requested_model": result.get("requested_model"),
        "attested_model": result.get("attested_model"),
        "attestation": result.get("attestation"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "credit_eligible": result.get("credit_eligible"),
        "brain_verification": (result.get("brain_verification") or {}).get("status"),
        "accepted": result.get("accepted"),
    }
    for key, value in fields.items():
        print(f"  {key:<20}: {value}")

    meta = broker.switchboard_meta()["switchboard"]
    print(f"  {'server_version':<20}: {meta['version']}")
    print(f"  {'server_build':<20}: {meta['build']}")

    receipt = result.get("receipt")
    if receipt:
        resolved = broker.lookup_consultation_receipt(str(receipt).split(":", 1)[-1])
        print(f"  {'ledger_row':<20}: {'found' if resolved else 'MISSING'}")
        if not resolved:
            print("canary: FAILED — the receipt does not resolve to a ledger row.")
            return 1

    if result.get("status") != "ok":
        # A missing/limited `agy` is a real answer, not a crash: report it plainly.
        print("canary: dispatch did not succeed. Response follows (truncated):")
        print("  " + str(result.get("response"))[:600])
        return 2
    if result.get("accepted") is not False:
        print("canary: FAILED — a Flash completion must never be pre-accepted.")
        return 1
    print("canary: OK")
    return 0


# --- deterministic gate harness --------------------------------------------
def _payload(tool: str, session: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "session_id": session,
        "tool_name": tool,
        "tool_input": {},
        "hook_event_name": "PreToolUse",
        # Hosts stamp this from the hook argv; the harness must match a real payload.
        "_switchboard_host": "claude",
    }
    payload.update(extra)
    return payload


def _denied(response: Any) -> bool:
    if not isinstance(response, dict) or not response:
        return False
    if response.get("decision") == "block":
        return True
    specific = response.get("hookSpecificOutput") or {}
    return specific.get("permissionDecision") == "deny"


def gate_harness(argv: list[str]) -> int:
    """Drive the gate with synthetic payloads and assert every decision.

    Fresh session id per case, so nothing depends on prior session state."""
    import routing_gate

    verbose = "--verbose" in argv
    limit = routing_gate.DIRECT_LABOUR_LIMIT
    failures: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        if not ok:
            failures.append(f"{name}: {detail}")

    def fresh() -> str:
        return f"harness-{uuid.uuid4()}"

    # 1. The allowance is spent, then the next eligible call is denied.
    session = fresh()
    decisions = []
    for i in range(limit + 1):
        response = routing_gate.pre_tool_use(
            _payload("Read", session, tool_use_id=f"call-{i}", tool_input={"file_path": "x.txt"})
        )
        decisions.append(_denied(response))
    check(
        f"allowance of {limit} direct calls is honoured",
        not any(decisions[:limit]),
        f"a call within the allowance was denied: {decisions[:limit]}",
    )
    check(
        f"call {limit + 1} is DENIED (negative control)",
        decisions[limit],
        "the gate did not deny the call past the allowance — enforcement is not engaged",
    )

    # 2. Switchboard support calls are neutral in BOTH namespace spellings.
    for spelling in ("mcp__agent-switchboard__consult_codex", "mcp__agent_switchboard__consult_codex"):
        session = fresh()
        neutral = True
        for i in range(limit + 2):
            response = routing_gate.pre_tool_use(_payload(spelling, session, tool_use_id=f"s-{i}"))
            if _denied(response):
                neutral = False
                break
        check(f"support tool is neutral: {spelling}", neutral, "a support call consumed the allowance")

    # 3. PowerShell is classified as labour (it is this machine's primary shell).
    session = fresh()
    ps_denied = False
    for i in range(limit + 1):
        response = routing_gate.pre_tool_use(
            _payload("PowerShell", session, tool_use_id=f"ps-{i}", tool_input={"command": "Get-ChildItem"})
        )
        ps_denied = _denied(response)
    check(
        "PowerShell counts as direct labour",
        ps_denied,
        "PowerShell calls never triggered the gate — the shell lane is invisible to it",
    )

    # 4. A direct `agy` invocation is denied outright, via PowerShell too.
    for tool, field in (("Bash", "command"), ("PowerShell", "command")):
        session = fresh()
        response = routing_gate.pre_tool_use(
            _payload(tool, session, tool_use_id="agy-1", tool_input={field: "agy --print 'hello'"})
        )
        check(f"direct agy via {tool} is denied", _denied(response), "sender-side agy was allowed")

    # 5. An override registered for a package opens the next block.
    session = fresh()
    for i in range(limit + 1):
        routing_gate.pre_tool_use(_payload("Read", session, tool_use_id=f"o-{i}"))
    registered = routing_gate.register_brain_override(session, "WP-HARNESS", "harness override reason")
    after = routing_gate.pre_tool_use(_payload("Read", session, tool_use_id="o-after"))
    check("registered override opens the next block", bool(registered) and not _denied(after),
          "the gate stayed closed after a valid override")

    # 6. A native cheap-agent package opens the next block.
    session = fresh()
    for i in range(limit + 1):
        routing_gate.pre_tool_use(_payload("Read", session, tool_use_id=f"n-{i}"))
    routing_gate.subagent_start({"session_id": session, "agent_id": "agent-1", "agent_type": "Explore"})
    after = routing_gate.pre_tool_use(_payload("Read", session, tool_use_id="n-after"))
    check("native cheap package opens the next block", not _denied(after),
          "a started native reader did not relieve the block")

    # 7. Delegation itself is never blocked — the escape hatch must always be open.
    session = fresh()
    for i in range(limit + 2):
        routing_gate.pre_tool_use(_payload("Read", session, tool_use_id=f"d-{i}"))
    delegation = routing_gate.pre_tool_use(_payload("Agent", session, tool_use_id="deleg"))
    route = routing_gate.pre_tool_use(
        _payload("mcp__agent-switchboard__route_agent_task", session, tool_use_id="route")
    )
    check("Agent/Task delegation is never denied", not _denied(delegation),
          "the gate blocked the very delegation it demands")
    check("route_agent_task is never denied", not _denied(route),
          "the gate blocked the Flash workhorse lane")

    # 8. Credit cannot be farmed. A dispatch only relieves the block when it actually
    #    completed AND its receipt resolves in the ledger. Each case below must leave
    #    the gate exactly as closed as it was.
    def dispatch_payload(session: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": session,
            "tool_name": "mcp__agent-switchboard__route_agent_task",
            "tool_input": {},
            "hook_event_name": "PostToolUse",
            "_switchboard_host": "claude",
            "tool_response": {"content": [{"type": "text", "text": json.dumps(result)}]},
        }

    farm_cases = [
        ("blocked outcome", {"receipt": f"broker:{uuid.uuid4()}", "outcome": "blocked"}),
        ("failed outcome", {"receipt": f"broker:{uuid.uuid4()}", "outcome": "failed_pre_mutation"}),
        ("rejected outcome", {"receipt": f"broker:{uuid.uuid4()}", "outcome": "rejected"}),
        ("unavailable outcome", {"receipt": f"broker:{uuid.uuid4()}", "outcome": "unavailable_pre_mutation"}),
        ("missing receipt", {"outcome": "completed_verified"}),
        ("malformed receipt", {"receipt": "broker:not-a-uuid", "outcome": "completed_verified"}),
        ("unknown receipt not in ledger",
         {"receipt": f"broker:{uuid.uuid4()}", "outcome": "completed_verified"}),
    ]
    for label, result in farm_cases:
        session = fresh()
        for i in range(limit + 1):
            routing_gate.pre_tool_use(_payload("Read", session, tool_use_id=f"f-{i}"))
        routing_gate.post_tool_use(dispatch_payload(session, result))
        after = routing_gate.pre_tool_use(_payload("Read", session, tool_use_id="f-after"))
        check(
            f"no credit for: {label}",
            _denied(after),
            "the block was relieved by a dispatch that did not complete-and-verify",
        )

    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name:<{width}}" + (f"   {detail}" if not ok else ""))
    print()
    relaxed = getattr(routing_gate, "policy_is_relaxed", lambda: False)()
    print(f"gate-harness: {len(checks) - len(failures)}/{len(checks)} checks passed "
          f"(allowance={limit}, mode={routing_gate.gate_mode()}"
          f"{', POLICY RELAXED' if relaxed else ''})")
    if verbose:
        print(json.dumps({"failures": failures}, indent=2))
    return 0 if not failures else 1


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command = argv[0].lower()
    if command == "flash":
        return canary_flash(argv[1:])
    if command in {"gate", "gate-harness"}:
        return gate_harness(argv[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
