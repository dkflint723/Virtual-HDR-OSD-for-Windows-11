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
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from sdr_hdr_profile_creator import app as app_module
    from sdr_hdr_profile_creator.controls import ControlSpec, SliderControl
    from sdr_hdr_profile_creator.dialogs import GUIDE_STEPS, HELP_SECTIONS
    from sdr_hdr_profile_creator.gamma_correction import CORRECTION_OPTIONS
    from sdr_hdr_profile_creator.edid import PanelMetadata
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
        # Whether the fixture pretends the standalone watchdog is running.
        self.watchdog_running = False
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
            # Both of these read the real monitor. Building the window called them,
            # so tests inherited whatever panel the developer happened to have --
            # and once the window started recording panel primaries, assertions
            # about "no panel data" passed or failed depending on the hardware.
            # Individual tests still override these where they need real values.
            "capability_for_device_name": lambda _name: None,
            "read_panel_metadata": lambda _path: None,
            # Probing the real watchdog would make the lock switch reflect the
            # developer's machine rather than the fixture.
            "watchdog_is_running": lambda: self.watchdog_running,
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

    def choose_profile(self, combo, text):
        """Simulate picking an item from a profile dropdown.

        setCurrentText on its own is not a user selection. The handlers listen to
        textActivated, which qfluentwidgets emits whenever an item is clicked --
        including when the item clicked is the one already displayed, where
        currentTextChanged stays silent because the index never moves.
        """
        combo.setCurrentText(text)
        combo.textActivated.emit(text)

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

        Rebuilding the top bar once dropped Revert and Reset Sliders
        while every test still passed, because the tests invoked the methods
        directly. Assert on the actual widget tree instead.
        """
        from PySide6.QtWidgets import QAbstractButton

        labels = {
            b.text().rstrip(" •") for b in self.window.findChildren(QAbstractButton) if b.text()
        }
        for expected in (
            "Refresh", "Display Settings", "Profile Folder",
            "Import…", "Export Copy…", "Revert", "Reset Sliders",
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

    @staticmethod
    def third_party_bytes(state=None) -> bytes:
        """A profile that is real but is not one of ours.

        Everything here is built with our own builder, which always embeds the
        private ``sdhs`` state tag. A profile from Calman or Windows HDR
        Calibration has no such tag, and the app now tells the two apart by
        content, so the signature is renamed to a harmless one. Without this
        every fixture profile would look app-generated.
        """
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = state if state is not None else ModeState.neutral("HDR")
        data = bytearray(build_profile("HDR", state, build_transform(state, hdr=True)))
        count = int.from_bytes(data[128:132], "big")
        for index in range(count):
            offset = 132 + index * 12
            if data[offset : offset + 4] == b"sdhs":
                data[offset : offset + 4] = b"targ"
        return bytes(data)

    def setUp(self):
        super().setUp()

        # Real profiles: selecting one loads it, and a zero-byte placeholder would
        # raise into a modal error dialog that blocks the test run.
        for name in ("Calman_SDR_Calibrated.icm", "HDR Calibrated Profile.icc", "sRGB.icm"):
            (self.color_dir / name).write_bytes(self.third_party_bytes())
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
        self.choose_profile(self.window.sdr_profile_combo, "Calman_SDR_Calibrated.icm")
        self.assertEqual(
            self.associations, [],
            "choosing an SDR profile must not immediately re-associate anything",
        )

    def test_pinned_sdr_profile_is_restored_on_an_hdr_to_sdr_switch(self):
        self.choose_profile(self.window.sdr_profile_combo, "Calman_SDR_Calibrated.icm")
        self.default_profiles["SDR"] = "somethingelse.icm"
        self.associations.clear()
        self.window._restore_remembered_sdr_profile(self.display, "test")
        self.assertIn("Calman_SDR_Calibrated.icm", self.associations)

    def test_unmanaged_sdr_is_never_touched(self):
        """Calman and friends own the SDR association; we must not fight them."""
        self.choose_profile(self.window.sdr_profile_combo, app_module.SDR_UNMANAGED)
        self.default_profiles["SDR"] = "somethingelse.icm"
        self.associations.clear()
        self.window._restore_remembered_sdr_profile(self.display, "test")
        self.assertEqual(self.associations, [], "unmanaged SDR was re-associated anyway")
        self.assertIn("unmanaged", self.window.status_label.text())

    def test_restore_is_a_no_op_when_windows_already_has_the_pinned_profile(self):
        """A redundant association write is what breaks third-party loaders."""
        self.choose_profile(self.window.sdr_profile_combo, "Calman_SDR_Calibrated.icm")
        self.default_profiles["SDR"] = "Calman_SDR_Calibrated.icm"
        self.associations.clear()
        self.window._restore_remembered_sdr_profile(self.display, "test")
        self.assertEqual(self.associations, [])

    def test_choosing_an_hdr_profile_loads_it_as_the_base_immediately(self):
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.peak_luminance_nits = 640.0
        (self.color_dir / "HDR Calibrated Profile.icc").write_bytes(
            self.third_party_bytes(state)
        )
        self.window._populate_profile_pickers()
        self.choose_profile(self.window.hdr_profile_combo, "HDR Calibrated Profile.icc")
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "HDR Calibrated Profile.icc"
        )
        # Recovered from the profile's curves, not from an embedded state tag.
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 640.0, places=0)

    def test_importing_a_saved_copy_of_our_own_profile_restores_exact_settings(self):
        """Export Copy then Import is how a tuned result is kept; it must round-trip."""
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.contrast = 4.0
        state.base_profile = str(self.color_dir / "HDR Calibrated Profile.icc")
        saved = self.temp / "my tuned copy.icm"
        saved.write_bytes(build_profile("HDR", state, build_transform(state, hdr=True)))

        self.window._load_profile_from_path(saved)
        self.assertAlmostEqual(self.window.state.hdr.contrast, 4.0)
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "HDR Calibrated Profile.icc",
            "importing a copy must keep building from the calibration it came from",
        )

    def test_our_own_profiles_are_not_offered_as_bases_under_any_name(self):
        """Older releases installed working profiles as <base>_HDR.icm."""
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        (self.color_dir / "HDR Calibrated Profile_HDR.icm").write_bytes(
            build_profile("HDR", state, build_transform(state, hdr=True))
        )
        self.window._populate_profile_pickers()
        listed = [self.window.hdr_profile_combo.itemText(i)
                  for i in range(self.window.hdr_profile_combo.count())]
        self.assertIn("HDR Calibrated Profile.icc", listed)
        self.assertNotIn("HDR Calibrated Profile_HDR.icm", listed)

    def test_a_pin_naming_one_of_our_own_profiles_is_dropped(self):
        """Such a pin outranks the Windows default forever, freezing the base."""
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        (self.color_dir / "Stale_HDR.icm").write_bytes(
            build_profile("HDR", state, build_transform(state, hdr=True))
        )
        binding = self.window.state.binding(self.display.stable_key)
        binding.hdr_profile = "Stale_HDR.icm"

        self.window._populate_profile_pickers()
        self.assertEqual(binding.hdr_profile, "")
        self.assertNotEqual(self.window.hdr_profile_combo.currentText(), "Stale_HDR.icm")

    def test_a_freshly_calibrated_windows_default_becomes_the_base(self):
        """Calman writing a new HDR profile must flow through, not be ignored."""
        (self.color_dir / "PG32UCDM_120_Standard.icm").write_bytes(self.third_party_bytes())
        binding = self.window.state.binding(self.display.stable_key)
        binding.hdr_profile = ""
        self.default_profiles["HDR"] = "PG32UCDM_120_Standard.icm"

        self.window._capture_current_hdr_base(self.display)
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "PG32UCDM_120_Standard.icm"
        )
        self.window._populate_profile_pickers()
        self.assertEqual(
            self.window.hdr_profile_combo.currentText(), "PG32UCDM_120_Standard.icm",
            "the picker must name the profile the sliders are actually editing",
        )

    def test_reloading_the_base_moves_the_pin_with_it(self):
        """The mode-switch path adopts the Windows default over any pin.

        The pin is what the picker displays, so leaving it behind made the box
        name one profile while the sliders edited another.
        """
        self.choose_profile(self.window.hdr_profile_combo, "HDR Calibrated Profile.icc")
        (self.color_dir / "PG32UCDM_120_Standard.icm").write_bytes(self.third_party_bytes())
        self.default_profiles["HDR"] = "PG32UCDM_120_Standard.icm"

        self.window._capture_current_hdr_base(self.display, load_controls=True)
        self.assertEqual(
            self.window.state.binding(self.display.stable_key).hdr_profile,
            "PG32UCDM_120_Standard.icm",
        )
        self.assertEqual(
            self.window.hdr_profile_combo.currentText(), "PG32UCDM_120_Standard.icm"
        )

    def test_a_deliberate_pin_is_kept_but_the_divergence_is_reported(self):
        self.choose_profile(self.window.hdr_profile_combo, "HDR Calibrated Profile.icc")
        (self.color_dir / "PG32UCDM_120_Standard.icm").write_bytes(self.third_party_bytes())
        self.default_profiles["HDR"] = "PG32UCDM_120_Standard.icm"

        self.window._capture_current_hdr_base(self.display)
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "HDR Calibrated Profile.icc",
            "an explicit pin must not be silently overwritten",
        )
        self.assertIn("PG32UCDM_120_Standard.icm", self.window.status_label.text())

    def test_a_pinned_hdr_profile_survives_apply(self):
        self.choose_profile(self.window.hdr_profile_combo, "HDR Calibrated Profile.icc")
        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.apply()
        self.assertEqual(
            Path(self.window.state.hdr.base_profile).name, "HDR Calibrated Profile.icc"
        )

    def test_bindings_are_keyed_on_a_reboot_stable_identity(self):
        """Adapter LUIDs are reissued on reboot; bindings must not be lost."""
        self.display.device_path = r"\\?\DISPLAY#AUS32F2#5&2564#{guid}"
        self.window._populate_profile_pickers()
        self.choose_profile(self.window.sdr_profile_combo, "Calman_SDR_Calibrated.icm")

        stored = self.window.state.display_bindings
        self.assertIn(self.display.device_path, stored)
        self.assertNotIn(self.display.key, stored)

    def test_bindings_round_trip_through_the_saved_state_file(self):
        from sdr_hdr_profile_creator.model import ApplicationState

        self.choose_profile(self.window.sdr_profile_combo, "Calman_SDR_Calibrated.icm")
        self.window._save_state_now()
        reloaded = ApplicationState.from_dict(
            json.loads(app_module.STATE_PATH.read_text(encoding="utf-8"))
        )
        key = self.display.stable_key
        self.assertEqual(reloaded.display_bindings[key].sdr_profile, "Calman_SDR_Calibrated.icm")


class PatternViewWiringTests(WindowTestCase):
    """The fullscreen view has to drive the real sliders, or it is a picture of a tool."""

    def test_bindings_exist_for_the_tonal_controls(self):
        labels = {binding.key for binding in self.window._pattern_view_bindings()}
        self.assertEqual(labels, set(app_module.MainWindow.PATTERN_VIEW_CONTROLS))

    def test_a_binding_reads_the_live_slider_value(self):
        self.window.control_widgets["gamma"].set_value(2.05)
        binding = next(b for b in self.window._pattern_view_bindings() if b.key == "gamma")
        self.assertAlmostEqual(binding.read(), 2.05, places=3)

    def test_nudging_moves_the_real_slider(self):
        binding = next(b for b in self.window._pattern_view_bindings() if b.key == "gamma")
        before = self.window.control_widgets["gamma"].value()
        binding.nudge(binding.step)
        self.assertAlmostEqual(
            self.window.control_widgets["gamma"].value(), before + binding.step, places=4)

    def test_nudging_emits_so_live_apply_still_runs(self):
        """Adjusting from a pattern that did not update the display would be worse than
        having no controls there at all."""
        seen: list[float] = []
        self.window.control_widgets["contrast"].valueChanged.connect(seen.append)
        binding = next(b for b in self.window._pattern_view_bindings() if b.key == "contrast")
        binding.nudge(binding.step)
        self.assertTrue(seen, "the slider changed without emitting")

    def test_the_step_matches_the_control_it_drives(self):
        for binding in self.window._pattern_view_bindings():
            with self.subTest(control=binding.key):
                self.assertAlmostEqual(
                    binding.step, self.window.control_widgets[binding.key].spec.step)

    def test_an_unavailable_hdr_surface_is_reported_not_raised(self):
        class Refusing:
            failure = "no HDR surface on this display"

            def __init__(self, *args, **kwargs):
                pass

            def setGeometry(self, *_a):
                pass

            def showFullScreen(self):
                pass

            def begin(self):
                return False

            def close(self):
                pass

        with mock.patch.object(app_module, "PatternWindow", Refusing):
            self.window._open_pattern_view()
        self.assertIn("no HDR surface on this display", self.window.status_label.text())
        self.assertIsNone(self.window._pattern_window)


class MeasurementRecordingTests(WindowTestCase):
    """A threshold found and then lost is the whole calibration wasted, so every reading
    has to reach the profile."""

    def test_each_pattern_answers_its_own_luminance_figure(self):
        for key, field, value in (
            ("black-level", "minimum_luminance_nits", 0.004),
            ("peak-white", "peak_luminance_nits", 1043.0),
        ):
            with self.subTest(pattern=key):
                self.window._record_measurement(key, value)
                self.assertAlmostEqual(getattr(self.window.state.hdr, field), value)

    def test_a_pattern_that_measures_nothing_changes_nothing(self):
        before = self.window.state.hdr.to_dict()
        self.window._record_measurement("colour-patches", 500.0)
        self.assertEqual(self.window.state.hdr.to_dict(), before)

    def test_readings_are_bounded_like_imported_ones(self):
        self.window._record_measurement("black-level", -5.0)
        self.assertGreaterEqual(self.window.state.hdr.minimum_luminance_nits, 0.0)
        self.window._record_measurement("peak-white", 99999.0)
        self.assertLessEqual(self.window.state.hdr.peak_luminance_nits, 10000.0)

    def test_full_frame_is_no_longer_written_by_a_pattern(self):
        """It found a clipping point, not sustained luminance, and writing one into a
        field meaning the other made a profile claim 1010 nits full-screen on a panel
        declaring 265. The figure comes from the EDID now."""
        before = self.window.state.hdr.full_frame_luminance_nits
        self.window._record_measurement("full-frame-white", 4000.0)
        self.assertAlmostEqual(self.window.state.hdr.full_frame_luminance_nits, before)

    def test_a_measurement_reaches_the_generated_profile(self):
        """The point of measuring: MHC2 carries min and peak, lumi carries full frame."""
        import struct

        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import _read_tags, build_profile

        self.window._record_measurement("black-level", 0.0012)
        self.window._record_measurement("peak-white", 1043.0)

        state = self.window.state.hdr
        data = build_profile("HDR", state, build_transform(state, hdr=True))
        tags = _read_tags(data)
        self.assertAlmostEqual(
            struct.unpack_from(">i", tags[b"MHC2"], 12)[0] / 65536.0, 0.0012, places=4)
        self.assertAlmostEqual(
            struct.unpack_from(">i", tags[b"MHC2"], 16)[0] / 65536.0, 1043.0, places=1)



class PanelGamutWarningTests(WindowTestCase):
    """A monitor's colour-space mode changes in its own OSD, unobservable from the PC."""

    P3 = ((0.6746, 0.3144), (0.2698, 0.6859), (0.1512, 0.0609))
    BT709 = ((0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600))

    def setUp(self):
        super().setUp()
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        base = self.color_dir / "PanelBase.icc"
        base.write_bytes(build_profile("HDR", state, build_transform(state, hdr=True)))
        self.window.state.hdr.base_profile = str(base)
        self.display.advanced_color_kind = "HDR"

    def fake_panel(self, primaries, *, is_hdr=True):
        from sdr_hdr_profile_creator import hdr_display

        capability = hdr_display.DisplayCapability(
            device_name=self.display.gdi_name, left=0, top=0, right=100, bottom=100,
            bits_per_color=10,
            color_space=(hdr_display.DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 if is_hdr
                         else hdr_display.DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709),
            min_nits=0.0, max_nits=1080.0, max_full_frame_nits=1080.0,
            red_primary=primaries[0], green_primary=primaries[1], blue_primary=primaries[2],
            white_point=(0.3127, 0.329),
        )
        return mock.patch.object(app_module, "capability_for_device_name", lambda _name: capability)

    def described_primaries(self):
        from sdr_hdr_profile_creator.icc import profile_primaries_xy

        return profile_primaries_xy(Path(self.window.state.hdr.base_profile).read_bytes())[:3]

    def test_a_changed_panel_gamut_is_reported(self):
        far = tuple((x + 0.08, y - 0.04) for x, y in self.described_primaries())
        with self.fake_panel(far):
            self.window._warn_if_panel_gamut_changed(self.display)
        self.assertIn("colour space", self.window.status_label.text())

    def test_a_matching_panel_says_nothing(self):
        self.window._set_status("baseline", "ok")
        unchanged = self.window.status_label.text()
        with self.fake_panel(self.described_primaries()):
            self.window._warn_if_panel_gamut_changed(self.display)
        self.assertEqual(self.window.status_label.text(), unchanged)

    def test_nothing_is_claimed_while_hdr_is_off(self):
        """DXGI reports the current mode's colour volume, so its primaries mean nothing
        about HDR while the display is in SDR. This is how a correctly set up machine --
        an sRGB-clamped SDR mode on a wide-gamut panel -- would otherwise be warned at."""
        self.display.advanced_color_kind = "SDR"
        self.window._set_status("baseline", "ok")
        unchanged = self.window.status_label.text()
        with self.fake_panel(self.BT709, is_hdr=False):
            self.window._warn_if_panel_gamut_changed(self.display)
        self.assertEqual(self.window.status_label.text(), unchanged)

    def test_a_missing_base_profile_is_not_an_error(self):
        self.window.state.hdr.base_profile = str(self.color_dir / "gone.icc")
        with self.fake_panel(self.BT709):
            self.window._warn_if_panel_gamut_changed(self.display)  # must not raise


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

    def test_the_sdr_choice_is_published_for_the_watchdog(self):
        """The watchdog force-restores SDR every five seconds regardless of drift.

        The GUI refusing to touch an unmanaged SDR association is worth nothing
        unless the watchdog is told to leave it alone too.
        """
        self.apply()
        self.assertFalse(self.read_runtime()["displays"][self.display.key]["sdr_unmanaged"])

        self.choose_profile(self.window.sdr_profile_combo, app_module.SDR_UNMANAGED)
        self.apply()
        self.assertTrue(self.read_runtime()["displays"][self.display.key]["sdr_unmanaged"])

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


