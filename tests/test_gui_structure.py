from pathlib import Path
import unittest


class GuiStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.app = (cls.root / "src/sdr_hdr_profile_creator/app.py").read_text(encoding="utf-8")
        cls.controls = (cls.root / "src/sdr_hdr_profile_creator/controls.py").read_text(encoding="utf-8")
        cls.curves = (cls.root / "src/sdr_hdr_profile_creator/curves.py").read_text(encoding="utf-8")
        cls.win = (cls.root / "src/sdr_hdr_profile_creator/windows_api.py").read_text(encoding="utf-8")

    def test_final_branding(self):
        self.assertIn("Virtual HDR OSD for Windows", self.app)

    def test_sdr_editor_remains_disabled_but_watchdog_restores_existing_profile(self):
        self.assertIn('if mode != "HDR"', self.app)
        self.assertNotIn('_apply_mode_profile("SDR"', self.app)
        self.assertIn("_remember_current_sdr_profile", self.app)
        self.assertIn("_restore_remembered_sdr_profile", self.app)
        self.assertIn("reapply_existing_default_profile", self.app)
        self.assertIn("ColorProfileSetDisplayDefaultAssociation", self.win)

    def test_piecewise_fix_is_removed(self):
        self.assertNotIn("Windows piecewise sRGB fix", self.app)
        self.assertNotIn("gamma_fix_checkbox", self.app)
        self.assertNotIn("_gamma_fix_toggled", self.app)
        self.assertNotIn("_srgb_piecewise_to_gamma", self.curves)

    def test_generous_slider_ranges_keep_fine_steps_and_no_arrow_buttons(self):
        self.assertIn('"gamma", "Gamma / Midtone Response", 1.600, 3.000, 2.200, 0.005', self.app)
        self.assertIn('"brightness_trim", "Midtone Brightness", -30.0, 30.0, 0.0, 0.05', self.app)
        self.assertIn('"contrast", "Contrast / Tonal Separation", -30.0, 30.0, 0.0, 0.05', self.app)
        self.assertIn('"temperature", "White Balance Temperature", -3000.0, 3000.0, 0.0, 5.0', self.app)
        self.assertIn('"tint", "Green–Magenta Tint", -25.0, 25.0, 0.0, 0.05', self.app)
        self.assertIn('"saturation", "Color Saturation", -50.0, 50.0, 0.0, 0.10', self.app)
        self.assertIn('"red_channel", "Red Fine Balance", -25.0, 25.0, 0.0, 0.05', self.app)
        self.assertIn("self.value_edit", self.controls)
        self.assertNotIn("self.decrement_button", self.controls)
        self.assertNotIn("self.increment_button", self.controls)
        self.assertNotIn("DoubleSpinBox", self.controls)

    def test_mouse_wheel_uses_declared_fine_step(self):
        self.assertIn("self.slider.installEventFilter(self)", self.controls)
        self.assertIn("self.value_edit.installEventFilter(self)", self.controls)
        self.assertIn("event.type() == QEvent.Type.Wheel", self.controls)
        self.assertIn("direction * self.spec.step", self.controls)

    def test_color_algorithm_uses_bradford_and_rec2020_matrix(self):
        self.assertIn("_BRADFORD", self.curves)
        self.assertIn("_white_balance_matrix", self.curves)
        self.assertIn("_REC2020_TO_XYZ", self.curves)
        self.assertIn("luminance", self.curves)

    def test_import_opens_windows_color_directory(self):
        self.assertIn("get_color_directory", self.app)
        self.assertIn("self._windows_color_directory_or_home()", self.app)

    def test_windows_settings_buttons(self):
        self.assertNotIn("Windows HDR Settings", self.app)
        self.assertIn("Windows Display Settings", self.app)
        self.assertIn("Windows Color Profile Folder", self.app)
        self.assertIn('os.startfile("ms-settings:display")', self.win)

    def test_metadata_tab_remains_removed(self):
        self.assertNotIn('"profilePage", "Profile"', self.app)
        self.assertNotIn("_build_profile_details_tab", self.app)
        self.assertNotIn("HDR Profile Metadata", self.app)

    def test_sdr_brightness_ui_is_removed(self):
        self.assertNotIn("SDR Content Brightness in HDR", self.app)
        self.assertNotIn("sdr_brightness_control", self.app)
        self.assertNotIn("_sync_sdr_brightness_from_windows", self.app)
        self.assertNotIn("sdr_white_label", self.app)

    def test_clear_labels_and_tooltips(self):
        self.assertIn("Target Display", self.app)
        self.assertIn("HDR Profile Application", self.app)
        self.assertIn("HDR Calibration Profile", self.app)
        self.assertIn("Tone & Brightness", self.app)
        self.assertIn("Color & White Balance", self.app)
        self.assertIn("setToolTip", self.app)
        self.assertIn("setToolTip", self.controls)

    def test_runtime_controls_are_unified_and_unambiguous(self):
        self.assertIn('PushButton("Reapply Profile"', self.app)
        self.assertIn('setOffText("Live Apply")', self.app)
        self.assertIn('setOffText("Automatic Mode Switching")', self.app)
        self.assertIn('def _automatic_mode_switching_toggled', self.app)
        self.assertNotIn('Mode Tracking: Manual', self.app)
        self.assertNotIn('Mode Tracking: Automatic', self.app)
        self.assertNotIn('Mode Watchdog: Off', self.app)
        self.assertNotIn('Mode Watchdog: On', self.app)
        self.assertNotIn('self.follow_checkbox', self.app)
        self.assertNotIn('self.auto_refresh_checkbox', self.app)

    def test_dedicated_watchdog_uninstaller_is_packaged(self):
        uninstaller_path = self.root / "Uninstall-Watchdog.bat"
        self.assertTrue(uninstaller_path.is_file())
        uninstaller = uninstaller_path.read_text(encoding="utf-8")
        installer = (self.root / "Install-Watchdog.bat").read_text(encoding="utf-8")
        self.assertIn('Virtual HDR OSD Watchdog.lnk', uninstaller)
        self.assertIn('sdr_hdr_profile_creator\\.watchdog', uninstaller)
        self.assertIn('Stop-Process', uninstaller)
        self.assertIn('Uninstall-Watchdog.bat', installer)

    def test_persistent_watchdog_is_packaged_and_conservative(self):
        watchdog = (self.root / "src/sdr_hdr_profile_creator/watchdog.py").read_text(encoding="utf-8")
        installer = (self.root / "Install-Watchdog.bat").read_text(encoding="utf-8")
        self.assertIn('get_default_profile(display, "SDR")', watchdog)
        self.assertIn('reapply_existing_default_profile(display, "SDR", profile)', watchdog)
        self.assertNotIn('build_profile', watchdog)
        self.assertIn('pythonw.exe', installer)
        self.assertIn('Virtual HDR OSD Watchdog.lnk', installer)
        self.assertIn('uninstall', installer.lower())


if __name__ == "__main__":
    unittest.main()
