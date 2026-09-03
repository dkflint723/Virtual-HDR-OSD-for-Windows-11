"""Checks on the shipped files themselves.

These verify things the Python tests cannot: that the three copies of the
watchdog script have not drifted apart, that the resources the app looks up at
runtime are actually packaged, and that the batch/PowerShell entry points refer
to files that exist.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import zipfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
RESOURCES = ROOT / "src/sdr_hdr_profile_creator/resources"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestFileShapeTests(unittest.TestCase):
    """The suite has to be honest about how much of itself it is running."""

    def test_no_test_file_strands_classes_after_its_main_guard(self):
        """`if __name__ == "__main__": unittest.main()` in the middle of a file collects
        only what is defined above it. Under `unittest discover` the module is merely
        imported, so everything runs and this never shows -- but running one file while
        working on it is the normal thing to do, and that reported a confident pass over
        part of the file.

        It was 405 tests across six files when this was found: 203 of them in
        test_gui.py, 111 in test_pattern_view.py.
        """
        import ast

        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            guard = next(
                (node.lineno for node in tree.body
                 if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)),
                None,
            )
            if guard is None:
                continue
            stranded = sum(
                1
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.lineno > guard
                for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test")
            )
            if stranded:
                offenders.append(f"{path.name}: {stranded} tests after line {guard}")

        self.assertEqual(offenders, [], "move the __main__ guard to the end of the file")


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
        guard = re.search(r"if \(\$sdrDesired -and \(-not \$sdrUnmanaged\)\)", script)
        self.assertIsNotNone(guard, "the STANDARD restore must be gated on the published choice")

    def test_watchdog_prefers_the_pinned_sdr_profile_over_its_own_capture(self):
        """The pin is what the user chose; the capture is only what happened to be
        associated when the watchdog was installed.

        Publishing just the "unmanaged" boolean was not enough: the forced write ran
        every five seconds against ``$SavedDisplay.StandardProfile``, so a pin the GUI
        reported as restored was reverted within five seconds, and re-running the
        installer re-captured the reverted profile rather than fixing it.
        """
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("sdr_profile", script, "the watchdog must read the published pin")
        self.assertIn("$sdrDesired = [string]$SavedDisplay.StandardProfile", script,
                      "the capture is the fallback, not the first choice")
        self.assertIn("if ($sdrPinned) { $sdrDesired = $sdrPinned }", script,
                      "a published pin must override the capture")
        # And the write itself must use the resolved value, not the raw capture.
        self.assertNotIn(
            "CPST_STANDARD_DISPLAY_COLOR_MODE,\n                [string]$SavedDisplay.StandardProfile",
            script.replace("\r\n", "\n"),
            "the STANDARD write still uses the install-time capture",
        )

    def test_the_app_publishes_the_sdr_pin_the_watchdog_reads(self):
        """Both halves of the contract, or the watchdog reads a key nobody writes."""
        app = (ROOT / "src/sdr_hdr_profile_creator/app.py").read_text(encoding="utf-8")
        self.assertIn('"sdr_profile"', app, "the app must publish the pinned SDR profile")

    def test_the_installer_refuses_to_arm_both_startup_mechanisms(self):
        """Arming the Run key beside a surviving task starts two watchdogs per sign-in.

        Observed: the task from an earlier elevated install could not be deleted by an
        ordinary account, ``DeleteTask`` raised E_ACCESSDENIED, the whole registration
        block fell into the catch, and the catch armed the Run key without looking.
        """
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("$taskSurvives", script, "the fallback must check whether the task is gone")
        self.assertIn("$probeRoot.GetTask($TaskName)", script,
                      "deleting is not proof; the task has to be looked for afterwards")
        catch_body = script[script.index("$registrationError = $_.Exception.Message"):]
        arm = catch_body.index("New-ItemProperty")
        check = catch_body.index("if ($taskSurvives)")
        self.assertLess(check, arm, "the Run key must only be armed after the task check")

    def test_both_sweeps_reap_the_launcher_and_not_only_its_child(self):
        """Killing the PowerShell alone accomplishes nothing: Launcher.vbs restarts it
        five seconds later. Every copy of the sweep must name wscript."""
        for name in ("2- OPTIONAL - Install-Watchdog.bat", "Uninstall-Watchdog.bat"):
            with self.subTest(script=name):
                body = (ROOT / name).read_text(encoding="utf-8", errors="replace")
                self.assertIn("wscript.exe", body, "the supervisor is a wscript.exe")
                self.assertIn("Launcher.vbs", body, "matched by the script it runs")

    def test_a_surplus_launcher_stands_down_instead_of_respawning(self):
        """Exit 4 means another instance holds the singleton. Looping on that respawns
        a doomed PowerShell every five seconds for the life of the session -- but
        standing down on the *first* one loses the handover the installer creates, so
        it takes four in a row."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("exit 4", script, "losing the singleton needs its own exit code")
        self.assertIn("code = shell.Run(", script, "the launcher must read the exit code")
        self.assertIn("If code = 2 Or code = 3 Then", script, "these will not fix themselves")
        self.assertIn("If surplus >= 4 Then", script, "a surplus supervisor must give up eventually")
        self.assertIn("WScript.Quit 0", script)
        # 9 is the startup guard asking for a fresh instance: it must NOT stand down.
        self.assertNotIn("code = 9", script, "the guard's restart request must be honoured")

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
        # bWaitOnReturn = True is what makes it supervise rather than fire and forget,
        # and is also how the loop learns the exit code it now decides on.
        supervises = re.search(r"Do\s+code = shell\.Run\(.*, 0, True\)", script)
        self.assertIsNotNone(supervises, "launcher must supervise, not fire and forget")
        # The guard is useless if it is armed after the step that blocks.
        self.assertLess(
            script.index("ArmStartupGuard($LogPath"),
            # The state load, however it reads the file. Get-Content -Raw used to do
            # it and now does not, which is a cheaper read but was not the leak; see
            # test_the_reconcile_path_does_not_terminate_a_pipeline_early for that.
            script.index("$state = [System.IO.File]::ReadAllText($StatePath)"),
            "the startup guard must be armed before the work it protects",
        )

    def payload(self) -> str:
        """The watchdog script the installer writes, without the .bat wrapper."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        marker = ":__WATCHDOG_POWERSHELL_PAYLOAD__"
        return script[script.rindex(marker) + len(marker):]

    def test_the_reconcile_path_does_not_use_get_content(self):
        """Not the leak fix -- the pipeline one below is. This is allocation churn.

        Get-Content -Raw builds a pipeline and a wrapped string to hand back one
        file's text, on a path that runs about 75 times a minute; ReadAllText is the
        same result without either. A build carrying this change and not the pipeline
        one still leaked at the full rate, which is how it is known not to be it.

        The two remaining uses are in the installer's own one-shot extraction and
        validation, which run once per install and are left alone.
        """
        offenders = [
            line.strip() for line in self.payload().splitlines()
            if "Get-Content" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [], "the watchdog itself must not use Get-Content")

    def test_the_reconcile_path_does_not_terminate_a_pipeline_early(self):
        """This is the leak fix.

        Where-Object piped into Select-Object -First 1 retains memory in Windows
        PowerShell 5.1 that no forced GC reclaims: Select-Object -First stops the
        upstream command by throwing, and that path holds on to what it was carrying.
        Two runs of the whole watchdog under an identical probe, differing only in
        these loops -- with them, committed bytes rose monotonically, 108.2 MB to
        113.0 MB over 2,168 passes; without them, 3,806 passes ended at 108.1 MB,
        below where they started.

        Where-Object on its own is clean, so the assertion is narrow: nothing the
        watchdog runs may cut a pipeline short.
        """
        offenders = [
            line.strip() for line in self.payload().splitlines()
            if "Select-Object" in line and "-First" in line
            and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [], "early pipeline termination leaks in 5.1")

    def test_the_runtime_lookup_identifies_the_monitor_not_the_slot(self):
        """State.json was moved onto the EDID device path because \\\\.\\DISPLAY1 is a slot
        Windows reassigns. gamma_hotkeys.json was left matching on the slot, so the two
        lookups disagreed after a hotplug and one monitor could be handed the other's
        Off/On pair -- which is exactly what the watchdog then asserts into Windows.

        app.runtime_record_matches applies the same rule on the Python side; if these
        two ever diverge the bug comes straight back.
        """
        payload = self.payload()
        self.assertIn("$wantPath = [string]$CurrentDisplay.DevicePath", payload)
        self.assertIn("$entryPath = [string]$property.Value.device_path", payload)
        # The slot is still the fallback: records written before the GUI published a
        # path have none, and refusing those would forget every existing display.
        self.assertIn("} elseif ($property.Value.gdi_name -eq $CurrentDisplay.GdiName) {", payload)

    def test_state_files_are_replaced_rather_than_truncated(self):
        """Set-Content truncates in place. A kill inside that window leaves a partial
        State.json, and the watchdog then cannot start at all -- far worse than losing
        the switch that was being written. The GUI reads gamma_hotkeys.json with no
        retry and answers a torn read by starting from an empty payload, which then
        drops every other display's record on its next publish."""
        payload = self.payload()
        truncating = [
            line.strip() for line in payload.splitlines()
            if "Set-Content -LiteralPath $StatePath " in line
            or "Set-Content -LiteralPath $GammaStatePath " in line
        ]
        self.assertEqual(truncating, [], "a state file is still written in place")
        self.assertIn("Move-Item -LiteralPath $stateTmp -Destination $StatePath -Force", payload)
        self.assertIn(
            "Move-Item -LiteralPath $gammaTmp -Destination $GammaStatePath -Force", payload
        )

    def test_an_unreadable_state_file_stops_the_watchdog_instead_of_respawning_it(self):
        """Under $ErrorActionPreference = 'Stop' a truncated State.json was a terminating
        error, so PowerShell exited 1 -- which Launcher.vbs reads as "restart me". One
        torn write became a silent respawn every five seconds for the rest of the
        session. 2 is the launcher's stop code, and retrying cannot fix a corrupt file.
        """
        payload = self.payload()
        guard = payload.find("State.json could not be parsed")
        self.assertNotEqual(-1, guard, "the startup parse is still unguarded")
        self.assertIn("exit 2", payload[guard:guard + 400])

    def test_a_failed_install_still_records_that_it_failed(self):
        """Every throw in -Install used to leave nothing behind: no watchdog, because the
        old one is stopped on the way in, and no result file, because the only writer is
        at the very end. The GUI then fell back to Watchdog.ps1's mtime -- stamped by the
        .bat before any of this runs -- and printed a green "Watchdog installed".

        The owner hit exactly this on 2026-08-28: Task Scheduler registration was refused
        with Access is denied, and the install reported success.
        """
        payload = self.payload()
        trap = payload.find("    trap {")
        self.assertNotEqual(-1, trap, "no trap around the install body")
        body = payload[trap:trap + 1200]
        self.assertIn("ok       = $false", body)
        self.assertIn("$ResultPath", body)
        self.assertIn("exit 1", body)

    def test_the_install_validates_before_it_stops_the_running_watchdog(self):
        """Stopping first meant a validation failure took a working watchdog down with
        it, leaving the machine with nothing until the next sign-in. Neither check needs
        the old watchdog stopped. The capture that follows genuinely does, because a
        running watchdog re-asserts the associations the capture is trying to read."""
        payload = self.payload()
        stop = payload.find("$clearedTheWay = Stop-ExistingWatchdog")
        self.assertNotEqual(-1, stop)
        for check in ("requires Windows build 20348 or newer",
                      "No active displays were found."):
            with self.subTest(check=check):
                at = payload.find(check)
                self.assertNotEqual(-1, at, check)
                self.assertLess(at, stop, f"{check!r} must be checked before stopping the watchdog")
        # And the capture must still come after it.
        self.assertLess(stop, payload.find("$item = Get-SavedProfileState -Display $display"))

    def test_the_periodic_pass_does_not_rewrite_a_correct_association(self):
        """The five-second pass used to pass -Force, which rewrites both associations
        whether or not anything drifted. Measured with the writes redirected to a trace
        file: two every 5.3 seconds, forever, about seventeen thousand a day, each one
        asking Windows to re-apply a profile that was already in place.

        No visible symptom is claimed. This was chased as the cause of a flickering
        screen and is not: with the watchdog running and stopped, the association value
        and the GPU gamma ramp were both unchanged across 221 samples at 10 Hz, and the
        flicker turned out to track a GPU-composited terminal under HDR. What is left
        is a real waste on a hot path, which is reason enough.

        -Force is still correct on the mode-change path, where the association can be
        right while the applied state is not, so the assertion counts rather than bans.
        """
        forced = [
            line.strip() for line in self.payload().splitlines()
            if "Restore-SavedProfiles" in line and "-Force" in line
        ]
        self.assertEqual(
            1, len(forced),
            "only the mode-change reassertion may write unconditionally; "
            f"found {len(forced)}: {forced}",
        )
        self.assertIn("$refreshed[0]", forced[0])

    def test_the_gamma_state_is_read_only_when_it_changes(self):
        """Also not the leak fix, and also worth keeping: the file changes when the
        user switches the correction, not 75 times a minute, so parsing it on every
        pass is work with nothing to show for it."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("GammaCacheStamp", script)
        self.assertIn("GetLastWriteTimeUtc($GammaStatePath)", script)

    def test_watchdog_registers_its_task_for_the_current_sid(self):
        """Registering by username breaks on renamed or non-ASCII accounts."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("Principal.UserId = $currentSid", script)
        self.assertIn("logonTrigger.UserId = $currentSid", script)
        self.assertIn("HKCU Run fallback", script)

    def test_a_watchdog_that_dies_between_sign_ins_is_revived(self):
        """With only a logon trigger, anything that ended the supervisor -- a crash, a
        security product, Task Manager -- left the display unprotected until the next
        sign-in, silently.

        The repetition is only affordable alongside starting through the task: with the
        supervisor running as the task's own instance, MultipleInstances = IgnoreNew
        suppresses every repeat while the watchdog is healthy. Started outside the task,
        as it used to be, Task Scheduler would see no instance and launch a spare
        supervisor every five minutes -- about 1,150 a day, each spawning a PowerShell
        that reads exit 4 and stands down, which is the churn the launcher's
        four-consecutive-4s counter exists to stop. The two must not be separated, so
        this test asserts both.
        """
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("$logonTrigger.Repetition.Interval = 'PT5M'", script)
        self.assertIn("$logonTrigger.Repetition.Duration = ''", script)
        self.assertIn("$taskDefinition.Settings.MultipleInstances = 2", script)
        self.assertIn("$registeredTask.Run($null)", script)
        # And the direct launch survives for the paths with no task of ours.
        self.assertIn("if (-not $started) {", script)

    def test_the_install_result_is_written_after_the_watchdog_is_started(self):
        """A task that registers but will not start now is a warning worth showing, and
        the GUI reads warnings only from install_result.json. Written first, that
        warning went nowhere."""
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertLess(
            script.index("$registeredTask.Run($null)"),
            script.index("$result | ConvertTo-Json"),
            "the start must happen before the result is recorded",
        )

    def test_the_task_is_not_given_a_lifetime(self):
        """Unset, ExecutionTimeLimit inherits Task Scheduler's default of PT72H, and the
        registered task on the development machine reads back exactly that with no
        ExecutionTimeLimit element in its XML.

        At the limit Task Scheduler terminates the task's job object, which takes the
        wscript.exe supervisor and its PowerShell child together. With only a logon
        trigger nothing re-fires, so a session signed in for three days -- sleep and
        hibernate included -- silently stops having its profiles protected. Nothing is
        logged, because the watchdog's finally block never runs on an abrupt kill, and
        the GUI cannot see it either: watchdog_is_running opens the singleton mutex the
        PowerShell child creates, so a dead supervisor with a live child still reads as
        healthy.
        """
        script = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("Settings.ExecutionTimeLimit = 'PT0S'", script)


