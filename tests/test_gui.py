"""Behavioural tests for the editor window and its profile-application pipeline.

Every Windows colour API is replaced with a recording fake backed by a temporary
directory, so these exercise real control flow without touching the machine's
actual colour configuration.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication, QMessageBox

    from sdr_hdr_profile_creator import app as app_module
    from sdr_hdr_profile_creator.controls import ControlSpec, SliderControl
    from sdr_hdr_profile_creator.dialogs import GUIDE_STEPS, HELP_SECTIONS
    from sdr_hdr_profile_creator.gamma_correction import CORRECTION_OPTIONS
    from sdr_hdr_profile_creator.edid import PanelMetadata
    from sdr_hdr_profile_creator.model import ApplicationState, normalize_primaries
    from sdr_hdr_profile_creator.windows_api import DisplayInfo

    GUI_AVAILABLE = True
    GUI_IMPORT_ERROR = ""
except ImportError as exc:  # qfluentwidgets is a project dependency; skip if absent
    GUI_AVAILABLE = False
    GUI_IMPORT_ERROR = str(exc)


def app_module_instrument():
    """A stand-in for one entry from Argyll's port list."""
    from sdr_hdr_profile_creator.meter import Instrument

    return Instrument(port=1, label="hid:/31 (X-Rite i1 DisplayPro)")

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
        self.overwritten: list[str] = []
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

        def fake_reapply(display, mode, profile_name):
            self.associations.append(profile_name)
            self.default_profiles[mode] = profile_name
            return profile_name

        def fake_remove(profile_name, display, mode):
            self.removed.append(profile_name)
            (self.color_dir / profile_name).unlink(missing_ok=True)
            return True, "uninstalled"

        def fake_overwrite(profile_name, payload):
            """The in-place repair, with the real one's refusals.

            The real call writes into the system colour folder, so leaving it
            unfaked would put this suite's fixtures into the machine's own
            profiles -- the mistake METER_LOG_PATH already made once.
            """
            self.overwritten.append(profile_name)
            target = self.color_dir / profile_name
            if not target.is_file():
                return False
            if len(payload) < target.stat().st_size:
                return False  # no truncate right: a short write would leave a tail
            target.write_bytes(payload)
            return True

        patches = {
            "LOCAL_ROOT": self.temp,
            "STATE_PATH": self.temp / "last_gui_state.json",
            "LIVE_ROOT": self.live_root,
            "LIVE_REGISTRY_PATH": self.temp / "live_registry.json",
            "GAMMA_HOTKEY_STATE_PATH": self.temp / "gamma_hotkeys.json",
            "METER_LOG_PATH": self.temp / "meter_log.jsonl",
            "GAMMA_PROFILE_ROOT": self.temp / "gamma_profiles",
            "enumerate_displays": lambda: [self.display],
            "get_color_directory": lambda: self.color_dir,
            "get_default_profile": lambda display, mode: self.default_profiles[mode],
            "get_sdr_white_level_nits": lambda display: 240.0,
            "install_and_associate_profile": fake_install,
            "associate_profile": fake_associate,
            # This changes the real display; never let it through.
            "set_hdr_enabled": fake_set_hdr,
            "reapply_existing_default_profile": fake_reapply,
            "remove_profile": fake_remove,
            "overwrite_installed_profile": fake_overwrite,
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
        "overwrite_installed_profile",
        "set_hdr_enabled",
    )

    # Every module-level path app.py writes to. A new one added without being
    # redirected here does not fail: it silently writes to the real profile
    # directory under LOCALAPPDATA. METER_LOG_PATH did exactly that, and the
    # evidence was a user's meter log full of this suite's fixtures -- peaks of
    # 99000 nits and -60% channel trims, sitting where their own readings should
    # have been.
    WRITTEN_PATHS = (
        "STATE_PATH",
        "GAMMA_HOTKEY_STATE_PATH",
        "METER_LOG_PATH",
        "LIVE_REGISTRY_PATH",
        "LOCAL_ROOT",
        "LIVE_ROOT",
        "GAMMA_PROFILE_ROOT",
    )

    def test_nothing_is_written_outside_the_temporary_directory(self):
        for name in self.WRITTEN_PATHS:
            with self.subTest(path=name):
                self.assertTrue(hasattr(app_module, name), f"{name} no longer exists")
                value = Path(str(getattr(app_module, name)))
                self.assertTrue(
                    self.temp in value.parents or value == self.temp,
                    f"{name} points at {value}, outside the fixture's temp directory",
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
            # The three figures the profile is actually built from. They reach the
            # MHC2 header and the lumi tag directly, and having no widgets is why a
            # stale peak carried over from another display, and a sustained figure
            # left above peak after a measurement, were both invisible while they
            # happened.
            "peak_luminance_nits", "full_frame_luminance_nits", "minimum_luminance_nits",
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
            # Matching ModeState's own limits, so a typed value and a loaded one are
            # constrained alike rather than the editor allowing what the model clamps.
            # 0.01, matching the two decimals shown. set_value quantises to the
            # declared step, so a whole-nit step silently snapped every typed value to
            # an integer -- this panel declares 1015.24 and the field would take 1015.
            "peak_luminance_nits": (80.0, 10000.0, 1000.0, 0.01),
            "full_frame_luminance_nits": (80.0, 10000.0, 400.0, 0.01),
            "minimum_luminance_nits": (0.0, 100.0, 0.0, 0.0001),
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
            "Getting Started", "Watchdog…", "Help",
            # One action rather than three: find the right entry among every
            # installed profile, apply, then know those were separate steps.
            "Calibrate Display",
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
        # These tests write placeholder scripts, which are by definition not the build
        # this app ships. "" is what the app reports when it cannot read its own
        # installer, and it is treated as unknown rather than as a mismatch -- so this
        # keeps the outcome reporting under test here and leaves the build comparison
        # to WatchdogBuildTests, instead of every case below reading as a foreign build.
        shipped = mock.patch.object(app_module, "shipped_watchdog_build", lambda: "")
        shipped.start()
        self.addCleanup(shipped.stop)
        self.script = self.install_root / "Watchdog.ps1"

    def write_result(self, **payload):
        """What the installer or uninstaller records for the GUI to read."""
        import json as _json

        path = self.install_root / "install_result.json"
        path.write_text(_json.dumps(payload), encoding="utf-8")
        return path

    def test_a_recorded_failure_beats_a_freshly_written_script(self):
        """The whole point of M3.

        The .bat writes Watchdog.ps1 before the integrity check, before -Install and
        before Task Scheduler registration, so its mtime proves extraction and nothing
        more. An -Install that threw afterwards -- the realistic first-time failure,
        when the Off/On pair is incomplete -- still moved that timestamp, and the status
        bar printed a green "Watchdog installed." nine seconds later.
        """
        before = self.window._watchdog_script_stamp()
        self.script.write_text("extracted", encoding="utf-8")
        self.write_result(action="install", ok=False, warnings=["the working pair is incomplete"])
        self.window._report_watchdog_outcome(True, before)
        text = self.window.status_label.text()
        self.assertIn("Error", text)
        self.assertIn("incomplete", text)

    def test_a_fallback_startup_entry_is_amber_rather_than_green(self):
        """It works, but not the way it was meant to: no ten-second logon delay."""
        before = self.window._watchdog_script_stamp()
        self.script.write_text("extracted", encoding="utf-8")
        self.write_result(
            action="install", ok=True, startup="HKCU Run fallback",
            warnings=["Windows refused to register the scheduled task"],
        )
        self.window._report_watchdog_outcome(True, before)
        text = self.window.status_label.text()
        self.assertIn("Attention", text, "a degraded install should not read as success")
        self.assertIn("scheduled task", text)

    def test_a_clean_install_names_how_it_will_start(self):
        before = self.window._watchdog_script_stamp()
        self.script.write_text("extracted", encoding="utf-8")
        self.write_result(action="install", ok=True, startup="Task Scheduler (COM / current-user SID)", warnings=[])
        self.window._report_watchdog_outcome(True, before)
        text = self.window.status_label.text()
        self.assertIn("Ready", text)
        self.assertIn("Task Scheduler", text)

    def test_a_result_from_a_previous_run_is_ignored(self):
        """Otherwise last week's failure is reported as this click's outcome.

        Freshness is judged against the moment the installer was launched, not against
        Watchdog.ps1's mtime: that is 0.0 when nothing is installed yet, and every
        stale result on disk is newer than zero.
        """
        import os
        import time

        self.write_result(action="install", ok=False, warnings=["ancient history"])
        stale = self.install_root / "install_result.json"
        old = time.time() - 3600
        os.utime(stale, (old, old))

        before = self.window._watchdog_script_stamp()
        launched_at = time.time()
        self.script.write_text("installed", encoding="utf-8")
        self.window._report_watchdog_outcome(True, before, launched_at)
        text = self.window.status_label.text()
        self.assertNotIn("ancient history", text)
        # Having correctly refused the stale record, this run has no record of its own,
        # which is an unknown rather than a success. This assertion used to read
        # "Watchdog installed" -- it was riding on the mtime fallback that has since been
        # removed, not on anything this test is about.
        self.assertIn("did not report an outcome", text)

    def test_a_stale_result_cannot_pass_when_nothing_is_installed_yet(self):
        """The case that made the first version of this wrong: with no Watchdog.ps1 the
        script stamp is 0.0, so a mtime comparison accepted anything."""
        import os
        import time

        self.write_result(action="install", ok=False, warnings=["ancient history"])
        stale = self.install_root / "install_result.json"
        old = time.time() - 3600
        os.utime(stale, (old, old))

        self.assertEqual(0.0, self.window._watchdog_script_stamp(), "fixture has a script")
        self.assertIsNone(
            self.window._watchdog_result(time.time()),
            "a result written an hour ago was accepted as this run's",
        )

    def test_an_uninstall_that_left_the_task_behind_is_reported(self):
        """The uninstaller used to hardcode exit 0 under SilentlyContinue, so the batch
        errorlevel test was dead and it always printed "removed successfully"."""
        before = self.window._watchdog_script_stamp()
        self.write_result(
            action="uninstall", ok=False,
            warnings=["The scheduled task could not be removed: it was created by an elevated install."],
        )
        self.window._report_watchdog_outcome(False, before)
        text = self.window.status_label.text()
        self.assertIn("Error", text)
        self.assertIn("elevated install", text)

    def test_install_that_records_nothing_is_not_reported_as_success(self):
        """This test used to assert the opposite, and the opposite was the bug.

        A moved mtime proves extraction and nothing else. Now that the installer traps
        its own throws and records them, reaching this branch means it did not get far
        enough to write anything at all -- which is an unknown, not a success. Reporting
        an unknown in green is what let a dead watchdog look installed for an evening,
        and it is what the owner hit on 2026-08-28 when Task Scheduler registration was
        refused.
        """
        before = self.window._watchdog_script_stamp()
        self.script.write_text("installed", encoding="utf-8")
        self.window._report_watchdog_outcome(True, before)
        text = self.window.status_label.text()
        self.assertIn("did not report an outcome", text)
        self.assertIn("Attention", text, "an unknown outcome must not read as success")
        self.assertNotIn("Ready", text)

    def test_the_outcome_is_waited_for_rather_than_sampled_once(self):
        """A first install enumerates displays, resolves the working pair and registers a
        scheduled task over COM, which routinely outruns the old nine-second shot. The
        poll reports as soon as the record appears and stops at its deadline."""
        before = self.window._watchdog_script_stamp()
        self.script.write_text("installed", encoding="utf-8")
        launched_at = time.time() - 1.0
        self.window._watchdog_poll_deadline = time.time() + 90.0

        # Nothing recorded yet: it must keep waiting rather than reporting.
        self.window._set_status("waiting", "warning")
        self.window._poll_watchdog_outcome(True, before, launched_at)
        self.assertIn("waiting", self.window.status_label.text())

        # The record lands; the next poll reports it.
        self.write_result(action="install", ok=True, startup="Task Scheduler", warnings=[])
        self.window._poll_watchdog_outcome(True, before, launched_at)
        self.assertIn("Ready", self.window.status_label.text())

    def test_the_poll_gives_up_at_its_deadline(self):
        before = self.window._watchdog_script_stamp()
        self.script.write_text("installed", encoding="utf-8")
        self.window._watchdog_poll_deadline = time.time() - 1.0
        self.window._poll_watchdog_outcome(True, before, time.time() - 5.0)
        self.assertIn("did not report an outcome", self.window.status_label.text())

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


class MonitorStateTests(WindowTestCase):
    """The run log records what the monitor itself was set to.

    Without it a run that behaves differently from its neighbours cannot be explained
    afterwards, and one already could not be: the saturation sweeps show a ~2x boost on
    saturated patches in four of fourteen logged runs on the same display, correlating
    with neither the SDR-in-HDR correction nor the trims already applied. Every remaining
    candidate is a monitor state nothing recorded.
    """

    def test_the_controls_that_could_change_what_the_meter_sees_are_recorded(self):
        from sdr_hdr_profile_creator import ddc

        class Link:
            description = "fake"

            def read(self, code):
                return ddc.Control(code=code, current=7, maximum=100)

            def set(self, code, value):
                return True

        with mock.patch.object(app_module.ddc, "open_link", lambda _n: Link()):
            state = self.window._monitor_state()
        self.assertTrue(state["available"])
        for key in ("picture_mode", "colour_preset", "brightness", "contrast",
                    "gamma", "red_gain", "green_gain", "blue_gain"):
            with self.subTest(key=key):
                self.assertEqual(7, state[key])

    def test_a_monitor_without_ddc_is_recorded_as_such_rather_than_stopping_the_run(self):
        """A display with DDC/CI turned off in its own menu is perfectly calibratable. A
        diagnostic that refused to run on one would be worse than the gap it fills."""
        with mock.patch.object(
            app_module.ddc, "open_link",
            lambda _n: app_module.ddc.UnavailableLink("it said nothing"),
        ):
            state = self.window._monitor_state()
        self.assertFalse(state["available"])
        self.assertIn("it said nothing", state["why"])

    def test_a_failure_in_the_ddc_layer_cannot_stop_a_measurement(self):
        def explode(_name):
            raise OSError("the handle went away")

        with mock.patch.object(app_module.ddc, "open_link", explode):
            state = self.window._monitor_state()
        self.assertFalse(state["available"])
        self.assertIn("handle went away", state["why"])


class RuntimePublishFailureTests(WindowTestCase):
    """A publish that does not land must not pass for one that did.

    The watchdog decides which gamma variant wins by comparing this file's updated_at
    against its own captured GammaUpdatedAt. A silently failed write leaves the old
    stamp, so the captured state wins and the correction the user just chose is put back
    within five seconds -- while the GUI says it applied.
    """

    def test_a_failed_publish_is_reported_rather_than_swallowed(self):
        with mock.patch.object(app_module.MainWindow, "_write_json_atomic",
                               staticmethod(lambda *_a, **_k: False)):
            self.window._publish_runtime_payload({"displays": {}})
        text = self.window.status_label.text()
        self.assertIn("Attention", text)
        self.assertIn("watchdog cannot see", text)

    def test_a_publish_that_lands_says_nothing(self):
        self.window._set_status("unchanged", "ok")
        with mock.patch.object(app_module.MainWindow, "_write_json_atomic",
                               staticmethod(lambda *_a, **_k: True)):
            self.window._publish_runtime_payload({"displays": {}})
        self.assertIn("unchanged", self.window.status_label.text())


class RuntimeRecordIdentityTests(unittest.TestCase):
    """Which monitor a gamma_hotkeys.json record belongs to.

    State.json was moved onto the monitor's EDID device path because \\\\.\\DISPLAY1 is a
    slot Windows reassigns between sessions. The runtime file was left matching on the
    slot, so after a hotplug the two lookups disagreed and one monitor could be handed
    the other's profile pair -- the pair being what the watchdog then asserts into
    Windows.
    """

    def make(self, key, gdi, path):
        display = hdr_display(key=key)
        display.gdi_name = gdi
        display.device_path = path
        return display

    def test_the_device_path_decides_when_both_sides_have_one(self):
        display = self.make("A", r"\\.\DISPLAY1", r"\\?\DISPLAY#AUS32F2#UID4357")
        # Same monitor, but Windows has moved it to a different slot since the record
        # was written. It is still this monitor.
        record = {"gdi_name": r"\\.\DISPLAY2", "device_path": r"\\?\DISPLAY#AUS32F2#UID4357"}
        self.assertTrue(app_module.runtime_record_matches(record, display))

    def test_a_different_monitor_in_the_same_slot_is_not_a_match(self):
        """The failure this exists to prevent: two monitors swap slots, and the record
        for one is applied to the other."""
        display = self.make("A", r"\\.\DISPLAY1", r"\\?\DISPLAY#AUS32F2#UID4357")
        record = {"gdi_name": r"\\.\DISPLAY1", "device_path": r"\\?\DISPLAY#DEL1234#UID9"}
        self.assertFalse(app_module.runtime_record_matches(record, display))

    def test_a_record_written_before_paths_were_published_still_matches_by_slot(self):
        display = self.make("A", r"\\.\DISPLAY1", r"\\?\DISPLAY#AUS32F2#UID4357")
        self.assertTrue(app_module.runtime_record_matches({"gdi_name": r"\\.\DISPLAY1"}, display))
        self.assertFalse(app_module.runtime_record_matches({"gdi_name": r"\\.\DISPLAY2"}, display))

    def test_a_display_windows_gives_no_path_for_falls_back_to_the_slot(self):
        display = self.make("A", r"\\.\DISPLAY1", "")
        self.assertTrue(app_module.runtime_record_matches(
            {"gdi_name": r"\\.\DISPLAY1", "device_path": r"\\?\DISPLAY#WHATEVER"}, display))

    def test_rubbish_is_not_a_match(self):
        display = self.make("A", r"\\.\DISPLAY1", r"\\?\DISPLAY#AUS32F2#UID4357")
        for value in (None, "", 7, [], {"device_path": 42}):
            with self.subTest(value=value):
                self.assertFalse(app_module.runtime_record_matches(value, display))


class RuntimeStateTests(WindowTestCase):
    def test_the_published_record_carries_the_monitors_device_path(self):
        """Without this the watchdog has nothing to match on, and its device-path lookup
        silently degrades to the slot match it was meant to replace."""
        self.apply()
        entry = self.read_runtime()["displays"][self.display.key]
        self.assertIn("device_path", entry)
        self.assertEqual(entry["device_path"], self.display.device_path)

    def test_runtime_state_publishes_the_keys_the_watchdog_reads(self):
        self.apply()
        payload = self.read_runtime()
        self.assertEqual(payload["schema"], app_module.GAMMA_RUNTIME_SCHEMA)
        entry = payload["displays"][self.display.key]
        # base_profile and base_profile_path are the two the watchdog's
        # Resolve-BaseExtendedProfile actually reads to decide what to fall back to,
        # and they were the two this list left out: blanking both to "" kept 61 tests
        # green across every class that touches the runtime record. The .bat side of
        # the same contract is asserted by name in test_watchdog.py, so only the
        # publisher was unguarded.
        for key in ("profiles", "paths", "enabled", "selected", "active_profile",
                    "base_profile", "base_profile_path"):
            with self.subTest(key=key):
                self.assertIn(key, entry)
        self.assertEqual(set(entry["profiles"]), {"Off", "On"})
        self.assertEqual(entry["active_profile"], self.associations[-1])

    def test_the_base_profile_the_watchdog_falls_back_to_is_the_real_one(self):
        """Present is not enough; the value has to be the profile to fall back to.

        base_profile is not the template the user picked. _capture_current_hdr_base
        records whatever Windows had as the HDR default *before* this app took over,
        skipping anything of ours -- that is what the watchdog restores if the working
        pair ever goes missing. Blanking both keys kept 61 tests green before this.
        """
        self.apply()
        entry = self.read_runtime()["displays"][self.display.key]
        self.assertEqual("BaseCalibration.icm", entry["base_profile"])
        self.assertIn("BaseCalibration.icm", entry["base_profile_path"] or "")

    def test_the_published_base_is_never_one_of_our_own_profiles(self):
        """The failure this guards is circular: the watchdog would restore
        already-edited output as its own source, and every later run would inherit
        the previous run's corrections. Resolve-BaseExtendedProfile refuses a
        Virtual_HDR_OSD_ name on the watchdog side; this is the publisher's half.
        """
        self.apply()
        # Windows now reports one of ours as the HDR default, which is the state a
        # second apply starts from.
        self.default_profiles["HDR"] = self.associations[-1]
        self.window._capture_current_hdr_base(self.display)
        self.apply()

        entry = self.read_runtime()["displays"][self.display.key]
        self.assertEqual(
            "BaseCalibration.icm", entry["base_profile"],
            "the app published its own generated profile as the fallback base",
        )

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

    def test_uncommitted_out_of_range_text_is_not_the_control_value(self):
        """The validator refuses to let editingFinished fire for text outside the range,
        so this text never gets clamped or quantised on its way anywhere. While value()
        read the field, it was handed straight to the profile build and the pattern
        view: a 0-100 control could report 9999, matching neither its own slider nor
        the state it was supposed to describe."""
        control = self.make()
        control.set_value(2.0, emit=False)
        control.value_edit.setText("9999")
        self.assertAlmostEqual(control.value(), 2.0)

    def test_unparseable_text_does_not_silently_become_the_default(self):
        """Falling back to spec.default meant a stray keystroke reported neutral for a
        control that was nowhere near neutral -- the one wrong answer with no signal
        that anything had gone wrong."""
        control = self.make()
        control.set_value(-6.5, emit=False)
        control.value_edit.setText("not a number")
        self.assertAlmostEqual(control.value(), -6.5)

    def test_losing_focus_puts_rejected_text_back_to_the_real_value(self):
        """Rejected text used to just sit there contradicting the slider beside it, with
        nothing to say which one the profile would be built from."""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QFocusEvent

        control = self.make()
        control.set_value(3.25, emit=False)
        control.value_edit.setText("9999")
        # sendEvent, not value_edit.event(): only the notify path runs installed event
        # filters, and the resync lives in one. Calling event() directly would test
        # nothing and pass whatever the filter did.
        self.qt_app.sendEvent(control.value_edit, QFocusEvent(QEvent.Type.FocusOut))
        self.assertEqual(control.value_edit.text(), "3.25")
        self.assertAlmostEqual(control.value(), 3.25)

    def test_a_committed_edit_still_reaches_the_value(self):
        """The guard above must not eat legitimate typing: acceptable text has to keep
        flowing through editingFinished."""
        control = self.make()
        control.set_value(0.0, emit=False)
        control.value_edit.setText("4.5")
        control.value_edit.editingFinished.emit()
        self.assertAlmostEqual(control.value(), 4.5)
        self.assertAlmostEqual(control.slider.value() / control._scale, 4.5, places=6)

    def test_dragging_the_slider_updates_the_value(self):
        """_slider_changed writes the field and emits; with the value held separately it
        has to write that too, or dragging would move the picture and report the old
        number."""
        control = self.make()
        control.set_value(0.0, emit=False)
        control.slider.setValue(round(7.5 * control._scale))
        self.assertAlmostEqual(control.value(), 7.5, places=6)


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

    def test_a_refused_apply_is_reported_as_failure(self):
        """_apply_mode_profile signals failure by returning False, not by raising --
        Windows not being in HDR is the ordinary case. This guarded only against the
        exception and returned True regardless, so the fullscreen surface latched
        "Written into the profile." in green and refused every further Enter while the
        real reason sat in a status bar that window covers."""
        with mock.patch.object(self.window, "_apply_mode_profile", return_value=False):
            self.assertFalse(self.window._apply_from_pattern_view())

    def test_the_result_is_the_applys_own_answer_not_a_constant(self):
        """Both branches through one call site, so a hardcoded return cannot pass."""
        answers = []
        for expected in (True, False, True):
            with mock.patch.object(self.window, "_apply_mode_profile", return_value=expected):
                answers.append(self.window._apply_from_pattern_view())
        self.assertEqual([True, False, True], answers)

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


class RefusedBalanceTests(WindowTestCase):
    """A run whose channels do not add up still measured peak, black and the ramp.

    The white balance is the only thing that rests on red plus green plus blue equalling
    the white beside them, so it is the only thing refused. Everything else was read
    directly or derived from ratios that survive a scale error on the primaries.
    """

    def calibration(self, refused=("Red, green and blue add up to 111% away from the "
                                   "measured white. Something between the signal and the "
                                   "panel is not linear, so a white balance derived from "
                                   "these would be wrong.")):
        from sdr_hdr_profile_creator.measure import Calibration

        return Calibration(
            peak_nits=450.0,
            black_nits=0.0,
            white_xy=(0.3289, 0.3285),
            channel_gains=(1.0, 1.0, 1.0),
            balance_refused=tuple(refused) if isinstance(refused, tuple) else (refused,),
        )

    def test_peak_and_black_are_still_adopted(self):
        self.window._measure_finished(self.calibration(), "")
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 450.0, places=2)

    def test_the_trims_already_in_force_are_left_alone(self):
        """Not merely unchanged by arithmetic -- deliberately not written. A correction
        that survives by accident survives only until the arithmetic changes."""
        self.window.state.hdr.red_channel = -12.5
        self.window.state.hdr.green_channel = -3.25
        self.window._measure_finished(self.calibration(), "")
        self.assertAlmostEqual(self.window.state.hdr.red_channel, -12.5)
        self.assertAlmostEqual(self.window.state.hdr.green_channel, -3.25)

    def test_the_status_says_which_half_was_rejected(self):
        """This is the one outcome where the profile keeps a correction the run did not
        verify. A user who is not told has no way to tell it from success."""
        self.window._measure_finished(self.calibration(), "")
        text = self.window.status_label.text()
        self.assertIn("NOT updated", text)
        self.assertIn("not linear", text)

    def test_it_does_not_report_success(self):
        self.window._measure_finished(self.calibration(), "")
        self.assertNotIn("Ready", self.window.status_label.text())

    def test_a_solved_run_says_nothing_about_a_refusal(self):
        from sdr_hdr_profile_creator.measure import Calibration

        solved = Calibration(
            peak_nits=450.0, black_nits=0.0, white_xy=(0.3127, 0.3290),
            channel_gains=(0.8, 1.0, 0.97),
        )
        self.window._measure_finished(solved, "")
        self.assertNotIn("NOT updated", self.window.status_label.text())


