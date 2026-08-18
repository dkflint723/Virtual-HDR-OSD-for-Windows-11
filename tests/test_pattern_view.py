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
    from sdr_hdr_profile_creator.patterns import MEASUREMENT_SEQUENCE, PATTERNS

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

    def press(self, win, key):
        win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


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
    def test_number_keys_select_patterns(self):
        win = self.window()
        self.press(win, Qt.Key.Key_3)
        self.assertEqual(win.pattern.key, PATTERNS[2].key)

    def test_every_number_key_reaches_its_pattern(self):
        win = self.window()
        for index in range(min(9, len(PATTERNS))):
            with self.subTest(key=index + 1):
                self.press(win, Qt.Key.Key_1 + index)
                self.assertEqual(win.pattern.key, PATTERNS[index].key)

    def test_a_number_beyond_the_pattern_list_is_ignored(self):
        """Otherwise a shorter pattern list would index off the end."""
        win = self.window()
        with mock.patch.object(pattern_view, "PATTERNS", PATTERNS[:2]):
            win.select_pattern(0)
            before = win.pattern.key
            self.press(win, Qt.Key.Key_5)
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


class ThresholdPatternTests(PatternViewTestCase):
    """On these the pattern moves and the display holds still, which is the whole trick:
    nobody can say what luminance a patch is, but anybody can say whether they can see a
    shape."""

    def select(self, win, key):
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == key))

    def test_arrows_move_the_probe_not_the_sliders(self):
        win = self.window()
        self.select(win, "black-level")
        before_probe, before_gamma = win.probe_nits, self.values["gamma"]
        self.press(win, Qt.Key.Key_Right)
        self.assertGreater(win.probe_nits, before_probe)
        self.assertEqual(self.values["gamma"], before_gamma, "a slider moved instead")

    def test_arrows_still_move_sliders_on_a_fixed_pattern(self):
        win = self.window()
        self.select(win, "gamma-match")
        before = win.probe_nits
        self.press(win, Qt.Key.Key_Right)
        self.assertNotEqual(self.values["gamma"], 2.2)
        self.assertEqual(win.probe_nits, before, "the probe moved on a fixed pattern")

    def test_the_probe_steps_perceptually_not_in_fixed_nits(self):
        """A fixed nit step is useless at both ends: far too coarse where a threshold
        actually sits, and far too fine to ever cross the range."""
        win = self.window()
        self.select(win, "black-level")
        win.set_probe(0.01)
        win.step_probe(1)
        small = win.probe_nits - 0.01

        win.set_probe(500.0)
        win.step_probe(1)
        large = win.probe_nits - 500.0
        self.assertGreater(large, small * 100, "steps are not perceptual")

    def test_the_probe_never_goes_negative_or_past_st2084(self):
        win = self.window()
        self.select(win, "black-level")
        win.set_probe(0.0)
        for _ in range(20):
            win.step_probe(-1)
        self.assertGreaterEqual(win.probe_nits, 0.0)

        win.set_probe(9999.0)
        for _ in range(50):
            win.step_probe(1)
        self.assertLessEqual(win.probe_nits, 10000.0)

    def test_a_meter_can_place_the_probe_exactly(self):
        """The same pattern a person drives by eye is what a meter loop steps."""
        win = self.window()
        self.select(win, "solid-patch")
        win.set_probe(203.0)
        self.assertAlmostEqual(win.probe_nits, 203.0)

    def test_full_frame_white_actually_fills_the_screen(self):
        """It is defined as the whole screen lit, so measuring it in a tenth measures
        nothing."""
        win = self.window(width=400, height=300)
        self.select(win, "full-frame-white")
        win.set_probe(100.0)
        frame = win.build_frame()
        corner = struct.unpack_from("<4e", frame, 0)[0]
        self.assertGreater(corner, 0.0, "the corner is black, so it is still windowed")

    def test_the_other_patterns_stay_windowed(self):
        win = self.window(width=400, height=300)
        self.select(win, "peak-white")
        win.set_probe(100.0)
        frame = win.build_frame()
        self.assertEqual(struct.unpack_from("<4e", frame, 0)[0], 0.0)


