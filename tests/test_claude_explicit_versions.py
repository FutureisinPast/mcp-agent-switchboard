"""WP-SB9: versioned Claude requests must resolve to an explicit model id,
never to a bare CLI alias. Bug: a Codex host asking for "opus 5.5" matched the
bare "opus" catalog alias, and `claude --model opus` resolved inside the
installed Claude CLI's own (possibly stale) alias table -- silently running
Opus 4.8 instead of the named 5.5. Covers: static catalog matching, the
generic versioned-model parser, prompt-text detection, the argv actually
passed to the Claude CLI, attestation verified/mismatch, and the
never-fall-back-to-Opus frontier rule.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_broker_mcp as broker  # noqa: E402


def claude_stream(model: str, response: str = "ok") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"model": model, "content": [{"text": response}]},
                }
            ),
            json.dumps({"type": "result", "result": response}),
        ]
    )


class MatchModelRequestVersionedTests(unittest.TestCase):
    """match_model_request(family="claude", ...) for versioned requests."""

    def test_opus_5_5_variants_resolve_to_explicit_id(self):
        for value in ("opus 5.5", "Opus 5.5", "claude-opus-5-5", "opus5.5"):
            with self.subTest(value=value):
                result = broker.match_model_request("claude", value)
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["model"], "claude-opus-5-5")

    def test_fable_5_1_resolves_to_explicit_id(self):
        result = broker.match_model_request("claude", "fable 5.1")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["model"], "claude-fable-5-1")

    def test_sonnet_5_resolves_to_explicit_id(self):
        result = broker.match_model_request("claude", "sonnet 5")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["model"], "claude-sonnet-5")

    def test_unknown_version_opus_4_8_uses_generic_parser(self):
        # Not in the static catalog at all -- must fall through to the
        # generic parser and still produce an explicit id, never "opus".
        result = broker.match_model_request("claude", "opus 4.8")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["model"], "claude-opus-4-8")

    def test_unknown_version_sonnet_5_2_uses_generic_parser(self):
        result = broker.match_model_request("claude", "sonnet 5.2")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["model"], "claude-sonnet-5-2")

    def test_bare_opus_still_resolves_to_bare_cli_alias(self):
        # Unversioned requests keep today's behaviour -- they mean "whatever
        # the installed CLI's own alias currently points to."
        result = broker.match_model_request("claude", "opus")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["model"], "opus")

    def test_parse_claude_versioned_model_direct(self):
        self.assertEqual(
            broker.parse_claude_versioned_model("opus 4.8"), ("claude-opus-4-8", "Claude Opus 4.8")
        )
        self.assertEqual(
            broker.parse_claude_versioned_model("sonnet 5.2"), ("claude-sonnet-5-2", "Claude Sonnet 5.2")
        )
        self.assertIsNone(broker.parse_claude_versioned_model("not a model"))


class PromptTextDetectionTests(unittest.TestCase):
    def test_opus_5_5_mention_detected_before_bare_pattern(self):
        self.assertEqual(broker.detect_model_in_prompt("please ask Opus 5.5 for a second opinion"), "opus 5.5")

    def test_bare_opus_mention_still_detected(self):
        self.assertEqual(broker.detect_model_in_prompt("please ask opus for a second opinion"), "opus")


class ArgvCarriesExplicitModelTests(unittest.TestCase):
    def test_claude_cli_invoked_with_explicit_opus_5_5_id(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(broker, "load_config", return_value={}), \
             mock.patch.object(broker, "find_executable", return_value="claude"), \
             mock.patch.object(broker, "resolve_project", return_value=broker.ProjectInfo("p", tmpdir)), \
             mock.patch.object(broker, "claude_empty_mcp_config_path", return_value=Path(tmpdir) / "empty.json"), \
             mock.patch.object(
                 broker, "run_process",
                 return_value=(0, claude_stream("claude-opus-5-5", "approved"), ""),
             ) as run:
            result = broker.consult_claude(tmpdir, "check", model_name="claude-opus-5-5", effort=None)
        argv = run.call_args[0][0]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertTrue(result.model_attested)


class AttestationExplicitVersionTests(unittest.TestCase):
    def test_matching_explicit_id_is_verified(self):
        self.assertTrue(broker.claude_model_attested("claude-opus-5-5", "claude-opus-5-5"))

    def test_mismatched_explicit_id_is_surfaced_as_mismatch(self):
        # Requested the named 5.5 but the CLI actually ran 4.8 -- this must
        # NOT be silently accepted the way a bare "opus" alias would be.
        self.assertFalse(broker.claude_model_attested("claude-opus-5-5", "claude-opus-4-8"))

    def test_bare_alias_attestation_unaffected(self):
        self.assertTrue(broker.claude_model_attested("opus", "claude-opus-4-8"))


class FrontierNeverFallsBackToOpusTests(unittest.TestCase):
    def test_best_chain_has_no_opus(self):
        self.assertEqual(broker.claude_frontier_candidates("best"), ["best", "fable"])

    def test_fable_chain_has_no_opus(self):
        self.assertEqual(broker.claude_frontier_candidates("fable"), ["fable"])

    def test_explicit_opus_request_is_still_honoured(self):
        self.assertEqual(broker.claude_frontier_candidates("opus"), ["opus"])

    def test_explicit_versioned_opus_request_is_still_honoured(self):
        self.assertEqual(broker.claude_frontier_candidates("claude-opus-5-5"), ["claude-opus-5-5"])


if __name__ == "__main__":
    unittest.main()