class PatternViewLiveApplyTests(WindowTestCase):
    """Live Apply is forced off at startup, so without this the tone steps in the pattern
    view moved sliders that rebuilt nothing and changed nothing on screen."""

    class FakeWindow:
        failure = ""

        def __init__(self, *args, **kwargs):
            PatternViewLiveApplyTests.last = self
            self.on_close = kwargs.get("on_close")

        def setGeometry(self, *_a):
            pass

        def showFullScreen(self):
            pass

        def begin(self):
            return True

        def setFocus(self, *_a):
            pass

        def close(self):
            if self.on_close:
                self.on_close()

    def test_opening_the_patterns_turns_live_apply_on(self):
        self.window.state.live_mode = False
        with mock.patch.object(app_module, "PatternWindow", self.FakeWindow):
            self.window._open_pattern_view()
        self.assertTrue(self.window.state.live_mode)

    def test_closing_puts_it_back_as_the_user_had_it(self):
        self.window.state.live_mode = False
        with mock.patch.object(app_module, "PatternWindow", self.FakeWindow):
            self.window._open_pattern_view()
            self.last.close()
        self.assertFalse(self.window.state.live_mode)

    def test_a_user_who_had_it_on_keeps_it_on(self):
        self.window.state.live_mode = True
        with mock.patch.object(app_module, "PatternWindow", self.FakeWindow):
            self.window._open_pattern_view()
            self.last.close()
        self.assertTrue(self.window.state.live_mode)


