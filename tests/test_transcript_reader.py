"""Regression: the model is read only from agy's own settings record, never from
file contents a worker read (seen 2026-11-16: a test fixture inside a MODEL record
was mistaken for the runtime model and a correct package was rejected)."""
import json

import agent_broker_mcp as broker

SEL = "changed setting `Model Selection` from {old} to {new}."


def _rec(source: str, kind: str, content: str) -> str:
    return json.dumps({"type": kind, "source": source, "step_index": 0, "content": content})


def _catalog(monkeypatch):
    monkeypatch.setattr(
        broker,
        "discover_antigravity_models",
        lambda *a, **k: [
            {"id": "gemini-4-hydrogen-xhigh", "display": "Gemini 4 Hydrogen (XHigh)"},
            {"id": "gemini-4-hydrogen-high", "display": "Gemini 4 Hydrogen (High)"},
            {"id": "gemini-3.6-flash-high", "display": "Gemini 3.6 Flash (High)"},
        ],
    )


def _settings(new: str) -> str:
    return _rec("USER_EXPLICIT", "USER_INPUT", "<USER_SETTINGS_CHANGE>\nThe user " + SEL.format(old="None", new=new) + " No need to comment on this change")


def test_worker_read_fixture_does_not_override_real_selection(monkeypatch):
    _catalog(monkeypatch)
    text = "\n".join([
        _settings("Gemini 4 Hydrogen (XHigh)"),
        _rec("MODEL", "GENERIC", '400: "' + SEL.format(old="Gemini 3.5 Flash (High)", new="Gemini 3.6 Flash (High)") + '",'),
    ])
    assert broker._extract_model_from_transcript_text(text) == "gemini-4-hydrogen-xhigh"


def test_only_worker_content_mentions_a_model_returns_none(monkeypatch):
    _catalog(monkeypatch)
    text = _rec("MODEL", "GENERIC", "x " + SEL.format(old="A", new="Gemini 3.6 Flash (High)") + " y")
    assert broker._extract_model_from_transcript_text(text) is None


def test_last_trusted_settings_record_wins(monkeypatch):
    _catalog(monkeypatch)
    text = "\n".join([_settings("Gemini 4 Hydrogen (High)"), _settings("Gemini 4 Hydrogen (XHigh)")])
    assert broker._extract_model_from_transcript_text(text) == "gemini-4-hydrogen-xhigh"


def test_plain_text_legacy_line_still_works(monkeypatch):
    _catalog(monkeypatch)
    assert broker._extract_model_from_transcript_text("The user " + SEL.format(old="None", new="Gemini 4 Hydrogen (High)") + "\n") == "gemini-4-hydrogen-high"