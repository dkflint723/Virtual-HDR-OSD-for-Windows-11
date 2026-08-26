from pathlib import Path
import unittest


class GuiStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.app = (cls.root / "src/sdr_hdr_profile_creator/app.py").read_text(encoding="utf-8")
        cls.controls = (cls.root / "src/sdr_hdr_profile_creator/controls.py").read_text(encoding="utf-8")
        cls.curves = (cls.root / "src/sdr_hdr_profile_creator/curves.py").read_text(encoding="utf-8")
        cls.gamma = (cls.root / "src/sdr_hdr_profile_creator/gamma_correction.py").read_text(encoding="utf-8")
        cls.win = (cls.root / "src/sdr_hdr_profile_creator/windows_api.py").read_text(encoding="utf-8")

    def test_clean_branding(self):
        self.assertIn("Virtual HDR OSD for Windows", self.app)
        self.assertNotIn("Virtual HDR OSD for Windows 1.", self.app)

    def test_sdr_editor_remains_passive(self):
        self.assertIn('if mode != "HDR"', self.app)
        self.assertNotIn('_apply_mode_profile("SDR"', self.app)
        self.assertIn("_remember_current_sdr_profile", self.app)
        self.assertIn("_restore_remembered_sdr_profile", self.app)

    def test_correct_piecewise_gamma_integration(self):
        self.assertIn("SDR-in-HDR Gamma Correction", self.app)
        self.assertIn("Auto (Recommended)", self.gamma)
        self.assertIn("100 nits / Brightness 5", self.gamma)
        self.assertIn("200 nits / Brightness 30", self.gamma)
        self.assertIn("300 nits / Brightness 55", self.gamma)
        self.assertIn("400 nits / Brightness 80", self.gamma)
        self.assertIn('"Unspecified"', self.gamma)
        self.assertIn('"SDR"', self.gamma)
        self.assertIn("pq_eotf", self.gamma)
        self.assertIn("srgb_inverse_eotf", self.gamma)
        self.assertIn("srgb_signal**2.2", self.gamma)
        self.assertIn("if luminance > white", self.gamma)

    def test_auto_reads_windows_white_without_sdr_brightness_slider(self):
        self.assertIn("get_sdr_white_level_nits", self.app)
        self.assertNotIn("SDR Content Brightness in HDR", self.app)
        self.assertNotIn("sdr_brightness_control", self.app)

    def test_global_gamma_hotkeys_are_exposed(self):
        hotkeys = (self.root / "src/sdr_hdr_profile_creator/hotkeys.py").read_text(encoding="utf-8")
        self.assertIn("RegisterHotKey", hotkeys)
        self.assertIn("VK_1", hotkeys)
        self.assertIn("VK_2", hotkeys)
        self.assertIn("GetMessageW", hotkeys)
        self.assertIn("MOD_NOREPEAT", hotkeys)
        self.assertIn("Alt+1", self.app)
        self.assertIn("Alt+2", self.app)

    def test_watchdog_settings_dialog(self):
        self.assertIn('PushButton("Watchdog Settings…"', self.app)
        self.assertIn("def _show_watchdog_settings", self.app)
        self.assertIn("Install Watchdog", self.app)
        self.assertIn("Uninstall Watchdog", self.app)

    def test_watchdog_is_packaged_for_source_and_onefile(self):
        resources = self.root / "src/sdr_hdr_profile_creator/resources"
        self.assertTrue((resources / "2- OPTIONAL - Install-Watchdog.bat").is_file())
        self.assertTrue((resources / "Uninstall-Watchdog.bat").is_file())
        installer = (self.root / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8")
        self.assertIn("GetSdrWhiteLevelNits", installer)
        self.assertIn("$entry.profiles.On", installer)
        self.assertIn("Alt+1 OFF, Alt+2 ON", installer)
        self.assertIn("RegisterHotKey", installer)
        self.assertIn("GetMessage", installer)
        self.assertIn("WorkingOff", installer)
        self.assertIn("WorkingOn", installer)
        self.assertIn("gamma_hotkeys.json", installer)
        self.assertIn("Get-DesiredExtendedProfile", installer)
        self.assertIn("active_profile", installer)
        self.assertIn("GammaEnabled", installer)
        self.assertIn("WorkingOff", installer)
        self.assertIn("WorkingOn", installer)
        self.assertIn("TryRegisterGammaHotkeys", installer)
        self.assertIn("PollGammaHotkey", installer)
        self.assertNotIn("GetAsyncKeyState", installer)
        self.assertIn("_publish_gamma_runtime_intent", self.app)
        self.assertIn("_update_gamma_runtime_state", self.app)
        self.assertNotIn("pythonw.exe", installer.lower())


    def test_watchdog_scheduler_uses_sid_com_registration_without_legacy_fallback(self):
        installer = (self.root / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8")
        embedded = (self.root / "src/sdr_hdr_profile_creator/resources/2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8")
        standalone = (self.root / "watchdogs standalone/Install-Watchdog.bat").read_text(encoding="utf-8")
        for payload in (installer, embedded, standalone):
            self.assertIn("Schedule.Service", payload)
            self.assertIn("RegisterTaskDefinition", payload)
            self.assertIn("currentSid", payload)
            self.assertIn("Principal.UserId = $currentSid", payload)
            self.assertIn("logonTrigger.UserId = $currentSid", payload)
            self.assertIn("LogonType = 3", payload)
            self.assertNotIn("HKCU Run fallback", payload)
            self.assertNotIn("Falling back to the current-user Run key", payload)
            self.assertIn("Task Scheduler registration failed; the watchdog was not installed", payload)
            self.assertNotIn("Register-ScheduledTask", payload)
            self.assertNotIn("New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME", payload)

    def test_gamma_off_is_immediate_and_authoritative(self):
        self.assertIn('self._apply_mode_profile("HDR", "Gamma correction changed")', self.app)
        self.assertIn('self.state.hdr.sdr_gamma_correction = "Off"', self.app)
        self.assertIn('self._publish_gamma_runtime_intent(display)', self.app)
        self.assertIn('if option == "Off":\n            return None', self.app)

    def test_builder_outputs_only_onefile_exe(self):
        builder = (self.root / "3- (Advanced users & developers) - Build Portable EXE.bat").read_text(encoding="utf-8")
        self.assertIn("--onefile", builder)
        self.assertIn('--windows-console-mode=disable', builder)
        self.assertIn('Virtual HDR OSD for Windows.exe', builder)
        self.assertNotIn("--standalone", builder)
        self.assertNotIn("file-version", builder.lower())
        self.assertNotIn("product-version", builder.lower())

    def test_generous_slider_ranges_keep_fine_steps_and_mouse_wheel(self):
        self.assertIn('"gamma", "Gamma / Midtone Response", 1.600, 3.000, 2.200, 0.005', self.app)
        self.assertIn('"brightness_trim", "Midtone Brightness", -30.0, 30.0, 0.0, 0.05', self.app)
        self.assertIn('"contrast", "Contrast / Tonal Separation", -30.0, 30.0, 0.0, 0.05', self.app)
        self.assertIn('"temperature", "White Balance Temperature", -3000.0, 3000.0, 0.0, 5.0', self.app)
        self.assertIn('"tint", "Green–Magenta Tint", -25.0, 25.0, 0.0, 0.05', self.app)
        self.assertIn('"saturation", "Color Saturation", -50.0, 50.0, 0.0, 0.10', self.app)
        self.assertIn("QEvent.Type.Wheel", self.controls)
        self.assertNotIn("DoubleSpinBox", self.controls)

    def test_import_opens_windows_color_directory(self):
        self.assertIn("get_color_directory", self.app)
        self.assertIn("self._windows_color_directory_or_home()", self.app)

    def test_metadata_tab_remains_removed(self):
        self.assertNotIn('"profilePage", "Profile"', self.app)
        self.assertNotIn("_build_profile_details_tab", self.app)


    def test_watchdog_resolves_stable_pair_and_formats_negative_hresult(self):
        installer = (self.root / "2- OPTIONAL - Install-Watchdog.bat").read_text(encoding="utf-8")
        self.assertIn("Resolve-StableWorkingPair", installer)
        self.assertIn("Test-InstalledColorProfile", installer)
        self.assertIn("GetWindowsColorDirectory", installer)
        self.assertIn("Virtual_HDR_OSD_[A-Fa-f0-9]+", installer)
        self.assertIn("Format-HResult", installer)
        self.assertNotIn("[uint32]$hr", installer)
        self.assertIn("working pair is incomplete", installer)

if __name__ == "__main__":
    unittest.main()
