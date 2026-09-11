"""Tests for the embedded PowerShell watchdog.

The watchdog is ~1100 lines of PowerShell inside a .bat, normally reachable only by
installing a scheduled task. These tests extract the decision function from the shipped
file and exercise it in a real PowerShell process with the native layer stubbed, so the
logic that decides which HDR profile Windows gets is actually covered.

Nothing here installs anything, and nothing touches a real colour profile association.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
WATCHDOG = ROOT / "2- OPTIONAL - Install-Watchdog.bat"
PAYLOAD_MARKER = ":__WATCHDOG_POWERSHELL_PAYLOAD__"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def payload() -> str:
    """The PowerShell the installer extracts, exactly as it does it."""
    raw = WATCHDOG.read_text(encoding="utf-8", errors="replace")
    index = raw.rindex(PAYLOAD_MARKER)
    return raw[index + len(PAYLOAD_MARKER):].lstrip("\r\n")


def extract_function(source: str, name: str) -> str:
    """Return one complete `function <name> { ... }` block by brace matching."""
    start = source.index(f"function {name} {{")
    depth = 0
    for offset in range(start, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"unterminated function {name}")


class PayloadIntegrityTests(unittest.TestCase):
    """The installer refuses to proceed unless these hold."""

    def test_payload_marker_is_present(self):
        self.assertIn(PAYLOAD_MARKER, WATCHDOG.read_text(encoding="utf-8", errors="replace"))

    def test_payload_passes_the_installers_own_checks(self):
        text = payload()
        self.assertTrue(text.lstrip().startswith("param("), "payload must start with param(")
        self.assertRegex(text, r"\$nativeSource\s*=\s*@'", "native API block missing")

    def test_the_gamma_decision_reads_both_sides(self):
        """Contract: the runtime file must be consulted, not just captured state."""
        text = payload()
        for needle in ("ConvertTo-GammaTimestamp", "GammaUpdatedAt", "Get-GammaEntryForDisplay",
                       "Resolve-BaseExtendedProfile", "base_profile_path"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable on this machine")
class GammaDecisionTests(unittest.TestCase):
    """Run the real decision function against fabricated state."""

    def test_get_desired_extended_profile_behaves(self):
        source = payload()
        functions = "\n\n".join(
            extract_function(source, name)
            # The real throttle rather than a stub: Get-DesiredExtendedProfile calls it
            # on both its recovery paths and on the unavailable path, so stubbing it
            # would leave those calls untested here and hide a rename.
            for name in ("ConvertTo-GammaTimestamp", "Get-GammaEntryForDisplay",
                         "Get-DesiredExtendedProfile", "Resolve-BaseExtendedProfile",
                         "Write-LogOnce", "Clear-LogOnce")
        )
        with tempfile.TemporaryDirectory() as directory:
            functions_path = Path(directory) / "funcs.ps1"
            functions_path.write_text(functions, encoding="utf-8")
            completed = subprocess.run(
                [
                    POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "tests" / "watchdog_gamma_decision.ps1"),
                    "-FunctionsPath", str(functions_path),
                ],
                capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(
            completed.returncode, 0,
            f"watchdog gamma decision test failed:\n{completed.stdout}\n{completed.stderr}",
        )
        self.assertIn("ALL PASS", completed.stdout)


class LogThrottleTests(unittest.TestCase):
    """A persistent fault must not erase the log it is being written to.

    Write-Log rotates at 512 KB keeping one .old. The three throttled sites sit on the
    reconcile path, which runs about 1.3 times a second per display, so an untreated
    persistent failure wrote roughly 4,700 lines an hour and the second rotation took
    every line from before the fault with it -- destroying exactly the history the log
    exists to provide.
    """

    def test_the_dedupe_table_is_initialised_in_the_payload(self):
        """The harness below has to declare this itself, because it is a top-level
        assignment that the function extractor cannot reach. If the payload ever stops
        initialising it, the harness would still pass while the watchdog threw on its
        first throttled line."""
        self.assertIn("$script:LastLogOnce = @{}", payload())

    def test_every_hot_path_log_site_goes_through_the_throttle(self):
        """The three sites are the ones whose condition is a state rather than an event:
        a requested correction whose profiles are not installed, and a failing STANDARD
        or EXTENDED write. A plain Write-Log at any of them reinstates the flood."""
        text = payload()
        for needle in (
            "Write-LogOnce ('{0}|GAMMA-UNAVAILABLE'",
            "Write-LogOnce ('{0}|STANDARD'",
            "Write-LogOnce ('{0}|EXTENDED'",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # And each is forgotten again on the matching success, or a fault that recurs
        # after being fixed would never be mentioned a second time.
        for needle in (
            "Clear-LogOnce ('{0}|GAMMA-UNAVAILABLE'",
            "Clear-LogOnce ('{0}|STANDARD'",
            "Clear-LogOnce ('{0}|EXTENDED'",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable on this machine")
class LogThrottleBehaviourTests(unittest.TestCase):
    """Run the real throttle, rather than asserting the call sites look right."""

    def test_write_log_once_behaves(self):
        source = payload()
        functions = "\n\n".join(
            extract_function(source, name) for name in ("Write-LogOnce", "Clear-LogOnce")
        )
        with tempfile.TemporaryDirectory() as directory:
            functions_path = Path(directory) / "funcs.ps1"
            functions_path.write_text(functions, encoding="utf-8")
            completed = subprocess.run(
                [
                    POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "tests" / "watchdog_log_throttle.ps1"),
                    "-FunctionsPath", str(functions_path),
                ],
                capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(
            completed.returncode, 0,
            f"watchdog log throttle test failed:\n{completed.stdout}\n{completed.stderr}",
        )
        self.assertIn("ALL PASS", completed.stdout)


if __name__ == "__main__":
    unittest.main()