class ShapingProvenanceTests(WindowTestCase):
    """A stored correction knows which tone settings it was measured through.

    The response records the code that was sent, and the shaping decides that code, so
    changing a tone control leaves it describing a pipeline that no longer exists. Not a
    fault -- the next run replaces it -- but a pass of reduced accuracy that is much
    better announced than discovered. Measured once at 0.84-0.92 of target through the
    midrange after switching SDR-in-HDR from Auto to Off, and 0.99-1.01 after one
    re-measure.
    """

    def measure(self):
        from sdr_hdr_profile_creator import measure as measure_mod
        weights = (0.2126, 0.7152, 0.0722)
        points = []
        levels = measure_mod.greyscale_levels(1000.0)
        for index, target in enumerate(levels):
            points.append(measure_mod.GreyPoint(
                index=index, target_nits=target, measured_nits=target * 0.9,
                x=0.3127, y=0.3290,
            ))
        columns = []
        for (x, y), weight in zip(((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)), weights):
            luminance = weight * 100.0
            columns.append((x / y * luminance, luminance, (1.0 - x - y) / y * luminance))
        result = measure_mod.Calibration(
            peak_nits=1000.0, black_nits=0.0005, white_xy=(0.3127, 0.3290),
            channel_gains=(1.0, 1.0, 1.0), greyscale=tuple(points),
            channel_xyz=tuple(columns), white_weights=weights,
        )
        self.window._measure_finished(result, "")

    def test_a_measurement_records_the_shaping_it_was_taken_through(self):
        self.measure()
        self.assertTrue(self.window.state.hdr.panel_response)
        self.assertTrue(self.window.state.hdr.panel_response_shaping)

    def test_nothing_is_said_while_the_shaping_is_unchanged(self):
        self.measure()
        self.assertFalse(self.window._shaping_moved_since_measuring())

    def test_changing_a_tone_control_is_noticed(self):
        self.measure()
        self.window.state.hdr.gamma = 2.6
        self.assertTrue(self.window._shaping_moved_since_measuring())

    def test_the_sdr_correction_counts_as_shaping(self):
        """The one that actually caught someone out."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        self.measure()
        self.window.state.hdr.sdr_gamma_correction = "Off"
        self.assertTrue(self.window._shaping_moved_since_measuring())

    def test_a_state_with_no_correction_never_complains(self):
        self.assertFalse(self.window._shaping_moved_since_measuring())

    def test_a_correction_predating_the_field_never_complains(self):
        """A state written before this existed says nothing either way, and guessing
        would put a warning on every profile that predates it."""
        self.measure()
        self.window.state.hdr.panel_response_shaping = ()
        self.window.state.hdr.gamma = 2.6
        self.assertFalse(self.window._shaping_moved_since_measuring())

    def test_changing_the_sdr_in_hdr_correction_says_the_ramp_is_now_stale(self):
        """The change that actually bit, and the one path that did not warn.

        Slider moves went through _control_changed, which checked. Switching SDR-in-HDR
        went through _select_gamma_correction, which did not -- so the setting that
        measured 0.84-0.92 of target through the midrange changed the shaping and said
        nothing about it.
        """
        self.measure()
        self.window._select_gamma_correction("Auto (Recommended)", "test")
        text = self.window.status_label.text()
        self.assertIn("different tone settings", text)
        self.assertIn("measure again", text)

    def test_the_warning_stays_quiet_when_the_shaping_has_not_moved(self):
        self.measure()
        self.window._set_status("nothing to add", "ok")
        self.window._warn_if_shaping_moved()
        self.assertEqual("nothing to add", self.window.status_label.text().split("·")[-1].strip())

    def test_adopting_panel_luminance_is_not_treated_as_a_shaping_change(self):
        """Peak reaches the profile's metadata and matrix, not the green intent curve
        the fingerprint samples: peak 1000 and peak 450 fingerprint identically. A
        warning here could only ever be a false one."""
        self.measure()
        self.window.state.hdr.peak_luminance_nits = 450.0
        self.assertFalse(self.window._shaping_moved_since_measuring())

    def test_resetting_clears_the_provenance_with_the_correction(self):
        self.measure()
        with mock.patch.object(
            app_module.QMessageBox, "question",
            return_value=app_module.QMessageBox.StandardButton.Yes,
        ):
            self.window._reset_all_controls()
        self.assertEqual(self.window.state.hdr.panel_response_shaping, ())


class MeasurementIntentTests(WindowTestCase):
    """The log records what the profile asks for, not only what PQ says.

    They are the same number only while every control is neutral. A reader that compares
    a reading against the PQ level reports the SDR-in-HDR correction as a 70% error, in
    exactly the part of the range someone would look at hardest.
    """

    def test_a_neutral_profile_intends_what_pq_says(self):
        self.window.state.hdr.sdr_gamma_correction = "Off"
        self.window.state.hdr.gamma = 2.2
        intent = self.window._measurement_intent(1000.0)
        self.assertTrue(intent)
        for key, nits in intent.items():
            asked = dict(
                (s.key, s.nits) for s in app_module.measure.plan(1000.0)
            )[key]
            self.assertAlmostEqual(nits / asked, 1.0, delta=0.02, msg=key)

    def test_the_sdr_correction_moves_the_intent_below_it(self):
        """Not a fault to be corrected -- a preference to be measured against."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        intent = self.window._measurement_intent(1000.0)
        asked = {s.key: s.nits for s in app_module.measure.plan(1000.0)}
        low = [k for k, v in asked.items() if k.startswith("grey-") and v < 2.0]
        self.assertTrue(low)
        for key in low:
            self.assertLess(intent[key], asked[key], key)

    def test_it_covers_every_greyscale_point_and_nothing_else(self):
        intent = self.window._measurement_intent(1000.0)
        grey = {s.key for s in app_module.measure.plan(1000.0) if s.key.startswith("grey-")}
        self.assertEqual(set(intent), grey)

    def test_a_stored_correction_does_not_move_the_target(self):
        """The intent is what the controls ask for. Including the measured correction
        makes it chase itself -- each pass lowers the code, which lowers the recorded
        intent, which makes the next pass lower it again -- and a report comparing
        against it measures a moving stick. Seen doing exactly that: 0.084 nits at the
        bottom of one run and 0.030 on the next, which read as a correction landing
        twice when the display was converging."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        before = self.window._measurement_intent(1000.0)

        from sdr_hdr_profile_creator import greyscale
        from sdr_hdr_profile_creator.gamma_correction import pq_eotf
        points = 24
        weights = (0.2126, 0.7152, 0.0722)
        samples = {name: [] for name in ("r", "g", "b")}
        for index in range(points):
            code = 0.05 + 0.9 * index / (points - 1)
            for key, weight in zip(("r", "g", "b"), weights):
                # Half the light it should give, so any correction built from it is large.
                samples[key].append((code, pq_eotf(code) * weight * 0.5))
        response = greyscale.PanelResponse(
            tuple(samples["r"]), tuple(samples["g"]), tuple(samples["b"]), weights
        )
        self.window.state.hdr.panel_response = greyscale.to_values(response)
        self.window.state.hdr.panel_response_weights = weights

        after = self.window._measurement_intent(1000.0)
        self.assertEqual(before, after, "the correction moved the target it is judged against")

    def test_it_is_empty_rather_than_wrong_when_the_curve_cannot_be_built(self):
        """A missing intent makes a reader fall back to PQ, which is merely
        uninformative. A wrong one is worse than none."""
        with mock.patch.object(app_module, "build_transform", side_effect=RuntimeError):
            self.assertEqual(self.window._measurement_intent(1000.0), {})


class RampReversalNoteTests(WindowTestCase):
    """A display that dims when asked for more light cannot be corrected by a curve.

    The fix is a setting on the monitor, so the message has to say that. "No correction
    was stored" on its own sends someone looking for the fault in the profile.
    """

    def calibration(self, reversal):
        from sdr_hdr_profile_creator.measure import Calibration

        return Calibration(
            peak_nits=450.0, black_nits=0.0, white_xy=(0.3127, 0.3290),
            channel_gains=(1.0, 1.0, 1.0), ramp_reversal=reversal,
        )

    def test_it_says_the_greyscale_was_not_corrected(self):
        self.window._measure_finished(self.calibration(0.42), "")
        text = self.window.status_label.text()
        self.assertIn("NOT corrected", text)
        self.assertIn("42%", text)

    def test_it_points_at_the_monitor_not_the_profile(self):
        self.window._measure_finished(self.calibration(0.42), "")
        self.assertIn("HDR preset", self.window.status_label.text())

    def test_peak_and_black_are_still_kept(self):
        """The ramp being uncorrectable does not unmeasure the two figures that were
        read directly."""
        self.window._measure_finished(self.calibration(0.42), "")
        self.assertAlmostEqual(self.window.state.hdr.peak_luminance_nits, 450.0, places=2)

    def test_an_ordinary_ramp_says_nothing_about_it(self):
        self.window._measure_finished(self.calibration(0.0), "")
        self.assertNotIn("NOT corrected", self.window.status_label.text())


class SustainedMeasurementTests(WindowTestCase):
    """The one figure in the profile that was still declared rather than measured.

    Calibrate Display fills it from the EDID's frame-average. On the panel this was
    built against that is 265.05 against the 243 the display actually holds -- about 9%
    optimistic, and invisible without measuring it.
    """

    def result(self, nits=243.0, readings=(300.0, 250.0, 243.0), settled=True):
        from sdr_hdr_profile_creator.measure import Sustained
        return Sustained(nits=nits, readings=tuple(readings), settled=settled)

    def test_the_button_is_offered_beside_the_number_it_sets(self):
        self.assertTrue(hasattr(self.window, 'sustained_button'))

    def test_a_settled_figure_is_adopted(self):
        self.window.state.hdr.peak_luminance_nits = 456.0
        self.window._sustained_finished(self.result(), '')
        self.assertAlmostEqual(self.window.state.hdr.full_frame_luminance_nits, 243.0, places=2)

    def test_it_cannot_exceed_peak(self):
        """A display that held more full-screen than it reached on a 3% window has told
        us one of the two readings is wrong, and the profile is not the place to record
        the contradiction."""
        self.window.state.hdr.peak_luminance_nits = 456.0
        self.window._sustained_finished(self.result(nits=900.0, readings=(900.0, 900.0)), '')
        self.assertAlmostEqual(self.window.state.hdr.full_frame_luminance_nits, 456.0, places=2)
        self.assertIn('cannot exceed', self.window.status_label.text())

    def test_a_figure_that_never_settled_is_called_an_upper_bound(self):
        """Reporting where it had got to as though it were where it stops would be the
        quietest possible way to overstate a panel."""
        self.window.state.hdr.peak_luminance_nits = 456.0
        self.window._sustained_finished(
            self.result(nits=260.0, readings=(400.0, 330.0, 290.0, 260.0), settled=False), ''
        )
        self.assertIn('upper bound', self.window.status_label.text())

    def test_a_cancelled_run_changes_nothing(self):
        before = self.window.state.hdr.full_frame_luminance_nits
        self.window._sustained_finished(None, '')
        self.assertEqual(self.window.state.hdr.full_frame_luminance_nits, before)
        self.assertIn('cancelled', self.window.status_label.text().lower())

    def test_a_failed_run_says_why_and_changes_nothing(self):
        before = self.window.state.hdr.full_frame_luminance_nits
        self.window._sustained_finished(None, 'the diffuser is closed')
        self.assertEqual(self.window.state.hdr.full_frame_luminance_nits, before)
        self.assertIn('diffuser', self.window.status_label.text())

    def test_it_says_what_it_replaced(self):
        self.window.state.hdr.peak_luminance_nits = 456.0
        self.window.state.hdr.full_frame_luminance_nits = 265.05
        self.window._sustained_finished(self.result(), '')
        text = self.window.status_label.text()
        self.assertIn('265.1', text)
        self.assertIn('243', text)


class AdditivityNoteTests(WindowTestCase):
    """Channels that do not add up are solved through, and said out loud."""

    def calibration(self, error):
        from sdr_hdr_profile_creator.measure import Calibration

        return Calibration(
            peak_nits=450.0, black_nits=0.0, white_xy=(0.3127, 0.3290),
            channel_gains=(0.79, 1.0, 0.996), additivity_error=error,
        )

    def test_a_large_departure_is_reported(self):
        self.window._measure_finished(self.calibration(1.147), "")
        text = self.window.status_label.text()
        self.assertIn("115%", text)
        self.assertIn("discarded", text)

    def test_the_correction_is_still_applied(self):
        """Reporting it is not refusing it. The trims have to actually move."""
        self.window._measure_finished(self.calibration(1.147), "")
        self.assertLess(self.window.state.hdr.red_channel, -15.0)

    def test_an_ordinary_run_says_nothing_about_it(self):
        self.window._measure_finished(self.calibration(0.02), "")
        self.assertNotIn("discarded", self.window.status_label.text())


class MeasuredResponseTests(WindowTestCase):
    """The greyscale response a meter run leaves behind, and what guards it.

    The arithmetic of the correction is proved against a synthetic panel in
    test_greyscale; what is checked here is only the wiring -- that a run stores one,
    that a run without a ramp does not throw one away, and that it never follows the
    user to a different display.
    """

    WEIGHTS = (0.2126, 0.7152, 0.0722)
    PRIMARY_XY = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))

    def channel_xyz(self, reference=100.0):
        columns = []
        for (x, y), weight in zip(self.PRIMARY_XY, self.WEIGHTS):
            luminance = weight * reference
            columns.append((x / y * luminance, luminance, (1.0 - x - y) / y * luminance))
        return tuple(columns)

    def calibration(self, ramp=True):
        from sdr_hdr_profile_creator import measure

        points = []
        if ramp:
            levels = measure.greyscale_levels(1000.0)
            for index, target in enumerate(levels):
                # Twelve percent dim, and drifting blue as level rises: an error that
                # varies with level is the only kind the curves can do anything about.
                drift = 0.0077 * index / (len(levels) - 1)
                points.append(
                    measure.GreyPoint(
                        index=index,
                        target_nits=target,
                        measured_nits=target * 0.88,
                        x=0.3127 - drift,
                        y=0.3290,
                    )
                )
        return measure.Calibration(
            peak_nits=1000.0,
            black_nits=0.0005,
            white_xy=(0.3127, 0.3290),
            channel_gains=(1.0, 1.0, 1.0),
            greyscale=tuple(points),
            channel_xyz=self.channel_xyz(),
            white_weights=self.WEIGHTS,
        )

    def test_a_measured_run_stores_a_response(self):
        self.window._measure_finished(self.calibration(), "")
        state = self.window.state.hdr
        self.assertTrue(state.panel_response)
        self.assertAlmostEqual(sum(state.panel_response_weights), 1.0, places=6)

    def test_the_stored_response_reaches_the_curves(self):
        """Storing it and never using it would be the quietest possible failure."""
        self.window._measure_finished(self.calibration(), "")
        transform = app_module.build_transform(self.window.state.hdr, hdr=True)
        self.assertNotEqual(transform.red, transform.blue)

    def test_a_run_without_a_ramp_keeps_the_response_it_cannot_replace(self):
        """A short check says nothing that contradicts the ramp already measured, and
        discarding a calibration because a later run was brief would be an expensive
        surprise with no warning attached."""
        self.window._measure_finished(self.calibration(), "")
        stored = self.window.state.hdr.panel_response
        self.assertTrue(stored)

        self.window._measure_finished(self.calibration(ramp=False), "")
        self.assertEqual(self.window.state.hdr.panel_response, stored)

    def test_the_response_is_stamped_with_the_display_it_came_from(self):
        self.window._measure_display_key = "PANEL-A"
        self.window._measure_finished(self.calibration(), "")
        self.assertEqual(self.window.state.hdr.panel_source_key, "PANEL-A")

    def test_resetting_the_sliders_clears_the_measurement_with_them(self):
        """They are one thing. The correction records what each channel delivered for
        the code the trims sent it, so keeping it while zeroing the trims pairs a
        measurement with a matrix that no longer exists -- measured on a PG32UCDM as a
        15% shortfall through the midrange after exactly that."""
        self.window._measure_finished(self.calibration(), "")
        self.assertTrue(self.window.state.hdr.panel_response)
        with mock.patch.object(
            app_module.QMessageBox, "question",
            return_value=app_module.QMessageBox.StandardButton.Yes,
        ):
            self.window._reset_all_controls()
        self.assertEqual(self.window.state.hdr.panel_response, ())
        self.assertEqual(self.window.state.hdr.panel_response_weights, ())
        self.assertIn("discarded", self.window.status_label.text())

    def test_the_dialog_says_the_measurement_goes_too(self):
        """Losing a four minute meter run is worth a sentence before it happens."""
        self.window._measure_finished(self.calibration(), "")
        with mock.patch.object(
            app_module.QMessageBox, "question",
            return_value=app_module.QMessageBox.StandardButton.Cancel,
        ) as ask:
            self.window._reset_all_controls()
        body = " ".join(str(arg) for arg in ask.call_args.args)
        self.assertIn("greyscale", body.lower())
        self.assertTrue(self.window.state.hdr.panel_response, "cancel must change nothing")

    def test_it_is_one_question_not_two(self):
        """Asking separately made keeping the mismatched pair the default."""
        self.window._measure_finished(self.calibration(), "")
        with mock.patch.object(
            app_module.QMessageBox, "question",
            return_value=app_module.QMessageBox.StandardButton.Yes,
        ) as ask:
            self.window._reset_all_controls()
        self.assertEqual(ask.call_count, 1)

    def test_nothing_is_asked_when_there_is_no_measurement(self):
        """A dialog about a correction that does not exist is just noise."""
        with mock.patch.object(
            app_module.QMessageBox, "question",
            return_value=app_module.QMessageBox.StandardButton.Yes,
        ) as ask:
            self.window._reset_all_controls()
        self.assertEqual(ask.call_count, 1)

    def test_the_status_line_says_the_greyscale_was_corrected(self):
        """A four minute run that silently reshapes the tone curve is worse than one
        that says what it did."""
        self.window._measure_finished(self.calibration(), "")
        self.assertIn("Greyscale corrected", self.window.status_label.text())

    def test_a_run_without_a_ramp_claims_no_greyscale_correction(self):
        self.window._measure_finished(self.calibration(ramp=False), "")
        self.assertNotIn("Greyscale corrected", self.window.status_label.text())

    def test_another_panel_does_not_inherit_it(self):
        """There is one HDR ModeState for every display. A transfer function measured
        on one panel is not stale on another, it is a correction for a display that is
        not there."""
        self.window._measure_display_key = "PANEL-A"
        self.window._measure_finished(self.calibration(), "")
        self.assertTrue(self.window.state.hdr.panel_response)

        from sdr_hdr_profile_creator.edid import PanelMetadata

        other = dataclasses.replace(
            self.display, device_path=r"\?\DISPLAY#OTHER#{guid}", friendly_name="Other"
        )
        panel = PanelMetadata(
            peak_nits=600.0, max_frame_average_nits=300.0, min_nits=0.001, supports_pq=True
        )
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel):
            self.assertTrue(self.window._prefill_luminance_from_panel(other))
        self.assertEqual(self.window.state.hdr.panel_response, ())
        self.assertEqual(self.window.state.hdr.panel_response_weights, ())


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

    def test_an_implausible_black_reading_is_named_as_a_meter_problem(self):
        """The run's own check bounds black only as a fraction of peak, so 2% of a
        1015-nit panel -- about 20 nits -- passes and lands in the profile's
        minimum-luminance field. No absolute threshold works, because one strict enough
        to catch that also refuses a good LCD; a contrast ratio does, since nothing this
        app is aimed at is worse than 200:1.

        Said rather than refused: the figure reaches one header field, the greyscale
        correction comes from the ramp, and the rest of the run is still worth keeping.
        """
        self.window._measure_finished(self.calibration(peak=450.0, black=20.0), "")
        text = self.window.status_label.text()
        self.assertIn("meter is probably seeing", text)
        self.assertIn("22:1", text, "the measured ratio should be quoted")
        self.assertAlmostEqual(self.window.state.hdr.minimum_luminance_nits, 20.0, places=3)

    def test_a_credible_black_reading_is_left_alone(self):
        """450 nits against 0.05 is 9,000:1 -- an ordinary OLED reading, and saying
        anything about it would train the user to ignore the message."""
        self.window._measure_finished(self.calibration(peak=450.0, black=0.05), "")
        self.assertNotIn("meter is probably seeing", self.window.status_label.text())

    def test_a_clamp_that_only_shows_up_after_composing_is_still_announced(self):
        """The normal case, and the one that said nothing.

        The clamp applies to the composed trims that get stored; the warning was gated
        on result.trims_exceed_range, which measure computes from this run's incremental
        gains alone. They agree only while no trim is in force -- so from the second pass
        onwards a real clamp went unmentioned, and the sentence that routes the user to
        the monitor's own colour temperature control was exactly the one suppressed.
        """
        self.window.state.hdr.blue_channel = -15.0
        self.window._measure_finished(self.calibration(gains=(1.0, 1.0, 0.85)), "")
        self.assertAlmostEqual(self.window.state.hdr.blue_channel, -25.0, places=3)
        text = self.window.status_label.text()
        self.assertIn("clamped", text)
        self.assertIn("B -25.0%", text, "the status must quote what was stored")
        self.assertNotIn("-27.8", text, "not the figure the profile does not carry")

    def test_an_incremental_overshoot_that_composes_back_inside_is_not_announced(self):
        """The other direction. A -30% correction against a +20% trim already in force
        composes to about -16%, which the profile carries perfectly well -- warning about
        it sends the user to change a monitor setting that is not the problem."""
        self.window.state.hdr.blue_channel = 20.0
        self.window._measure_finished(self.calibration(gains=(1.0, 1.0, 0.70)), "")
        self.assertGreater(self.window.state.hdr.blue_channel, -25.0)
        self.assertNotIn("clamped", self.window.status_label.text())

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
        """Peak luminance means nothing without it. This panel is rated 1015 nits, reads
        978 on the 3% window peak is measured at, and 456 on the 10% window the rest of
        the run uses -- three true numbers that only agree once the size is stated."""
        self.window._measure_finished(self.calibration(), "")
        self.assertIn("3% window", self.window.status_label.text())

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
class MeasurementBriefingTests(WindowTestCase):
    """What the user is told before a minute of black screen, and whether Esc works.

    Once the run starts the screen shows one patch on black and nothing else -- that is
    deliberate, since any text on the frame is light the meter integrates along with the
    patch -- and _set_status writes to a status bar this window covers. So everything the
    user needs has to be said beforehand, and the status line's promise of "Esc cancels"
    was invisible. It was also untrue: nothing focused the window, so Escape went to
    whatever had focus before and MeasureWindow.keyPressEvent was never reached.
    """

    def drive(self, answer):
        """Run _measure_with_meter far enough to reach the confirmation."""
        started = {}

        class FakeWindow(QObject):
            # The run now starts on the window's ready signal rather than immediately,
            # so the fake has to be a QObject and has to emit it. `closed` is wired to
            # the placement poll, which has to stop when the window does.
            ready = Signal()
            closed = Signal()
            failure = ""
            focused = False
            activated = False
            targeted = False

            def __init__(self, *args, **kwargs):
                super().__init__()
                FakeWindow.instance = self

            def show_placement_target(self, fraction=None):
                # On the class: drive() hands back the class, not an instance.
                type(self).target_fraction = fraction
                FakeWindow.targeted = True
                # Stands in for the user putting the meter down and pressing Enter.
                # Emitted synchronously, which only works because the app connects the
                # signal before showing the target -- and that ordering is the point.
                self.ready.emit()
                return True

            def setGeometry(self, *args):
                pass

            def showFullScreen(self):
                pass

            def begin(self):
                return True

            def close(self):
                pass

            def activateWindow(self):
                FakeWindow.activated = True

            def setFocus(self, *args):
                FakeWindow.focused = True

        def fake_start(*args, **kwargs):
            started["yes"] = True
            return mock.MagicMock(), mock.MagicMock()

        patches = [
            mock.patch.object(app_module, "find_spotread", return_value=Path("spotread")),
            mock.patch.object(app_module, "list_instruments",
                              return_value=[app_module_instrument()]),
            mock.patch.object(app_module, "read_panel_metadata", lambda _p: None),
            mock.patch.object(app_module.measure_view, "MeasureWindow", FakeWindow),
            mock.patch.object(app_module.measure_view, "start", fake_start),
            mock.patch.object(app_module.QMessageBox, "question", return_value=answer),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window._measure_with_meter()
        return FakeWindow, started.get("yes", False)

    def test_cancelling_the_briefing_measures_nothing(self):
        _window, started = self.drive(QMessageBox.StandardButton.Cancel)
        self.assertFalse(started, "a declined briefing still started the run")
        self.assertIn("cancelled", self.window.status_label.text().lower())

    def test_confirming_starts_the_run(self):
        _window, started = self.drive(QMessageBox.StandardButton.Ok)
        self.assertTrue(started)

    def test_the_target_is_drawn_at_the_smallest_window_in_the_plan(self):
        """Not the window most patches use. Peak is measured on a 3% window and the rest
        on 10%, and a meter centred well enough for the 10% box can still overhang one a
        third of its size -- a patch edge under the aperture reads part black and returns
        a luminance nothing downstream can tell is wrong. Drawing the target at the
        common size would put that failure on exactly the reading the small window
        exists to get right."""
        window, _started = self.drive(QMessageBox.StandardButton.Ok)
        smallest = min(
            step.window_fraction
            for step in app_module.measure.plan(self.window.state.hdr.peak_luminance_nits)
        )
        self.assertEqual(window.target_fraction, smallest)
        self.assertLess(window.target_fraction, app_module.measure.WINDOW_AREA_FRACTION)

    def test_the_surface_is_focused_so_escape_can_reach_it(self):
        """The abort path exists and is wired, but Escape has to arrive for any of it
        to run. Measured with a real MeasureWindow: without setFocus the focus widget
        is not the window and the closed signal never fires."""
        window, _started = self.drive(QMessageBox.StandardButton.Ok)
        self.assertTrue(window.focused, "the measurement surface never took focus")
        self.assertTrue(window.activated, "the measurement surface was never activated")

    def test_the_briefing_says_how_many_patches_and_how_to_stop(self):
        with mock.patch.object(
            app_module, "find_spotread", return_value=Path("spotread")
        ), mock.patch.object(
            app_module, "list_instruments", return_value=[app_module_instrument()]
        ), mock.patch.object(
            app_module, "read_panel_metadata", lambda _p: None
        ), mock.patch.object(
            app_module.QMessageBox, "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as ask:
            self.window._measure_with_meter()

        body = " ".join(str(arg) for arg in ask.call_args.args)
        steps = len(app_module.measure.plan(self.window.state.hdr.peak_luminance_nits))
        self.assertIn(str(steps), body, "the patch count is not stated")
        self.assertIn("Esc", body, "the way out is not stated")
        self.assertIn("still", body.lower(), "holding the meter still is not stated")


class LuminanceControlTests(WindowTestCase):
    """Peak, sustained and black, now that they have widgets.

    They reach the MHC2 header and the lumi tag directly, so a wrong one describes a
    display that does not exist -- and having nowhere to see them is why a stale peak
    carried over from another display, and a sustained figure left above peak after a
    measurement, were both invisible for as long as they lasted.
    """

    def widget(self, key):
        control = self.window.control_widgets.get(key)
        self.assertIsNotNone(control, f"no widget for {key}")
        return control

    def test_the_three_figures_are_editable(self):
        for key in ("peak_luminance_nits", "full_frame_luminance_nits", "minimum_luminance_nits"):
            with self.subTest(control=key):
                self.widget(key).set_value(300.0 if "minimum" not in key else 0.01, emit=True)
        self.assertAlmostEqual(300.0, self.window.state.hdr.peak_luminance_nits, places=3)
        self.assertAlmostEqual(0.01, self.window.state.hdr.minimum_luminance_nits, places=4)

    def test_a_typed_fraction_is_not_snapped_to_a_whole_number(self):
        """What the owner hit: the field took 1015 for a panel declaring 1015.24.

        set_value quantises to the declared step so the slider and the field agree, so
        a step of one nit silently rounded every typed value — including the panel's
        own figure, which meant the number on screen was not the number in the profile.
        The step now matches the two decimals the field displays.
        """
        for key, typed in (
            ("peak_luminance_nits", 1015.24),
            ("full_frame_luminance_nits", 265.05),
        ):
            with self.subTest(control=key):
                widget = self.widget(key)
                widget.set_value(typed, emit=True)
                self.assertAlmostEqual(typed, widget.value(), places=2)
                self.assertAlmostEqual(typed, getattr(self.window.state.hdr, key), places=2)

    def test_the_declared_figures_survive_being_shown(self):
        """Loading the panel's own numbers into the controls must not round them into
        something the display never claimed."""
        state = self.window.state.hdr
        state.peak_luminance_nits = 1015.2407657533865
        state.full_frame_luminance_nits = 265.0473286319483
        state.minimum_luminance_nits = 0.00015613083671716822
        self.window._load_mode_into_controls()
        self.assertAlmostEqual(1015.24, self.widget("peak_luminance_nits").value(), places=2)
        self.assertAlmostEqual(265.05, self.widget("full_frame_luminance_nits").value(), places=2)
        self.assertAlmostEqual(0.0002, self.widget("minimum_luminance_nits").value(), places=4)

    def test_black_keeps_the_precision_an_oled_needs(self):
        """0.00015613 is this panel's real black. Two decimals would store zero."""
        self.widget("minimum_luminance_nits").set_value(0.0002, emit=True)
        self.assertAlmostEqual(0.0002, self.window.state.hdr.minimum_luminance_nits, places=6)

    def test_lowering_peak_takes_sustained_with_it(self):
        """Sustained above peak is fiction, and ModeState.from_dict silently clamps it
        on the next load -- so leaving them disagreeing shows one thing in the editor
        and writes another into the profile."""
        self.widget("peak_luminance_nits").set_value(1000.0, emit=True)
        self.widget("full_frame_luminance_nits").set_value(600.0, emit=True)
        self.widget("peak_luminance_nits").set_value(400.0, emit=True)

        state = self.window.state.hdr
        self.assertAlmostEqual(400.0, state.peak_luminance_nits, places=3)
        self.assertAlmostEqual(400.0, state.full_frame_luminance_nits, places=3)
        self.assertAlmostEqual(
            400.0, self.widget("full_frame_luminance_nits").value(),
            places=3,
            msg="the state was reconciled but the widget still shows the old figure",
        )

    def test_raising_sustained_past_peak_raises_peak(self):
        """The other direction, because either is a reasonable thing to mean."""
        self.widget("peak_luminance_nits").set_value(500.0, emit=True)
        self.widget("full_frame_luminance_nits").set_value(900.0, emit=True)

        state = self.window.state.hdr
        self.assertAlmostEqual(900.0, state.full_frame_luminance_nits, places=3)
        self.assertAlmostEqual(900.0, state.peak_luminance_nits, places=3)
        self.assertAlmostEqual(900.0, self.widget("peak_luminance_nits").value(), places=3)

    def test_reconciling_does_not_count_as_a_second_edit(self):
        """The paired widget is updated with emit=False; a signal there would recurse
        straight back into this handler."""
        self.widget("peak_luminance_nits").set_value(1000.0, emit=True)
        self.widget("full_frame_luminance_nits").set_value(600.0, emit=True)
        before = self.window._edit_signature()
        self.widget("peak_luminance_nits").set_value(400.0, emit=True)
        self.assertNotEqual(before, self.window._edit_signature())
        # And the state is self-consistent rather than mid-recursion.
        self.assertLessEqual(
            self.window.state.hdr.full_frame_luminance_nits,
            self.window.state.hdr.peak_luminance_nits,
        )

    def test_a_calibration_still_overwrites_them(self):
        """They are editable, not authoritative: the panel and the meter both win."""
        self.widget("peak_luminance_nits").set_value(250.0, emit=True)
        panel = PanelMetadata(
            peak_nits=1015.24, max_frame_average_nits=265.05, min_nits=0.0002,
            supports_pq=True, primaries=(),
        )
        self.window.state.hdr.panel_source_key = ""
        self.window.state.hdr.minimum_luminance_nits = self.window.UNSET_LUMINANCE[0]
        self.window.state.hdr.peak_luminance_nits = self.window.UNSET_LUMINANCE[1]
        self.window.state.hdr.full_frame_luminance_nits = self.window.UNSET_LUMINANCE[2]
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel):
            self.assertTrue(self.window._prefill_luminance_from_panel(self.display))
        self.assertAlmostEqual(1015.24, self.window.state.hdr.peak_luminance_nits, places=1)