class ApplyFromPatternViewTests(WindowTestCase):
    """The measurements only reach the editor state; something has to write them out."""

    def test_it_applies_and_reports_success(self):
        self.window._record_measurement("peak-white", 940.0)
        self.installed.clear()
        self.assertTrue(self.window._apply_from_pattern_view())
        self.assertTrue(self.installed, "nothing was written")

    def test_it_reports_failure_rather_than_raising(self):
        with mock.patch.object(self.window, "_apply_mode_profile", side_effect=RuntimeError("no")):
            self.assertFalse(self.window._apply_from_pattern_view())

    def test_the_measured_values_reach_the_installed_profile(self):
        import struct

        from sdr_hdr_profile_creator.icc import _read_tags

        self.window._record_measurement("black-level", 0.0012)
        self.window._record_measurement("peak-white", 1043.0)
        self.window._apply_from_pattern_view()
        written = sorted(self.color_dir.glob("Virtual_HDR_OSD_*_On.icm"))
        self.assertTrue(written, "no working profile was installed")
        tags = _read_tags(written[0].read_bytes())
        self.assertAlmostEqual(
            struct.unpack_from(">i", tags[b"MHC2"], 16)[0] / 65536.0, 1043.0, places=1)


class PanelLuminanceFallbackTests(WindowTestCase):
    """A profile with no MHC2 says nothing about luminance, so importing one replaced
    measured figures with a neutral state's defaults. On this display that turned a
    measured 1080/1080 into 1000/400 and wrote it into the next generated profile."""

    def capability(self, **overrides):
        from sdr_hdr_profile_creator.hdr_display import (
            DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
            DisplayCapability,
        )

        base = dict(
            device_name=self.display.gdi_name, left=0, top=0, right=3840, bottom=2160,
            bits_per_color=10, color_space=DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
            min_nits=0.0, max_nits=1080.0, max_full_frame_nits=1080.0,
            red_primary=(0.67, 0.31), green_primary=(0.27, 0.69),
            blue_primary=(0.15, 0.06), white_point=(0.313, 0.329),
        )
        base.update(overrides)
        return DisplayCapability(**base)

    def plain_profile(self, name="Calman_Style.icm"):
        """A profile with colorimetry but no MHC2, as a third-party ICC would be."""
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        data = bytearray(build_profile("HDR", state, build_transform(state, hdr=True)))
        count = int.from_bytes(data[128:132], "big")
        for index in range(count):
            offset = 132 + index * 12
            if data[offset:offset + 4] in (b"MHC2", b"sdhs"):
                data[offset:offset + 4] = b"targ"
        path = self.color_dir / name
        path.write_bytes(bytes(data))
        return path

    def test_a_profile_without_mhc2_takes_the_panels_luminance(self):
        # Nothing with MHC2 applied, so what the panel reports is the panel and not this
        # app's own output coming back. See MetadataEchoTests.
        self.plain_profile("Applied.icm")
        self.default_profiles["HDR"] = "Applied.icm"
        path = self.plain_profile()
        with mock.patch.object(app_module, "capability_for_device_name",
                               lambda _n: self.capability()):
            self.window._load_profile_from_path(path)
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 1080.0)
        self.assertAlmostEqual(self.window.state.hdr.full_frame_luminance_nits, 1080.0)

    def test_it_says_where_the_figures_came_from(self):
        """Silently adopting numbers from elsewhere would be worse than the bug."""
        self.plain_profile("Applied.icm")
        self.default_profiles["HDR"] = "Applied.icm"
        path = self.plain_profile()
        with mock.patch.object(app_module, "capability_for_device_name",
                               lambda _n: self.capability()):
            self.window._load_profile_from_path(path)
        self.assertIn("display's own figures", self.window.status_label.text())

    def test_a_profile_carrying_mhc2_is_left_alone(self):
        """Its own measured values outrank anything the panel claims about itself."""
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.peak_luminance_nits = 640.0
        state.full_frame_luminance_nits = 210.0
        path = self.color_dir / "Measured.icm"
        path.write_bytes(build_profile("HDR", state, build_transform(state, hdr=True)))
        with mock.patch.object(app_module, "capability_for_device_name",
                               lambda _n: self.capability()):
            self.window._load_profile_from_path(path)
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 640.0)

    def test_a_panel_reporting_nonsense_is_not_believed(self):
        path = self.plain_profile()
        with mock.patch.object(app_module, "capability_for_device_name",
                               lambda _n: self.capability(max_nits=0.0)):
            self.window._load_profile_from_path(path)
        self.assertNotIn("display's own figures", self.window.status_label.text())

    def test_full_frame_never_exceeds_peak_even_if_the_panel_claims_it(self):
        path = self.plain_profile()
        with mock.patch.object(
            app_module, "capability_for_device_name",
            lambda _n: self.capability(max_nits=600.0, max_full_frame_nits=4000.0),
        ):
            self.window._load_profile_from_path(path)
        self.assertLessEqual(self.window.state.hdr.full_frame_luminance_nits,
                             self.window.state.hdr.peak_luminance_nits)

    def test_no_display_selected_is_not_an_error(self):
        path = self.plain_profile()
        with mock.patch.object(self.window, "_selected_display", lambda: None):
            self.window._load_profile_from_path(path)  # must not raise


