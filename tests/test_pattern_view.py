"""Behaviour of the fullscreen pattern view.

Nothing here needs a real HDR surface: the swapchain is the only part that does, and it is
isolated behind HdrSurface so the framing, the overlay and every keystroke can be checked
against a widget that was never shown.
"""

from __future__ import annotations

import os
import struct
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from sdr_hdr_profile_creator import pattern_view
    from sdr_hdr_profile_creator.hdr_display import (
        DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709,
        DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
        DisplayCapability,
        HdrDisplayError,
    )
    from sdr_hdr_profile_creator.pattern_view import (
        ControlBinding,
        PatternWindow,
        context_for,
        render_overlay,
    )
    from sdr_hdr_profile_creator.patterns import PATTERNS

    GUI_AVAILABLE = True
    GUI_IMPORT_ERROR = ""
except ImportError as exc:
    GUI_AVAILABLE = False
    GUI_IMPORT_ERROR = str(exc)


def capability(**overrides) -> "DisplayCapability":
    base = dict(
        device_name="TEST", left=0, top=0, right=3840, bottom=2160, bits_per_color=10,
        color_space=DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
        min_nits=0.0, max_nits=1080.0, max_full_frame_nits=250.0,
        red_primary=(0.67, 0.31), green_primary=(0.27, 0.69),
        blue_primary=(0.15, 0.06), white_point=(0.313, 0.329),
    )
    base.update(overrides)
    return DisplayCapability(**base)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class PatternViewTestCase(unittest.TestCase):
    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.values = {"gamma": 2.2, "contrast": 0.0}
        self.controls = [
            ControlBinding(
                "gamma", "Gamma",
                lambda: self.values["gamma"],
                lambda delta: self.values.__setitem__("gamma", self.values["gamma"] + delta),
                step=0.005,
            ),
            ControlBinding(
                "contrast", "Contrast",
                lambda: self.values["contrast"],
                lambda delta: self.values.__setitem__("contrast", self.values["contrast"] + delta),
                step=0.5, suffix="%",
            ),
        ]

    def window(self, cap=None, width=800, height=600) -> "PatternWindow":
        win = PatternWindow(cap if cap is not None else capability(), 240.0, self.controls)
        win.resize(width, height)
        self.addCleanup(win.deleteLater)
        return win


class ContextTests(PatternViewTestCase):
    def test_a_credible_panel_supplies_its_own_peak(self):
        context = context_for(capability(max_nits=1080.0, max_full_frame_nits=250.0), 240.0)
        self.assertTrue(context.is_hdr)
        self.assertEqual(context.peak_nits, 1080.0)

    def test_an_unusable_panel_gets_a_conservative_peak_not_a_fabricated_one(self):
        """Patterns against a made-up peak would have the user chasing nothing."""
        context = context_for(capability(max_nits=0.0), 240.0)
        self.assertEqual(context.peak_nits, 400.0)

    def test_no_capability_at_all_falls_back_to_relative_levels(self):
        context = context_for(None, 240.0)
        self.assertFalse(context.is_hdr)
        self.assertFalse(context.absolute)

    def test_an_sdr_panel_is_never_treated_as_absolute(self):
        context = context_for(capability(color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709), 240.0)
        self.assertFalse(context.absolute)


class OverlayTests(PatternViewTestCase):
    def test_the_overlay_is_rgba8_of_exactly_the_requested_size(self):
        raw, width, height = render_overlay(
            300, 500, PATTERNS[0], context_for(capability(), 240.0), self.controls, 0)
        self.assertEqual((width, height), (300, 500))
        self.assertEqual(len(raw), 300 * 500 * 4)

    def test_the_overlay_actually_has_text_on_it(self):
        raw, _, _ = render_overlay(
            300, 500, PATTERNS[0], context_for(capability(), 240.0), self.controls, 0)
        opaque = sum(1 for index in range(3, len(raw), 4) if raw[index] > 0)
        self.assertGreater(opaque, 200, "nothing was drawn into the overlay")

    def test_an_assumed_peak_is_labelled_as_assumed(self):
        """Presenting a guess as a measurement is how a user calibrates to nothing."""
        plain, _, _ = render_overlay(
            420, 600, PATTERNS[0], context_for(capability(), 240.0), self.controls, 0)
        flagged, _, _ = render_overlay(
            420, 600, PATTERNS[0], context_for(capability(), 240.0), self.controls, 0,
            assumed_peak=True)
        self.assertNotEqual(plain, flagged)