class LiveApplyPersistenceTests(WindowTestCase):
    """Live Apply is a preference, and was discarded at every launch.

    The constructor set state.live_mode = False after loading, so the guide's own
    step 4 had to be repeated every session: turn it on, close the app, find it off
    again with nothing to say why.
    """

    def test_the_setting_survives_a_reload(self):
        self.window.state.live_mode = True
        self.window._save_state_now()
        restored = app_module.ApplicationState.from_dict(
            json.loads((self.temp / "last_gui_state.json").read_text(encoding="utf-8"))
        )
        self.assertTrue(restored.live_mode)

    def test_the_switch_is_told_what_the_state_says(self):
        """It used to agree by accident, because the state was forced off."""
        self.assertEqual(self.window.state.live_mode, self.window.live_checkbox.isChecked())

    def test_restoring_it_does_not_count_as_switching_it_on(self):
        """Building the window must not install a profile before the user has touched
        anything, which an emitting setChecked would do."""
        self.assertEqual([], self.installed, "constructing the window installed a profile")


class PanelProvenanceTests(WindowTestCase):
    """One HDR ModeState serves every display, so its figures need an owner.

    Switching the target used to touch none of the colorimetry: _capture_panel_primaries
    ran only from _refresh_displays and _build_from_panel, and _prefill_luminance_from_panel
    refused to re-run once anything was set. So display B's profile was written with
    display A's peak, sustained, black and gamut -- and icc.py regenerates the MHC2
    header and lumi tag from state, so those override anything a base profile carried.
    """

    A_XY = (0.6836, 0.3047, 0.2441, 0.7090, 0.1436, 0.0557, 0.3135, 0.3291)
    B_XY = (0.6400, 0.3300, 0.3000, 0.6000, 0.1500, 0.0600, 0.3127, 0.3290)

    def panel(self, primaries, peak, sustained, black=0.0005):
        # credible is derived from supports_pq and the peak, not stored.
        return PanelMetadata(
            peak_nits=peak,
            max_frame_average_nits=sustained,
            min_nits=black,
            supports_pq=True,
            primaries=primaries,
        )

    def other_display(self):
        """A genuinely different display.

        Changing only ``key`` is not enough: stable_key falls back to
        friendly_name|gdi_name when device_path is empty, which the shared fixture
        leaves blank, so two displays built that way are the same display as far as
        every per-display lookup is concerned.
        """
        from dataclasses import replace

        other = replace(
            self.display,
            key="BBBB:CCCC:0:2",
            friendly_name="Second Monitor",
            gdi_name=r"\\.\DISPLAY2",
            device_path=r"\\?\DISPLAY#SEC0002#5&second#{guid}",
        )
        self.assertNotEqual(self.display.stable_key, other.stable_key)
        return other

    def reading_panel(self, primaries, peak, sustained):
        return mock.patch.object(
            app_module, "read_panel_metadata",
            lambda _path: self.panel(primaries, peak, sustained),
        )

    def test_switching_target_replaces_the_other_panels_figures(self):
        state = self.window.state.hdr
        state.panel_primaries = normalize_primaries(self.A_XY)
        state.panel_source_key = self.display.stable_key
        state.peak_luminance_nits = 1015.0
        state.full_frame_luminance_nits = 265.0
        state.minimum_luminance_nits = 0.0002

        other = self.other_display()
        with self.reading_panel(self.B_XY, 600.0, 350.0):
            self.window._adopt_panel_for(other)

        self.assertEqual(normalize_primaries(self.B_XY), state.panel_primaries)
        self.assertAlmostEqual(600.0, state.peak_luminance_nits, places=3)
        self.assertAlmostEqual(350.0, state.full_frame_luminance_nits, places=3)
        self.assertEqual(other.stable_key, state.panel_source_key)

    def test_a_measured_response_does_not_follow_you_to_a_display_with_no_readable_edid(self):
        """The one branch that skipped the provenance check.

        _prefill_luminance_from_panel clears the measured response, but only after it
        has read a credible EDID -- a display whose EDID cannot be read returns before
        that. _capture_panel_primaries then stamped panel_source_key with the new
        display anyway, via its DXGI fallback, so the previous display's measured
        greyscale curve stayed in state and was relabelled as this one's. From then on
        nothing could tell it was foreign, and it went into the profile.
        """
        state = self.window.state.hdr
        state.panel_source_key = "PANEL-A"
        state.panel_response = (0.1, 0.5, 0.1, 0.5, 0.1, 0.5) * 8
        state.panel_response_weights = (0.2126, 0.7152, 0.0722)
        state.panel_response_shaping = (0.1, 0.2, 0.3)

        other = self.other_display()
        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: None):
            self.window._adopt_panel_for(other)

        self.assertEqual((), state.panel_response, "another display's ramp was kept")
        self.assertEqual((), state.panel_response_weights)
        self.assertEqual((), state.panel_response_shaping)

    def test_reselecting_the_same_display_keeps_its_own_measured_response(self):
        """The other half: clearing must key on the display, not on the switch. A
        response taken on this display has to survive re-selecting it."""
        state = self.window.state.hdr
        state.panel_source_key = self.display.stable_key
        state.panel_response = (0.1, 0.5, 0.1, 0.5, 0.1, 0.5) * 8
        state.panel_response_weights = (0.2126, 0.7152, 0.0722)

        with mock.patch.object(app_module, "read_panel_metadata", lambda _p: None):
            self.window._adopt_panel_for(self.display)

        self.assertNotEqual((), state.panel_response, "this display's own ramp was discarded")

    def test_reselecting_the_same_display_leaves_its_measurements_alone(self):
        """A reading taken on this display outranks its own EDID; only figures whose
        recorded source is a different display are replaced."""
        state = self.window.state.hdr
        state.panel_primaries = normalize_primaries(self.A_XY)
        state.panel_source_key = self.display.stable_key
        state.peak_luminance_nits = 812.5      # measured, not declared
        state.full_frame_luminance_nits = 243.0
        state.minimum_luminance_nits = 0.0001

        with self.reading_panel(self.A_XY, 1015.0, 265.0):
            self.window._adopt_panel_for(self.display)

        self.assertAlmostEqual(812.5, state.peak_luminance_nits, places=3)
        self.assertAlmostEqual(243.0, state.full_frame_luminance_nits, places=3)

    def test_figures_with_no_recorded_owner_are_left_as_they_are(self):
        """Every state file written before the field existed deserialises with an empty
        source, and those figures are far likelier to be this display's than not."""
        state = self.window.state.hdr
        state.panel_source_key = ""
        state.peak_luminance_nits = 812.5
        state.full_frame_luminance_nits = 243.0
        state.minimum_luminance_nits = 0.0001

        with self.reading_panel(self.B_XY, 600.0, 350.0):
            self.window._adopt_panel_for(self.other_display())

        self.assertAlmostEqual(812.5, state.peak_luminance_nits, places=3)

    def test_an_unchanged_gamut_still_records_its_owner(self):
        """Two displays of one model share a gamut, so the early return on an identical
        value must not leave the previous display recorded as the source."""
        state = self.window.state.hdr
        state.panel_primaries = normalize_primaries(self.A_XY)
        state.panel_source_key = self.display.stable_key
        other = self.other_display()
        with self.reading_panel(self.A_XY, 1015.0, 265.0):
            self.window._capture_panel_primaries(other)
        self.assertEqual(other.stable_key, state.panel_source_key)

    def test_the_field_survives_a_round_trip_through_json(self):
        self.window.state.hdr.panel_source_key = "AAAA:BBBB:0:1"
        restored = ApplicationState.from_dict(self.window.state.to_dict())
        self.assertEqual("AAAA:BBBB:0:1", restored.hdr.panel_source_key)

    def test_a_state_file_predating_the_field_still_loads(self):
        payload = self.window.state.to_dict()
        payload["hdr"].pop("panel_source_key", None)
        restored = ApplicationState.from_dict(payload)
        self.assertEqual("", restored.hdr.panel_source_key)


