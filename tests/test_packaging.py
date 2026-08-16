"""Checks on the shipped files themselves.

These verify things the Python tests cannot: that the three copies of the
watchdog script have not drifted apart, that the resources the app looks up at
runtime are actually packaged, and that the batch/PowerShell entry points refer
to files that exist.
"""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
RESOURCES = ROOT / "src/sdr_hdr_profile_creator/resources"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WatchdogPackagingTests(unittest.TestCase):
    def test_resources_the_app_launches_are_packaged(self):
        """app._run_watchdog_script resolves these names inside the package."""
        for name in ("2- OPTIONAL - Install-Watchdog.bat", "Uninstall-Watchdog.bat"):
            with self.subTest(name=name):
                self.assertTrue((RESOURCES / name).is_file(), f"missing packaged resource: {name}")

    def test_all_copies_of_the_watchdog_stay_identical(self):
        """Three copies of a 40 KB script are shipped; drift between them is a bug.

        The packaged copy is what the GUI launches, the root copy is what users
        double-click, and the standalone copy is distributed on its own.
        """
        install = ROOT / "2- OPTIONAL - Install-Watchdog.bat"
        self.assertEqual(digest(install), digest(RESOURCES / install.name))
        self.assertEqual(digest(install), digest(ROOT / "watchdogs standalone/Install-Watchdog.bat"))

        uninstall = ROOT / "Uninstall-Watchdog.bat"
        self.assertEqual(digest(uninstall), digest(RESOURCES / uninstall.name))
        self.assertEqual(digest(uninstall), digest(ROOT / "watchdogs standalone/Uninstall-Watchdog.bat"))

    def test_watchdog_reads_the_runtime_keys_the_app_writes(self):
        """Contract between app._write_gamma_runtime_state and the watchdog."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        for key in ("profiles.Off", "profiles.On", "active_profile", "gamma_hotkeys.json"):
            with self.subTest(key=key):
                self.assertIn(key, script)

    def test_watchdog_registers_its_task_for_the_current_sid(self):
        """Registering by username breaks on renamed or non-ASCII accounts."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("Principal.UserId = $currentSid", script)
        self.assertIn("logonTrigger.UserId = $currentSid", script)
        self.assertIn("HKCU Run fallback", script)


class EntryPointTests(unittest.TestCase):
    def test_scripts_reference_files_that_exist(self):
        """Catches instructions pointing at a launcher that was renamed away."""
        pattern = re.compile(r"[\"']?([\w \-&()]+\.bat)[\"']?")
        for script in (ROOT / "Install.ps1", ROOT / "1- Install & Run.bat"):
            text = script.read_text(encoding="utf-8", errors="replace")
            for referenced in set(pattern.findall(text)):
                with self.subTest(script=script.name, referenced=referenced):
                    self.assertTrue(
                        (ROOT / referenced).is_file(),
                        f"{script.name} refers to {referenced!r}, which does not exist",
                    )

    def test_launcher_starts_the_package_entry_point(self):
        launcher = (ROOT / "1- Install & Run.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("-m sdr_hdr_profile_creator", launcher)

    def test_portable_build_is_a_single_windowless_exe(self):
        builder = (ROOT / "3- (Advanced users & developers) - Build Portable EXE.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("--onefile", builder)
        self.assertIn("--windows-console-mode=disable", builder)
        self.assertNotIn("--standalone", builder)


if __name__ == "__main__":
    unittest.main()
