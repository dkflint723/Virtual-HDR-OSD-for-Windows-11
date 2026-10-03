"""Tests for the embedded PowerShell watchdog.

The watchdog is ~1850 lines of PowerShell inside a .bat, normally reachable only by
installing a scheduled task. These tests extract the decision function from the shipped
file and exercise it in a real PowerShell process with the native layer stubbed, so the
logic that decides which HDR profile Windows gets is actually covered.

Nothing here installs anything, and nothing touches a real colour profile association.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
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


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable on this machine")
class GammaHotkeyTests(unittest.TestCase):
    """Run the real Alt+1 / Alt+2 handler against a stubbed native layer."""

    def test_the_hotkeys_respect_a_restore(self):
        source = payload()
        functions = "\n\n".join(
            extract_function(source, name)
            for name in ("ConvertTo-GammaTimestamp", "Get-GammaEntryForDisplay",
                         "Invoke-GammaHotkey", "Write-LogOnce", "Clear-LogOnce")
        )
        with tempfile.TemporaryDirectory() as directory:
            functions_path = Path(directory) / "funcs.ps1"
            functions_path.write_text(functions, encoding="utf-8")
            completed = subprocess.run(
                [
                    POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "tests" / "watchdog_gamma_hotkey.ps1"),
                    "-FunctionsPath", str(functions_path),
                ],
                capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(
            completed.returncode, 0,
            f"watchdog gamma hotkey test failed:\n{completed.stdout}\n{completed.stderr}",
        )
        self.assertIn("ALL PASS", completed.stdout)


REAL_MUTEX = r"Local\ColorProfileModeWatchdogStandalone"

# A mutex that denies everyone, which is what a medium-integrity process meets when the
# watchdog was started by an elevated install. Created under a test-only name: the real
# one belongs to a watchdog that may be running on this machine.
DENIED_MUTEX_PS = r"""
$sec = New-Object System.Security.AccessControl.MutexSecurity
$everyone = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')
$sec.AddAccessRule((New-Object System.Security.AccessControl.MutexAccessRule($everyone, 'FullControl', 'Deny')))
$created = $false
$held = New-Object System.Threading.Mutex($false, '__NAME__', [ref]$created, $sec)
"""


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable on this machine")
class ElevatedWatchdogLivenessTests(unittest.TestCase):
    """A watchdog this account may not open is running, not absent."""

    def run_ps(self, body: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "probe.ps1"
            script.write_text(body, encoding="utf-8")
            completed = subprocess.run(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout.strip()

    def names(self) -> tuple[str, str]:
        import uuid
        token = uuid.uuid4().hex
        return rf"Local\VhdrTestDenied-{token}", rf"Local\VhdrTestMissing-{token}"

    def test_the_installer_probe_counts_a_denied_mutex_as_running(self):
        denied, missing = self.names()
        function = extract_function(payload(), "Test-WatchdogSingletonHeld")
        self.assertIn(REAL_MUTEX, function)
        body = DENIED_MUTEX_PS.replace("__NAME__", denied) + "\n" + "\n".join((
            function.replace(REAL_MUTEX, denied),
            "$a = Test-WatchdogSingletonHeld",
            function.replace(REAL_MUTEX, missing),
            "$b = Test-WatchdogSingletonHeld",
            "Write-Output ('{0} {1}' -f $a, $b)",
        ))
        self.assertEqual("True False", self.run_ps(body))

    def test_the_uninstaller_notices_a_watchdog_it_could_not_stop(self):
        denied, missing = self.names()
        text = (ROOT / "Uninstall-Watchdog.bat").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "$running=$true; for" in l)
        probe = line.strip()[1:-len('" ^')]
        self.assertIn(REAL_MUTEX, probe)
        body = DENIED_MUTEX_PS.replace("__NAME__", denied) + "\n" + "\n".join((
            probe.replace(REAL_MUTEX, denied), "$a = $running",
            probe.replace(REAL_MUTEX, missing), "$b = $running",
            "Write-Output ('{0} {1}' -f $a, $b)",
        ))
        self.assertEqual("True False", self.run_ps(body))

    @unittest.skipUnless(sys.platform == "win32", "OpenMutexW is Windows")
    def test_the_app_probe_counts_a_denied_mutex_as_running(self):
        import ctypes
        import uuid
        from ctypes import wintypes
        from unittest import mock

        from sdr_hdr_profile_creator import windows_api

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        descriptor = ctypes.c_void_p()
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        self.assertTrue(advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:(D;;GA;;;WD)", 1, ctypes.byref(descriptor), None))
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        kernel32.CreateMutexW.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        name = rf"Local\VhdrTestDenied-{uuid.uuid4().hex}"
        handle = kernel32.CreateMutexW(ctypes.byref(attributes), False, name)
        self.assertTrue(handle)
        self.addCleanup(kernel32.CloseHandle, handle)

        with mock.patch.object(windows_api, "WATCHDOG_SINGLETON_MUTEX", name):
            self.assertTrue(windows_api.watchdog_is_running())
        with mock.patch.object(windows_api, "WATCHDOG_SINGLETON_MUTEX", name + "-missing"):
            self.assertFalse(windows_api.watchdog_is_running())


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