class SilentInstallTests(WindowTestCase):
    """The install that reports success and copies nothing.

    On the owner's machine the two working profiles were owned by
    BUILTIN\\Administrators from a single earlier elevated run. remove_profile could
    not delete them, its failure was swallowed, InstallColorProfileW then returned
    TRUE without copying, and the digest of the payload was cached as though it had
    been installed. Every edit after that was written to LOCALAPPDATA and thrown away
    while the status bar said "Rebuilt the Off, On variant."
    """

    def stage(self, stale=b"OLD" + b"\x00" * 300):
        """A destination Windows refuses to replace, and a payload for it."""
        name = "Virtual_HDR_OSD_stale_On.icm"
        (self.color_dir / name).write_bytes(stale)
        payload = b"NEW" + b"\x00" * 300
        path = self.live_root / name
        return name, path, payload

    def freeze_windows(self):
        """remove_profile cannot delete, and install silently keeps the old bytes."""

        def refuse_remove(profile_name, display, mode):
            self.removed.append(profile_name)
            return False, "uninstall failed (Win32 5)"

        def no_op_install(path, display, mode, make_default=True):
            self.installed.append(path.name)
            self.associated.append(path.name)
            target = self.color_dir / path.name
            if not target.exists():
                shutil.copyfile(path, target)
            return path.name

        return (
            mock.patch.object(app_module, "remove_profile", refuse_remove),
            mock.patch.object(app_module, "install_and_associate_profile", no_op_install),
        )

    def test_a_frozen_destination_is_repaired_in_place(self):
        name, path, payload = self.stage()
        repaired = []

        def repair(profile_name, data):
            repaired.append(profile_name)
            (self.color_dir / profile_name).write_bytes(data)
            return True

        remove, install = self.freeze_windows()
        with remove, install, mock.patch.object(
            app_module, "overwrite_installed_profile", repair
        ):
            self.window._install_variant(self.display, path, payload)

        self.assertEqual([name], repaired, "the no-op install was not noticed")
        self.assertEqual(payload, (self.color_dir / name).read_bytes())

    def test_an_unrepairable_destination_raises_instead_of_reporting_success(self):
        name, path, payload = self.stage()
        remove, install = self.freeze_windows()
        with remove, install, mock.patch.object(
            app_module, "overwrite_installed_profile", lambda *_: False
        ):
            with self.assertRaises(app_module.WindowsColorError) as caught:
                self.window._install_variant(self.display, path, payload)

        message = str(caught.exception)
        self.assertIn(name, message, "the message should name the profile")
        self.assertIn("Run as Admin", message, "and the remedy that works")

    def test_a_failed_install_does_not_leave_the_cache_claiming_success(self):
        """The cache is consulted before re-hashing, so one false entry makes every
        later apply in the session a silent no-op too."""
        name, path, payload = self.stage()
        remove, install = self.freeze_windows()
        with remove, install, mock.patch.object(
            app_module, "overwrite_installed_profile", lambda *_: False
        ):
            with self.assertRaises(app_module.WindowsColorError):
                self.window._install_variant(self.display, path, payload)

        self.assertNotEqual(
            app_module.content_digest(payload),
            self.window._installed_digests.get(name),
            "a digest was cached for bytes Windows never accepted",
        )

    def test_an_install_that_really_lands_needs_no_repair(self):
        """The ordinary path must not start writing over files by hand."""
        name = "Virtual_HDR_OSD_fresh_On.icm"
        path = self.live_root / name
        payload = b"FRESH" + b"\x00" * 300
        repaired = []
        with mock.patch.object(
            app_module, "overwrite_installed_profile",
            lambda profile_name, data: repaired.append(profile_name) or True,
        ):
            self.window._install_variant(self.display, path, payload)

        self.assertEqual([], repaired, "a successful install was second-guessed")
        self.assertEqual(payload, (self.color_dir / name).read_bytes())
        self.assertEqual(
            app_module.content_digest(payload), self.window._installed_digests.get(name)
        )

    def test_the_verification_does_not_ask_the_cache_to_vouch_for_itself(self):
        """_install_variant checks with trust_cache=False. Consulting the cache there
        would let a stale entry from an earlier failed apply confirm the next one."""
        name = "Virtual_HDR_OSD_stale_On.icm"
        path = self.live_root / name
        payload = b"NEW" + b"\x00" * 300
        (self.color_dir / name).write_bytes(b"OLD" + b"\x00" * 300)
        # Poison the cache exactly as the old code would have.
        self.window._installed_digests[name] = app_module.content_digest(payload)

        remove, install = self.freeze_windows()
        with remove, install, mock.patch.object(
            app_module, "overwrite_installed_profile", lambda *_: False
        ):
            with self.assertRaises(app_module.WindowsColorError):
                self.window._install_variant(self.display, path, payload)