class ClampedMeasurementTests(WindowTestCase):
    """A reading that hits a bound is no longer the reading that was taken. Storing a
    different number than the user measured, without saying so, is the one thing a
    measurement step must never do."""

    def test_a_reading_below_the_floor_is_reported(self):
        """60 nits peak is plausible on a dim panel and became 80 in silence."""
        self.window._record_measurement("peak-white", 60.0)
        text = self.window.status_label.text()
        self.assertIn("60", text)
        self.assertIn("80", text)

    def test_a_reading_above_the_ceiling_is_reported(self):
        self.window._record_measurement("peak-white", 50000.0)
        self.assertIn("outside the range", self.window.status_label.text())

    def test_the_stored_value_is_still_bounded(self):
        """The ICC fields have limits; saying so is the fix, not removing them."""
        self.window._record_measurement("peak-white", 50000.0)
        self.assertLessEqual(self.window.state.hdr.peak_luminance_nits, 10000.0)

    def test_a_reading_inside_the_range_is_not_flagged(self):
        self.window._record_measurement("peak-white", 940.0)
        text = self.window.status_label.text()
        self.assertNotIn("outside the range", text)
        self.assertIn("Recorded", text)

    def test_black_level_uses_its_own_bounds(self):
        """Zero is a legitimate black level on an emissive panel, not a clamp."""
        self.window._record_measurement("black-level", 0.0)
        self.assertNotIn("outside the range", self.window.status_label.text())