class DevicePixelTests(PatternViewTestCase):
    """Qt measures in logical units; the swapchain needs real pixels.

    On a 125% display a fullscreen widget reports 3206x1803 while its client area is
    3840x2160. Handing the smaller figure to the swapchain makes DXGI stretch the buffer,
    and a stretched frame resamples the gamma-match lines -- which is the exact failure
    the whole D3D path exists to avoid.
    """

    def test_the_frame_is_built_in_device_pixels_not_logical_ones(self):
        win = self.window(width=1000, height=800)
        with mock.patch.object(type(win), "devicePixelRatioF", lambda _self: 1.25):
            self.assertEqual(win.device_size(), (1250, 1000))
            self.assertEqual(len(win.build_frame()), 1250 * 1000 * 8)

    def test_a_display_without_scaling_is_unaffected(self):
        win = self.window(width=640, height=480)
        with mock.patch.object(type(win), "devicePixelRatioF", lambda _self: 1.0):
            self.assertEqual(win.device_size(), (640, 480))

    def test_the_surface_is_created_at_device_size(self):
        win = self.window(width=1000, height=800)
        created: list[tuple[int, int]] = []

        class Recording:
            def __init__(self, _hwnd, width, height):
                created.append((width, height))

            def present(self, *_a, **_k):
                pass

        with mock.patch.object(type(win), "devicePixelRatioF", lambda _self: 1.25), \
             mock.patch.object(pattern_view, "HdrSurface", Recording):
            win.begin()
        self.assertEqual(created, [(1250, 1000)])


class OverlayScalingTests(PatternViewTestCase):
    """A fixed pixel size is comfortable on 1080p and barely legible across a 32in 4K."""

    def widths(self, width):
        win = self.window(width=width, height=round(width * 9 / 16))
        with mock.patch.object(type(win), "devicePixelRatioF", lambda _self: 1.0):
            raw, overlay_width, _height = pattern_view.render_overlay(
                min(width, max(pattern_view.OVERLAY_MIN_WIDTH,
                               round(width * pattern_view.OVERLAY_WIDTH_FRACTION))),
                round(width * 9 / 16 * 0.85),
                PATTERNS[0], context_for(capability(), 240.0), self.controls, 0,
                scale=max(1.0, min(3.0, width / pattern_view.OVERLAY_REFERENCE_WIDTH)),
            )
        return overlay_width, sum(1 for i in range(3, len(raw), 4) if raw[i] > 0)

    def test_the_panel_grows_with_the_display(self):
        narrow, _ = self.widths(1920)
        wide, _ = self.widths(3840)
        self.assertGreater(wide, narrow)

    def test_text_grows_too_rather_than_just_the_box(self):
        """Widening the panel without scaling the type would leave the same tiny text."""
        _, narrow_ink = self.widths(1920)
        _, wide_ink = self.widths(3840)
        self.assertGreater(wide_ink, narrow_ink * 1.5)

    def test_a_small_display_keeps_a_usable_minimum(self):
        width, _ = self.widths(1280)
        self.assertGreaterEqual(width, pattern_view.OVERLAY_MIN_WIDTH)


class AcceptMeasurementTests(PatternViewTestCase):
    def build(self):
        recorded: list[tuple[str, float]] = []
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda key, nits: recorded.append((key, nits)))
        win.resize(400, 300)
        self.addCleanup(win.deleteLater)
        return win, recorded

    def select(self, win, key):
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == key))

    def test_enter_records_the_current_level(self):
        win, recorded = self.build()
        self.select(win, "peak-white")
        win.set_probe(812.0)
        self.press(win, Qt.Key.Key_Return)
        self.assertEqual(recorded, [("peak-white", 812.0)])

    def test_enter_does_nothing_on_a_pattern_that_measures_nothing(self):
        win, recorded = self.build()
        self.select(win, "gamma-match")
        self.press(win, Qt.Key.Key_Return)
        self.assertEqual(recorded, [])

    def test_the_view_remembers_what_was_recorded(self):
        win, _ = self.build()
        self.select(win, "black-level")
        win.set_probe(0.003)
        self.press(win, Qt.Key.Key_Enter)
        self.assertAlmostEqual(win.accepted["black-level"], 0.003)

    def test_recording_survives_moving_to_another_pattern_and_back(self):
        win, _ = self.build()
        self.select(win, "black-level")
        win.set_probe(0.003)
        self.press(win, Qt.Key.Key_Return)
        self.select(win, "peak-white")
        self.select(win, "black-level")
        self.assertIn("black-level", win.accepted)

    def test_a_view_with_no_measure_callback_ignores_enter(self):
        win = self.window()
        self.select(win, "peak-white")
        self.press(win, Qt.Key.Key_Return)  # must not raise
        self.assertEqual(win.accepted, {})


