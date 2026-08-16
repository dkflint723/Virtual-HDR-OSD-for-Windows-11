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
        for needle in ("ConvertTo-GammaTimestamp", "GammaUpdatedAt", "Get-GammaEntryForDisplay"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable on this machine")
class GammaDecisionTests(unittest.TestCase):
    """Run the real decision function against fabricated state."""

    def test_get_desired_extended_profile_behaves(self):
        source = payload()
        functions = "\n\n".join(
            extract_function(source, name)
            for name in ("ConvertTo-GammaTimestamp", "Get-GammaEntryForDisplay",
                         "Get-DesiredExtendedProfile")
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


if __name__ == "__main__":
    unittest.main()