class ElevationButtonTests(WindowTestCase):
    """Restarting elevated closes this window, so the outcomes have to be told apart.

    Dismissing the UAC prompt is the ordinary case -- it is one keystroke and people do
    it by reflex -- and treating that as success would close the editor with no elevated
    copy coming to replace it, taking the unapplied edits along with it.
    """

    def press(self, answer, outcome):
        """Drive the handler, recording the order things happened in."""
        log = []
        result = app_module.elevation.RelaunchResult(outcome, "the prompt was dismissed")

        def relaunch(*_args, **_kwargs):
            log.append("relaunch")
            return result

        with mock.patch.object(QMessageBox, "question", return_value=answer), \
             mock.patch.object(app_module.elevation, "is_elevated", return_value=False), \
             mock.patch.object(app_module.elevation, "relaunch_elevated", relaunch), \
             mock.patch.object(type(self.window), "close",
                               lambda _self: log.append("close")), \
             mock.patch.object(type(self.window), "_save_state_now",
                               lambda _self: log.append("save")):
            self.window._relaunch_elevated()
        return log

    def test_confirming_saves_before_asking_windows_and_only_then_closes(self):
        """The elevated copy reads the state file while starting. Saving afterwards is
        a race it usually wins, and the edits are gone."""
        self.assertEqual(
            ["save", "relaunch", "close"],
            self.press(QMessageBox.StandardButton.Yes, app_module.elevation.Relaunch.STARTED),
        )

    def test_a_dismissed_prompt_leaves_the_window_open(self):
        log = self.press(
            QMessageBox.StandardButton.Yes, app_module.elevation.Relaunch.DECLINED
        )
        self.assertEqual(["save", "relaunch"], log)
        self.assertNotIn("close", log)

    def test_a_dismissed_prompt_is_reported_as_a_choice_not_an_error(self):
        self.press(QMessageBox.StandardButton.Yes, app_module.elevation.Relaunch.DECLINED)
        text = self.window.status_label.text()
        self.assertIn("dismissed", text.lower())
        self.assertNotIn("Error", text)

    def test_a_real_failure_is_reported_as_an_error(self):
        self.press(QMessageBox.StandardButton.Yes, app_module.elevation.Relaunch.FAILED)
        self.assertIn("Error", self.window.status_label.text())

    def test_cancelling_the_confirmation_touches_nothing(self):
        self.assertEqual(
            [], self.press(QMessageBox.StandardButton.Cancel,
                           app_module.elevation.Relaunch.STARTED)
        )

    def test_pressing_it_while_already_elevated_asks_nothing(self):
        with mock.patch.object(app_module.elevation, "is_elevated", return_value=True), \
             mock.patch.object(QMessageBox, "question") as ask, \
             mock.patch.object(app_module.elevation, "relaunch_elevated") as relaunch:
            self.window._relaunch_elevated()
        ask.assert_not_called()
        relaunch.assert_not_called()

    def test_the_button_is_withdrawn_once_the_app_is_elevated(self):
        """It is the whole reason the button is conditional: offering a way to gain
        rights the process already holds is noise in a row that has none to spare."""
        with mock.patch.object(app_module.elevation, "is_elevated", return_value=True):
            self.window._refresh_elevation_button()
        self.assertTrue(self.window.elevate_button.isHidden())
        with mock.patch.object(app_module.elevation, "is_elevated", return_value=False):
            self.window._refresh_elevation_button()
        self.assertFalse(self.window.elevate_button.isHidden())

    def test_the_access_denied_message_names_the_button_that_fixes_it(self):
        """The remedy used to be "close the app and relaunch it as administrator",
        which is a chore rather than a remedy now that there is a button."""
        from sdr_hdr_profile_creator import windows_api

        with mock.patch.object(windows_api.ctypes, "get_last_error", return_value=5), \
             mock.patch.object(windows_api.ctypes, "FormatError", return_value="Access is denied."):
            error = windows_api._format_windows_error("InstallColorProfileW failed")
        self.assertIn(self.window.elevate_button.text(), str(error))


class LayoutMembershipTests(WindowTestCase):
    """A control with a parent but no layout is still a real widget.

    Qt gives it the parent's origin and draws it over whatever is already there,
    and nothing warns. That is how the HDR switch came to sit on top of the
    "1 - Target Display" label: one line reading ``display_row.addWidget(
    self.hdr_switch)`` went missing, the switch kept its parent, and the whole
    suite stayed green, because no test asked where any control had ended up.

    ``titleBar`` is the one real exception -- qfluentwidgets positions the
    frameless title bar itself rather than through a layout.
    """

    LAID_OUT_ELSEWHERE = {"titleBar"}

    def walk(self):
        """Every widget and every layout reachable from the window, in one pass.

        Tab pages and scroll-area contents are held by their container rather
        than by a layout item, so they have to be followed by hand or half the
        window looks orphaned.
        """
        from PySide6.QtWidgets import QScrollArea, QStackedWidget, QTabWidget

        widgets, layouts, stack = set(), [], [self.window]
        while stack:
            widget = stack.pop()
            if widget is None or id(widget) in widgets:
                continue
            widgets.add(id(widget))
            if isinstance(widget, (QTabWidget, QStackedWidget)):
                stack.extend(widget.widget(i) for i in range(widget.count()))
            elif isinstance(widget, QScrollArea):
                stack.append(widget.widget())
            pending = [widget.layout()] if widget.layout() is not None else []
            while pending:
                layout = pending.pop()
                layouts.append(layout)
                for index in range(layout.count()):
                    item = layout.itemAt(index)
                    if item.widget() is not None:
                        stack.append(item.widget())
                    if item.layout() is not None:
                        pending.append(item.layout())
        return widgets, layouts

    def test_every_named_control_reaches_a_layout(self):
        from PySide6.QtWidgets import QWidget

        widgets, _ = self.walk()
        orphans = sorted(
            name
            for name, control in vars(self.window).items()
            if isinstance(control, QWidget)
            and name not in self.LAID_OUT_ELSEWHERE
            and id(control) not in widgets
        )
        self.assertEqual(
            [], orphans,
            "given a parent but never added to a layout, so drawn at the "
            f"parent's origin on top of other controls: {orphans}",
        )

    def test_the_hdr_switch_sits_between_the_display_picker_and_refresh(self):
        """Being in *a* layout is not enough; it has to be in the right one."""
        from PySide6.QtWidgets import QAbstractButton

        _, layouts = self.walk()
        row = next(
            (layout for layout in layouts
             if any(layout.itemAt(i).widget() is self.window.hdr_switch
                    for i in range(layout.count()))),
            None,
        )
        self.assertIsNotNone(row, "the HDR switch is not in any layout")
        order = [row.itemAt(i).widget() for i in range(row.count())]
        refresh = [
            widget for widget in order
            if isinstance(widget, QAbstractButton) and widget.text() == "Refresh"
        ]
        self.assertIn(self.window.display_combo, order, "not the target-display row")
        self.assertTrue(refresh, "Refresh is not in the target-display row")
        self.assertLess(
            order.index(self.window.display_combo), order.index(self.window.hdr_switch),
            "the HDR switch belongs after the display it applies to",
        )
        self.assertLess(
            order.index(self.window.hdr_switch), order.index(refresh[0]),
            "the HDR switch belongs before the row's buttons",
        )