class MarkerTests(PatternViewTestCase):
    """A pattern with no visible target cannot be aimed at."""

    def test_the_gamma_pattern_marks_which_patch_is_the_answer(self):
        from sdr_hdr_profile_creator.patterns import pattern_by_key

        markers = pattern_by_key("gamma-match").markers(context_for(capability(), 240.0))
        self.assertTrue(any(marker.target for marker in markers))
        self.assertIn("TARGET", [marker.text for marker in markers])

    def test_markers_are_rendered_into_the_pattern(self):
        from sdr_hdr_profile_creator.pattern_view import render_markers
        from sdr_hdr_profile_creator.patterns import pattern_by_key

        result = render_markers(400, 300, pattern_by_key("gamma-match"),
                                context_for(capability(), 240.0))
        self.assertIsNotNone(result)
        raw, width, height = result
        self.assertEqual(len(raw), width * height * 4)
        self.assertGreater(sum(1 for i in range(3, len(raw), 4) if raw[i] > 0), 100)

    def test_a_pattern_with_no_markers_renders_none(self):
        from sdr_hdr_profile_creator.pattern_view import render_markers
        from sdr_hdr_profile_creator.patterns import pattern_by_key

        self.assertIsNone(render_markers(
            400, 300, pattern_by_key("neutral-ramp"), context_for(capability(), 240.0)))

    def test_the_probe_marker_reports_the_current_level(self):
        from sdr_hdr_profile_creator.patterns import pattern_by_key

        from dataclasses import replace

        context = replace(context_for(capability(), 240.0), probe_nits=0.25)
        markers = pattern_by_key("black-level").markers(context)
        self.assertIn("0.25", markers[0].text)

    def test_markers_stay_far_dimmer_than_the_patch(self):
        """A bright label beside a near-black shape moves the threshold being measured."""
        from sdr_hdr_profile_creator.patterns import MARKER_NITS, OVERLAY_NITS

        self.assertLess(MARKER_NITS, OVERLAY_NITS)


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


class GuidedSequenceTests(PatternViewTestCase):
    """Nine patterns and a page of theory is not a procedure. A user who has to work out
    what to do first will do nothing, so the view opens on step 1 of 3 and says so."""

    def build(self, guided=True):
        recorded: list[tuple[str, float]] = []
        win = PatternWindow(capability(), 240.0, self.controls, guided=guided,
                            measure=lambda key, nits: recorded.append((key, nits)))
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step() if guided else None
        return win, recorded

    def test_it_opens_on_the_first_measurement_step(self):
        win, _ = self.build()
        self.assertEqual(win.guided_step, 1)
        self.assertEqual(win.pattern.key, MEASUREMENT_SEQUENCE[0])

    def test_recording_advances_to_the_next_step(self):
        win, _ = self.build()
        win.accept_measurement()
        self.assertEqual(win.guided_step, 2)
        self.assertEqual(win.pattern.key, MEASUREMENT_SEQUENCE[1])

    def test_the_whole_sequence_records_every_figure_once(self):
        win, recorded = self.build()
        for _ in MEASUREMENT_SEQUENCE:
            win.accept_measurement()
        self.assertEqual([key for key, _ in recorded], list(MEASUREMENT_SEQUENCE))

    def test_the_run_ends_rather_than_looping(self):
        win, _ = self.build()
        for _ in MEASUREMENT_SEQUENCE:
            win.accept_measurement()
        self.assertIsNone(win.guided_step)

    def test_each_step_starts_near_where_its_answer_lives(self):
        """Starting every step at the same level would mean holding an arrow for seconds
        before anything happened."""
        win, _ = self.build()
        self.assertLess(win.probe_nits, 1.0, "black level should start near black")
        win.accept_measurement()
        self.assertGreater(win.probe_nits, 100.0, "peak should start high")

    def test_choosing_a_pattern_by_hand_leaves_the_sequence(self):
        """Continuing to number the steps afterwards would misreport where the user is."""
        win, _ = self.build()
        win.select_pattern(0)
        self.assertIsNone(win.guided_step)

    def test_free_mode_is_available_for_someone_who_knows_the_tool(self):
        win, _ = self.build(guided=False)
        self.assertIsNone(win.guided_step)

    def test_a_view_that_cannot_record_is_never_guided(self):
        """A guided run whose readings go nowhere would waste the user's time entirely."""
        win = PatternWindow(capability(), 240.0, self.controls, guided=True, measure=None)
        self.addCleanup(win.deleteLater)
        self.assertIsNone(win.guided_step)

    def test_the_step_number_reaches_the_overlay(self):
        raw_plain, _, _ = render_overlay(
            420, 700, PATTERNS[0], context_for(capability(), 240.0), self.controls, 0)
        raw_step, _, _ = render_overlay(
            420, 700, PATTERNS[0], context_for(capability(), 240.0), self.controls, 0,
            step=2, total=3)
        self.assertNotEqual(raw_plain, raw_step)
