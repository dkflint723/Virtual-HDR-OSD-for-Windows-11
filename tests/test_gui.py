"""Behavioural tests for the editor window and its profile-application pipeline.

Every Windows colour API is replaced with a recording fake backed by a temporary
directory, so these exercise real control flow without touching the machine's
actual colour configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from sdr_hdr_profile_creator import app as app_module
    from sdr_hdr_profile_creator.controls import ControlSpec, SliderControl
    from sdr_hdr_profile_creator.dialogs import GUIDE_STEPS, HELP_SECTIONS
    from sdr_hdr_profile_creator.gamma_correction import CORRECTION_OPTIONS
    from sdr_hdr_profile_creator.windows_api import DisplayInfo

    GUI_AVAILABLE = True
    GUI_IMPORT_ERROR = ""
except ImportError as exc:  # qfluentwidgets is a project dependency; skip if absent
    GUI_AVAILABLE = False
    GUI_IMPORT_ERROR = str(exc)


def hdr_display(key: str = "AAAA:BBBB:0:1") -> "DisplayInfo":
    return DisplayInfo(
        key=key,
        friendly_name="Test Monitor",
        gdi_name=r"\\.\DISPLAY1",
        device_path="",
        adapter_low=1,
        adapter_high=0,
        source_id=0,
        target_id=1,
        advanced_color_supported=True,
        advanced_color_enabled=True,
        bits_per_color_channel=10,
        advanced_color_kind="HDR",
    )


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class WindowTestCase(unittest.TestCase):
    """Builds a MainWindow whose every side effect lands in a temp directory."""

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="vhdrosd-test-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.color_dir = self.temp / "color"
        self.live_root = self.temp / "live"
        for directory in (self.color_dir, self.live_root):
            directory.mkdir(parents=True, exist_ok=True)

        self.display = hdr_display()
        # What Windows "currently has" associated, and what got installed.
        self.default_profiles = {"HDR": "BaseCalibration.icm", "SDR": "sRGB.icm"}
        self.installed: list[str] = []
        self.removed: list[str] = []
        self.associations: list[str] = []
        (self.color_dir / "BaseCalibration.icm").write_bytes(b"")

        def fake_install(path: Path, display, mode, make_default=True):
            shutil.copyfile(path, self.color_dir / path.name)
            self.installed.append(path.name)
            return path.name

        def fake_reapply(display, mode, profile_name):
            self.associations.append(profile_name)
            self.default_profiles[mode] = profile_name
            return profile_name

        def fake_remove(profile_name, display, mode):
            self.removed.append(profile_name)
            (self.color_dir / profile_name).unlink(missing_ok=True)
            return True, "uninstalled"

        patches = {
            "LOCAL_ROOT": self.temp,
            "STATE_PATH": self.temp / "last_gui_state.json",
            "LIVE_ROOT": self.live_root,
            "LIVE_REGISTRY_PATH": self.temp / "live_registry.json",
            "GAMMA_HOTKEY_STATE_PATH": self.temp / "gamma_hotkeys.json",
            "GAMMA_PROFILE_ROOT": self.temp / "gamma_profiles",
            "enumerate_displays": lambda: [self.display],
            "get_color_directory": lambda: self.color_dir,
            "get_default_profile": lambda display, mode: self.default_profiles[mode],
            "get_sdr_white_level_nits": lambda display: 240.0,
            "install_and_associate_profile": fake_install,
            "reapply_existing_default_profile": fake_reapply,
            "remove_profile": fake_remove,
            # Registering real global hotkeys from a test would be antisocial.
            "GammaHotkeyListener": mock.MagicMock(),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.window = app_module.MainWindow()
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        # Stop the polling timers so nothing fires mid-assertion.
        self.window.mode_timer.stop()
        self.window.gamma_runtime_timer.stop()
        self.window.live_timer.stop()

    def apply(self, reason="Apply Edits", **kwargs):
        self.window._apply_mode_profile(reason, **kwargs)


class EditorStructureTests(WindowTestCase):
    def test_both_editor_pages_are_present(self):
        self.assertEqual(
            sorted(self.window._editor_pages),
            ["colorPage", "tonePage"],
        )

    def test_every_documented_control_is_wired_to_state(self):
        expected = {
            "gamma", "brightness_trim", "contrast",
            "temperature", "tint", "saturation",
            "red_channel", "green_channel", "blue_channel",
        }
        self.assertEqual(set(self.window.control_widgets), expected)
        for key in expected:
            with self.subTest(control=key):
                self.assertTrue(hasattr(self.window.state.hdr, key))

    def test_control_ranges_match_the_documented_calibration_envelope(self):
        expected = {
            # key: (minimum, maximum, default, step)
            "gamma": (1.600, 3.000, 2.200, 0.005),
            "brightness_trim": (-30.0, 30.0, 0.0, 0.05),
            "contrast": (-30.0, 30.0, 0.0, 0.05),
            "temperature": (-3000.0, 3000.0, 0.0, 5.0),
            "tint": (-25.0, 25.0, 0.0, 0.05),
            "saturation": (-50.0, 50.0, 0.0, 0.10),
            "red_channel": (-25.0, 25.0, 0.0, 0.05),
            "green_channel": (-25.0, 25.0, 0.0, 0.05),
            "blue_channel": (-25.0, 25.0, 0.0, 0.05),
        }
        for key, (low, high, default, step) in expected.items():
            spec = self.window.control_widgets[key].spec
            with self.subTest(control=key):
                self.assertAlmostEqual(spec.minimum, low)
                self.assertAlmostEqual(spec.maximum, high)
                self.assertAlmostEqual(spec.default, default)
                self.assertAlmostEqual(spec.step, step)

    def test_gamma_correction_dropdown_offers_exactly_the_supported_options(self):
        combo = self.window.gamma_correction_combo
        listed = tuple(combo.itemText(index) for index in range(combo.count()))
        self.assertEqual(listed, CORRECTION_OPTIONS)

    def test_help_and_guide_content_exists(self):
        self.assertGreaterEqual(len(HELP_SECTIONS), 5)
        self.assertGreaterEqual(len(GUIDE_STEPS), 5)
        for step in GUIDE_STEPS:
            with self.subTest(step=step.title):
                # A step offering a button must name one, and vice versa.
                self.assertEqual(bool(step.action_key), bool(step.action_text))

    def test_guide_actions_and_checks_all_resolve(self):
        actions, checks = {}, {}
        with mock.patch.object(app_module.GuideDialog, "exec", return_value=0) as fake_exec:
            def capture(a, c, parent=None):
                actions.update(a)
                checks.update(c)
                return mock.MagicMock(exec=fake_exec)

            with mock.patch.object(app_module, "GuideDialog", side_effect=capture):
                self.window._show_guide()
        for step in GUIDE_STEPS:
            if step.action_key:
                with self.subTest(action=step.action_key):
                    self.assertIn(step.action_key, actions)
            if step.check_key:
                with self.subTest(check=step.check_key):
                    self.assertIn(step.check_key, checks)
                    satisfied, detail = checks[step.check_key]()
                    self.assertIsInstance(satisfied, bool)
                    self.assertTrue(detail)


class EditStateTests(WindowTestCase):
    def test_moving_a_control_updates_state_and_marks_edits_unapplied(self):
        self.apply()
        self.assertEqual(self.window.dirty_label.text(), "No unapplied edits")

        self.window.control_widgets["gamma"].set_value(2.45, emit=True)
        self.assertAlmostEqual(self.window.state.hdr.gamma, 2.45)
        self.assertEqual(self.window.dirty_label.text(), "Unapplied edits")

        self.apply()
        self.assertEqual(self.window.dirty_label.text(), "No unapplied edits")

    def test_edit_signature_tracks_the_gamma_correction_choice(self):
        before = self.window._edit_signature()
        self.window.state.hdr.sdr_gamma_correction = "200 nits / Brightness 30"
        self.assertNotEqual(before, self.window._edit_signature())

    def test_reset_all_returns_every_control_to_its_default(self):
        for key, control in self.window.control_widgets.items():
            control.set_value(control.spec.default + control.spec.step * 4, emit=True)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window._reset_all_controls()
        for key, control in self.window.control_widgets.items():
            with self.subTest(control=key):
                self.assertAlmostEqual(control.value(), control.spec.default, places=6)
                self.assertAlmostEqual(float(getattr(self.window.state.hdr, key)), control.spec.default, places=6)

    def test_reset_all_is_cancellable(self):
        self.window.control_widgets["contrast"].set_value(5.0, emit=True)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.window._reset_all_controls()
        self.assertAlmostEqual(self.window.state.hdr.contrast, 5.0)

    def test_revert_to_base_reloads_the_imported_file_and_drops_edits(self):
        base = self.temp / "base.icm"
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        pristine = ModeState.neutral("HDR")
        base.write_bytes(build_profile("HDR", pristine, build_transform(pristine, hdr=True)))
        self.window.state.hdr.base_profile = str(base)

        self.window.control_widgets["saturation"].set_value(12.0, emit=True)
        self.assertAlmostEqual(self.window.state.hdr.saturation, 12.0)

        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window._revert_to_base()
        self.assertAlmostEqual(self.window.state.hdr.saturation, 0.0)

    def test_revert_without_a_base_reports_instead_of_raising(self):
        self.window.state.hdr.base_profile = ""
        self.window.state.hdr.imported_profile = ""
        self.window._revert_to_base()
        self.assertIn("No base profile", self.window.status_label.text())


class ApplyPipelineTests(WindowTestCase):
    def test_apply_installs_both_variants_and_activates_the_selected_one(self):
        self.apply()
        self.assertEqual(len(self.installed), 2, "expected an Off and an On working profile")
        self.assertTrue(all(name.startswith("Virtual_HDR_OSD_") for name in self.installed))
        # Correction defaults to Off, so the Off variant must be the active one.
        self.assertTrue(self.associations[-1].endswith("_Off.icm"))

    def test_reapplying_unchanged_settings_reinstalls_nothing(self):
        self.apply()
        self.installed.clear()
        self.removed.clear()
        self.apply()
        self.assertEqual(self.installed, [], "unchanged settings must not be reinstalled")
        self.assertEqual(self.removed, [], "unchanged settings must not be uninstalled")
        # The association is still refreshed, which is the cheap part.
        self.assertTrue(self.associations[-1].endswith("_Off.icm"))

    def test_the_cache_is_not_defeated_by_the_clock(self):
        """A profile built later carries a later timestamp but the same calibration.

        Without a timestamp-insensitive comparison this reinstalls roughly once
        per second of wall clock, which is exactly the thrash the cache exists
        to prevent.
        """
        import datetime as real_datetime

        from sdr_hdr_profile_creator import icc

        self.apply()
        self.installed.clear()
        self.removed.clear()

        later = real_datetime.datetime.now(real_datetime.timezone.utc) + real_datetime.timedelta(days=3)

        class LaterDateTime(real_datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return later

        with mock.patch.object(icc.dt, "datetime", LaterDateTime):
            self.apply()
        self.assertEqual(self.installed, [], "a later build time counted as a settings change")
        self.assertEqual(self.removed, [])

    def test_force_reinstalls_even_when_nothing_changed(self):
        self.apply()
        self.installed.clear()
        self.apply(reason="Reapply", force=True)
        self.assertEqual(len(self.installed), 2)

    def test_changing_a_slider_reinstalls_both_variants(self):
        self.apply()
        self.installed.clear()
        self.window.control_widgets["contrast"].set_value(6.0, emit=True)
        self.apply()
        self.assertEqual(len(self.installed), 2)

    def test_toggling_the_correction_only_switches_the_association(self):
        """The headline fast path: Alt+1 / Alt+2 must not rebuild anything."""
        self.apply()
        self.installed.clear()
        self.removed.clear()

        self.window._gamma_hotkey_enable()
        self.assertEqual(self.installed, [], "enabling the correction rebuilt a profile")
        self.assertTrue(self.associations[-1].endswith("_On.icm"))

        self.window._gamma_hotkey_disable()
        self.assertEqual(self.installed, [], "disabling the correction rebuilt a profile")
        self.assertTrue(self.associations[-1].endswith("_Off.icm"))

    def test_selecting_a_different_correction_preset_rebuilds_only_the_on_variant(self):
        self.window._gamma_hotkey_enable()
        self.installed.clear()
        self.window._select_gamma_correction("300 nits / Brightness 55", "test")
        self.assertEqual(len(self.installed), 1)
        self.assertTrue(self.installed[0].endswith("_On.icm"))

    def test_the_two_variants_differ_only_in_the_correction(self):
        payloads, on_option = self.window._build_working_payloads(self.display)
        self.assertEqual(set(payloads), {"Off", "On"})
        self.assertNotEqual(payloads["Off"][1], payloads["On"][1])
        self.assertNotEqual(on_option, "Off")

        # With the correction disabled the On variant would be identical to Off,
        # which is exactly why "Off" is never used as the On option.
        self.window._last_enabled_gamma_correction = "Off"
        _, fallback_option = self.window._build_working_payloads(self.display)
        self.assertEqual(fallback_option, "Auto (Recommended)")

    def test_apply_is_refused_when_windows_is_not_in_hdr(self):
        self.display = hdr_display()
        self.display.advanced_color_kind = "SDR"
        self.display.advanced_color_enabled = False
        self.window._current_display_snapshot = self.display
        self.installed.clear()
        self.apply()
        self.assertEqual(self.installed, [])
        self.assertIn("not in HDR mode", self.window.status_label.text())

    def test_a_managed_profile_is_never_adopted_as_the_base(self):
        """Otherwise each HDR transition would edit an already-edited profile."""
        self.apply()
        base_before = self.window.state.hdr.base_profile
        self.default_profiles["HDR"] = "Virtual_HDR_OSD_abc123_Off.icm"
        self.window._capture_current_hdr_base(self.display)
        self.assertEqual(self.window.state.hdr.base_profile, base_before)

    def test_cleanup_never_removes_the_pair_it_is_about_to_activate(self):
        self.apply()
        off_path, on_path = self.window._working_profile_paths(self.display)
        self.window._persisted_live_registry["stale"] = {
            "profile_name": off_path.name,
            "profile_path": str(off_path),
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertNotIn(off_path.name, self.removed)
        self.assertNotIn(on_path.name, self.removed)

    def test_cleanup_runs_once_per_display(self):
        self.window._legacy_cleaned.clear()
        self.window._persisted_live_registry["stale"] = {
            "profile_name": "Virtual_HDR_OSD_deadbeef_Off.icm",
            "profile_path": "",
        }
        self.window._cleanup_legacy_managed_profiles(self.display)
        first = list(self.removed)
        self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertEqual(self.removed, first, "cleanup repeated for the same display")

    def test_failed_live_update_disables_live_apply(self):
        """A repeating failure must not fire every 420 ms forever."""
        self.window.state.live_mode = True
        with mock.patch.object(
            app_module, "reapply_existing_default_profile", side_effect=RuntimeError("nope")
        ):
            self.window._apply_mode_profile("Live update")
        self.assertFalse(self.window.state.live_mode)
        self.assertIn("Live Apply was switched off", self.window.status_label.text())


class ActivityReportingTests(WindowTestCase):
    def test_a_fresh_session_does_not_claim_unapplied_edits(self):
        """Nothing applied yet is not the same as edited-since-applying."""
        self.assertIsNone(self.window._applied_signature)
        self.assertEqual(self.window.dirty_label.text(), "Not applied this session")

    def test_active_profile_description_follows_the_installed_filename(self):
        """The label must never say OFF while the _On variant is associated."""
        self.window._active_profile_name = "Virtual_HDR_OSD_abc1234567_On.icm"
        self.window.state.hdr.sdr_gamma_correction = "Off"
        self.assertIn("gamma correction ON", self.window._describe_active_profile())

        self.window._active_profile_name = "Virtual_HDR_OSD_abc1234567_Off.icm"
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        self.assertIn("gamma correction OFF", self.window._describe_active_profile())

    def test_a_foreign_profile_is_labelled_as_not_ours(self):
        self.window._active_profile_name = "BaseCalibration.icm"
        self.assertIn("not generated by this app", self.window._describe_active_profile())
        self.window._active_profile_name = ""
        self.assertIn("none set by this app", self.window._describe_active_profile())

    def test_hotkey_registration_result_reaches_the_status_label(self):
        self.window._hotkey_registration_changed(True, "active")
        self.assertIn("active", self.window.hotkey_status_label.text())
        self.window._hotkey_registration_changed(False, "the watchdog owns them")
        self.assertIn("not owned", self.window.hotkey_status_label.text())
        self.assertIn("the watchdog owns them", self.window.status_label.text())

    def test_hotkey_listener_is_told_where_to_report_before_it_starts(self):
        """Connecting after construction would race the worker thread."""
        listener = app_module.GammaHotkeyListener
        _positional, keyword = listener.call_args
        self.assertEqual(len(_positional) + len(keyword), 3, "on_registration must be passed in")


class DisplayLabelTests(unittest.TestCase):
    @unittest.skipUnless(GUI_AVAILABLE, GUI_IMPORT_ERROR)
    def test_label_states_capability_and_current_mode_once_each(self):
        display = hdr_display()
        self.assertIn("HDR on", display.label)
        self.assertNotIn("HDR  ·  HDR", display.label)

        display.advanced_color_kind = "SDR"
        display.advanced_color_enabled = False
        self.assertIn("HDR off", display.label)

        display.advanced_color_kind = "WCG"
        self.assertIn("ACM/WCG", display.label)

        display.advanced_color_supported = False
        self.assertIn("SDR only", display.label)


class RuntimeStateTests(WindowTestCase):
    def read_runtime(self) -> dict:
        return json.loads(app_module.GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))

    def test_runtime_state_publishes_the_keys_the_watchdog_reads(self):
        self.apply()
        payload = self.read_runtime()
        self.assertEqual(payload["schema"], app_module.GAMMA_RUNTIME_SCHEMA)
        entry = payload["displays"][self.display.key]
        for key in ("profiles", "paths", "enabled", "selected", "active_profile"):
            with self.subTest(key=key):
                self.assertIn(key, entry)
        self.assertEqual(set(entry["profiles"]), {"Off", "On"})
        self.assertEqual(entry["active_profile"], self.associations[-1])

    def test_intent_is_published_before_the_profile_changes(self):
        self.apply()
        self.window._publish_gamma_runtime_intent(self.display)
        entry = self.read_runtime()["displays"][self.display.key]
        # Publishing intent must not wipe the profile pair the watchdog needs.
        self.assertEqual(set(entry["profiles"]), {"Off", "On"})

    def test_runtime_state_survives_a_corrupt_file(self):
        app_module.GAMMA_HOTKEY_STATE_PATH.write_text("{ not json", encoding="utf-8")
        self.apply()
        self.assertIn("displays", self.read_runtime())

    def test_enabling_the_correction_is_reflected_in_the_runtime_state(self):
        self.window._gamma_hotkey_enable()
        entry = self.read_runtime()["displays"][self.display.key]
        self.assertTrue(entry["enabled"])
        self.window._gamma_hotkey_disable()
        entry = self.read_runtime()["displays"][self.display.key]
        self.assertFalse(entry["enabled"])

    def test_external_watchdog_state_is_followed(self):
        self.apply()
        payload = self.read_runtime()
        payload["displays"][self.display.key]["enabled"] = True
        payload["displays"][self.display.key]["selected"] = "400 nits / Brightness 80"
        app_module.GAMMA_HOTKEY_STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
        self.installed.clear()
        self.window._sync_external_gamma_hotkey_state()
        self.assertEqual(self.window.state.hdr.sdr_gamma_correction, "400 nits / Brightness 80")
        # The watchdog owns the association in this mode; the GUI only mirrors it.
        self.assertEqual(self.installed, [])


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class SliderControlTests(unittest.TestCase):
    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def make(self, **overrides) -> SliderControl:
        spec = ControlSpec(
            key="demo", title="Demo", minimum=-10.0, maximum=10.0,
            default=0.0, step=0.05, decimals=2, **overrides,
        )
        control = SliderControl(spec)
        self.addCleanup(control.deleteLater)
        return control

    def test_values_are_clamped_to_the_declared_range(self):
        control = self.make()
        control.set_value(999.0)
        self.assertAlmostEqual(control.value(), 10.0)
        control.set_value(-999.0)
        self.assertAlmostEqual(control.value(), -10.0)

    def test_values_are_quantised_to_the_declared_step(self):
        control = self.make()
        control.set_value(1.234)
        self.assertAlmostEqual(control.value(), 1.25, places=6)

    def test_setting_a_value_without_emit_does_not_signal(self):
        control = self.make()
        seen: list[float] = []
        control.valueChanged.connect(seen.append)
        control.set_value(2.0, emit=False)
        self.assertEqual(seen, [])
        control.set_value(3.0, emit=True)
        self.assertEqual(len(seen), 1)

    def test_slider_and_text_field_stay_in_agreement(self):
        control = self.make()
        control.set_value(-4.35, emit=False)
        self.assertAlmostEqual(control.slider.value() / control._scale, control.value(), places=6)


if __name__ == "__main__":
    unittest.main()