class ControlRowFitTests(WindowTestCase):
    """Every control must fit at the smallest size the window allows itself.

    Adding the meter button pushed row 3 to ten controls needing 2572px on one
    line. At the shipped width the switches truncated to "Automatic Moc" and
    "Keep Profile Lo" and the meter button lost its ellipsis -- and nothing
    failed, because Qt elides silently. Splitting the row was not enough on its
    own: the two halves still wanted 1196 and 1626 against a 1080 minimum.

    Every width measured here is an OFFSCREEN width, and offscreen has no font
    database: Qt advances a flat ~12px per character for tofu. Under
    QT_QPA_PLATFORM=offscreen, which is what line 21 sets and what the suite
    runs under, the switch row measures 1028 and the action row 1054 against a
    1080 minimum -- 52px and 26px of headroom. The same rows under
    QT_QPA_PLATFORM=windows with Segoe UI 9pt measure 584 and 611: 496px and
    469px. So the "about 30px" this used to claim was an artefact, wrong by
    roughly sixteen times about the shipped UI.

    The guard is kept anyway, because 12px a character is wider than the widest
    Segoe UI 9pt ASCII glyph (W, 11px). The offscreen total therefore
    over-estimates, so this can only ever fail a row that would have fitted --
    never pass one that elides. That is the direction a tripwire should err in,
    and the surplus doubles as headroom for text scaling and longer localised
    labels.

    If this ever blocks a control you want to add, re-measure with
    QT_QPA_PLATFORM=windows before splitting a row.
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
class PanelPrimarySourceTests(WindowTestCase):
    """Where the panel's gamut is read from, and why not from DXGI.

    DXGI_OUTPUT_DESC1 reports whatever ICC profile is associated, not the panel.
    On the development display it answered (0.6746, 0.3144) for red under one
    profile and (0.6486, 0.3312) under the next -- each matching that profile's
    colorant tags to four decimals -- while the EDID said (0.6836, 0.3047)
    throughout. A profile written from DXGI's answer becomes its next answer, and
    this app watched its own BT.709 output come back as the panel's gamut.
    """

    EDID_PRIMARIES = (0.68359375, 0.3046875, 0.244140625, 0.708984375,
                      0.1435546875, 0.0556640625, 0.3134765625, 0.3291015625)
    # What DXGI says while our own BT.709-ish profile is applied.
    ECHOED = (0.648591, 0.33118, 0.314107, 0.589249, 0.152411, 0.059645, 0.312725, 0.329022)

    def use_edid(self, primaries=None):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        panel = PanelMetadata(
            peak_nits=1015.24, max_frame_average_nits=265.05, min_nits=0.000156,
            supports_pq=True,
            primaries=self.EDID_PRIMARIES if primaries is None else primaries,
        )
        patcher = mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_dxgi(self, primaries=None):
        values = self.ECHOED if primaries is None else primaries
        capability = SimpleNamespace(
            device_name=r"\\.\DISPLAY1", is_hdr=True,
            red_primary=values[0:2], green_primary=values[2:4],
            blue_primary=values[4:6], white_point=values[6:8],
        )
        patcher = mock.patch.object(
            app_module, "capability_for_device_name", lambda _n: capability
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_edid_outranks_dxgi(self):
        self.use_edid()
        self.use_dxgi()
        self.window._capture_panel_primaries(self.display)
        self.assertEqual(self.window.state.hdr.panel_primaries, self.EDID_PRIMARIES)

    def test_the_echoed_values_are_never_adopted_while_edid_answers(self):
        self.use_edid()
        self.use_dxgi()
        self.window._capture_panel_primaries(self.display)
        self.assertNotEqual(self.window.state.hdr.panel_primaries, self.ECHOED)

    def test_a_profile_already_written_from_the_echo_is_corrected(self):
        """The loop has to be breakable, not merely avoidable."""
        self.window.state.hdr.panel_primaries = self.ECHOED
        self.use_edid()
        self.use_dxgi()
        self.assertTrue(self.window._capture_panel_primaries(self.display))
        self.assertEqual(self.window.state.hdr.panel_primaries, self.EDID_PRIMARIES)

    def test_dxgi_is_still_used_when_the_edid_cannot_be_read(self):
        """A possibly-echoed answer still beats the generic BT.2020 table."""
        self.use_edid(primaries=())
        self.use_dxgi()
        self.window._capture_panel_primaries(self.display)
        self.assertEqual(self.window.state.hdr.panel_primaries, self.ECHOED)

    def test_dxgi_is_not_consulted_in_sdr(self):
        """With HDR off the same panel reports BT.709, which would describe a
        wide-gamut display as sRGB."""
        self.display.advanced_color_kind = "SDR"
        self.window._current_display_snapshot = self.display
        self.use_edid(primaries=())
        self.use_dxgi()
        self.window._capture_panel_primaries(self.display)
        self.assertEqual(self.window.state.hdr.panel_primaries, ())


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class MeasurementCompositionTests(WindowTestCase):
    """A second measurement must verify the first, not undo it."""

    def calibration(self, gains=(1.0, 1.0, 1.0), white_xy=(0.3127, 0.3290)):
        from sdr_hdr_profile_creator.measure import Calibration

        return Calibration(
            peak_nits=450.49, black_nits=0.0,
            white_xy=white_xy, channel_gains=gains,
        )

    def set_trims(self, red, green, blue):
        self.window.state.hdr.red_channel = red
        self.window.state.hdr.green_channel = green
        self.window.state.hdr.blue_channel = blue

    def trims(self):
        state = self.window.state.hdr
        return (state.red_channel, state.green_channel, state.blue_channel)

    def test_a_neutral_re_measure_leaves_an_applied_correction_in_place(self):
        """The display reads neutral *because* the correction is working. Storing
        the (1, 1, 1) that implies would throw it away and the display would go
        straight back to where it started."""
        self.set_trims(-21.323, -1.576, 0.0)
        self.window._measure_finished(self.calibration(), "")
        red, green, blue = self.trims()
        self.assertAlmostEqual(red, -21.323, places=2)
        self.assertAlmostEqual(green, -1.576, places=2)
        self.assertAlmostEqual(blue, 0.0, places=2)

    def test_a_residual_error_tightens_the_correction(self):
        self.set_trims(-21.323, -1.576, 0.0)
        self.window._measure_finished(
            self.calibration(gains=(0.97, 1.0, 1.0), white_xy=(0.3180, 0.3291)), ""
        )
        self.assertLess(self.trims()[0], -21.323)

    def test_the_first_measurement_on_an_uncorrected_display_applies_in_full(self):
        self.set_trims(0.0, 0.0, 0.0)
        self.window._measure_finished(
            self.calibration(gains=(0.7865, 0.9833, 1.0), white_xy=(0.3300, 0.3291)), ""
        )
        red, green, blue = self.trims()
        self.assertAlmostEqual(red, -21.35, places=1)
        self.assertAlmostEqual(green, -1.67, places=1)

    def test_a_verified_run_says_so(self):
        self.set_trims(-21.323, -1.576, 0.0)
        self.window._measure_finished(self.calibration(), "")
        self.assertIn("verified", self.window.status_label.text().lower())

    def test_an_unverified_run_reports_how_far_off_it_still_is(self):
        self.set_trims(-21.323, -1.576, 0.0)
        self.window._measure_finished(
            self.calibration(gains=(0.97, 1.0, 1.0), white_xy=(0.3300, 0.3291)), ""
        )
        text = self.window.status_label.text().lower()
        self.assertIn("still", text)

    def test_the_colour_temperature_is_reported(self):
        """A number a person can recognise, unlike a chromaticity pair."""
        self.set_trims(0.0, 0.0, 0.0)
        self.window._measure_finished(
            self.calibration(gains=(0.79, 0.98, 1.0), white_xy=(0.3300, 0.3291)), ""
        )
        self.assertIn("5,616K", self.window.status_label.text())


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


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class PeakPatchTargetTests(WindowTestCase):
    """The peak patch has to ask for more than the display last managed.

    Driving it at the stored peak makes the measurement self-fulfilling. The
    first real run asked for the EDID's 1015 nits and found 450; every run after
    asked for 450 and found 450, which confirms only that the display can produce
    what it was told to and says nothing about its ceiling.
    """

    def panel(self, peak=1015.24):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        return PanelMetadata(
            peak_nits=peak, max_frame_average_nits=265.05, min_nits=0.000156,
            supports_pq=True, primaries=(),
        )

    def requested_peak(self, stored, panel):
        captured = {}

        def fake_start(window, read, peak, on_progress, on_finished, on_reading=None,
                       **kwargs):
            captured["peak"] = peak
            raise RuntimeError("stop before the run begins")

        class FakeWindow(QObject):
            """The real one needs a D3D swapchain, which offscreen Qt cannot give."""

            # The run starts on this rather than immediately, so the stand-in needs it.
            ready = Signal()
            closed = Signal()
            failure = ""

            def __init__(self, *args, **kwargs):
                super().__init__()

            def setGeometry(self, *args):
                pass

            def showFullScreen(self):
                pass

            def begin(self):
                return True

            def close(self):
                pass

            def activateWindow(self):
                pass

            def setFocus(self, *args):
                # Recorded because Escape is unreachable without it; see
                # MeasureFocusTests.
                type(self).focused = True

            def show_placement_target(self, fraction=None):
                # On the class: drive() hands back the class, not an instance.
                type(self).target_fraction = fraction
                # The target is shown, then the run waits for the user to confirm the
                # meter is on it. Confirm immediately so the sequence still runs here.
                self.ready.emit()
                return True

        self.window.state.hdr.peak_luminance_nits = stored
        patches = [
            mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel),
            mock.patch.object(app_module, "find_spotread", return_value=Path("spotread")),
            mock.patch.object(app_module, "list_instruments",
                              return_value=[app_module_instrument()]),
            mock.patch.object(app_module.measure_view, "MeasureWindow", FakeWindow),
            mock.patch.object(app_module.measure_view, "start", fake_start),
            # The run is preceded by a confirmation, because once the surface is up
            # there is nowhere left to say how long it takes or how to stop it.
            mock.patch.object(
                app_module.QMessageBox, "question",
                return_value=app_module.QMessageBox.StandardButton.Ok,
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.window._measure_with_meter()
        except RuntimeError:
            pass
        return captured.get("peak")

    def test_asks_for_the_panels_declared_peak_not_the_last_measurement(self):
        peak = self.requested_peak(stored=450.49, panel=self.panel(1015.24))
        self.assertAlmostEqual(peak, 1015.24, places=2)

    def test_a_measurement_above_the_panels_claim_is_still_honoured(self):
        """A panel that beats its own specification must not be capped to it."""
        peak = self.requested_peak(stored=1200.0, panel=self.panel(1015.24))
        self.assertAlmostEqual(peak, 1200.0, places=2)

    def test_an_unreadable_panel_falls_back_to_the_stored_figure(self):
        peak = self.requested_peak(stored=450.49, panel=None)
        self.assertAlmostEqual(peak, 450.49, places=2)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class FullscreenSurfaceLifetimeTests(WindowTestCase):
    """Only one fullscreen swapchain at a time, and it must be released.

    Both the pattern view and the measurement window are fullscreen
    WA_PaintOnScreen windows owning a D3D swapchain on the display. The pattern
    window's reference was assigned and never closed anywhere, so every open
    leaked one; after enough of them the display stops handing them out and the
    next open appears to freeze.
    """

    class FakeSurface:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def test_opening_the_patterns_twice_does_not_leak_the_first(self):
        first = self.FakeSurface()
        self.window._pattern_window = first
        self.window._close_fullscreen_surfaces()
        self.assertTrue(first.closed)
        self.assertIsNone(self.window._pattern_window)

    def test_a_measurement_window_is_released_too(self):
        surface = self.FakeSurface()
        self.window._measure_window = surface
        self.window._close_fullscreen_surfaces()
        self.assertTrue(surface.closed)
        self.assertIsNone(self.window._measure_window)

    def test_closing_the_app_releases_a_fullscreen_surface(self):
        """Otherwise it outlives the window that could have closed it, leaving a
        black screen with no way back."""
        from PySide6.QtGui import QCloseEvent

        surface = self.FakeSurface()
        self.window._pattern_window = surface
        self.window.closeEvent(QCloseEvent())
        self.assertTrue(surface.closed)

    def test_the_patterns_refuse_to_open_during_a_measurement(self):
        self.window._measure_window = self.FakeSurface()
        self.window._open_pattern_view()
        self.assertIn("measurement", self.window.status_label.text().lower())

    def test_a_measurement_refuses_to_start_while_the_patterns_are_open(self):
        self.window._pattern_window = self.FakeSurface()
        with mock.patch.object(app_module, "find_spotread") as finder:
            self.window._measure_with_meter()
        finder.assert_not_called()
        self.assertIn("patterns", self.window.status_label.text().lower())

    def test_a_window_qt_already_destroyed_does_not_raise(self):
        class Destroyed:
            def close(self):
                raise RuntimeError("wrapped C/C++ object has been deleted")

        self.window._pattern_window = Destroyed()
        self.window._close_fullscreen_surfaces()
        self.assertIsNone(self.window._pattern_window)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class CalibrateDisplayTests(WindowTestCase):
    """One action, because three decisions were three too many.

    Reaching a correct profile previously meant finding one entry among every
    profile installed on the machine, then applying, then knowing those were
    separate steps. Everything it needs is already declared by the panel.
    """

    PRIMARIES = (0.68359375, 0.3046875, 0.244140625, 0.708984375,
                 0.1435546875, 0.0556640625, 0.3134765625, 0.3291015625)

    def use_panel(self, primaries=None, peak=1015.24, frame_average=265.05):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        panel = PanelMetadata(
            peak_nits=peak, max_frame_average_nits=frame_average,
            min_nits=0.000156, supports_pq=True,
            primaries=self.PRIMARIES if primaries is None else primaries,
        )
        patcher = mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_button_exists(self):
        self.assertTrue(hasattr(self.window, "calibrate_button"))

    def test_one_click_reads_the_panel_and_installs(self):
        self.use_panel()
        self.window._calibrate_display()
        state = self.window.state.hdr
        self.assertEqual(state.panel_primaries, self.PRIMARIES)
        self.assertAlmostEqual(state.peak_luminance_nits, 1015.24, places=2)
        self.assertAlmostEqual(state.full_frame_luminance_nits, 265.05, places=2)
        self.assertTrue(self.installed, "nothing was installed")

    def test_it_builds_from_the_panel_rather_than_any_installed_profile(self):
        """The whole point: no choosing between fifty profiles."""
        self.use_panel()
        self.window.state.hdr.base_profile = str(self.color_dir / "BaseCalibration.icm")
        self.window._calibrate_display()
        self.assertEqual(self.window.state.hdr.base_profile, "")

    def test_the_sustained_figure_is_the_panels_not_its_peak(self):
        """Windows HDR Calibration writes peak into this field -- 1000 against the
        265 this panel declares -- which tells Windows to tone-map for a display
        that cannot exist."""
        self.use_panel()
        self.window._calibrate_display()
        state = self.window.state.hdr
        self.assertLess(state.full_frame_luminance_nits, state.peak_luminance_nits / 2)

    def test_it_turns_hdr_on_rather_than_asking_the_user_to(self):
        self.display.advanced_color_kind = "SDR"
        self.window._current_display_snapshot = self.display
        self.use_panel()
        self.window._calibrate_display()
        self.assertIn((self.display.key, True), self.hdr_switch_calls)

    def test_a_panel_that_declares_nothing_says_so_rather_than_inventing(self):
        self.use_panel(primaries=())
        with mock.patch.object(app_module, "capability_for_device_name", lambda _n: None):
            self.window._calibrate_display()
        self.assertIn("could not read", self.window.status_label.text().lower())

    def test_the_result_is_reported_in_the_panels_own_figures(self):
        self.use_panel()
        self.window._calibrate_display()
        text = self.window.status_label.text()
        self.assertIn("1015", text)
        self.assertIn("265", text)

    def test_it_says_whether_the_profile_is_locked(self):
        self.use_panel()
        self.watchdog_running = True
        self.window._calibrate_display()
        self.assertIn("locked in place", self.window.status_label.text())

    def test_it_offers_the_lock_when_the_watchdog_is_not_running(self):
        self.use_panel()
        self.watchdog_running = False
        self.window._calibrate_display()
        self.assertIn("Lock Profile", self.window.status_label.text())


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class DeferredCallbackTests(WindowTestCase):
    """A deferred callback must not outlive the widgets it touches.

    Pressing Calibrate Display with HDR off schedules a continuation 1.2 seconds
    later. Closing the window inside that window ran it against deleted widgets:
    "Internal C++ object (ComboBox) already deleted", raised from inside a Qt
    timer -- where Qt prints the traceback and carries on, so the suite stayed
    green while an unhandled exception went through it on every run.
    """

    def test_every_deferred_callback_passes_a_context_object(self):
        """QTimer.singleShot cancels the call when its context is destroyed.
        Without one it fires regardless, which is the bug."""
        import re
        from pathlib import Path

        source = Path(app_module.__file__).read_text(encoding="utf-8")
        calls = re.findall(r"QTimer\.singleShot\(\s*\d+\s*,\s*([^\n]{0,20})", source)
        self.assertTrue(calls, "no deferred callbacks found; has the API changed?")
        for call in calls:
            with self.subTest(call=call.strip()[:40]):
                self.assertTrue(
                    call.lstrip().startswith("self,"),
                    f"singleShot without a context object: {call.strip()[:40]}",
                )

    # There is deliberately no test calling the continuation directly on a
    # destroyed window. That bypasses the timer, which is the only path that
    # exists, and asserts a guarantee the context object does not make: Qt
    # cancels the *scheduled call*, it does not make the method safe to invoke
    # by hand afterwards. A test that did was written, failed, and was wrong.


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class CalibrationDurabilityTests(WindowTestCase):
    """Findings from a review pass, each of which silently produced a wrong profile."""

    PANEL_XY = (0.68359375, 0.3046875, 0.244140625, 0.708984375,
                0.1435546875, 0.0556640625, 0.3134765625, 0.3291015625)

    def use_panel(self, primaries=None, peak=1015.24, frame_average=265.05, pq=True):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        panel = PanelMetadata(
            peak_nits=peak, max_frame_average_nits=frame_average, min_nits=0.000156,
            supports_pq=pq, primaries=self.PANEL_XY if primaries is None else primaries,
        )
        patcher = mock.patch.object(app_module, "read_panel_metadata", lambda _p: panel)
        patcher.start()
        self.addCleanup(patcher.stop)
        return panel

    # -- reporting the truth ---------------------------------------------------

    def test_a_failed_install_is_not_reported_as_success(self):
        """_apply_mode_profile catches everything internally, so a caller that
        expected an exception got silence and painted a green line over the
        error it had already written."""
        self.use_panel()
        with mock.patch.object(self.window, "_apply_mode_profile", return_value=False):
            self.window._calibrate_display()
        self.assertNotIn("calibrated", self.window.status_label.text().lower())

    def test_apply_reports_failure_when_the_display_is_not_in_hdr(self):
        self.display.advanced_color_kind = "SDR"
        self.window._current_display_snapshot = self.display
        self.assertFalse(self.window._apply_mode_profile("test"))

    def test_apply_reports_success_when_it_installs(self):
        self.assertTrue(self.window._apply_mode_profile("test"))

    def test_a_panel_that_declares_no_luminance_is_not_called_panel_data(self):
        """Otherwise the defaults 1000/400 are reported as the display's own."""
        self.use_panel(peak=0.0, frame_average=0.0, pq=False)
        self.window._calibrate_display()
        text = self.window.status_label.text().lower()
        self.assertIn("defaults rather than measured", text)

    # -- not inheriting another display's gamut --------------------------------

    def test_primaries_do_not_survive_a_display_that_cannot_be_read(self):
        """panel_primaries is one value shared by every display. Left in place,
        a second monitor is calibrated with the first one's gamut."""
        self.window.state.hdr.panel_primaries = self.PANEL_XY
        self.use_panel(primaries=())
        with mock.patch.object(app_module, "capability_for_device_name", lambda _n: None):
            self.window._build_from_panel(self.window._selected_binding())
        self.assertEqual(self.window.state.hdr.panel_primaries, ())

    def test_the_could_not_read_branch_is_reachable(self):
        """It was unreachable once any display had ever been read."""
        self.window.state.hdr.panel_primaries = self.PANEL_XY
        self.use_panel(primaries=())
        with mock.patch.object(app_module, "capability_for_device_name", lambda _n: None):
            self.window._build_from_panel(self.window._selected_binding())
        self.assertIn("could not read", self.window.status_label.text().lower())

    # -- surviving a display mode change ---------------------------------------

    def test_an_sdr_to_hdr_transition_does_not_wipe_a_panel_calibration(self):
        """A game that flips the display to SDR and back ran the load_controls
        path, which replaced the pin and overwrote the whole HDR state with what
        could be estimated from a third-party profile: generic primaries and
        1000/400 nits."""
        self.use_panel()
        self.window._calibrate_display()
        before = (
            self.window.state.hdr.panel_primaries,
            self.window.state.hdr.peak_luminance_nits,
            self.window.state.hdr.full_frame_luminance_nits,
        )
        self.assertEqual(before[0], self.PANEL_XY)

        # What the SDR->HDR branch does: adopt whatever Windows now reports.
        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.window._capture_current_hdr_base(self.display, load_controls=True)

        after = (
            self.window.state.hdr.panel_primaries,
            self.window.state.hdr.peak_luminance_nits,
            self.window.state.hdr.full_frame_luminance_nits,
        )
        self.assertEqual(after, before)

    def test_the_from_panel_pin_survives_the_transition(self):
        self.use_panel()
        self.window._calibrate_display()
        self.default_profiles["HDR"] = "BaseCalibration.icm"
        self.window._capture_current_hdr_base(self.display, load_controls=True)
        binding = self.window._selected_binding()
        self.assertEqual(binding.hdr_profile, app_module.HDR_FROM_PANEL)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class GuideProgressTests(WindowTestCase):
    """The guide reports progress; it has to be able to see the work being done."""

    PANEL_XY = (0.68359375, 0.3046875, 0.244140625, 0.708984375,
                0.1435546875, 0.0556640625, 0.3134765625, 0.3291015625)

    def calibrated(self):
        """The state Calibrate Display actually leaves behind."""
        self.window.state.hdr.base_profile = ""
        self.window.state.hdr.imported_profile = ""
        self.window.state.hdr.panel_primaries = self.PANEL_XY
        binding = self.window._selected_binding()
        self.assertIsNotNone(binding, "the fixture display has no binding")
        binding.hdr_profile = app_module.HDR_FROM_PANEL
        return binding

    def test_calibrating_from_the_panel_satisfies_the_step_that_asks_for_it(self):
        """_build_from_panel clears base_profile and imported_profile by design,
        so a check that tested only for those reported the guide's own Calibrate
        Display step as outstanding however many times it was pressed."""
        self.calibrated()
        satisfied, detail = self.window._check_profile_imported()
        self.assertTrue(satisfied, detail)
        self.assertIn("Built from this display", detail)

    def test_panel_data_alone_does_not_tick_the_step_off(self):
        """The regression this step exists to catch.

        _capture_panel_primaries runs on every _refresh_displays, including the one in
        __init__, so panel_primaries is populated on a genuinely first launch -- before
        anything has been built, installed or associated. Gating on it reported the step
        green immediately and invited a first-time user to click straight past the only
        step in the guide that does any work.
        """
        self.window.state.hdr.base_profile = ""
        self.window.state.hdr.imported_profile = ""
        self.window.state.hdr.panel_primaries = self.PANEL_XY
        binding = self.window._selected_binding()
        binding.hdr_profile = ""          # nothing has been calibrated yet
        satisfied, detail = self.window._check_profile_imported()
        self.assertFalse(satisfied, detail)
        self.assertIn("No HDR profile", detail)

    def test_a_display_with_nothing_chosen_is_still_reported_as_outstanding(self):
        self.window.state.hdr.base_profile = ""
        self.window.state.hdr.imported_profile = ""
        self.window.state.hdr.panel_primaries = ()
        satisfied, detail = self.window._check_profile_imported()
        self.assertFalse(satisfied)
        self.assertIn("No HDR profile", detail)

    def test_an_installed_base_profile_still_satisfies_it(self):
        self.window.state.hdr.panel_primaries = ()
        self.window.state.hdr.base_profile = str(self.color_dir / "BaseCalibration.icm")
        satisfied, detail = self.window._check_profile_imported()
        self.assertTrue(satisfied)
        self.assertIn("BaseCalibration.icm", detail)

    def test_every_guide_step_action_and_check_resolves(self):
        """A step naming an action or check the window does not provide is a
        dead button in the walkthrough."""
        from sdr_hdr_profile_creator.dialogs import GUIDE_STEPS

        actions, checks = self.window._guide_wiring()
        for step in GUIDE_STEPS:
            if step.action_key:
                with self.subTest(action=step.action_key):
                    self.assertIn(step.action_key, actions)
            if step.check_key:
                with self.subTest(check=step.check_key):
                    self.assertIn(step.check_key, checks)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class HotkeyOwnershipTests(WindowTestCase):
    """Losing the hotkeys to the watchdog is the normal outcome, not a fault.

    RegisterHotKey is exclusive, so turning on Lock Profile -- which the guide
    tells you to do -- means this window cannot hold Alt+1 / Alt+2. They still
    work; the watchdog owns them, which is the entire point of installing it.
    Reporting that as an amber "unavailable" greeted the user with a broken
    feature once per launch because they had followed the instructions.
    """

    def test_losing_them_to_the_watchdog_is_not_a_warning(self):
        self.watchdog_running = True
        self.window._set_status("baseline", "ok")
        self.window._hotkey_registration_changed(False, "already registered by another process")
        self.assertNotIn("unavailable", self.window.status_label.text().lower())

    def test_the_label_says_who_has_them(self):
        self.watchdog_running = True
        self.window._hotkey_registration_changed(False, "already registered")
        self.assertIn("watchdog", self.window.hotkey_status_label.text().lower())

    def test_a_real_refusal_is_still_reported(self):
        """With no watchdog running, failing to register is a genuine fault."""
        self.watchdog_running = False
        self.window._hotkey_registration_changed(False, "Win32 error 1409")
        self.assertIn("unavailable", self.window.status_label.text().lower())
        self.assertIn("1409", self.window.status_label.text())

    def test_holding_them_is_reported_plainly(self):
        self.window._hotkey_registration_changed(True, "registered")
        self.assertIn("active", self.window.hotkey_status_label.text().lower())


