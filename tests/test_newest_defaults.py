"""Owner 2026-11-19: bare family names and the Neon evaluator default to the newest
live model; an exact model id still pins that model."""
import agent_broker_mcp as broker

CODEX = [
    {"id": i, "display": i, "aliases": [], "source": "codex-debug"}
    for i in ["gpt-6-astra", "gpt-6.2-astra", "gpt-5.6-sol", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.2-sol",
              "gpt-5.6-luna", "gpt-6-luna", "gpt-5.6-terra", "gpt-6.1-terra"]
]
CODEX[0]["aliases"] = ["gpt-6-astra", "gpt 6 astra", "gpt-6", "gpt 6", "astra"]


def _codex(monkeypatch):
    monkeypatch.setattr(broker, "list_agent_models", lambda *a, **k: {"catalogs": {"codex": {"models": CODEX}}})


def test_bare_family_words_pick_newest(monkeypatch):
    _codex(monkeypatch)
    assert broker.match_model_request("codex", "astra")["model"] == "gpt-6.2-astra"
    assert broker.match_model_request("codex", "sol")["model"] == "gpt-6.2-sol"
    assert broker.match_model_request("codex", "luna")["model"] == "gpt-6-luna"
    assert broker.match_model_request("codex", "terra")["model"] == "gpt-6.1-terra"
    assert broker.match_model_request("codex", "gpt-6")["model"] == "gpt-6.2-astra"


def test_exact_ids_still_pin_older_models(monkeypatch):
    _codex(monkeypatch)
    assert broker.match_model_request("codex", "gpt-6.1-sol")["model"] == "gpt-6.1-sol"
    assert broker.match_model_request("codex", "gpt-6-astra")["model"] == "gpt-6-astra"
    assert broker.match_model_request("codex", "gpt-5.6-sol")["model"] == "gpt-5.6-sol"


def test_prompt_mentions_resolve_through_family_words(monkeypatch):
    _codex(monkeypatch)
    assert broker.detect_model_in_prompt("ask sol to review this") == "sol"
    assert broker.detect_model_in_prompt("get the gpt 5.6 sol opinion") == "gpt-5.6-sol"


def _agy(monkeypatch, ids):
    monkeypatch.setattr(
        broker, "discover_antigravity_models",
        lambda *a, **k: [{"id": i, "display": i, "source": "antigravity-cli"} for i in ids],
    )


NEON_CATALOG = [
    "gemini-4-neon-high",
    "gemini-4.1-neon-high",
    "gemini-4-neon-xhigh",
    "gemini-4-neon-low",
    "gemini-4.2-argon-high",
]


def test_neon_evaluator_bounded_and_none_pick_newest_neon_high_ignoring_argon(monkeypatch):
    monkeypatch.delenv("AGENT_BROKER_DECISION_EVALUATOR_MODEL", raising=False)
    _agy(monkeypatch, NEON_CATALOG)
    assert broker._decision_evaluator_model() == "gemini-4.1-neon-high"
    assert broker._decision_evaluator_model("bounded") == "gemini-4.1-neon-high"


def test_neon_evaluator_heavy_complexities_pick_newest_neon_xhigh(monkeypatch):
    monkeypatch.delenv("AGENT_BROKER_DECISION_EVALUATOR_MODEL", raising=False)
    _agy(monkeypatch, NEON_CATALOG)
    assert broker._decision_evaluator_model("architecture") == "gemini-4-neon-xhigh"
    assert broker._decision_evaluator_model("critical") == "gemini-4-neon-xhigh"


def test_neon_env_pin_and_off_still_win_for_every_complexity(monkeypatch):
    _agy(monkeypatch, NEON_CATALOG)
    for complexity in (None, "bounded", "architecture", "critical"):
        monkeypatch.setenv("AGENT_BROKER_DECISION_EVALUATOR_MODEL", "gemini-4-argon-high")
        assert broker._decision_evaluator_model(complexity) == "gemini-4-argon-high"
        monkeypatch.setenv("AGENT_BROKER_DECISION_EVALUATOR_MODEL", "off")
        assert broker._decision_evaluator_model(complexity) is None


def test_neon_falls_back_without_live_catalog(monkeypatch):
    monkeypatch.delenv("AGENT_BROKER_DECISION_EVALUATOR_MODEL", raising=False)
    _agy(monkeypatch, [])
    assert broker._decision_evaluator_model("bounded") == broker.DECISION_EVALUATOR_DEFAULT_MODEL
    assert broker._decision_evaluator_model("critical") == broker.DECISION_EVALUATOR_HEAVY_DEFAULT_MODEL


def test_neon_slug_classified_as_antigravity_family():
    assert broker.model_family_for(target_model="gemini-4-neon-high") == "antigravity"
    assert broker.model_family_for("gemini", "gemini-4-neon-high") == "antigravity"
    entry = broker.antigravity_model_entry_from_slug("gemini-4-neon-high")
    assert entry["display"] == "Gemini 4 Neon (High)"
