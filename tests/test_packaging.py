"""Checks on the shipped files themselves.

These verify things the Python tests cannot: that the three copies of the
watchdog script have not drifted apart, that the resources the app looks up at
runtime are actually packaged, and that the batch/PowerShell entry points refer
to files that exist.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
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

    def test_watchdog_leaves_an_unmanaged_sdr_association_alone(self):
        """Calman reloads STANDARD itself; a forced write every five seconds fights it."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("sdr_unmanaged", script, "the app publishes this; the watchdog must read it")
        guard = re.search(r"if \(\$SavedDisplay\.StandardProfile -and \(-not \$sdrUnmanaged\)\)", script)
        self.assertIsNotNone(guard, "the STANDARD restore must be gated on the published choice")

    def test_watchdog_guards_its_own_startup(self):
        """A hung instance holds the singleton and blocks every healthy one."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        for needle in ("ArmStartupGuard", "MarkStartupComplete", "Startup: singleton acquired."):
            with self.subTest(needle=needle):
                self.assertIn(needle, script)
        for needle in ("MarkAlive", "Reconcile loop stalled"):
            with self.subTest(needle=needle):
                self.assertIn(needle, script)
        # A guard that only covers startup misses a loop that stops later, and a
        # watchdog that exits itself needs something to start it again.
        self.assertIn("shell.Run", script)
        supervises = re.search(r"Do\s+shell\.Run.*, 0, True", script)
        self.assertIsNotNone(supervises, "launcher must supervise, not fire and forget")
        # The guard is useless if it is armed after the step that blocks.
        self.assertLess(
            script.index("ArmStartupGuard($LogPath"),
            script.index("$state = Get-Content -Raw -LiteralPath $StatePath"),
            "the startup guard must be armed before the work it protects",
        )

    def test_watchdog_registers_its_task_for_the_current_sid(self):
        """Registering by username breaks on renamed or non-ASCII accounts."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("Principal.UserId = $currentSid", script)
        self.assertIn("logonTrigger.UserId = $currentSid", script)
        self.assertIn("HKCU Run fallback", script)


class StandaloneArchiveTests(unittest.TestCase):
    """The zip is distributed on its own, so it must not go stale or unlicensed."""

    ARCHIVE = ROOT / "watchdogs standalone" / "watchdogs-standalone.zip"

    def members(self) -> dict:
        with zipfile.ZipFile(self.ARCHIVE) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_archive_matches_the_scripts_beside_it(self):
        """It shipped a build predating every watchdog fix in this fork."""
        members = self.members()
        for name in ("Install-Watchdog.bat", "Uninstall-Watchdog.bat"):
            with self.subTest(member=name):
                on_disk = (ROOT / "watchdogs standalone" / name).read_bytes()
                self.assertIn(name, members)
                self.assertEqual(
                    hashlib.sha256(members[name]).hexdigest(),
                    hashlib.sha256(on_disk).hexdigest(),
                    f"{name} in the archive differs from the one beside it",
                )

    def test_archive_carries_the_licence(self):
        """GPL-3.0: the archive is distributed separately from the repository."""
        members = self.members()
        self.assertIn("LICENSE", members)
        self.assertEqual(
            hashlib.sha256(members["LICENSE"]).hexdigest(),
            hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(),
        )

    def test_archive_contains_the_watchdog_fixes(self):
        body = self.members()["Install-Watchdog.bat"].decode("utf-8", "replace")
        for needle in ("Resolve-BaseExtendedProfile", "GammaUpdatedAt", "ConvertTo-GammaTimestamp"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


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


class ReadmeAccuracyTests(unittest.TestCase):
    """Documentation drifts silently. These check only claims that would become false if
    the code moved -- not prose, which is the author's business."""

    README = ROOT / "README.md"

    def text(self) -> str:
        return self.README.read_text(encoding="utf-8", errors="replace")

    def test_every_offered_correction_option_is_documented(self):
        from sdr_hdr_profile_creator.gamma_correction import CORRECTION_OPTIONS

        for option in CORRECTION_OPTIONS:
            with self.subTest(option=option):
                self.assertIn(option, self.text())

    def test_retired_options_are_not_presented_as_choices(self):
        """They still resolve for old profiles, but offering them would be a lie."""
        for entry in ("- **Unspecified**", "- **SDR** —"):
            with self.subTest(entry=entry):
                self.assertNotIn(entry, self.text())

    def test_every_guided_step_is_named(self):
        from sdr_hdr_profile_creator.patterns import GUIDED_SEQUENCE, pattern_by_key

        for key in GUIDED_SEQUENCE:
            with self.subTest(step=key):
                self.assertIn(pattern_by_key(key).title, self.text())

    def test_the_documented_pattern_keys_match_the_pattern_count(self):
        from sdr_hdr_profile_creator.patterns import PATTERNS

        mentions_zero = "`1`–`9`, `0`" in self.text()
        self.assertEqual(mentions_zero, len(PATTERNS) > 9,
                         "the key hint and the pattern list disagree")

    def test_it_does_not_still_describe_itself_as_only_an_editor(self):
        """It measures luminance and writes it into profiles now."""
        self.assertNotIn("lightweight Windows 11 HDR profile editor", self.text())

    def test_it_no_longer_denies_doing_what_it_does(self):
        """The old text ruled out a colorimeter workflow; a meter driver is planned and
        the by-eye measurements already exist."""
        self.assertNotIn("colorimeter/spectrophotometer workflow", self.text())

    def test_the_by_eye_limitation_is_still_stated_plainly(self):
        """Broadening the claims must not quietly drop the caveat that matters."""
        self.assertIn("made by eye", self.text())


class InstallerWriteVerificationTests(unittest.TestCase):
    """A failed Set-Content is a non-terminating error, so powershell.exe still exits 0.
    The installer's only check was that exit code, and its follow-up validation read the
    file back and confirmed it looked like a watchdog -- which a stale copy from a previous
    install does. Together those reported success while leaving the old version in place."""

    def script(self) -> str:
        return (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(
            encoding="utf-8", errors="replace")

    def batch_part(self) -> str:
        """Everything before the embedded payload.

        Split on the LAST occurrence of the marker, as the installer itself does: the
        literal also appears in the extraction command, so splitting on the first cuts the
        batch section in half.
        """
        script = self.script()
        return script[: script.rindex(":__WATCHDOG_POWERSHELL_PAYLOAD__")]

    def test_the_extraction_stops_on_error_rather_than_exiting_zero(self):
        self.assertIn("$ErrorActionPreference='Stop'", self.batch_part())

    def test_the_write_is_verified_by_reading_it_back(self):
        """Checking the file looks like a watchdog passes on the previous version."""
        self.assertIn("read back different content", self.batch_part())

    def test_a_blocked_write_names_the_usual_cause(self):
        """AppData write blocking is nearly always ransomware protection, and a user with
        no lead spends the next hour on the wrong thing."""
        self.assertIn("Controlled Folder Access", self.script())

    def test_a_failed_write_leaves_the_previous_version_alone(self):
        self.assertIn("previous version, if any, has been left untouched", self.script())
