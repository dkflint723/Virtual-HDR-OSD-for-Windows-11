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

        self.associated: list[str] = []

        def fake_install(path: Path, display, mode, make_default=True):
            shutil.copyfile(path, self.color_dir / path.name)
            self.installed.append(path.name)
            self.associated.append(path.name)
            return path.name

        def fake_associate(profile_name, display, mode):
            self.associated.append(profile_name)

        self.hdr_switch_calls: list[tuple[str, bool]] = []

        def fake_set_hdr(display, enabled):
            self.hdr_switch_calls.append((display.key, enabled))

        def fake_send_toggle():
            self.hdr_switch_calls.append(("win+alt+b", True))

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
            "associate_profile": fake_associate,
            # Both of these change the real display; never let them through.
            "set_hdr_enabled": fake_set_hdr,
            "send_hdr_toggle_shortcut": fake_send_toggle,
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

    def read_runtime(self) -> dict:
        return json.loads(app_module.GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))


class FixtureSafetyTests(WindowTestCase):
    """Every call that can change the machine's colour configuration must be faked.

    A new Windows-mutating helper was once added to app.py without being added
    here, so the suite and the UI harness called the real mscms API against a
    fabricated display. It failed on the bogus adapter LUID rather than doing
    damage, but on a machine where that LUID resolved it would have altered real
    profile associations.
    """

    MUTATING = (
        "install_and_associate_profile",
        "associate_profile",
        "reapply_existing_default_profile",
        "remove_profile",
        "set_hdr_enabled",
        "send_hdr_toggle_shortcut",
    )

    def test_every_mutating_windows_call_is_faked(self):
        import inspect

        from sdr_hdr_profile_creator import windows_api

        for name in self.MUTATING:
            with self.subTest(call=name):
                self.assertTrue(hasattr(windows_api, name), f"{name} no longer exists")
                patched = getattr(app_module, name)
                real = getattr(windows_api, name)
                self.assertIsNot(
                    patched, real,
                    f"{name} is not faked; the test would call the real Windows API",
                )
                self.assertNotEqual(
                    inspect.getmodule(patched), windows_api,
                    f"{name} still resolves into windows_api",
                )

    def test_the_mutating_list_still_covers_windows_api(self):
        """Catches a newly added mutator that nobody remembered to fake."""
        from sdr_hdr_profile_creator import windows_api

        # Reads may reach the real machine; only writes must be faked.
        read_only = {"list_installed_profiles"}
        suspicious = {
            name for name in dir(windows_api)
            if not name.startswith("_")
            and callable(getattr(windows_api, name))
            and any(word in name for word in ("install", "associate", "remove", "set_", "send_"))
        }
        unlisted = suspicious - set(self.MUTATING) - read_only
        self.assertFalse(
            unlisted,
            f"windows_api gained mutating call(s) {sorted(unlisted)} that are not faked in tests",
        )


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

    def test_every_documented_action_has_a_reachable_button(self):
        """A method with no button is dead to the user even though tests call it.

        Rebuilding the top bar once dropped Revert to Base and Reset All Sliders
        while every test still passed, because the tests invoked the methods
        directly. Assert on the actual widget tree instead.
        """
        from PySide6.QtWidgets import QAbstractButton

        labels = {
            b.text().rstrip(" •") for b in self.window.findChildren(QAbstractButton) if b.text()
        }
        for expected in (
            "Refresh", "Display Settings", "Profile Folder",
            "Import…", "Export Copy…", "Revert to Base", "Reset All Sliders",
            "Reapply", "Apply Edits",
            "Getting Started", "Watchdog Settings…", "Help",
        ):
            with self.subTest(button=expected):
                self.assertIn(expected, labels, f"no button labelled {expected!r} in the window")

    def test_the_per_display_switches_and_pickers_exist(self):
        for name in ("hdr_switch", "sdr_profile_combo", "hdr_profile_combo",
                     "live_checkbox", "automatic_mode_checkbox", "display_combo"):
            with self.subTest(widget=name):
                self.assertTrue(hasattr(self.window, name))

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

    def test_slider_edits_are_persisted_without_an_apply(self):
        """A kill between editing and applying used to lose the change."""
        self.window.control_widgets["contrast"].set_value(7.5, emit=True)
        self.assertTrue(self.window.state_save_timer.isActive())
        self.window.state_save_timer.stop()
        self.window._save_state_now()
        saved = json.loads(app_module.STATE_PATH.read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["hdr"]["contrast"], 7.5)

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

    def test_an_unchanged_variant_is_still_re_associated(self):
        """Installing and associating are separate; only the first may be skipped.

        remove_profile drops a profile from the display's association list, and
        setting an unassociated profile as the default does not persist -- the
        read-back succeeds and Windows reverts moments later. Skipping the
        association on a cache hit made Reapply work while changing the
        correction silently did not.
        """
        self.apply()
        self.installed.clear()
        self.associated.clear()
        self.apply()
        self.assertEqual(self.installed, [], "unchanged content should not reinstall")
        pair = {p.name for p in self.window._working_profile_paths(self.display)}
        self.assertEqual(
            set(self.associated), pair,
            "both variants must be re-associated even when nothing was reinstalled",
        )

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

    def make_profile(self, name: str) -> Path:
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.profile_name = name
        path = self.temp / f"{name}.icm"
        path.write_bytes(build_profile("HDR", state, build_transform(state, hdr=True)))
        return path

    def test_apply_keeps_the_base_the_user_imported(self):
        """The imported file is the ICC tag template; Apply must not swap it out.

        Windows' own HDR default is still the factory profile at this point, and
        adopting it would build the profile from the wrong colorimetry while the
        path field kept showing the imported name.
        """
        vendor = self.make_profile("MyVendorProfile")
        self.window._load_profile_from_path(vendor)
        self.assertEqual(Path(self.window.state.hdr.base_profile).name, "MyVendorProfile.icm")

        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.apply()
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name,
            "MyVendorProfile.icm",
            "Apply discarded the imported base profile",
        )
        # A file outside the colour directory is tracked by full path so it stays
        # findable; the picker shows that path, but it must name the same profile.
        self.assertEqual(
            Path(self.window.hdr_profile_combo.currentText()).name,
            Path(self.window.state.hdr.base_profile).name,
            "the HDR picker and the actual base disagree",
        )

    def test_an_hdr_transition_still_adopts_the_windows_default(self):
        """Protecting an import must not freeze out a genuine Windows change."""
        self.window._load_profile_from_path(self.make_profile("Imported"))
        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.window._capture_current_hdr_base(self.display, load_controls=True)
        self.assertEqual(Path(self.window.state.hdr.base_profile).name, "BaseCalibration.icm")

    def test_enabling_live_apply_keeps_the_configured_debounce(self):
        """QTimer.start(int) reassigns the interval; the kick must not do that."""
        configured = self.window.live_timer.interval()
        self.window.live_checkbox.setChecked(True)
        self.assertEqual(
            self.window.live_timer.interval(), configured,
            "enabling Live Apply permanently shortened the debounce",
        )
        self.window.control_widgets["gamma"].set_value(2.3, emit=True)
        self.assertEqual(self.window.live_timer.interval(), configured)

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


