"""Tests for setup.py legacy bridge extension removal and handling."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Mock non-standard sibling modules so setup.py can be tested in an isolated tree
for mod in ("hierarchy_install", "model_roles"):
    if mod not in sys.modules:
        sys.modules[mod] = mock.MagicMock()
if "switchboard_version" not in sys.modules:
    _sb = mock.MagicMock()
    _sb.BROKER_VERSION = "1.0.0"
    sys.modules["switchboard_version"] = _sb

import setup  # noqa: E402


class FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SetupLegacyBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded_calls: list[list[str]] = []
        self._patch("_host_cli", lambda host_cli: "fake-cli")
        self._patch("durable_vsix", lambda: Path("bridge-9.9.9.vsix"))

    def _patch(self, name: str, value: object) -> None:
        original = getattr(setup, name)
        setattr(setup, name, value)
        self.addCleanup(setattr, setup, name, original)

    def _patch_subprocess_run(self, handler) -> None:
        original_run = setup.subprocess.run
        setup.subprocess.run = handler
        self.addCleanup(setattr, setup.subprocess, "run", original_run)

    # (a) legacy installed: calls are [uninstall legacy id] then [install with the vsix and "--force"],
    # result == "removed legacy local.antigravity-agent-broker-bridge; installed bridge-9.9.9.vsix"
    def test_legacy_installed_install_bridge(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            return FakeProc(stdout="Extension 'local.antigravity-agent-broker-bridge' was successfully uninstalled.\n")

        self._patch_subprocess_run(fake_run)
        result = setup.install_bridge("antigravity", dry=False)

        expected_calls = [
            ["fake-cli", "--uninstall-extension", "local.antigravity-agent-broker-bridge"],
            ["fake-cli", "--install-extension", "bridge-9.9.9.vsix", "--force"],
        ]
        self.assertEqual(self.recorded_calls, expected_calls)
        self.assertEqual(
            result,
            "removed legacy local.antigravity-agent-broker-bridge; installed bridge-9.9.9.vsix",
        )

    # (b) legacy not installed (fake stderr "Extension 'local.antigravity-agent-broker-bridge' is not installed."):
    # result == "installed bridge-9.9.9.vsix"
    def test_legacy_not_installed_install_bridge(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            if "--uninstall-extension" in argv:
                return FakeProc(stderr="Extension 'local.antigravity-agent-broker-bridge' is not installed.")
            return FakeProc()

        self._patch_subprocess_run(fake_run)
        result = setup.install_bridge("antigravity", dry=False)

        expected_calls = [
            ["fake-cli", "--uninstall-extension", "local.antigravity-agent-broker-bridge"],
            ["fake-cli", "--install-extension", "bridge-9.9.9.vsix", "--force"],
        ]
        self.assertEqual(self.recorded_calls, expected_calls)
        self.assertEqual(result, "installed bridge-9.9.9.vsix")

    # (c) dry install makes zero subprocess calls and the result names the legacy uninstall before the install
    def test_dry_install_makes_zero_calls(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            return FakeProc()

        self._patch_subprocess_run(fake_run)
        result = setup.install_bridge("antigravity", dry=True)

        self.assertEqual(self.recorded_calls, [])
        self.assertEqual(
            result,
            "would run fake-cli --uninstall-extension local.antigravity-agent-broker-bridge; "
            "would run fake-cli --install-extension bridge-9.9.9.vsix",
        )
        uninstall_pos = result.find("--uninstall-extension local.antigravity-agent-broker-bridge")
        install_pos = result.find("--install-extension bridge-9.9.9.vsix")
        self.assertGreaterEqual(uninstall_pos, 0)
        self.assertGreater(install_pos, uninstall_pos)

    # (d) uninstall_bridge removes the new id first, then the legacy id, both in the result
    def test_uninstall_bridge_removes_new_then_legacy(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            return FakeProc(stdout="uninstalled\n")

        self._patch_subprocess_run(fake_run)
        result = setup.uninstall_bridge("antigravity", dry=False)

        expected_calls = [
            ["fake-cli", "--uninstall-extension", setup.BRIDGE_EXTENSION_ID],
            ["fake-cli", "--uninstall-extension", "local.antigravity-agent-broker-bridge"],
        ]
        self.assertEqual(self.recorded_calls, expected_calls)
        self.assertEqual(
            result,
            f"uninstalled {setup.BRIDGE_EXTENSION_ID}; removed legacy local.antigravity-agent-broker-bridge",
        )

    # (e) a legacy uninstall that raises OSError does not stop the install, and the result contains
    # "legacy local.antigravity-agent-broker-bridge: ERROR" and "installed bridge-9.9.9.vsix"
    def test_legacy_uninstall_error_does_not_stop_install(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            if "--uninstall-extension" in argv:
                raise OSError("mocked permission denied")
            return FakeProc()

        self._patch_subprocess_run(fake_run)
        result = setup.install_bridge("antigravity", dry=False)

        expected_calls = [
            ["fake-cli", "--uninstall-extension", "local.antigravity-agent-broker-bridge"],
            ["fake-cli", "--install-extension", "bridge-9.9.9.vsix", "--force"],
        ]
        self.assertEqual(self.recorded_calls, expected_calls)
        self.assertIn("legacy local.antigravity-agent-broker-bridge: ERROR", result)
        self.assertIn("installed bridge-9.9.9.vsix", result)

    def test_legacy_bridge_constants(self) -> None:
        self.assertEqual(setup.LEGACY_BRIDGE_EXTENSION_IDS, ("local.antigravity-agent-broker-bridge",))

    def test_uninstall_bridge_dry(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            return FakeProc()

        self._patch_subprocess_run(fake_run)
        result = setup.uninstall_bridge("antigravity", dry=True)

        self.assertEqual(self.recorded_calls, [])
        self.assertEqual(
            result,
            f"would run fake-cli --uninstall-extension {setup.BRIDGE_EXTENSION_ID}; "
            "would run fake-cli --uninstall-extension local.antigravity-agent-broker-bridge",
        )

    def test_uninstall_extension_helpers(self) -> None:
        def fake_run(argv, *args, **kwargs):
            self.recorded_calls.append(list(argv))
            if "missing.ext" in argv:
                return FakeProc(stderr="Extension 'missing.ext' is not installed.")
            return FakeProc(stdout="success")

        self._patch_subprocess_run(fake_run)
        self.assertEqual(setup._uninstall_extension("fake-cli", "missing.ext"), "not installed")
        self.assertEqual(setup._uninstall_extension("fake-cli", "present.ext"), "uninstalled present.ext")

    def test_remove_legacy_bridges_helpers(self) -> None:
        # Dry
        dry_res = setup.remove_legacy_bridges("fake-cli", dry=True)
        self.assertEqual(dry_res, ["would run fake-cli --uninstall-extension local.antigravity-agent-broker-bridge"])

        # Non-dry installed
        def fake_run_ok(argv, *args, **kwargs):
            return FakeProc(stdout="uninstalled")

        self._patch_subprocess_run(fake_run_ok)
        self.assertEqual(setup.remove_legacy_bridges("fake-cli", dry=False), ["removed legacy local.antigravity-agent-broker-bridge"])

        # Non-dry not installed
        def fake_run_not_installed(argv, *args, **kwargs):
            return FakeProc(stderr="not installed")

        self._patch_subprocess_run(fake_run_not_installed)
        self.assertEqual(setup.remove_legacy_bridges("fake-cli", dry=False), [])

        # Non-dry exception
        def fake_run_exc(argv, *args, **kwargs):
            raise RuntimeError("unexpected failure")

        self._patch_subprocess_run(fake_run_exc)
        self.assertEqual(
            setup.remove_legacy_bridges("fake-cli", dry=False),
            ["legacy local.antigravity-agent-broker-bridge: ERROR: unexpected failure"],
        )


if __name__ == "__main__":
    unittest.main()