class InstallerOutcomeTests(unittest.TestCase):
    """The scripts have to record what happened; the GUI cannot see their console.

    "Watchdog installed" used to be decided from Watchdog.ps1's mtime, which the .bat
    writes before the integrity check, before -Install, before the display capture and
    before Task Scheduler registration. An -Install that threw afterwards still moved
    that timestamp and still printed a green success line nine seconds later.
    """

    def install(self) -> str:
        return (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")

    def uninstall(self) -> str:
        return (ROOT / "Uninstall-Watchdog.bat").read_text(encoding="utf-8", errors="replace")

    def test_the_installer_writes_a_result_the_gui_reads(self):
        script = self.install()
        self.assertIn("install_result.json", script)
        self.assertIn("$ResultPath", script)
        self.assertIn("startup  = $startupMethod", script)

    def test_the_result_is_cleared_before_the_install_starts(self):
        """A file left by the previous run must not be read as this run's answer."""
        script = self.install()
        clear = script.index("Remove-Item -LiteralPath $ResultPath")
        write = script.index("$result | ConvertTo-Json")
        self.assertLess(clear, write, "the stale result is cleared after being written")

    def test_the_degraded_startup_paths_record_a_warning(self):
        """Both are installs that work but not as intended, and the console they say so
        in is behind a `pause` the user may never read."""
        script = self.install()
        self.assertEqual(
            4, script.count("$script:InstallWarnings +="),
            "each degraded outcome should record its own warning: the Run-key fallback, "
            "the un-replaceable task, a running watchdog that cannot be stopped, and a "
            "task that registered but would not start now",
        )

    def test_the_gui_reads_the_same_file_the_scripts_write(self):
        app = (ROOT / "src/sdr_hdr_profile_creator/app.py").read_text(encoding="utf-8")
        self.assertIn("install_result.json", app, "the GUI must read the recorded result")

    def test_the_uninstaller_can_actually_fail(self):
        """It hardcoded `exit 0` under SilentlyContinue with the task deletion in an
        empty catch{}, so the batch errorlevel test below it was unreachable and it
        printed "removed successfully" whatever happened."""
        script = self.uninstall()
        # Matched line by line: the prose above explaining the old bug quotes it too.
        unconditional = [
            line for line in script.splitlines() if line.strip() == '"exit 0"'
        ]
        self.assertEqual([], unconditional, "the exit code is still hardcoded")
        self.assertIn("if ($keep) { exit 1 } else { exit 0 }", script)

    def test_the_uninstaller_verifies_the_task_is_gone(self):
        """Deleting is not proof: the task belongs to an elevated install and this
        account is refused."""
        script = self.uninstall()
        self.assertIn("GetTask($task)", script)

    def test_the_uninstaller_keeps_its_files_when_the_task_survives(self):
        """Deleting Launcher.vbs while the task survives leaves the task launching a
        script that no longer exists, silently, at every sign-in."""
        script = self.uninstall()
        self.assertIn("$keep = $problems.Count -gt 0", script)
        self.assertIn("if (-not $keep) { Remove-Item -LiteralPath $app -Recurse -Force }", script)

    def test_the_summary_does_not_call_a_managed_hdr_profile_untouched(self):
        """The owner's screenshot: "HDR / EXTENDED : <none - left untouched>" printed at
        the same moment the app's own bar read "Active HDR profile: ..._On.icm". The
        empty case is the one the watchdog rewrites with -Force every five seconds."""
        script = self.install()
        self.assertIn("<managed by the Gamma OFF/ON pair below>", script)
        self.assertNotIn(
            "'    HDR / EXTENDED : {0}' -f $(if ($item.ExtendedProfile) { $item.ExtendedProfile } else { '<none - left untouched>' })",
            script,
        )


class WatchdogIdentityTests(unittest.TestCase):
    """Which saved entry belongs to which monitor, and how much the log says.

    GdiName is a slot, not an identity: \\\\.\\DISPLAY1 is reassigned between sessions,
    so after a hotplug a saved entry could be applied to a different panel than the one
    it was captured from. The two obvious alternatives are both wrong -- the adapter
    LUID is reissued every reboot, and SourceId is the field GdiName derives from -- so
    the fix is the monitor's own EDID-derived device path, which is exactly what the
    GUI anchors on and was simply never available to the watchdog.
    """

    def install(self) -> str:
        return (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")

    def test_the_watchdog_can_read_a_monitors_device_path(self):
        script = self.install()
        self.assertIn("DISPLAYCONFIG_TARGET_DEVICE_NAME", script)
        self.assertIn("DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2", script)
        self.assertIn("info.DevicePath = GetDevicePath(path.targetInfo)", script)

    def test_the_capture_records_it(self):
        self.assertIn("DevicePath      = $Display.DevicePath", self.install())

    def test_every_saved_lookup_goes_through_one_place(self):
        """Six separate `Where-Object { $_.GdiName -eq ... }` lookups is six chances to
        fix five of them."""
        script = self.install()
        self.assertIn("function Find-SavedDisplay", script)
        self.assertEqual(
            0, script.count("$state.Displays | Where-Object { $_.GdiName -eq $current.GdiName }"),
            "a State.json lookup still matches on the slot rather than the monitor",
        )

    def test_a_path_on_both_sides_that_disagrees_is_not_a_miss(self):
        """Falling through to GdiName when both sides name a monitor and the names
        differ is precisely the confusion this exists to prevent."""
        self.assertIn("if ($anyPaths) { return $null }", self.install())

    def test_state_written_before_the_field_existed_still_matches(self):
        """Every existing install has no DevicePath in State.json; refusing those would
        make the watchdog forget every display it was already protecting.

        An entry with no path is skipped rather than counted as one, so it never trips
        the "both sides name a monitor" test above and the GdiName fallback still runs.
        """
        script = self.install()
        self.assertIn("$entryPath = [string]$entry.DevicePath", script)
        self.assertIn("if (-not $entryPath) { continue }", script)
        self.assertIn(
            "if ($entry.GdiName -eq $CurrentDisplay.GdiName) { return $entry }", script
        )

    def test_the_installer_notices_when_it_cannot_replace_the_running_watchdog(self):
        """Both reaper filters require a readable CommandLine, and Windows returns null
        for a process at a higher integrity level -- so a watchdog started by an
        elevated run is invisible to them and cannot be terminated either. It keeps the
        singleton, every replacement exits as surplus, and the install used to report
        success while the old build carried on. Observed for a whole evening.
        """
        script = self.install()
        self.assertIn("function Test-WatchdogSingletonHeld", script)
        self.assertIn("$clearedTheWay = Stop-ExistingWatchdog", script)
        self.assertIn("ok       = [bool]$clearedTheWay", script,
                      "a blocked replacement is still being reported as a clean install")

    def test_the_liveness_check_is_the_singleton_not_a_process_list(self):
        """A process list cannot see what it is not allowed to read; the mutex can."""
        script = self.install()
        self.assertIn("OpenExisting('Local\ColorProfileModeWatchdogStandalone')", script)

    def test_a_surplus_launcher_waits_before_standing_down(self):
        """Standing down on the first surplus exit loses the install-time handover: the
        installer stops the old watchdog and starts a new supervisor, and if the old
        process has not released the singleton yet the replacement quits for good."""
        script = self.install()
        self.assertIn("surplus = surplus + 1", script)
        self.assertIn("If surplus >= 4 Then", script)
        self.assertIn("    surplus = 0", script, "the counter must reset on any other exit")

    def test_the_log_records_corrections_rather_than_every_reassertion(self):
        """614 of 618 lines were identical "Restored..." entries: 37 B/s, the 512 KB cap
        reached in about four hours, one .old kept, so eight hours of history at most --
        for a log whose only job is answering when something took the profile."""
        script = self.install()
        self.assertIn("Corrected STANDARD profile on", script)
        self.assertIn("Corrected EXTENDED profile on", script)
        self.assertEqual(
            0, script.count("Write-Log ('Restored STANDARD profile on"),
            "the forced re-assert is still logged as if it were a correction",
        )

    def test_the_correction_names_what_windows_had(self):
        """What replaced the profile is the interesting half; a log saying only what was
        put back cannot answer why."""
        self.assertIn("was {1}, restored {2}", self.install())

    def test_a_quiet_log_is_distinguishable_from_a_dead_one(self):
        """Silence otherwise means either "nothing drifted" or "this stopped running an
        hour ago", which are the two answers most worth telling apart."""
        script = self.install()
        self.assertIn("Watchdog alive; no profile drift since the last entry.", script)
        self.assertIn("TotalMinutes -ge 60", script)


@unittest.skipUnless(sys.platform == "win32", "cscript.exe is a Windows host")
class LauncherBehaviourTests(unittest.TestCase):
    """Run the generated Launcher.vbs for real, rather than reading it.

    Everything else in this file asserts against the text of a script, which is worth
    something but cannot tell whether the thing works. This one writes the launcher the
    installer would write, points it at a stub that exits with a chosen code, and
    watches what it does -- which is the whole of the supervisor's job: deciding
    whether to restart the watchdog or stand down.
    """

    #: Long enough for a second attempt (the loop sleeps 5s), short enough to live in
    #: a suite.
    LOOP_BUDGET = 7.0

    #: A surplus supervisor now takes four consecutive refusals before quitting, so it
    #: needs roughly four sleeps to get there. Deliberately not shortened by making the
    #: production wait smaller: the wait is what survives the installer's handover.
    SURPLUS_BUDGET = 30.0

    def launcher_body(self) -> str:
        raw = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8", errors="replace")
        found = re.search(r'\$vbs = @"\r?\n(.*?)\r?\n"@', raw, re.S)
        self.assertIsNotNone(found, "the Launcher.vbs here-string is gone")
        return found.group(1)

    def outcome_for(self, exit_code: int, budget: float | None = None) -> str:
        """"stood-down" or "still-looping" for a watchdog that exits with exit_code."""
        import subprocess
        import tempfile

        workspace = Path(tempfile.mkdtemp(prefix="vhdr-launcher-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        stub = workspace / "stub.cmd"
        stub.write_text(f"@echo off\r\nexit /b {exit_code}\r\n", encoding="ascii")

        replacement = f'shell.Run("""{stub}""", 0, True)'
        body = re.sub(
            r'shell\.Run\("powershell\.exe[^\n]*?, 0, True\)',
            lambda _match: replacement,   # callable: the path's backslashes are literal
            self.launcher_body(),
        )
        self.assertIn("stub.cmd", body, "failed to point the launcher at the stub")

        launcher = workspace / "Launcher.vbs"
        launcher.write_text(body, encoding="ascii")
        try:
            subprocess.run(
                ["cscript.exe", "//B", "//Nologo", str(launcher)],
                timeout=self.LOOP_BUDGET if budget is None else budget,
                capture_output=True,
            )
            return "stood-down"
        except subprocess.TimeoutExpired:
            return "still-looping"

    def test_a_surplus_launcher_exits_rather_than_respawning_forever(self):
        """Exit 4 is "another instance owns the singleton". Restarting that every five
        seconds is what burned a PowerShell process per five seconds, indefinitely --
        but it waits out four of them first, so an install-time handover is not
        mistaken for being permanently surplus."""
        self.assertEqual("stood-down", self.outcome_for(4, budget=self.SURPLUS_BUDGET))

    def test_a_launcher_keeps_restarting_when_the_guard_asks_for_a_fresh_instance(self):
        """Exit 9 is the startup guard deliberately standing aside so a healthy
        instance can take over. Standing down on it would leave nothing running --
        the opposite failure, and a worse one."""
        self.assertEqual("still-looping", self.outcome_for(9))


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

    def test_every_source_of_a_figure_is_still_caveated(self):
        """Broadening the claims must not quietly drop the caveats that matter.

        There are three sources now, not one, and each is weak in its own way:
        EDID describes the model rather than the unit, the patterns are judged by
        eye, and a colorimeter without a spectral correction matched to the panel
        can be hundreds of kelvin out on a quantum-dot display."""
        text = self.text()
        self.assertIn("made by eye", text)
        self.assertIn("not metrology", text)
        self.assertIn("spectral correction", text)

    def test_the_gamut_is_not_claimed_as_measured(self):
        """The colorimeter path cannot characterise a gamut: the patches are
        presented in scRGB, so they report the encoding rather than the panel.
        Claiming otherwise sent a wrong gamut into a profile once already."""
        text = self.text()
        self.assertNotIn("The real primaries, replacing the ones DXGI reports", text)
        self.assertNotIn("primaries through DXGI", text)


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


if __name__ == "__main__":
    unittest.main()