class MetadataSourceTests(WindowTestCase):
    """Whether DXGI reports the panel or the profile in force decides whether reading it
    back is a measurement or a loop. Tested directly on hardware: associating a profile
    with no MHC2 left the reported luminance unchanged for six seconds, so it is the panel.

    The suspicion came from the reported figures changing between two days and matching the
    profile applied on each. A monitor setting changing what the panel advertises explains
    that without any echo, and the hardware test rules the echo out.
    """

    def capability(self, **overrides):
        from sdr_hdr_profile_creator.hdr_display import (
            DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
            DisplayCapability,
        )

        base = dict(
            device_name=self.display.gdi_name, left=0, top=0, right=3840, bottom=2160,
            bits_per_color=10, color_space=DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
            min_nits=0.0, max_nits=1080.0, max_full_frame_nits=1080.0,
            red_primary=(0.67, 0.31), green_primary=(0.27, 0.69),
            blue_primary=(0.15, 0.06), white_point=(0.313, 0.329),
        )
        base.update(overrides)
        return DisplayCapability(**base)

    def test_an_applied_mhc2_profile_does_not_disqualify_the_reading(self):
        """The guard this replaces made the fallback a near-permanent no-op, since an
        MHC2 profile is applied almost all the time."""
        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.assertFalse(self.window._active_profile_overrides_metadata(self.display))

    def test_an_unknown_association_is_still_not_vouched_for(self):
        with mock.patch.object(app_module, "get_default_profile",
                               side_effect=RuntimeError("no")):
            self.assertTrue(self.window._active_profile_overrides_metadata(self.display))

    def test_no_association_at_all_is_not_vouched_for(self):
        self.default_profiles["HDR"] = ""
        self.assertTrue(self.window._active_profile_overrides_metadata(self.display))


class BlackLevelPlausibilityTests(WindowTestCase):
    """The pattern asks whether a shape is visible, which cannot separate "the panel
    cannot show it" from "I cannot see it". Only the first belongs in a profile: recorded
    as minimum luminance it tells Windows the display cannot go darker, and Windows
    tone-maps against that."""

    def test_a_black_reading_that_looks_like_room_lighting_is_flagged(self):
        self.window._record_measurement("black-level", 0.1956)
        self.assertIn("darkest level you can see", self.window.status_label.text())

    def test_a_credible_black_reading_is_not_flagged(self):
        self.window._record_measurement("black-level", 0.004)
        self.assertIn("Recorded", self.window.status_label.text())
        self.assertNotIn("darkest level you can see", self.window.status_label.text())

    def test_zero_is_accepted_without_complaint(self):
        """Effectively zero is the right answer on an emissive panel."""
        self.window._record_measurement("black-level", 0.0)
        self.assertNotIn("darkest level you can see", self.window.status_label.text())

    def test_the_reading_is_still_stored(self):
        """It is the user's measurement; the app doubts it, it does not discard it."""
        self.window._record_measurement("black-level", 0.1956)
        self.assertAlmostEqual(self.window.state.hdr.minimum_luminance_nits, 0.1956)

    def test_peak_readings_are_not_subject_to_the_black_check(self):
        self.window._record_measurement("peak-white", 940.0)
        self.assertNotIn("darkest level you can see", self.window.status_label.text())