class MultiMonitorTests(WindowTestCase):
    """Two HDR displays, each with its own stable working pair."""

    def setUp(self):
        super().setUp()
        self.second = hdr_display(key="ADAPTER1:0:2")
        self.second.friendly_name = "Second Monitor"
        self.displays = [self.display, self.second]
        patcher = mock.patch.object(app_module, "enumerate_displays", lambda: list(self.displays))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Repopulate the combo: the window was built while only one display existed.
        self.window._refresh_displays()
        self.assertEqual(self.window.display_combo.count(), 2)

    def select(self, display):
        """Switch the target the way the user does — through the combo.

        _selected_display() resolves via display_combo.currentData(), so setting
        only the snapshot leaves the apply pipeline pointed at the old display.
        """
        for index in range(self.window.display_combo.count()):
            if self.window.display_combo.itemData(index).key == display.key:
                self.window.display_combo.setCurrentIndex(index)
                break
        else:
            self.fail(f"display {display.key} is not in the combo")
        self.assertEqual(self.window._selected_display().key, display.key)

    def test_each_display_gets_a_distinct_working_pair(self):
        first = {p.name for p in self.window._working_profile_paths(self.display)}
        second = {p.name for p in self.window._working_profile_paths(self.second)}
        self.assertEqual(len(first), 2)
        self.assertFalse(first & second, "two displays share working-profile filenames")

    def test_applying_to_one_display_keeps_the_others_profiles_installed(self):
        """Calibrating monitor B must not uninstall monitor A's calibration."""
        self.select(self.display)
        self.apply("apply to A")
        first_pair = [p.name for p in self.window._working_profile_paths(self.display)]
        for name in first_pair:
            self.assertTrue((self.color_dir / name).is_file(), f"{name} was not installed")

        self.removed.clear()
        self.select(self.second)
        self.apply("apply to B")

        for name in first_pair:
            with self.subTest(profile=name):
                self.assertNotIn(name, self.removed, "the other display's profile was uninstalled")
                self.assertTrue(
                    (self.color_dir / name).is_file(),
                    "applying to a second display removed the first display's working profile",
                )

    def test_legacy_timestamped_profiles_are_still_removed(self):
        """Protecting attached displays must not neuter legacy cleanup."""
        legacy = "Virtual_HDR_OSD_20240101_120000_HDR.icm"
        (self.color_dir / legacy).write_bytes(b"")
        self.window._persisted_live_registry["old"] = {
            "profile_name": legacy,
            "profile_path": str(self.color_dir / legacy),
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertIn(legacy, self.removed, "a legacy timestamped profile was not cleaned up")

    def test_stale_pairs_from_a_previous_adapter_luid_are_removed(self):
        """Adapter LUIDs are reissued across reboots, changing the filename token.

        Those orphaned pairs look exactly like current ones, so they must be
        identified by not belonging to any attached display, not by their shape.
        """
        stale = "Virtual_HDR_OSD_deadbeef01_Off.icm"
        (self.color_dir / stale).write_bytes(b"")
        self.window._persisted_live_registry["previous-boot"] = {
            "profile_name": stale,
            "profile_path": str(self.color_dir / stale),
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertIn(
            stale, self.removed,
            "a working pair orphaned by an adapter-LUID change was left installed forever",
        )

    def test_cleanup_protects_the_current_display_even_if_enumeration_fails(self):
        own = [p.name for p in self.window._working_profile_paths(self.display)]
        self.window._persisted_live_registry["mine"] = {
            "profile_name": own[0], "profile_path": "",
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        with mock.patch.object(app_module, "enumerate_displays", side_effect=RuntimeError("boom")):
            self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertNotIn(own[0], self.removed)

    def test_returning_to_a_display_does_not_rebuild(self):
        self.select(self.display)
        self.apply("apply to A")
        self.select(self.second)
        self.apply("apply to B")
        self.installed.clear()
        self.select(self.display)
        self.apply("back to A")
        self.assertEqual(self.installed, [], "returning to a display reinstalled unchanged profiles")


class ProfileBindingTests(WindowTestCase):
    """Pinning the SDR and HDR profiles per display, persisted across restarts."""

    def setUp(self):
        super().setUp()
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        # Real profiles: selecting one loads it, and a zero-byte placeholder would
        # raise into a modal error dialog that blocks the test run.
        for name in ("Calman_SDR_Calibrated.icm", "HDR Calibrated Profile.icc", "sRGB.icm"):
            state = ModeState.neutral("HDR")
            (self.color_dir / name).write_bytes(
                build_profile("HDR", state, build_transform(state, hdr=True))
            )
        # Any unexpected modal would hang the suite rather than fail it.
        blocker = mock.patch.object(QMessageBox, "critical")
        blocker.start()
        self.addCleanup(blocker.stop)
        patcher = mock.patch.object(
            app_module, "list_installed_profiles",
            lambda: sorted(p.name for p in self.color_dir.iterdir() if p.suffix.lower() in (".icc", ".icm")),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window._populate_profile_pickers()

    def test_pickers_offer_installed_profiles_and_exclude_our_own(self):
        self.apply()  # installs the app's working pair into the colour dir
        self.window._populate_profile_pickers()
        listed = [self.window.hdr_profile_combo.itemText(i)
                  for i in range(self.window.hdr_profile_combo.count())]
        self.assertIn("HDR Calibrated Profile.icc", listed)
        self.assertFalse(
            [n for n in listed if n.startswith("Virtual_HDR_OSD_")],
            "the app's own working profiles must not be offered as an editable base",
        )

    def test_sdr_defaults_to_auto_and_never_applies_on_selection(self):
        self.assertEqual(self.window.sdr_profile_combo.currentText(), app_module.SDR_AUTO)
        self.associations.clear()
        self.window.sdr_profile_combo.setCurrentText("Calman_SDR_Calibrated.icm")
        self.assertEqual(
            self.associations, [],
            "choosing an SDR profile must not immediately re-associate anything",
        )

    def test_pinned_sdr_profile_is_restored_on_an_hdr_to_sdr_switch(self):
        self.window.sdr_profile_combo.setCurrentText("Calman_SDR_Calibrated.icm")
        self.default_profiles["SDR"] = "somethingelse.icm"
        self.associations.clear()
        self.window._restore_remembered_sdr_profile(self.display, "test")
        self.assertIn("Calman_SDR_Calibrated.icm", self.associations)

    def test_unmanaged_sdr_is_never_touched(self):
        """Calman and friends own the SDR association; we must not fight them."""
        self.window.sdr_profile_combo.setCurrentText(app_module.SDR_UNMANAGED)
        self.default_profiles["SDR"] = "somethingelse.icm"
        self.associations.clear()
        self.window._restore_remembered_sdr_profile(self.display, "test")
        self.assertEqual(self.associations, [], "unmanaged SDR was re-associated anyway")
        self.assertIn("unmanaged", self.window.status_label.text())

    def test_restore_is_a_no_op_when_windows_already_has_the_pinned_profile(self):
        """A redundant association write is what breaks third-party loaders."""
        self.window.sdr_profile_combo.setCurrentText("Calman_SDR_Calibrated.icm")
        self.default_profiles["SDR"] = "Calman_SDR_Calibrated.icm"
        self.associations.clear()
        self.window._restore_remembered_sdr_profile(self.display, "test")
        self.assertEqual(self.associations, [])

    def test_choosing_an_hdr_profile_loads_it_as_the_base_immediately(self):
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.contrast = 4.0
        (self.color_dir / "HDR Calibrated Profile.icc").write_bytes(
            build_profile("HDR", state, build_transform(state, hdr=True))
        )
        self.window._populate_profile_pickers()
        self.window.hdr_profile_combo.setCurrentText("HDR Calibrated Profile.icc")
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "HDR Calibrated Profile.icc"
        )
        self.assertAlmostEqual(self.window.state.hdr.contrast, 4.0)

    def test_a_pinned_hdr_profile_survives_apply(self):
        self.window.hdr_profile_combo.setCurrentText("HDR Calibrated Profile.icc")
        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.apply()
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "HDR Calibrated Profile.icc"
        )

    def test_bindings_are_keyed_on_a_reboot_stable_identity(self):
        """Adapter LUIDs are reissued on reboot; bindings must not be lost."""
        self.display.device_path = r"\\?\DISPLAY#AUS32F2#5&2564#{guid}"
        self.window._populate_profile_pickers()
        self.window.sdr_profile_combo.setCurrentText("Calman_SDR_Calibrated.icm")

        stored = self.window.state.display_bindings
        self.assertIn(self.display.device_path, stored)
        self.assertNotIn(self.display.key, stored)

    def test_bindings_round_trip_through_the_saved_state_file(self):
        from sdr_hdr_profile_creator.model import ApplicationState

        self.window.sdr_profile_combo.setCurrentText("Calman_SDR_Calibrated.icm")
        self.window._save_state_now()
        reloaded = ApplicationState.from_dict(
            json.loads(app_module.STATE_PATH.read_text(encoding="utf-8"))
        )
        key = self.display.stable_key
        self.assertEqual(reloaded.display_bindings[key].sdr_profile, "Calman_SDR_Calibrated.icm")


class HdrSwitchTests(WindowTestCase):
    def test_switch_reflects_the_current_display_mode(self):
        self.assertTrue(self.window.hdr_switch.isChecked())

    def test_turning_hdr_on_targets_the_selected_display(self):
        calls = []
        self.display.advanced_color_kind = "SDR"
        self.display.advanced_color_enabled = False
        self.window._sync_display_widgets(self.display)
        with mock.patch.object(app_module, "set_hdr_enabled", lambda d, e: calls.append((d.key, e))):
            self.window.hdr_switch.setChecked(True)
        self.assertEqual(calls, [(self.display.key, True)])

    def test_a_failed_switch_reports_and_reverts_the_toggle(self):
        self.display.advanced_color_kind = "SDR"
        self.display.advanced_color_enabled = False
        self.window._sync_display_widgets(self.display)
        with mock.patch.object(app_module, "set_hdr_enabled", side_effect=RuntimeError("denied")):
            self.window.hdr_switch.setChecked(True)
        self.assertIn("Could not turn HDR on", self.window.status_label.text())
        self.assertFalse(self.window.hdr_switch.isChecked())

    def test_switch_is_disabled_for_a_display_without_hdr_support(self):
        self.display.advanced_color_supported = False
        self.window._sync_display_widgets(self.display)
        self.assertFalse(self.window.hdr_switch.isEnabled())


class WatchdogLaunchTests(WindowTestCase):
    """The installer runs detached, so its outcome has to be verified separately."""

    def setUp(self):
        super().setUp()
        self.install_root = self.temp / "ColorProfileModeWatchdog"
        self.install_root.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(app_module, "WATCHDOG_INSTALL_ROOT", self.install_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.script = self.install_root / "Watchdog.ps1"

    def test_install_that_updates_the_script_is_reported_as_success(self):
        before = self.window._watchdog_script_stamp()
        self.script.write_text("installed", encoding="utf-8")
        self.window._report_watchdog_outcome(True, before)
        self.assertIn("Watchdog installed", self.window.status_label.text())
        self.assertNotIn("Error", self.window.status_label.text())

    def test_install_that_silently_does_nothing_is_reported_as_failure(self):
        """The original `start`-based launch made this case invisible."""
        self.script.write_text("stale", encoding="utf-8")
        before = self.window._watchdog_script_stamp()
        self.window._report_watchdog_outcome(True, before)
        self.assertIn("did not complete", self.window.status_label.text())
        self.assertIn("Error", self.window.status_label.text())

    def test_uninstall_outcome_is_reported_both_ways(self):
        self.script.write_text("still here", encoding="utf-8")
        self.window._report_watchdog_outcome(False, 0.0)
        self.assertIn("did not complete", self.window.status_label.text())

        self.script.unlink()
        self.window._report_watchdog_outcome(False, 0.0)
        self.assertIn("Watchdog uninstalled", self.window.status_label.text())

    def test_missing_script_stamps_as_zero_rather_than_raising(self):
        self.assertEqual(self.window._watchdog_script_stamp(), 0.0)

    def test_launch_is_not_detached_through_start(self):
        """`start` returns success even when the batch file never runs."""
        calls = []
        with mock.patch.object(app_module.subprocess, "Popen", lambda *a, **k: calls.append(a[0])),              mock.patch.object(app_module.QTimer, "singleShot"):
            self.window._run_watchdog_script("Uninstall-Watchdog.bat")
        self.assertTrue(calls, "the installer was never launched")
        self.assertNotIn("start", calls[0], "launching via `start` discards the exit code")


class RebootStabilityTests(WindowTestCase):
    """Adapter LUIDs are reissued on reboot; nothing user-visible may depend on them."""

    def setUp(self):
        super().setUp()
        self.display.device_path = r"\?\DISPLAY#AUS32F2#5&2564&0&UID4357#{guid}"

    def reboot(self):
        """Same monitor, new adapter LUID -- exactly what a reboot does."""
        self.display.key = self.display.key.replace("BBBB", "CCCC")
        self.display.adapter_low += 1

    def test_working_profile_names_survive_a_reboot(self):
        before = [p.name for p in self.window._working_profile_paths(self.display)]
        self.reboot()
        after = [p.name for p in self.window._working_profile_paths(self.display)]
        self.assertEqual(
            before, after,
            "the working-profile filename changed across a reboot, orphaning the old pair",
        )

    def test_two_monitors_still_get_distinct_names(self):
        other = hdr_display(key="ADAPTER1:0:2")
        other.device_path = r"\?\DISPLAY#DEL1234#5&9999&0&UID4358#{guid}"
        self.assertNotEqual(
            {p.name for p in self.window._working_profile_paths(self.display)},
            {p.name for p in self.window._working_profile_paths(other)},
        )

    def test_a_display_with_no_device_path_still_gets_a_name(self):
        self.display.device_path = ""
        names = [p.name for p in self.window._working_profile_paths(self.display)]
        self.assertTrue(all(n.startswith("Virtual_HDR_OSD_") for n in names))
        self.assertEqual(len(set(names)), 2)

    def test_cleanup_reclaims_both_halves_of_an_orphaned_pair(self):
        """The registry records only the active variant; the sibling is derivable.

        A real machine leaked Virtual_HDR_OSD_79255fb06f_Off.icm exactly this way:
        its _On sibling was named in the registry and removed, while the _Off half
        was named nowhere and survived forever.
        """
        for name in ("Virtual_HDR_OSD_deadbeef01_Off.icm", "Virtual_HDR_OSD_deadbeef01_On.icm"):
            (self.color_dir / name).write_bytes(b"")
        self.window._persisted_live_registry["previous-boot"] = {
            "profile_name": "Virtual_HDR_OSD_deadbeef01_On.icm",  # active variant only
            "profile_path": "",
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertIn("Virtual_HDR_OSD_deadbeef01_On.icm", self.removed)
        self.assertIn(
            "Virtual_HDR_OSD_deadbeef01_Off.icm", self.removed,
            "the sibling of an orphaned pair was left installed forever",
        )

    def test_cleanup_spares_a_configured_display_that_is_not_attached(self):
        """A monitor that is switched off is not enumerated; it is not abandoned."""
        absent = r"\?\DISPLAY#DEL9999#OFF#{guid}"
        self.window.state.binding(absent).hdr_profile = "SomeBase.icm"
        pair = [p.name for p in self.window._working_profile_paths_for(absent)]
        for name in pair:
            (self.color_dir / name).write_bytes(b"")
        self.window._persisted_live_registry["absent"] = {
            "profile_name": pair[1], "profile_path": "",
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        self.window._cleanup_legacy_managed_profiles(self.display)
        for name in pair:
            with self.subTest(profile=name):
                self.assertNotIn(name, self.removed)

    def test_cleanup_still_reclaims_a_display_with_no_binding(self):
        forgotten = r"\?\DISPLAY#OLD0000#GONE#{guid}"
        pair = [p.name for p in self.window._working_profile_paths_for(forgotten)]
        self.window._persisted_live_registry["forgotten"] = {
            "profile_name": pair[1], "profile_path": "",
        }
        self.window._legacy_cleaned.clear()
        self.removed.clear()
        self.window._cleanup_legacy_managed_profiles(self.display)
        self.assertIn(pair[1], self.removed)

    def test_runtime_state_drops_records_for_a_previous_luid(self):
        """The watchdog looks entries up by gdi_name, so duplicates are rivals."""
        self.apply()
        first_key = self.display.key
        self.reboot()
        self.apply()

        entries = self.read_runtime()["displays"]
        self.assertIn(self.display.key, entries)
        self.assertNotIn(
            first_key, entries,
            "a stale record for the same monitor survived and would rival the current one",
        )
        same_gdi = [k for k, v in entries.items() if v.get("gdi_name") == self.display.gdi_name]
        self.assertEqual(len(same_gdi), 1, f"{len(same_gdi)} records share one gdi_name")

    def test_pruning_leaves_other_monitors_alone(self):
        self.apply()
        other = hdr_display(key="ADAPTER9:0:9")
        other.friendly_name = "Other"
        other.gdi_name = r"\.\DISPLAY2"
        other.device_path = r"\?\DISPLAY#DEL1234#5&1&0&UID9#{guid}"
        payload, displays_state, record = self.window._runtime_entry(other)
        displays_state[other.key] = {"gdi_name": other.gdi_name, "profiles": {}}
        app_module.MainWindow._write_json_atomic(app_module.GAMMA_HOTKEY_STATE_PATH, payload)

        self.apply()
        entries = self.read_runtime()["displays"]
        self.assertIn(other.key, entries, "another monitor's record was pruned")


class RuntimeStateTests(WindowTestCase):
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

    def test_updated_at_is_timezone_aware(self):
        """The watchdog compares this against its own stamp to decide who acted last.

        A naive local time is ambiguous across the DST fall-back hour, which is
        exactly when a wrong comparison would revert the user's setting.
        """
        from datetime import datetime

        self.apply()
        entry = self.read_runtime()["displays"][self.display.key]
        stamp = datetime.fromisoformat(entry["updated_at"])
        self.assertIsNotNone(stamp.tzinfo, f"updated_at {entry['updated_at']!r} has no offset")

        self.window._publish_gamma_runtime_intent(self.display)
        entry = self.read_runtime()["displays"][self.display.key]
        self.assertIsNotNone(datetime.fromisoformat(entry["updated_at"]).tzinfo)

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