class FrameTests(PatternViewTestCase):
    def test_the_frame_matches_the_widget_exactly(self):
        win = self.window(width=640, height=480)
        self.assertEqual(len(win.build_frame()), 640 * 480 * 8)

    def test_the_surround_stays_black_with_the_overlay_present(self):
        win = self.window(width=800, height=600)
        frame = win.build_frame()
        # Far left, opposite the default right-hand overlay.
        value = struct.unpack_from("<4e", frame, ((300 * 800) + 3) * 8)[0]
        self.assertEqual(value, 0.0)

    def test_a_tiny_window_still_produces_a_valid_frame(self):
        win = self.window(width=4, height=4)
        self.assertEqual(len(win.build_frame()), 4 * 4 * 8)


class InputTests(PatternViewTestCase):
    def press(self, win, key):
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))

    def test_number_keys_select_patterns(self):
        win = self.window()
        self.press(win, Qt.Key.Key_3)
        self.assertEqual(win.pattern.key, PATTERNS[2].key)

    def test_a_number_beyond_the_pattern_list_is_ignored(self):
        win = self.window()
        before = win.pattern.key
        self.press(win, Qt.Key.Key_9)
        self.assertEqual(win.pattern.key, before)

    def test_tab_cycles_controls_and_wraps(self):
        win = self.window()
        self.assertEqual(win.active_control.label, "Gamma")
        self.press(win, Qt.Key.Key_Tab)
        self.assertEqual(win.active_control.label, "Contrast")
        self.press(win, Qt.Key.Key_Tab)
        self.assertEqual(win.active_control.label, "Gamma")

    def test_arrows_move_the_active_control_by_its_own_step(self):
        win = self.window()
        self.press(win, Qt.Key.Key_Right)
        self.assertAlmostEqual(self.values["gamma"], 2.205)
        self.press(win, Qt.Key.Key_Left)
        self.press(win, Qt.Key.Key_Left)
        self.assertAlmostEqual(self.values["gamma"], 2.195)

    def test_adjustment_follows_the_selection_not_the_first_control(self):
        win = self.window()
        self.press(win, Qt.Key.Key_Tab)
        self.press(win, Qt.Key.Key_Up)
        self.assertAlmostEqual(self.values["contrast"], 0.5)
        self.assertAlmostEqual(self.values["gamma"], 2.2, msg="the wrong control moved")

    def test_h_moves_the_panel_to_the_other_edge(self):
        win = self.window()
        self.assertEqual(win._overlay_side, "right")
        self.press(win, Qt.Key.Key_H)
        self.assertEqual(win._overlay_side, "left")

    def test_escape_closes(self):
        win = self.window()
        with mock.patch.object(win, "close") as closed:
            self.press(win, Qt.Key.Key_Escape)
        closed.assert_called_once()

    def test_a_view_with_no_controls_ignores_adjustment(self):
        win = PatternWindow(capability(), 240.0, [])
        self.addCleanup(win.deleteLater)
        win.resize(200, 200)
        self.assertIsNone(win.active_control)
        self.press(win, Qt.Key.Key_Right)  # must not raise


class SurfaceFailureTests(PatternViewTestCase):
    """HDR presentation can simply be unavailable; that has to be a message, not a crash."""

    def test_begin_reports_failure_instead_of_raising(self):
        win = self.window()
        with mock.patch.object(
            pattern_view, "HdrSurface", side_effect=HdrDisplayError("no HDR surface here")
        ):
            self.assertFalse(win.begin())
        self.assertIn("no HDR surface here", win.failure)

    def test_a_present_failure_stops_the_keepalive_rather_than_repeating(self):
        win = self.window()
        surface = mock.MagicMock()
        surface.present.side_effect = HdrDisplayError("device lost")
        win._surface = surface
        win._frame = b"x"
        win._keepalive.start()
        win._present()
        self.assertIn("device lost", win.failure)
        self.assertFalse(win._keepalive.isActive())


if __name__ == "__main__":
    unittest.main()