class WatchdogBuildTests(WindowTestCase):
    """Telling our own watchdog apart from somebody else's.

    On 2026-09-03 an August portable build kept in a second checkout installed its own
    watchdog over the current one. The scheduled task kept showing the old settings
    through two reinstalls, every check the app performs still reported success, and an
    hour went into hunting a bug in the registration code that was correct throughout.
    Nothing could see it because the deployed script carries no identity of its own.
    """

    def setUp(self):
        super().setUp()
        self.install_root = self.temp / "ColorProfileModeWatchdog"
        self.install_root.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(app_module, "WATCHDOG_INSTALL_ROOT", self.install_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.script = self.install_root / "Watchdog.ps1"

    def ship(self, build):
        """Pin what this app claims to ship, without touching the real resource."""
        patcher = mock.patch.object(app_module, "shipped_watchdog_build", lambda: build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_marker_is_found_from_the_end_of_the_installer(self):
        """The .bat names the marker twice: once in the extraction command near the top,
        and once where the payload actually starts. Matching the first would hand back
        the whole installer as though it were the script, and every comparison after
        that would be against the wrong thing."""
        installer = (
            "@echo off\n"
            "powershell -Command \"$marker=':__WATCHDOG_POWERSHELL_PAYLOAD__'\"\n"
            ":__WATCHDOG_POWERSHELL_PAYLOAD__\n"
            "param([switch]$Install)\n"
        )
        payload = app_module.watchdog_payload(installer)
        self.assertIn("param([switch]$Install)", payload)
        # The assertions that actually separate rfind from find. Taking the first
        # marker leaves the payload starting midway through the extraction command,
        # which still excludes "@echo off" and still contains the param line -- so
        # checking only those two would pass with the bug in place.
        self.assertNotIn(
            app_module.WATCHDOG_PAYLOAD_MARKER, payload,
            "a payload that still contains the marker was cut at the wrong one",
        )
        self.assertNotIn("powershell -Command", payload)
        self.assertTrue(
            payload.strip().startswith("param([switch]$Install)"),
            f"the payload should begin at the script, not inside the .bat: {payload[:60]!r}",
        )

    def test_an_installer_without_the_marker_yields_nothing(self):
        self.assertEqual("", app_module.watchdog_payload("no marker anywhere in here"))

    def test_the_same_script_gets_the_same_id_however_it_reached_us(self):
        """The deployed copy is written by PowerShell's Set-Content -Encoding UTF8,
        which adds a byte-order mark and keeps CRLF; the shipped copy is sliced out of
        the .bat. A comparison that tripped on that would call every build foreign and
        would be worse than having no check."""
        plain = app_module.watchdog_build_id("param($Install)\nexit 0\n")
        self.assertEqual(plain, app_module.watchdog_build_id("param($Install)\r\nexit 0\r\n"))
        self.assertEqual(
            plain, app_module.watchdog_build_id("\ufeffparam($Install)\r\nexit 0\r\n")
        )
        self.assertEqual(plain, app_module.watchdog_build_id("param($Install)\nexit 0"))

    def test_a_changed_script_gets_a_different_id(self):
        self.assertNotEqual(
            app_module.watchdog_build_id("exit 0\n"),
            app_module.watchdog_build_id("exit 1\n"),
        )

    def test_an_empty_script_has_no_id_at_all(self):
        """"" means unknown everywhere in this check, so it must not collide with the
        id of some real script."""
        self.assertEqual("", app_module.watchdog_build_id(""))
        self.assertEqual("", app_module.watchdog_build_id("   \r\n  "))

    def test_this_app_can_identify_the_build_it_ships(self):
        """Guards the silent failure: rename the marker, or drop the .bat out of the
        packaged resources, and shipped_watchdog_build() quietly returns "" -- which is
        treated as unknown, so the check would never fire again and nothing would say
        so."""
        app_module.shipped_watchdog_build.cache_clear()
        self.addCleanup(app_module.shipped_watchdog_build.cache_clear)
        self.assertNotEqual(
            "", app_module.shipped_watchdog_build(),
            "the app must be able to fingerprint its own installer payload",
        )

    def test_nothing_installed_is_not_a_mismatch(self):
        """A fresh machine has no watchdog. Reporting that as a foreign build is how a
        warning gets ignored by the time it matters."""
        self.ship("aaaaaaaaaaaa")
        self.assertFalse(self.script.exists())
        self.assertEqual("", self.window._watchdog_build_mismatch())

    def test_our_own_build_is_not_reported(self):
        script = "param([switch]$Install)\nexit 0\n"
        self.script.write_text(script, encoding="utf-8")
        self.ship(app_module.watchdog_build_id(script))
        self.assertEqual("", self.window._watchdog_build_mismatch())

    def test_a_build_written_the_way_powershell_writes_it_is_still_ours(self):
        """Set-Content -Encoding UTF8 is what actually puts the file on disk, so the
        bytes that arrive carry a BOM and CRLF. This is the case that decides whether
        the check is usable at all."""
        script = "param([switch]$Install)\nexit 0\n"
        as_powershell_writes_it = "\ufeff" + script.replace("\n", "\r\n")
        self.script.write_bytes(as_powershell_writes_it.encode("utf-8"))
        self.ship(app_module.watchdog_build_id(script))
        self.assertEqual(
            "", self.window._watchdog_build_mismatch(),
            "a BOM and CRLF are how the installer writes every file; they are not a "
            "different build",
        )

    def test_a_foreign_build_names_both_sides(self):
        """Both ids, because "yours is wrong" without saying which is which leaves the
        user no way to tell whether the reinstall worked."""
        self.script.write_text("this is somebody else's watchdog\n", encoding="utf-8")
        self.ship("bbbbbbbbbbbb")
        mismatch = self.window._watchdog_build_mismatch()
        self.assertIn("bbbbbbbbbbbb", mismatch)
        self.assertIn(
            app_module.watchdog_build_id("this is somebody else's watchdog\n"), mismatch
        )

    def test_a_successful_install_of_a_foreign_build_is_not_reported_as_ready(self):
        """The whole point. The install genuinely succeeded -- ok=True, a real startup
        method, no warnings -- and the green line it earned is exactly what hid an
        August watchdog sitting on disk for an evening."""
        import json as _json

        before = self.window._watchdog_script_stamp()
        self.script.write_text("somebody else's build\n", encoding="utf-8")
        (self.install_root / "install_result.json").write_text(
            _json.dumps({
                "action": "install", "ok": True,
                "startup": "Task Scheduler (COM / current-user SID)", "warnings": [],
            }),
            encoding="utf-8",
        )
        self.ship("cccccccccccc")
        self.window._report_watchdog_outcome(True, before)
        text = self.window.status_label.text()
        self.assertIn("Attention", text, "a foreign build must not read as success")
        self.assertIn("cccccccccccc", text, "the status has to name what we ship")
        self.assertNotIn("Ready", text)

    def test_our_own_build_still_reports_ready(self):
        """The other half: the check must not turn every good install amber."""
        import json as _json

        script = "param([switch]$Install)\nexit 0\n"
        before = self.window._watchdog_script_stamp()
        self.script.write_text(script, encoding="utf-8")
        (self.install_root / "install_result.json").write_text(
            _json.dumps({
                "action": "install", "ok": True,
                "startup": "Task Scheduler (COM / current-user SID)", "warnings": [],
            }),
            encoding="utf-8",
        )
        self.ship(app_module.watchdog_build_id(script))
        self.window._report_watchdog_outcome(True, before)
        text = self.window.status_label.text()
        self.assertIn("Ready", text)
        self.assertIn("Task Scheduler", text)


class PlacementCancelTests(WindowTestCase):
    """Pressing Esc while the meter is still being placed.

    The run has not started at that point -- there is no worker and no thread, only a
    fullscreen window showing a green square -- so _measure_finished, which is the one
    place that releases the surface, never runs.
    """

    def arrange(self):
        """Get as far as the placement target and stop there.

        Deliberately not MeasurementBriefingTests.drive: its fake emits `ready` from
        show_placement_target, which starts the run immediately and steps straight over
        the state this class is about.
        """
        from PySide6.QtCore import QObject, Signal

        class PlacingWindow(QObject):
            ready = Signal()
            closed = Signal()
            failure = ""

            def __init__(self, *args, **kwargs):
                super().__init__()
                PlacingWindow.instance = self

            def show_placement_target(self, fraction=None):
                # The meter has not found the target yet, so `ready` is NOT emitted.
                return True

            def setGeometry(self, *args):
                pass

            def showFullScreen(self):
                pass

            def begin(self):
                return True

            def close(self):
                # A real MeasureWindow emits closed from closeEvent unconditionally.
                self.closed.emit()

            def activateWindow(self):
                pass

            def setFocus(self, *args):
                pass

        patches = [
            mock.patch.object(app_module, "find_spotread", return_value=Path("spotread")),
            mock.patch.object(app_module, "list_instruments",
                              return_value=[app_module_instrument()]),
            mock.patch.object(app_module, "read_panel_metadata", lambda _p: None),
            mock.patch.object(app_module.measure_view, "MeasureWindow", PlacingWindow),
            mock.patch.object(app_module.measure_view, "start",
                              lambda *a, **k: (mock.MagicMock(), mock.MagicMock())),
            mock.patch.object(app_module.QMessageBox, "question",
                              return_value=QMessageBox.StandardButton.Ok),
            # The placement watcher is a REAL QThread here, and its poll calls
            # read_emissive. Left unpatched it shells out to a "spotread" that is not
            # on PATH, once per poll, for as long as the fixture lives -- a test that
            # reaches outside the process for hardware it must never touch.
            mock.patch.object(app_module, "read_emissive",
                              side_effect=app_module.MeterError("no meter in tests")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window._measure_with_meter()
        return PlacingWindow.instance

    def test_the_surface_is_released_when_placement_is_abandoned(self):
        """The bug: one Esc during placement used to block every later measurement AND
        every pattern window for the rest of the session, with "A measurement is still
        running" and no measurement running. Only a restart cleared it."""
        window = self.arrange()
        self.assertIsNotNone(
            getattr(self.window, "_measure_window", None),
            "the fixture did not reach the placement stage",
        )
        window.close()
        self.assertIsNone(
            getattr(self.window, "_measure_window", None),
            "the closed window is still held as the live measurement surface",
        )
        self.assertEqual(
            "", self.window._fullscreen_surface_busy(),
            "a later measurement or pattern window is refused after a cancelled "
            "placement",
        )
        # The last thing on screen was "Put the meter flat on the green square ... Esc
        # cancels", or after a timeout "Press Enter to start anyway, or Esc to stop".
        # Left standing, either one tells the user to place a meter on a square that is
        # no longer there and promises a start that will never come. This is the only
        # abandonment in the app that used to say nothing.
        status = self.window.status_label.text()
        self.assertIn("cancelled", status.lower())
        self.assertNotIn("green square", status)

    def test_a_run_still_holds_the_surface_while_it_is_running(self):
        """The other half: releasing on every close would let a second surface open on
        top of a live run, and two swapchains on one display present nothing."""
        self.arrange()
        self.window._measure_thread = mock.MagicMock()
        self.addCleanup(setattr, self.window, "_measure_thread", None)
        self.window._placement_surface_closed()
        self.assertIsNotNone(
            getattr(self.window, "_measure_window", None),
            "a running measurement must keep its surface",
        )


class ModePollDuringMeasurementTests(WindowTestCase):
    """The 900 ms Windows-mode poll, while a measurement is in flight.

    _poll_windows_mode reacts to an SDR/HDR transition by rewriting state.hdr through
    _capture_current_hdr_base(load_controls=True) and then scheduling _apply_mode_profile
    with force=True -- a full profile reinstall with the installed-content cache
    deliberately bypassed. Both are correct when the user flips HDR at the desk. Both
    are destructive during a run: the profile is what shapes the signal at scanout, so
    reinstalling it between two patches means the readings either side describe
    different displays, and nothing downstream can tell.
    """

    def setUp(self):
        super().setUp()
        self.window._last_detected_mode = "SDR"
        # Without both of these the automatic response is never armed, so a test that
        # watched for it would pass whatever the guard did.
        self.window.state.follow_windows_mode = True
        self.window.state.auto_refresh_after_mode_change = True
        self.captured = []
        self.applied = []
        patcher = mock.patch.object(
            app_module.MainWindow, "_capture_current_hdr_base",
            lambda _self, display, **kw: self.captured.append(display),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        applier = mock.patch.object(
            app_module.MainWindow, "_apply_mode_profile",
            lambda _self, *a, **kw: self.applied.append(a),
        )
        applier.start()
        self.addCleanup(applier.stop)

    def test_a_mode_change_mid_run_does_not_rewrite_the_display_state(self):
        self.window._measure_window = object()
        self.addCleanup(setattr, self.window, "_measure_window", None)
        self.window._poll_windows_mode()
        self.assertEqual([], self.captured, "state.hdr was recaptured during a run")

    def test_the_transition_is_handled_once_the_run_is_over(self):
        """Deferred, not swallowed. The poll returns before it records what it saw, so
        the next tick after the run still sees the change and acts on it -- otherwise
        suppressing the poll would quietly lose a real mode switch."""
        self.window._measure_window = object()
        self.window._poll_windows_mode()
        self.assertEqual(
            "SDR", self.window._last_detected_mode,
            "the poll recorded the new mode while refusing to act on it, so the "
            "transition can never be handled",
        )
        self.window._measure_window = None
        self.window._poll_windows_mode()
        self.assertEqual(
            1, len(self.captured), "the deferred transition was never handled"
        )


class DisplaySurfaceGuardTests(WindowTestCase):
    """Everything that may not touch the display while a fullscreen surface owns it.

    The first version of this guard covered the measurement window and the 900 ms mode
    poll, and an adversarial review found three ways round it: the pattern window owns
    the display just as exclusively, the mode response is armed 650 ms ahead and never
    re-checked, and two other things rewrite the same state on their own timers.
    """

    def test_the_pattern_window_counts_as_owning_the_display(self):
        """Not a measurement, but the same exclusivity and the same damage: someone is
        judging those patches by eye while a reinstall changes them."""
        self.assertFalse(self.window._display_surface_in_use())
        self.window._pattern_window = object()
        self.addCleanup(setattr, self.window, "_pattern_window", None)
        self.assertTrue(self.window._display_surface_in_use())

    def test_a_response_armed_before_the_run_does_not_fire_during_it(self):
        """The reachable gap: QMessageBox.question runs a nested event loop, so the
        900 ms poll keeps firing while the measurement briefing is on screen and can arm
        a forced reinstall in the seconds before the surface goes up."""
        ran = []
        self.window._measure_window = object()
        self.addCleanup(setattr, self.window, "_measure_window", None)
        self.window._last_detected_mode = "HDR"
        self.window._deferred_mode_response("SDR", lambda: ran.append(1))
        self.assertEqual([], ran, "a profile reinstall landed inside a run")

    def test_a_response_held_back_is_rewound_rather_than_dropped(self):
        """Otherwise suppressing it loses a real mode change: the poll would have
        already recorded the new mode, so nothing would ever detect it again."""
        self.window._measure_window = object()
        self.window._last_detected_mode = "HDR"
        self.window._deferred_mode_response("SDR", lambda: None)
        self.assertEqual(
            "SDR", self.window._last_detected_mode,
            "the transition can never be re-detected after the run",
        )
        self.window._measure_window = None
        ran = []
        self.window._deferred_mode_response("SDR", lambda: ran.append(1))
        self.assertEqual([1], ran, "the response never ran once the display was free")

    def test_the_gamma_runtime_poll_stands_down_during_a_run(self):
        """450 ms, and it assigns state.hdr.sdr_gamma_correction from the watchdog's
        shared JSON -- the response the run is solving for, changed mid-run."""
        GAMMA = app_module.GAMMA_HOTKEY_STATE_PATH
        GAMMA.write_text(
            json.dumps({"displays": {self.display.key: {"correction": "Off"}}}),
            encoding="utf-8",
        )
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        self.window._measure_window = object()
        self.addCleanup(setattr, self.window, "_measure_window", None)
        self.window._sync_external_gamma_hotkey_state()
        self.assertEqual(
            "Auto (Recommended)", self.window.state.hdr.sdr_gamma_correction,
            "the watchdog's state was adopted in the middle of a measurement",
        )

    def test_alt_1_is_refused_while_a_surface_is_up(self):
        """Both handlers end in _select_gamma_correction, which installs and associates
        a different profile -- at scanout, between two patches."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        self.window._measure_window = object()
        self.addCleanup(setattr, self.window, "_measure_window", None)
        self.window._gamma_hotkey_disable()
        self.assertEqual(
            "Auto (Recommended)", self.window.state.hdr.sdr_gamma_correction,
            "Alt+1 swapped the profile during a measurement",
        )
        self.assertIn("Alt+1", self.window.status_label.text())

    def test_alt_2_is_refused_while_a_surface_is_up(self):
        self.window.state.hdr.sdr_gamma_correction = "Off"
        self.window._pattern_window = object()
        self.addCleanup(setattr, self.window, "_pattern_window", None)
        self.window._gamma_hotkey_enable()
        self.assertEqual(
            "Off", self.window.state.hdr.sdr_gamma_correction,
            "Alt+2 swapped the profile while patterns were on screen",
        )

    def test_the_hotkeys_still_work_with_nothing_on_screen(self):
        """The refusal must be conditional, or the feature is simply gone."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        self.window._gamma_hotkey_disable()
        self.assertEqual("Off", self.window.state.hdr.sdr_gamma_correction)


class DisplayProbeTests(WindowTestCase):
    """The run's view of the display comes from Windows, not from this app's state.

    _shaping_fingerprint is a function of self.state and cannot see the display move.
    The two things that move it from outside -- Windows leaving HDR, and the standalone
    watchdog re-applying a profile from another process -- are exactly what nothing
    else in the app can prevent, so the probe has to ask the OS.
    """

    def test_the_probe_reports_what_windows_says_not_what_the_app_believes(self):
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        probe = self.window._display_state_probe(self.display)
        self.assertEqual(
            {"mode": "HDR", "profile": "BaseCalibration.icm", "sdr_white": 240.0},
            probe(),
        )
        # The fixture's fake of Windows changes; nothing in self.state does.
        self.default_profiles["HDR"] = "SomethingTheWatchdogPutBack.icm"
        self.assertEqual("SomethingTheWatchdogPutBack.icm", probe()["profile"])

    def test_a_display_that_left_hdr_reads_as_sdr(self):
        probe = self.window._display_state_probe(self.display)
        dropped = dataclasses.replace(
            self.display, advanced_color_enabled=False, advanced_color_kind="SDR"
        )
        with mock.patch.object(app_module, "enumerate_displays", lambda: [dropped]):
            self.assertEqual("SDR", probe()["mode"])

    def test_the_sdr_white_level_is_watched_only_when_the_run_will_read_it(self):
        """The measurement surface is scRGB, 1.0 = 80 nits absolute on an HDR
        display, so the slider cannot change what the meter sees. The one consumer is
        _effective_sdr_white_nits, which consults Windows only under Auto and only
        after the run. Refusing an Off-mode run because the slider moved would blame
        the user for a thing that changed nothing."""
        self.window.state.hdr.sdr_gamma_correction = "Off"
        self.assertNotIn("sdr_white", self.window._display_state_probe(self.display)())
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        self.assertEqual(240.0, self.window._display_state_probe(self.display)()["sdr_white"])

    def test_a_failed_advanced_colour_query_reads_as_unknown_not_as_sdr(self):
        """enumerate_displays does not raise when the per-path query fails; it falls
        back to supported=False, kind="SDR", bits=0 and returns the display anyway. On
        a panel the run already proved HDR-capable, that is the signature of a failed
        query -- and reporting it as SDR refused good runs with a message telling the
        user Windows had left HDR when it had not."""
        failed_query = dataclasses.replace(
            self.display, advanced_color_supported=False, advanced_color_enabled=False,
            bits_per_color_channel=0, advanced_color_kind="SDR",
        )
        probe = self.window._display_state_probe(self.display)
        with mock.patch.object(app_module, "enumerate_displays", lambda: [failed_query]):
            self.assertIsNone(probe()["mode"])

    def test_each_field_fails_on_its_own(self):
        """The docstring's claim, pinned: one unreadable field must not take the
        others with it, or a single flaky call blinds the whole probe."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        probe = self.window._display_state_probe(self.display)

        def boom(*_a, **_k):
            raise app_module.WindowsColorError("no")

        with mock.patch.object(app_module, "get_default_profile", boom):
            sample = probe()
        self.assertEqual({"mode": "HDR", "profile": None, "sdr_white": 240.0}, sample)
        with mock.patch.object(app_module, "get_sdr_white_level_nits", boom):
            sample = probe()
        self.assertEqual({"mode": "HDR", "profile": "BaseCalibration.icm", "sdr_white": None}, sample)

    def test_a_removed_association_is_a_change_not_an_unreadable_field(self):
        """The watchdog dropping the HDR profile mid-run is a real change to the
        display. get_default_profile raises for it -- the same base type as a failed
        call -- and treating both as "unread" would swallow exactly the event the probe
        exists to catch."""
        from sdr_hdr_profile_creator.measure import describe_drift

        probe = self.window._display_state_probe(self.display)

        def gone(*_a, **_k):
            raise app_module.NoDefaultProfile("Windows returned an empty default profile name")

        with mock.patch.object(app_module, "get_default_profile", gone):
            sample = probe()
        self.assertEqual("(none)", sample["profile"])
        self.assertEqual(
            "profile went from BaseCalibration.icm to (none)",
            describe_drift({"profile": "BaseCalibration.icm"}, sample),
        )
        # And every existing `except WindowsColorError` still catches it.
        self.assertTrue(issubclass(app_module.NoDefaultProfile, app_module.WindowsColorError))

    def test_a_failing_windows_call_reads_as_unknown_not_as_an_error(self):
        """A diagnostic that could end a four-minute run over one failed call would be
        worse than the gap it fills, so every field degrades to None on its own."""
        self.window.state.hdr.sdr_gamma_correction = "Auto (Recommended)"
        probe = self.window._display_state_probe(self.display)

        def boom(*_a, **_k):
            raise app_module.WindowsColorError("no")

        with mock.patch.object(app_module, "get_default_profile", boom), \
             mock.patch.object(app_module, "get_sdr_white_level_nits", boom), \
             mock.patch.object(app_module, "enumerate_displays", boom):
            sample = probe()
        self.assertEqual({"mode": None, "profile": None, "sdr_white": None}, sample)

    def test_the_run_is_started_with_the_probe(self):
        """A probe that exists but is never handed to start() guards nothing."""
        from PySide6.QtCore import QObject, Signal

        class Surface(QObject):
            closed = Signal()

        captured = {}

        def fake_start(*args, **kwargs):
            captured.update(kwargs)
            return mock.MagicMock(), mock.MagicMock()

        with mock.patch.object(app_module.measure_view, "start", fake_start), \
             mock.patch.object(app_module.MainWindow, "_monitor_state", lambda _s: {}):
            self.window._start_measurement(
                Surface(), self.display, Path("spotread"), app_module_instrument(), 450.0
            )
        self.addCleanup(setattr, self.window, "_measure_thread", None)
        self.addCleanup(setattr, self.window, "_measure_worker", None)
        probe = captured.get("probe")
        self.assertIsNotNone(probe, "start() was called without a display probe")
        # Not a tautology: the value comes from the fixture's fake of Windows.
        self.assertEqual("BaseCalibration.icm", probe()["profile"])
        self.assertEqual(
            {"mode": "HDR"}, captured.get("expected"),
            "the run was started without the state it needs the display to be in",
        )
        # The baseline is written to the log with the start record, so a refusal can
        # be read against what it changed from and an unread field is visible as such.
        last = json.loads(
            app_module.METER_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        self.assertEqual("start", last["event"])
        self.assertEqual("BaseCalibration.icm", last["display_state"]["profile"])

    def test_the_sustained_run_is_started_with_the_probe_and_the_expected_state(self):
        """start_sustained is reached by a different method with its own window and
        its own start record; a probe only the sweep passed would leave the one
        measurement that lights every pixel unguarded."""
        from types import SimpleNamespace
        from PySide6.QtCore import QObject, Signal

        class Surface(QObject):
            ready = Signal()
            closed = Signal()
            failure = ""

            def __init__(self, *args, **kwargs):
                super().__init__()

            def setGeometry(self, *args):
                pass

            def showFullScreen(self):
                pass

            def begin(self):
                return True

            def close(self):
                pass

            def activateWindow(self):
                pass

            def setFocus(self, *args):
                pass

        ready = SimpleNamespace(
            display=self.display, spotread=Path("spotread"),
            instrument=app_module_instrument(), sdr_white=240.0, panel=None,
            capability=mock.MagicMock(), peak=450.0,
        )
        captured = {}

        def fake_start_sustained(*args, **kwargs):
            captured.update(kwargs)
            return mock.MagicMock(), mock.MagicMock()

        with mock.patch.object(app_module.MainWindow, "_meter_preconditions", lambda _s, _r: ready), \
             mock.patch.object(app_module.MainWindow, "_screen_for", lambda _s, _d: None), \
             mock.patch.object(app_module.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Ok), \
             mock.patch.object(app_module.measure_view, "MeasureWindow", Surface), \
             mock.patch.object(app_module.measure_view, "start_sustained", fake_start_sustained):
            self.window._measure_sustained()
        for attribute in ("_measure_window", "_measure_thread", "_measure_worker"):
            self.addCleanup(setattr, self.window, attribute, None)
        probe = captured.get("probe")
        self.assertIsNotNone(probe, "start_sustained() was called without a display probe")
        self.assertEqual("BaseCalibration.icm", probe()["profile"])
        self.assertEqual({"mode": "HDR"}, captured.get("expected"))
        last = json.loads(
            app_module.METER_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        self.assertEqual("sustained-start", last["event"])
        self.assertEqual("BaseCalibration.icm", last["display_state"]["profile"])


if __name__ == "__main__":
    unittest.main()