class PanelPrefillTests(WindowTestCase):
    """A profile generated before anyone opens the patterns used to claim 1000 nits peak
    and 400 full-frame for every display in the world. The panel states its own figures;
    1015/265 describes one, 1000/400 describes none."""

    def panel(self, **overrides):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        base = dict(peak_nits=1015.24, max_frame_average_nits=265.05,
                    min_nits=0.0002, supports_pq=True)
        base.update(overrides)
        return PanelMetadata(**base)

    def test_untouched_defaults_are_replaced_by_the_declaration(self):
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: self.panel()):
            self.assertTrue(self.window._prefill_luminance_from_panel(self.display))
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 1015.24, places=1)
        self.assertAlmostEqual(self.window.state.hdr.full_frame_luminance_nits, 265.05, places=1)

    def test_a_measurement_is_never_overwritten(self):
        """A measurement is about this display; the EDID is about the model."""
        self.window._record_measurement("peak-white", 940.0)
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: self.panel()):
            self.assertFalse(self.window._prefill_luminance_from_panel(self.display))
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 940.0)

    def test_a_panel_that_declares_nothing_leaves_the_defaults(self):
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: None):
            self.assertFalse(self.window._prefill_luminance_from_panel(self.display))
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 1000.0)

    def test_an_incredible_declaration_is_ignored(self):
        with mock.patch.object(app_module, "read_panel_metadata",
                               lambda _p: self.panel(supports_pq=False)):
            self.assertFalse(self.window._prefill_luminance_from_panel(self.display))

    def test_full_frame_is_still_held_at_or_below_peak(self):
        with mock.patch.object(app_module, "read_panel_metadata",
                               lambda _p: self.panel(peak_nits=300.0, max_frame_average_nits=900.0)):
            self.window._prefill_luminance_from_panel(self.display)
        self.assertLessEqual(self.window.state.hdr.full_frame_luminance_nits,
                             self.window.state.hdr.peak_luminance_nits)

    def test_it_says_the_figures_are_a_specification(self):
        """Presenting a manufacturer's claim as a measurement is the thing to avoid."""
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: self.panel()):
            self.window._refresh_displays()
        self.assertIn("specification", self.window.status_label.text())


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class BuildFromPanelTests(WindowTestCase):
    """Generating a profile with no base profile at all.

    This is the path that removes the dependency on Microsoft's separate HDR
    Calibration app: with nothing to inherit ICC tags from, the panel's own
    reported primaries and luminance are all the profile has to go on.
    """

    PANEL_XY = (0.674586, 0.314418, 0.269814, 0.685949, 0.151222, 0.060916, 0.313786, 0.329268)

    def fake_capability(self, is_hdr=True):
        return SimpleNamespace(
            device_name=r"\.\DISPLAY1",
            is_hdr=is_hdr,
            red_primary=self.PANEL_XY[0:2],
            green_primary=self.PANEL_XY[2:4],
            blue_primary=self.PANEL_XY[4:6],
            white_point=self.PANEL_XY[6:8],
        )

    def use_panel(self, *, is_hdr=True, capability=True):
        patcher = mock.patch.object(
            app_module, "capability_for_device_name",
            lambda name: self.fake_capability(is_hdr) if capability else None,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_option_is_offered_in_the_picker(self):
        self.window._populate_profile_pickers()
        items = [self.window.hdr_profile_combo.itemText(i)
                 for i in range(self.window.hdr_profile_combo.count())]
        self.assertIn(app_module.HDR_FROM_PANEL, items)

    def test_choosing_it_clears_the_base_profile(self):
        self.use_panel()
        self.window.state.hdr.base_profile = str(self.color_dir / "BaseCalibration.icm")
        self.window.state.hdr.base_profile_name = "BaseCalibration.icm"
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.assertEqual(self.window.state.hdr.base_profile, "")
        self.assertEqual(self.window.state.hdr.imported_profile, "")

    def test_captures_the_panels_primaries_in_hdr(self):
        self.use_panel()
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.assertEqual(self.window.state.hdr.panel_primaries, self.PANEL_XY)

    def test_refuses_to_capture_primaries_while_the_display_is_in_sdr(self):
        """The driver reports BT.709 for a wide-gamut panel while HDR is off.

        A profile built from that would describe a P3 display as sRGB, which is
        exactly how one of the reference profiles on the development machine
        came to claim 0.640/0.330 for red.
        """
        self.use_panel()
        # current_mode reads advanced_color_kind, not advanced_color_enabled.
        self.display.advanced_color_kind = "SDR"
        self.display.advanced_color_enabled = False
        self.window._current_display_snapshot = self.display
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.assertEqual(self.window.state.hdr.panel_primaries, ())
        self.assertIn("HDR", self.window.status_label.text())

    def test_survives_a_display_that_reports_no_capability(self):
        self.use_panel(capability=False)
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.assertEqual(self.window.state.hdr.panel_primaries, ())
        self.assertEqual(self.window.state.hdr.base_profile, "")

    def test_applied_profile_describes_the_panel_not_bt2020(self):
        self.use_panel()
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.apply()
        installed = [n for n in self.installed if n.lower().endswith((".icm", ".icc"))]
        self.assertTrue(installed, "nothing was installed")
        described = app_module.profile_primaries_xy(
            (self.color_dir / installed[-1]).read_bytes()
        )
        flat = [v for pair in described[:3] for v in pair]
        for index, expected in enumerate(self.PANEL_XY[:6]):
            self.assertAlmostEqual(flat[index], expected, places=3)

    def test_applying_does_not_readopt_a_base_behind_the_users_back(self):
        """_capture_current_hdr_base runs on every Apply and adopts the Windows
        default when no base is pinned. The sentinel is a deliberate choice of
        'no base', so it must outrank that."""
        self.use_panel()
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.apply()
        self.assertEqual(self.window.state.hdr.base_profile, "")

    def test_no_divergence_warning_for_a_mode_that_is_not_a_filename(self):
        self.use_panel()
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        with mock.patch.object(self.window, "_announce_diverged_base") as announce:
            self.apply()
        announce.assert_not_called()

    def use_edid(self, peak=1015.24, frame_average=265.05, minimum=0.0002):
        panel = PanelMetadata(
            peak_nits=peak,
            max_frame_average_nits=frame_average,
            min_nits=minimum,
            supports_pq=True,
        )
        patcher = mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_luminance_is_replaced_with_what_the_panel_declares(self):
        """Choosing this option is a request for the panel's own figures.

        Whatever was there before generally came from DXGI, which reports this
        panel's sustained full-frame luminance as equal to its peak -- impossible
        on an emissive display, and the reason EDID is read at all."""
        self.use_panel()
        self.use_edid()
        self.window.state.hdr.peak_luminance_nits = 1019.5
        self.window.state.hdr.full_frame_luminance_nits = 1019.5
        self.window.state.hdr.minimum_luminance_nits = 0.0
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        state = self.window.state.hdr
        self.assertAlmostEqual(state.peak_luminance_nits, 1015.24, places=2)
        self.assertAlmostEqual(state.full_frame_luminance_nits, 265.05, places=2)
        self.assertAlmostEqual(state.minimum_luminance_nits, 0.0002, places=4)

    def test_luminance_is_left_alone_when_the_panel_declares_nothing_usable(self):
        """No EDID answer is not a reason to overwrite with defaults."""
        self.use_panel()
        self.window.state.hdr.peak_luminance_nits = 742.0
        self.window.state.hdr.minimum_luminance_nits = 0.004
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 742.0)
        self.assertAlmostEqual(self.window.state.hdr.minimum_luminance_nits, 0.004)

    def test_slider_corrections_survive_the_switch(self):
        """They are relative trims, not colorimetry; discarding them silently
        would throw away work with no warning."""
        self.use_panel()
        self.use_edid()
        self.window.state.hdr.temperature = -180.0
        self.window.state.hdr.contrast = 4.5
        self.window._hdr_profile_chosen(app_module.HDR_FROM_PANEL)
        self.assertAlmostEqual(self.window.state.hdr.temperature, -180.0)
        self.assertAlmostEqual(self.window.state.hdr.contrast, 4.5)
    def test_selecting_the_entry_already_shown_still_applies_it(self):
        """The picker put this entry at position 0, so it could already be the
        displayed text before anyone touched it. qfluentwidgets emits
        currentTextChanged only from inside setCurrentIndex, which returns early
        when the index has not moved -- so listening to that signal made clicking
        the visible entry do nothing at all, and Apply then silently used the old
        base profile instead."""
        self.use_panel()
        self.window.hdr_profile_combo.textActivated.emit(app_module.HDR_FROM_PANEL)
        self.assertEqual(self.window.state.hdr.panel_primaries, self.PANEL_XY)
        self.assertEqual(self.window.state.hdr.base_profile, "")

    def test_picker_shows_the_profile_actually_in_force_when_nothing_is_pinned(self):
        """A combo showing one thing while the state holds another is how the
        build-from-panel entry appeared selected without ever being applied."""
        binding = self.window._selected_binding()
        binding.hdr_profile = ""
        self.window.state.hdr.base_profile = ""
        self.window._base_hdr_profiles[self.display.key] = {
            "profile_name": "BaseCalibration.icm",
            "profile_path": str(self.color_dir / "BaseCalibration.icm"),
        }
        self.window._populate_profile_pickers()
        self.assertEqual(
            self.window.hdr_profile_combo.currentText(), "BaseCalibration.icm"
        )


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class MeterIntegrationTests(WindowTestCase):
    """Driving a colorimeter from the window.

    The instrument and ArgyllCMS are both replaced. What matters here is that a
    measurement is adopted only when there is one, and that nothing is changed
    when there is not -- a half-applied calibration is indistinguishable from a
    real one once it is in the profile.
    """

    PRIMARIES = (0.674586, 0.314418, 0.269814, 0.685949, 0.151222, 0.060916, 0.3127, 0.3290)

    def calibration(self, peak=454.25, black=0.0, gains=(1.0, 1.0, 1.0),
                    white_xy=(0.3270, 0.3295)):
        from sdr_hdr_profile_creator.measure import Calibration

        return Calibration(
            peak_nits=peak,
            black_nits=black,
            white_xy=white_xy,
            channel_gains=gains,
        )

    def test_the_button_is_offered(self):
        self.assertTrue(hasattr(self.window, "meter_button"))

    def test_refuses_to_measure_while_the_display_is_in_sdr(self):
        """Patches are shown in absolute luminance, which means nothing in SDR."""
        self.display.advanced_color_kind = "SDR"
        self.window._current_display_snapshot = self.display
        with mock.patch.object(app_module, "find_spotread") as finder:
            self.window._measure_with_meter()
        finder.assert_not_called()
        self.assertIn("HDR", self.window.status_label.text())

    def test_offers_to_locate_argyll_when_it_is_missing(self):
        with mock.patch.object(app_module, "find_spotread", return_value=None):
            with mock.patch.object(
                app_module.QMessageBox, "question",
                return_value=app_module.QMessageBox.StandardButton.No,
            ) as ask:
                self.window._measure_with_meter()
        ask.assert_called_once()

    def test_a_configured_argyll_directory_is_preferred(self):
        self.window.state.argyll_path = r"D:\Argyll\bin"
        with mock.patch.object(app_module, "find_spotread") as finder:
            self.window._spotread()
        finder.assert_called_once_with(r"D:\Argyll\bin")

    def test_reports_when_no_instrument_is_connected(self):
        with mock.patch.object(app_module, "find_spotread", return_value=Path("spotread")):
            with mock.patch.object(app_module, "list_instruments", return_value=[]):
                self.window._measure_with_meter()
        self.assertIn("no colorimeter", self.window.status_label.text().lower())

    def test_a_completed_measurement_is_adopted(self):
        self.window._measure_finished(self.calibration(), "")
        state = self.window.state.hdr
        self.assertAlmostEqual(state.peak_luminance_nits, 454.25, places=2)
        self.assertAlmostEqual(state.minimum_luminance_nits, 0.0, places=6)

    def test_measured_primaries_never_reach_the_profile_gamut(self):
        """scRGB is defined on BT.709, so a measured "red" is BT.709 red as the
        display renders it. Writing that to the colorant tags replaced the
        correct DXGI figures with a narrower, wrong gamut -- and nothing
        downstream could tell the difference."""
        self.window.state.hdr.panel_primaries = self.PRIMARIES
        self.window._measure_finished(self.calibration(), "")
        self.assertEqual(self.window.state.hdr.panel_primaries, self.PRIMARIES)

    def test_the_white_balance_is_applied_to_the_channel_trims(self):
        """The same readings that are useless as a gamut are exactly right here:
        the correction acts on the signal this app sends."""
        self.window._measure_finished(self.calibration(gains=(0.86, 1.0, 0.97)), "")
        state = self.window.state.hdr
        self.assertAlmostEqual(state.red_channel, -14.0, places=1)
        self.assertAlmostEqual(state.green_channel, 0.0, places=1)
        self.assertAlmostEqual(state.blue_channel, -3.0, places=1)

    def test_an_oversized_correction_is_clamped_and_said_so(self):
        """Clamping silently would leave white visibly off with nothing said."""
        self.window._measure_finished(self.calibration(gains=(0.4, 1.0, 1.0)), "")
        self.assertGreaterEqual(self.window.state.hdr.red_channel, -25.0)
        self.assertIn("clamped", self.window.status_label.text())

    def test_measured_values_outrank_whatever_was_there(self):
        """A measurement describes this unit; everything else describes a model."""
        self.window.state.hdr.peak_luminance_nits = 1019.5
        self.window._measure_finished(self.calibration(), "")
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 454.25, places=2)

    def test_a_failed_measurement_changes_nothing(self):
        before = (
            self.window.state.hdr.peak_luminance_nits,
            self.window.state.hdr.minimum_luminance_nits,
            self.window.state.hdr.panel_primaries,
        )
        self.window._measure_finished(None, "The meter is in the wrong position.")
        after = (
            self.window.state.hdr.peak_luminance_nits,
            self.window.state.hdr.minimum_luminance_nits,
            self.window.state.hdr.panel_primaries,
        )
        self.assertEqual(before, after)
        self.assertIn("wrong position", self.window.status_label.text())

    def test_a_cancelled_measurement_is_not_reported_as_an_error(self):
        self.window._measure_finished(None, "")
        self.assertIn("cancelled", self.window.status_label.text().lower())

    def test_the_reading_is_reported_with_its_contrast(self):
        self.window._measure_finished(self.calibration(black=0.05), "")
        text = self.window.status_label.text()
        self.assertIn("454.2", text)
        self.assertIn("contrast", text)

    def test_the_window_size_is_reported_beside_the_peak(self):
        """Peak luminance means nothing without it: this panel is rated 1015
        nits and reads 454 on a tenth of the screen."""
        self.window._measure_finished(self.calibration(), "")
        self.assertIn("10% window", self.window.status_label.text())

    def test_an_unmeasurable_black_is_not_reported_as_infinite_contrast(self):
        """0.0000 is the instrument's floor, not a measurement of zero."""
        self.window._measure_finished(self.calibration(black=0.0), "")
        self.assertIn("too high to measure", self.window.status_label.text())

    def test_implausible_measurements_are_still_clamped_before_storage(self):
        """derive already refuses these, so this is the second line rather than
        the first -- but the profile fields have hard limits of their own."""
        self.window._measure_finished(self.calibration(peak=99000.0), "")
        self.assertLessEqual(self.window.state.hdr.peak_luminance_nits, 10000.0)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class ControlRowFitTests(WindowTestCase):
    """Every control must fit at the smallest size the window allows itself.

    Adding the meter button pushed row 3 to ten controls needing 2572px on one
    line. At the shipped width the switches truncated to "Automatic Moc" and
    "Keep Profile Lo" and the meter button lost its ellipsis -- and nothing
    failed, because Qt elides silently. Splitting the row was not enough on its
    own: the two halves still wanted 1196 and 1626 against a 1080 minimum.

    Headroom is now about 30px per row, so this is the guard that catches the
    next control before a user does.
    """

    SPACING = 12

    def row_width(self, widgets, indent=0):
        return indent + sum(w.sizeHint().width() + self.SPACING for w in widgets)

    def button(self, text):
        from PySide6.QtWidgets import QAbstractButton

        found = [b for b in self.window.findChildren(QAbstractButton) if b.text() == text]
        self.assertTrue(found, f"no button labelled {text!r}")
        return found[0]

    def test_the_switch_row_fits_at_the_minimum_window_width(self):
        from qfluentwidgets import StrongBodyLabel

        labels = [
            w for w in self.window.findChildren(StrongBodyLabel)
            if "Edits & Apply" in w.text()
        ]
        self.assertTrue(labels, "row 3 label missing")
        switches = [
            self.window.live_checkbox,
            self.window.automatic_mode_checkbox,
            self.window.lock_switch,
        ]
        required = self.row_width(switches, indent=labels[0].sizeHint().width() + self.SPACING)
        self.assertLessEqual(required, self.window.minimumWidth(), f"switch row needs {required}px")

    def test_the_action_row_fits_at_the_minimum_window_width(self):
        buttons = [
            self.button(text)
            for text in ("Revert", "Reset Sliders", "Test Patterns…",
                         "Measure…", "Reapply", "Apply Edits")
        ]
        required = self.row_width(buttons)
        self.assertLessEqual(required, self.window.minimumWidth(), f"action row needs {required}px")

    def test_the_elastic_widgets_are_the_ones_allowed_to_shrink(self):
        """Row 2 is not covered by the rule above, and should not be.

        Its two combo boxes are added with a stretch factor and hold profile
        names of arbitrary length, so they are meant to shrink and elide. What
        must not shrink is their floor -- 210px keeps a name readable -- and the
        buttons beside them, whose labels are fixed and short.
        """
        for combo in (self.window.sdr_profile_combo, self.window.hdr_profile_combo):
            self.assertGreaterEqual(combo.minimumWidth(), 200)
        fixed = [self.button("Import…"), self.button("Export Copy…")]
        floor = sum(combo.minimumWidth() for combo in
                    (self.window.sdr_profile_combo, self.window.hdr_profile_combo))
        required = floor + self.row_width(fixed)
        self.assertLessEqual(
            required, self.window.minimumWidth(),
            f"row 2 cannot shrink below {required}px",
        )
    def test_terse_labels_still_explain_themselves_in_a_tooltip(self):
        """The labels were shortened to fit, so the tooltip now carries the
        meaning that used to be on the face of the button."""
        for text, expected in (
            ("Revert", "discard"),
            ("Reset Sliders", "neutral"),
            ("Measure…", "colorimeter"),
        ):
            with self.subTest(button=text):
                self.assertIn(expected, self.button(text).toolTip().lower())


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class ProfileLockSwitchTests(WindowTestCase):
    """The switch that keeps an applied HDR profile applied.

    It reports whether the watchdog process is running, not what was last
    clicked: the installer is a separate elevated program that the user can
    dismiss at the UAC prompt, and the watchdog can be stopped from outside
    this app entirely.
    """

    def setUp(self):
        super().setUp()
        self.scripts: list[str] = []
        patcher = mock.patch.object(
            self.window, "_run_watchdog_script", lambda name: self.scripts.append(name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_starts_off_when_the_watchdog_is_not_running(self):
        self.assertFalse(self.window.lock_switch.isChecked())

    def test_follows_a_watchdog_that_starts_outside_the_app(self):
        self.watchdog_running = True
        self.window._sync_lock_switch()
        self.assertTrue(self.window.lock_switch.isChecked())

    def test_follows_a_watchdog_that_stops_outside_the_app(self):
        self.watchdog_running = True
        self.window._sync_lock_switch()
        self.watchdog_running = False
        self.window._sync_lock_switch()
        self.assertFalse(self.window.lock_switch.isChecked())

    def test_switching_on_runs_the_installer(self):
        self.window._lock_toggled(True)
        self.assertEqual(len(self.scripts), 1)
        self.assertIn("Install-Watchdog", self.scripts[0])

    def test_switching_off_runs_the_uninstaller(self):
        self.watchdog_running = True
        self.window._lock_toggled(False)
        self.assertEqual(self.scripts, ["Uninstall-Watchdog.bat"])

    def test_no_script_when_the_switch_already_agrees_with_reality(self):
        """The poll writes the switch back; that must not re-run the installer."""
        self.watchdog_running = True
        self.window._lock_toggled(True)
        self.assertEqual(self.scripts, [])

    def test_syncing_the_switch_does_not_trigger_an_install(self):
        self.watchdog_running = True
        self.window._sync_lock_switch()
        self.assertEqual(self.scripts, [])

    def test_reports_the_outcome_once_the_install_actually_lands(self):
        self.window._lock_toggled(True)
        self.assertEqual(self.window._lock_pending, "install")
        self.watchdog_running = True
        self.window._sync_lock_switch()
        self.assertEqual(self.window._lock_pending, "")
        self.assertIn("lock is on", self.window.status_label.text().lower())

    def test_a_dismissed_uac_prompt_leaves_the_switch_off(self):
        """Nothing starts, so the switch must go back to telling the truth."""
        self.window.lock_switch.setChecked(True)
        self.window._sync_lock_switch()
        self.assertFalse(self.window.lock_switch.isChecked())
