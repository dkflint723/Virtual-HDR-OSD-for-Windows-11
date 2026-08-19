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
    from sdr_hdr_profile_creator.patterns import (
        GUIDED_SEQUENCE,
        MEASUREMENT_SEQUENCE,
        PATTERNS,
    )

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

    def advance_step(self, win, level=None):
        """Complete one guided step the way a user would.

        A level-driven step will not record an untouched probe -- a starting value is not
        a reading -- so the level has to actually move first.
        """
        if win.pattern.level_driven:
            if level is None:
                win.step_probe(1)
            else:
                win.set_probe(level)
        return win.confirm_step()

    def complete_run(self, win, levels=(0.004, 940.0, 612.0)):
        """Walk the whole guided sequence, measuring at each step."""
        supplied = list(levels)
        while win.guided_step is not None:
            level = supplied.pop(0) if (supplied and win.pattern.level_driven) else None
            self.advance_step(win, level)
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
        self.advance_step(win)
        self.assertEqual(win.guided_step, 2)
        self.assertEqual(win.pattern.key, MEASUREMENT_SEQUENCE[1])

    def test_the_whole_sequence_records_every_figure_once(self):
        win, recorded = self.build()
        self.complete_run(win)
        self.assertEqual([key for key, _ in recorded], list(MEASUREMENT_SEQUENCE))

    def test_the_run_ends_rather_than_looping(self):
        win, _ = self.build()
        self.complete_run(win)
        self.assertIsNone(win.guided_step)

    def test_the_last_step_sets_the_tone_controls(self):
        """Three measurements and no adjustment ends the run halfway through the job."""
        win, _ = self.build()
        for _ in MEASUREMENT_SEQUENCE:
            self.advance_step(win)
        self.assertEqual(win.pattern.key, "tone-tracking")
        self.assertEqual(win.guided_step, len(GUIDED_SEQUENCE))

    def test_enter_advances_a_step_that_measures_nothing(self):
        """Otherwise the run stalls on it with no way forward the overlay mentions."""
        win, _ = self.build()
        for _ in MEASUREMENT_SEQUENCE:
            self.advance_step(win)
        self.assertTrue(win.confirm_step())
        self.assertIsNone(win.guided_step)

    def test_each_step_starts_near_where_its_answer_lives(self):
        """Starting every step at the same level would mean holding an arrow for seconds
        before anything happened."""
        win, _ = self.build()
        self.assertLess(win.probe_nits, 1.0, "black level should start near black")
        self.advance_step(win)
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


class CompletionTests(PatternViewTestCase):
    """Ending the run by quietly dropping the step counter left the last pattern on screen
    looking exactly as it had a moment before, so there was no way to tell Enter had done
    anything at all."""

    def finished(self):
        win = PatternWindow(capability(), 240.0, self.controls, measure=lambda *_: None)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        return win

    def test_the_run_ends_in_a_stated_finished_state(self):
        self.assertTrue(self.finished()._complete)

    def test_the_finished_screen_goes_dark(self):
        """A bright pattern left up after the last step keeps the eyes adapted for nothing."""
        win = self.finished()
        frame = win.build_frame()
        width, height = win.device_size()
        centre = struct.unpack_from("<4e", frame, ((height // 2) * width + width // 2) * 8)[0]
        self.assertEqual(centre, 0.0)

    def test_the_summary_lists_every_step(self):
        win = self.finished()
        raw, w, h = win.render_summary(500, 400, 1.0)
        self.assertEqual(len(raw), w * h * 4)
        self.assertGreater(sum(1 for i in range(3, len(raw), 4) if raw[i] > 0), 200)

    def test_picking_a_pattern_leaves_the_finished_screen(self):
        """It leaves the summary, but the run stays finished: forgetting that was what
        made the results unreachable after a single digit key."""
        win = self.finished()
        win.select_pattern(0)
        self.assertFalse(win._showing_summary)
        self.assertTrue(win._complete)


class FullFrameCriterionTests(PatternViewTestCase):
    """'Watch until it stops getting brighter' gives the eye nothing to compare against.
    A full white screen has no reference in it, so the judgement cannot be made; the same
    disappearing-shape test as the other steps can."""

    def test_the_whole_screen_is_lit_not_just_a_window(self):
        win = self.window(width=400, height=300)
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "full-frame-white"))
        win.set_probe(300.0)
        frame = win.build_frame()
        corner = struct.unpack_from("<4e", frame, 0)[0]
        self.assertGreater(corner, 0.0, "the corner is black, so the limiter is not engaged")

    def test_there_is_a_shape_brighter_than_its_surround_to_judge(self):
        win = self.window(width=400, height=300)
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "full-frame-white"))
        win.set_probe(300.0)
        frame = win.build_frame()
        width, height = win.device_size()
        corner = struct.unpack_from("<4e", frame, 0)[0]
        centre = struct.unpack_from("<4e", frame, ((height // 2) * width + width // 2) * 8)[0]
        from sdr_hdr_profile_creator.patterns import SHAPE_CONTRAST

        self.assertAlmostEqual(centre / corner, SHAPE_CONTRAST, places=2,
                               msg="nothing to separate, so nothing to judge")

    def test_it_matches_the_criterion_of_the_step_before_it(self):
        """Two steps that ask the same question should ask it the same way."""
        from sdr_hdr_profile_creator.patterns import pattern_by_key

        peak = pattern_by_key("peak-white").criterion
        full = pattern_by_key("full-frame-white").criterion
        self.assertIn("separates", peak)
        self.assertIn("separates", full)


class SummaryPlacementTests(PatternViewTestCase):
    """Edge placement exists to keep stray light away from the patch being measured. A
    finished screen has no patch, so hiding the results in a corner serves nothing."""

    def finished(self, width=1200, height=800):
        win = PatternWindow(capability(), 240.0, self.controls, measure=lambda *_: None)
        win.resize(width, height)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        for value in (0.004, 940.0, 612.0):
            win.set_probe(value)
            win.confirm_step()
        win.confirm_step()   # the tone-tracking step records nothing
        return win

    def ink_columns(self, win):
        frame = win.build_frame()
        width, height = win.device_size()
        return [x for x in range(0, width, 8)
                if any(struct.unpack_from("<4e", frame, (y * width + x) * 8)[0] > 0
                       for y in range(0, height, 8))]

    def test_the_results_are_centred_not_pushed_to_an_edge(self):
        win = self.finished()
        columns = self.ink_columns(win)
        self.assertTrue(columns)
        width, _ = win.device_size()
        centre = (columns[0] + columns[-1]) / 2
        self.assertAlmostEqual(centre / width, 0.5, delta=0.08)

    def test_the_finished_text_is_brighter_than_the_measuring_overlay(self):
        from sdr_hdr_profile_creator.pattern_view import SUMMARY_NITS
        from sdr_hdr_profile_creator.patterns import OVERLAY_NITS

        self.assertGreater(SUMMARY_NITS, OVERLAY_NITS)

    def test_all_three_readings_appear(self):
        win = self.finished()
        raw, pw, ph = win.render_summary(600, 500, 1.0)
        rows = [any(raw[(y * pw + x) * 4 + 3] for x in range(pw)) for y in range(ph)]
        bands, run = [], 0
        for filled in rows:
            if filled:
                run += 1
            elif run:
                bands.append(run)
                run = 0
        if run:
            bands.append(run)
        self.assertGreaterEqual(len(bands), 5, f"only {len(bands)} lines; a reading is missing")


class MeasuredPeakFeedbackTests(PatternViewTestCase):
    """The overlay was still quoting the panel's EDID after the user had measured the real
    figure on the same screen thirty seconds earlier."""

    def build(self):
        win = PatternWindow(capability(max_nits=1080.0), 240.0, self.controls,
                            measure=lambda *_: None)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        return win

    def test_a_measured_peak_replaces_the_panels_claim(self):
        win = self.build()
        self.assertEqual(win._context.peak_nits, 1080.0)
        self.advance_step(win, 0.004)    # black level
        self.advance_step(win, 1127.0)   # peak white
        self.assertAlmostEqual(win._context.peak_nits, 1127.0)

    def test_a_measured_full_frame_replaces_it_too(self):
        win = self.build()
        self.advance_step(win, 0.004)
        self.advance_step(win, 1127.0)
        self.advance_step(win, 280.0)    # full frame
        self.assertAlmostEqual(win._context.max_full_frame_nits, 280.0)

    def test_later_patterns_use_the_measured_ceiling(self):
        """Otherwise the staircase and tracking cells would still be scaled to the claim."""
        win = self.build()
        self.advance_step(win, 0.004)
        self.advance_step(win, 600.0)
        self.assertAlmostEqual(win._context.ceiling_nits, 600.0)

    def test_measuring_clears_an_assumed_peak_warning(self):
        """Once it is measured it is no longer assumed, and must stop saying so."""
        win = PatternWindow(capability(max_nits=0.0), 240.0, self.controls,
                            measure=lambda *_: None)
        self.addCleanup(win.deleteLater)
        win.resize(800, 600)
        win._apply_guided_step()
        self.assertTrue(win._assumed_peak)
        self.advance_step(win, 0.004)
        self.advance_step(win, 700.0)
        self.assertFalse(win._assumed_peak)


class LiveApplyTests(PatternViewTestCase):
    def test_the_window_reports_when_it_closes(self):
        """The editor uses this to put Live Apply back as the user had it."""
        closed: list[bool] = []
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, on_close=lambda: closed.append(True))
        win.resize(200, 200)
        win.show()
        win.close()
        self.assertEqual(closed, [True])

    def test_closing_twice_only_reports_once(self):
        closed: list[bool] = []
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, on_close=lambda: closed.append(True))
        win.resize(200, 200)
        win.show()
        win.close()
        win.close()
        self.assertEqual(len(closed), 1)


class LevelWalkingTests(PatternViewTestCase):
    """Adaptation follows the brightest thing in view, so a near-threshold judgement has to
    be made with one level on screen and nothing else competing for the eye."""

    def tracking(self):
        win = self.window()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "tone-tracking"))
        return win

    def test_it_starts_in_the_middle_of_the_range(self):
        """Neither end is a good place to begin: one blinds, the other shows nothing."""
        from sdr_hdr_profile_creator.patterns import tone_tracking_levels

        win = self.tracking()
        levels = tone_tracking_levels(win._context)
        self.assertAlmostEqual(win.probe_nits, levels[len(levels) // 2], places=3)

    def test_up_and_down_walk_the_levels(self):
        win = self.tracking()
        start = win.probe_nits
        self.press(win, Qt.Key.Key_Up)
        self.assertGreater(win.probe_nits, start)
        self.press(win, Qt.Key.Key_Down)
        self.assertAlmostEqual(win.probe_nits, start, places=3)

    def test_the_walk_stops_at_both_ends_rather_than_wrapping(self):
        from sdr_hdr_profile_creator.patterns import tone_tracking_levels

        win = self.tracking()
        levels = tone_tracking_levels(win._context)
        for _ in range(len(levels) + 5):
            self.press(win, Qt.Key.Key_Down)
        self.assertAlmostEqual(win.probe_nits, levels[0], places=3)
        for _ in range(len(levels) + 5):
            self.press(win, Qt.Key.Key_Up)
        self.assertAlmostEqual(win.probe_nits, levels[-1], places=3)

    def test_left_and_right_still_drive_the_sliders(self):
        """Both jobs the step needs, without a mode to remember."""
        win = self.tracking()
        level = win.probe_nits
        self.press(win, Qt.Key.Key_Right)
        self.assertNotEqual(self.values["gamma"], 2.2)
        self.assertAlmostEqual(win.probe_nits, level, places=3)

    def test_a_pattern_with_no_levels_ignores_up_and_down_as_a_walk(self):
        win = self.window()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "colour-patches"))
        before = win.probe_nits
        self.press(win, Qt.Key.Key_Up)
        self.assertAlmostEqual(win.probe_nits, before, places=6)

    def test_the_overlay_offers_the_walk_only_where_it_exists(self):
        plain, _, _ = render_overlay(
            420, 700, pattern_view.pattern_by_key("colour-patches"),
            context_for(capability(), 240.0), self.controls, 0)
        stepped, _, _ = render_overlay(
            420, 700, pattern_view.pattern_by_key("tone-tracking"),
            context_for(capability(), 240.0), self.controls, 0)
        self.assertNotEqual(plain, stepped)


class ApplyFromTheSummaryTests(PatternViewTestCase):
    """Live Apply covers the sliders, but a measurement only reaches the editor state, so
    leaving without applying quietly threw all three readings away."""

    def finished(self, apply=None):
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, apply=apply)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        return win

    def test_enter_on_the_finished_screen_applies(self):
        calls: list[bool] = []
        win = self.finished(apply=lambda: calls.append(True) or True)
        self.assertTrue(win._complete)
        self.press(win, Qt.Key.Key_Return)
        self.assertEqual(len(calls), 1)
        self.assertTrue(win.applied)

    def test_applying_twice_does_nothing_the_second_time(self):
        calls: list[bool] = []
        win = self.finished(apply=lambda: calls.append(True) or True)
        self.press(win, Qt.Key.Key_Return)
        self.press(win, Qt.Key.Key_Return)
        self.assertEqual(len(calls), 1)

    def test_a_failed_apply_is_not_reported_as_applied(self):
        win = self.finished(apply=lambda: False)
        self.press(win, Qt.Key.Key_Return)
        self.assertFalse(win.applied)

    def test_the_summary_changes_once_applied(self):
        win = self.finished(apply=lambda: True)
        before, _, _ = win.render_summary(600, 500, 1.0)
        self.press(win, Qt.Key.Key_Return)
        after, _, _ = win.render_summary(600, 500, 1.0)
        self.assertNotEqual(before, after, "nothing told the user it had been written")

    def test_enter_mid_run_still_confirms_the_step_rather_than_applying(self):
        calls: list[bool] = []
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, apply=lambda: calls.append(True) or True)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.advance_step(win)
        self.assertEqual(calls, [], "Enter applied instead of advancing the run")
        self.assertEqual(win.guided_step, 2)

    def test_a_view_with_no_apply_callback_does_not_crash(self):
        win = self.finished(apply=None)
        self.press(win, Qt.Key.Key_Return)
        self.assertFalse(win.applied)


class ControlRangeTests(PatternViewTestCase):
    """A number with no range around it says nothing about how far there is left to go."""

    def binding(self, value=2.2):
        held = {"v": value}
        return ControlBinding(
            "gamma", "Gamma", lambda: held["v"],
            lambda delta: held.__setitem__("v", held["v"] + delta),
            step=0.005, minimum=1.6, maximum=3.0,
            write=lambda new: held.__setitem__("v", new),
        ), held

    def test_the_fraction_places_the_value_in_its_range(self):
        binding, _ = self.binding(2.3)
        self.assertAlmostEqual(binding.fraction(), (2.3 - 1.6) / 1.4, places=4)

    def test_the_fraction_is_bounded_even_if_the_value_is_not(self):
        binding, held = self.binding(99.0)
        self.assertEqual(binding.fraction(), 1.0)
        held["v"] = -99.0
        self.assertEqual(binding.fraction(), 0.0)

    def test_a_zero_width_range_does_not_divide_by_zero(self):
        binding = ControlBinding("x", "X", lambda: 1.0, lambda _d: None,
                                 minimum=5.0, maximum=5.0)
        self.assertEqual(binding.fraction(), 0.0)

    def test_writing_clamps_to_the_range(self):
        binding, held = self.binding()
        self.assertTrue(binding.set_value(99.0))
        self.assertEqual(held["v"], 3.0)
        binding.set_value(-99.0)
        self.assertEqual(held["v"], 1.6)

    def test_a_binding_with_no_write_reports_it_rather_than_failing(self):
        binding = ControlBinding("x", "X", lambda: 1.0, lambda _d: None)
        self.assertFalse(binding.set_value(2.0))


class TypedEntryTests(PatternViewTestCase):
    """Stepping from 2.200 to 1.700 an arrow at a time is not a control anyone would use,
    and a level-driven pattern spans four orders of magnitude."""

    def setUp(self):
        super().setUp()
        self.controls = [
            ControlBinding(
                "gamma", "Gamma", lambda: self.values["gamma"],
                lambda delta: self.values.__setitem__("gamma", self.values["gamma"] + delta),
                step=0.005, minimum=1.6, maximum=3.0,
                write=lambda v: self.values.__setitem__("gamma", v),
            ),
        ]

    def typing(self, win, text):
        self.press(win, Qt.Key.Key_E)
        for character in text:
            win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_0,
                                        Qt.KeyboardModifier.NoModifier, character))

    def tracking(self):
        win = self.window()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "tone-tracking"))
        return win

    def test_a_typed_value_is_applied_on_enter(self):
        win = self.tracking()
        self.typing(win, "1.85")
        self.press(win, Qt.Key.Key_Return)
        self.assertAlmostEqual(self.values["gamma"], 1.85)

    def test_digits_while_typing_do_not_switch_pattern(self):
        """They are pattern keys everywhere else, which is exactly the trap."""
        win = self.tracking()
        before = win.pattern.key
        self.typing(win, "2.5")
        self.assertEqual(win.pattern.key, before)

    def test_escape_while_typing_cancels_instead_of_leaving(self):
        """Esc closes the view everywhere else; losing the session over a typo would be
        an unpleasant surprise."""
        win = self.tracking()
        closed = []
        win.close = lambda: closed.append(True)
        self.typing(win, "2.5")
        self.press(win, Qt.Key.Key_Escape)
        self.assertEqual(closed, [], "the view closed instead of cancelling the edit")
        self.assertAlmostEqual(self.values["gamma"], 2.2)

    def test_backspace_corrects_a_mistake(self):
        win = self.tracking()
        self.typing(win, "1.855")
        self.press(win, Qt.Key.Key_Backspace)
        self.press(win, Qt.Key.Key_Return)
        self.assertAlmostEqual(self.values["gamma"], 1.85)

    def test_nonsense_is_discarded_rather_than_applied(self):
        win = self.tracking()
        self.typing(win, "..")
        self.press(win, Qt.Key.Key_Return)
        self.assertAlmostEqual(self.values["gamma"], 2.2)

    def test_a_typed_value_out_of_range_is_clamped(self):
        win = self.tracking()
        self.typing(win, "99")
        self.press(win, Qt.Key.Key_Return)
        self.assertAlmostEqual(self.values["gamma"], 3.0)

    def test_a_level_driven_pattern_takes_a_typed_level(self):
        win = self.window()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "peak-white"))
        self.typing(win, "812")
        self.press(win, Qt.Key.Key_Return)
        self.assertAlmostEqual(win.probe_nits, 812.0)

    def test_a_typed_level_cannot_exceed_st2084(self):
        win = self.window()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "peak-white"))
        self.typing(win, "99999")
        self.press(win, Qt.Key.Key_Return)
        self.assertLessEqual(win.probe_nits, 10000.0)

    def test_the_finished_screen_is_not_editable(self):
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, apply=lambda: True)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        self.assertFalse(win.begin_edit())

    def test_the_overlay_shows_what_is_being_typed(self):
        win = self.tracking()
        plain = win.build_frame()
        self.typing(win, "1.8")
        self.assertNotEqual(win.build_frame(), plain)


class TrackRenderingTests(PatternViewTestCase):
    def test_the_track_moves_with_the_value(self):
        low = ControlBinding("g", "Gamma", lambda: 1.7, lambda _d: None,
                             minimum=1.6, maximum=3.0)
        high = ControlBinding("g", "Gamma", lambda: 2.9, lambda _d: None,
                              minimum=1.6, maximum=3.0)
        context = context_for(capability(), 240.0)
        left, _, _ = pattern_view.render_overlay_fitted(
            461, 918, pattern_view.pattern_by_key("tone-tracking"), context, [low], 0)
        right, _, _ = pattern_view.render_overlay_fitted(
            461, 918, pattern_view.pattern_by_key("tone-tracking"), context, [high], 0)
        self.assertNotEqual(left, right, "the track did not move with the value")

    def test_a_level_driven_pattern_draws_its_own_track(self):
        from dataclasses import replace

        context = context_for(capability(), 240.0)
        dim, _, _ = render_overlay(420, 700, pattern_view.pattern_by_key("peak-white"),
                                   replace(context, probe_nits=10.0), self.controls, 0)
        bright, _, _ = render_overlay(420, 700, pattern_view.pattern_by_key("peak-white"),
                                      replace(context, probe_nits=900.0), self.controls, 0)
        self.assertNotEqual(dim, bright)


class OverlayFittingTests(PatternViewTestCase):
    """Scale picked from resolution alone clipped the controls off the bottom at 1440p --
    the sliders and key hints were simply not on the panel, with nothing to say so."""

    CONTROLS = None

    def setUp(self):
        super().setUp()
        self.CONTROLS = [
            ControlBinding(f"c{index}", label, lambda: 2.2, lambda _d: None,
                           minimum=1.6, maximum=3.0)
            for index, label in enumerate(
                ("Gamma / Midtone Response", "Midtone Brightness", "Contrast / Tonal Separation"))
        ]

    def panel(self, screen_width, screen_height):
        scale = max(1.0, min(3.0, screen_width / pattern_view.OVERLAY_REFERENCE_WIDTH))
        width = min(screen_width, max(pattern_view.OVERLAY_MIN_WIDTH,
                                      round(screen_width * pattern_view.OVERLAY_WIDTH_FRACTION)))
        return width, round(screen_height * 0.85), scale

    def test_every_pattern_fits_at_every_common_resolution(self):
        context = context_for(capability(), 240.0)
        for screen in ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)):
            width, height, scale = self.panel(*screen)
            for pattern in PATTERNS:
                with self.subTest(screen=screen, pattern=pattern.key):
                    raw, _w, _h = pattern_view.render_overlay_fitted(
                        width, height, pattern, context, self.CONTROLS, 0, scale=scale)
                    self.assertLess(pattern_view._ink_extent(raw, width, height), height - 2,
                                    "content runs off the bottom of the panel")

    def test_content_that_fits_is_not_shrunk_needlessly(self):
        """Shrinking text nobody needed shrunk would make every panel harder to read."""
        context = context_for(capability(), 240.0)
        width, height, scale = self.panel(3840, 2160)
        short = pattern_view.pattern_by_key("neutral-ramp")
        fitted, _w, _h = pattern_view.render_overlay_fitted(
            width, height, short, context, [], 0, scale=scale)
        plain, _w2, _h2 = render_overlay(width, height, short, context, [], 0, scale=scale)
        self.assertEqual(fitted, plain)

    def test_the_extent_helper_finds_the_lowest_drawn_row(self):
        blank = bytes(4 * 10 * 10)
        self.assertEqual(pattern_view._ink_extent(blank, 10, 10), -1)
        marked = bytearray(blank)
        marked[(4 * 10 + 2) * 4 + 3] = 255
        self.assertEqual(pattern_view._ink_extent(bytes(marked), 10, 10), 4)


class MouseDraggingTests(PatternViewTestCase):
    """A cursor was hidden here because of the light it adds. A black pointer with a grey
    edge, drawn by the compositor at SDR white, works out around nine nits over a few
    hundred pixels -- against a patch that can be five hundred across a million."""

    def setUp(self):
        super().setUp()
        self.held = {"gamma": 2.2}
        self.controls = [
            ControlBinding(
                "gamma", "Gamma", lambda: self.held["gamma"],
                lambda delta: self.held.__setitem__("gamma", self.held["gamma"] + delta),
                minimum=1.6, maximum=3.0,
                write=lambda value: self.held.__setitem__("gamma", value),
            ),
        ]

    def tracking(self):
        win = self.window(width=1600, height=900)
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "tone-tracking"))
        win.build_frame()
        return win

    def test_the_cursor_is_dark_rather_than_hidden(self):
        cursor = pattern_view.dim_cursor()
        self.assertFalse(cursor.pixmap().isNull())
        image = cursor.pixmap().toImage()
        lit = [image.pixelColor(x, y) for x in range(image.width())
               for y in range(image.height()) if image.pixelColor(x, y).alpha() > 0]
        self.assertTrue(lit, "the cursor is empty")
        self.assertLess(max(colour.red() for colour in lit), 140,
                        "a bright cursor is exactly what was being avoided")

    def test_tracks_report_where_they_were_drawn(self):
        """Recomputing this layout in the view would drift from the one that drew it."""
        win = self.tracking()
        self.assertTrue(win._tracks)
        key, _x, _y, width, _h = win._tracks[0]
        self.assertEqual(key, "gamma")
        self.assertGreater(width, 0)

    def test_dragging_sets_the_value_from_the_position(self):
        win = self.tracking()
        key, track_x, _y, track_width, _h = win._tracks[0]
        origin_x, _origin_y = win._overlay_origin
        win._set_from_track(key, origin_x + track_x + track_width * 0.75)
        self.assertAlmostEqual(self.held["gamma"], 1.6 + 0.75 * 1.4, places=2)

    def test_dragging_past_either_end_clamps(self):
        win = self.tracking()
        key, track_x, _y, track_width, _h = win._tracks[0]
        origin_x, _origin_y = win._overlay_origin
        win._set_from_track(key, origin_x + track_x + track_width * 5)
        self.assertAlmostEqual(self.held["gamma"], 3.0, places=3)
        win._set_from_track(key, origin_x + track_x - track_width * 5)
        self.assertAlmostEqual(self.held["gamma"], 1.6, places=3)

    def test_a_point_away_from_every_track_hits_nothing(self):
        win = self.tracking()
        self.assertIsNone(win._track_at(5, 5))

    def test_a_level_driven_pattern_offers_its_probe_track(self):
        win = self.window(width=1600, height=900)
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "peak-white"))
        win.build_frame()
        self.assertTrue(any(key == pattern_view.PROBE_TRACK_KEY for key, *_ in win._tracks))

    def test_the_probe_track_moves_in_pq_not_nits(self):
        """A linear bar would spend nearly its whole length on highlights."""
        win = self.window(width=1600, height=900)
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "peak-white"))
        win.build_frame()
        entry = next(t for t in win._tracks if t[0] == pattern_view.PROBE_TRACK_KEY)
        _key, track_x, _y, track_width, _h = entry
        origin_x, _origin_y = win._overlay_origin
        win._set_from_track(pattern_view.PROBE_TRACK_KEY, origin_x + track_x + track_width * 0.5)
        midpoint = win.probe_nits
        self.assertLess(midpoint, 500.0, "halfway is nowhere near halfway in nits")
        self.assertGreater(midpoint, 10.0)


class LastStepGuidanceTests(PatternViewTestCase):
    """The final step told the user to press Esc, which discards every measurement and
    skips the screen offering to save them. They pressed it, because it said to."""

    def overlay_for(self, step, total):
        return render_overlay(
            600, 1400, pattern_view.pattern_by_key("tone-tracking"),
            context_for(capability(), 240.0), self.controls, 0, step=step, total=total)

    def rendered_text_differs(self, a, b):
        return a[0] != b[0]

    def test_the_last_step_says_enter_rather_than_next_step(self):
        middle = self.overlay_for(2, 4)
        final = self.overlay_for(4, 4)
        self.assertTrue(self.rendered_text_differs(middle, final),
                        "the last step reads the same as a middle one")

    def test_the_source_no_longer_tells_the_user_to_press_escape(self):
        """A direct check on the text, since it is the instruction that caused the loss."""
        from pathlib import Path

        source = Path(pattern_view.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Esc, then Apply Edits in the app", source)

    def test_escape_is_described_by_what_it_actually_does(self):
        """The first fix replaced a false instruction with its mirror image: Esc was said
        to discard readings that _record_measurement had already written and persisted."""
        from pathlib import Path

        source = Path(pattern_view.__file__).read_text(encoding="utf-8")
        self.assertNotIn("discards the measurements", source)
        self.assertIn("stay in the editor", source)

    def test_pressing_enter_on_the_last_step_reaches_the_summary(self):
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, apply=lambda: True)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        self.assertTrue(win._complete, "Enter did not finish the run")
        raw, width, height = win.render_summary(600, 500, 1.0)
        self.assertGreater(pattern_view._ink_extent(raw, width, height), 0)


class MeasurementPersistenceHonestyTests(PatternViewTestCase):
    """_record_measurement writes into editor state and persists at the moment of capture,
    and the only close handler restores Live Apply. Leaving therefore discards nothing, and
    saying otherwise would strand a bad reading in the profile of a user who believed they
    had thrown it away."""

    def source(self):
        from pathlib import Path

        return Path(pattern_view.__file__).read_text(encoding="utf-8")

    def test_nothing_claims_that_leaving_discards_measurements(self):
        for phrase in ("discards the measurements", "discard them and leave",
                       "threw the three readings away"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.source())

    def test_the_summary_says_where_the_readings_go_instead(self):
        self.assertIn("stay in the editor", self.source())


class SummaryReturnTests(PatternViewTestCase):
    """The summary invites browsing the patterns, so there has to be a way back. Without
    one, a single digit key destroyed the results screen permanently."""

    def finished(self):
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, apply=lambda: True)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        return win

    def test_the_summary_is_showing_when_the_run_ends(self):
        win = self.finished()
        self.assertTrue(win._complete)
        self.assertTrue(win._showing_summary)

    def test_browsing_a_pattern_leaves_the_summary_but_not_the_finished_state(self):
        win = self.finished()
        win.select_pattern(0)
        self.assertFalse(win._showing_summary)
        self.assertTrue(win._complete, "the run was forgotten, so there is no way back")

    def test_s_returns_to_the_summary(self):
        win = self.finished()
        win.select_pattern(0)
        self.press(win, Qt.Key.Key_S)
        self.assertTrue(win._showing_summary)

    def test_s_does_nothing_before_the_run_has_finished(self):
        win = self.window()
        self.press(win, Qt.Key.Key_S)
        self.assertFalse(win._showing_summary)

    def test_applying_still_works_after_browsing_and_returning(self):
        applied: list[bool] = []
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None,
                            apply=lambda: applied.append(True) or True)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        win.select_pattern(0)
        self.press(win, Qt.Key.Key_S)
        self.press(win, Qt.Key.Key_Return)
        self.assertEqual(len(applied), 1)

    def test_the_summary_says_how_to_come_back(self):
        from pathlib import Path

        self.assertIn("S returns here",
                      Path(pattern_view.__file__).read_text(encoding="utf-8"))


class SummaryFittingTests(PatternViewTestCase):
    """render_summary walks its own y with unconditional increments and QPainter clips at
    the image edge without complaint, so the readings could scroll off the one screen whose
    entire job is showing them."""

    def finished(self, width=1200, height=800):
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda *_: None, apply=lambda: True)
        win.resize(width, height)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        self.complete_run(win)
        return win

    def test_the_summary_fits_at_every_common_shape(self):
        win = self.finished()
        for width, height in ((460, 300), (653, 648), (922, 500), (1306, 1296)):
            with self.subTest(panel=(width, height)):
                raw, _w, _h = win._fitted_summary(width, height, 2.0)
                self.assertLess(pattern_view._ink_extent(raw, width, height), height - 2,
                                "the readings run off the bottom of the results screen")

    def test_a_summary_that_already_fits_is_not_shrunk(self):
        win = self.finished()
        fitted, _w, _h = win._fitted_summary(1306, 1296, 2.0)
        plain, _w2, _h2 = win.render_summary(1306, 1296, 2.0)
        self.assertEqual(fitted, plain)

    def test_every_reading_survives_a_cramped_panel(self):
        win = self.finished()
        raw, width, height = win._fitted_summary(460, 300, 2.0)
        rows = [any(raw[(y * width + x) * 4 + 3] for x in range(width)) for y in range(height)]
        bands, run = [], 0
        for filled in rows:
            if filled:
                run += 1
            elif run:
                bands.append(run)
                run = 0
        if run:
            bands.append(run)
        self.assertGreaterEqual(len(bands), 5, f"only {len(bands)} lines survived")


class UntouchedProbeTests(PatternViewTestCase):
    """Every step opens somewhere plausible so the first press moves towards the answer.
    That convenience made an untouched step look exactly like a completed one: three
    starting values were once recorded and reported back as measurements."""

    def build(self):
        recorded: list[tuple[str, float]] = []
        win = PatternWindow(capability(), 240.0, self.controls,
                            measure=lambda key, nits: recorded.append((key, nits)))
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        return win, recorded

    def test_enter_on_an_untouched_step_records_nothing(self):
        win, recorded = self.build()
        self.assertFalse(win.accept_measurement())
        self.assertEqual(recorded, [])

    def test_it_does_not_advance_either(self):
        """Skipping forward would leave a gap that looks like a completed run."""
        win, _ = self.build()
        self.press(win, Qt.Key.Key_Return)
        self.assertEqual(win.guided_step, 1)

    def test_moving_the_level_makes_it_recordable(self):
        win, recorded = self.build()
        self.press(win, Qt.Key.Key_Right)
        self.assertTrue(win.accept_measurement())
        self.assertEqual(len(recorded), 1)

    def test_typing_a_value_counts_as_moving_it(self):
        win, recorded = self.build()
        win.set_probe(0.004)
        self.assertTrue(win.accept_measurement())
        self.assertEqual(len(recorded), 1)

    def test_each_new_step_starts_untouched_again(self):
        """Otherwise one adjustment would authorise every later step."""
        win, _ = self.build()
        self.press(win, Qt.Key.Key_Right)
        win.accept_measurement()
        self.assertEqual(win.guided_step, 2)
        self.assertFalse(win.accept_measurement())

    def test_the_overlay_says_why_nothing_happened(self):
        win, _ = self.build()
        untouched = win.build_frame()
        self.press(win, Qt.Key.Key_Right)
        self.assertNotEqual(win.build_frame(), untouched)

    def test_a_pattern_reached_by_number_key_also_starts_untouched(self):
        win, recorded = self.build()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "peak-white"))
        self.assertFalse(win.accept_measurement())


class PanelDeclarationTests(PatternViewTestCase):
    """The panel's own EDID outranks DXGI where both answer, because DXGI repeats peak in
    place of maximum frame-average. On the display this was measured against that is 1010
    nits where the panel declares 265 -- so the full-frame step opened four times too high
    and every arrow press was spent climbing back down."""

    def panel(self, **overrides):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        base = dict(peak_nits=1015.24, max_frame_average_nits=265.05,
                    min_nits=0.0002, supports_pq=True)
        base.update(overrides)
        return PanelMetadata(**base)

    def test_the_declaration_replaces_dxgis_repeated_peak(self):
        context = context_for(capability(max_nits=1010.4, max_full_frame_nits=1010.4),
                              240.0, self.panel())
        self.assertAlmostEqual(context.peak_nits, 1015.24, places=1)
        self.assertAlmostEqual(context.max_full_frame_nits, 265.05, places=1)

    def test_dxgi_is_used_when_the_panel_declares_nothing(self):
        context = context_for(capability(max_nits=1010.4, max_full_frame_nits=1010.4),
                              240.0, None)
        self.assertAlmostEqual(context.max_full_frame_nits, 1010.4, places=1)

    def test_an_incredible_declaration_is_ignored(self):
        context = context_for(capability(max_nits=1010.4, max_full_frame_nits=1010.4),
                              240.0, self.panel(supports_pq=False))
        self.assertAlmostEqual(context.peak_nits, 1010.4, places=1)

    def test_the_full_frame_step_now_opens_near_the_right_answer(self):
        win = PatternWindow(capability(), 240.0, self.controls,
                            panel=self.panel(), measure=lambda *_: None)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        for _ in range(2):
            self.advance_step(win)
        self.assertEqual(win.pattern.key, "full-frame-white")
        self.assertLess(win.probe_nits, 300.0,
                        "still opening near peak, so the step starts far above the answer")

    def test_a_declared_peak_stops_the_assumed_label(self):
        """A panel that answered is not an assumption, whatever DXGI managed."""
        win = PatternWindow(capability(max_nits=0.0), 240.0, self.controls,
                            panel=self.panel(), measure=lambda *_: None)
        self.addCleanup(win.deleteLater)
        win.resize(800, 600)
        self.assertFalse(win._assumed_peak)


class DeclaredBesideMeasuredTests(PatternViewTestCase):
    """Agreement between the declaration and the reading is reassurance; a gap is the more
    interesting result. Either way the user should not have to remember the number."""

    def panel(self, **overrides):
        from sdr_hdr_profile_creator.edid import PanelMetadata

        base = dict(peak_nits=1015.24, max_frame_average_nits=265.05,
                    min_nits=0.0002, supports_pq=True)
        base.update(overrides)
        return PanelMetadata(**base)

    def view(self, panel=True):
        win = PatternWindow(capability(), 240.0, self.controls,
                            panel=self.panel() if panel else None,
                            measure=lambda *_: None)
        win.resize(800, 600)
        self.addCleanup(win.deleteLater)
        win._apply_guided_step()
        return win

    def test_each_measured_step_is_paired_with_its_declaration(self):
        win = self.view()
        expected = {"black-level": 0.0002, "peak-white": 1015.24, "full-frame-white": 265.05}
        for key, value in expected.items():
            win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == key))
            with self.subTest(step=key):
                self.assertAlmostEqual(win._declared_for(win.pattern), value, places=2)

    def test_a_step_the_panel_says_nothing_about_shows_nothing(self):
        win = self.view()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "tone-tracking"))
        self.assertIsNone(win._declared_for(win.pattern))

    def test_no_declaration_at_all_shows_nothing(self):
        win = self.view(panel=False)
        self.assertIsNone(win._declared_for(win.pattern))

    def test_the_declaration_reaches_the_overlay(self):
        plain, _, _ = render_overlay(
            460, 900, pattern_view.pattern_by_key("peak-white"),
            context_for(capability(), 240.0), self.controls, 0)
        with_claim, _, _ = render_overlay(
            460, 900, pattern_view.pattern_by_key("peak-white"),
            context_for(capability(), 240.0), self.controls, 0, declared=1015.24)
        self.assertNotEqual(plain, with_claim)

    def test_the_declaration_does_not_move_when_a_reading_replaces_the_context(self):
        """It is the panel's claim; a measurement must not silently rewrite it."""
        win = self.view()
        win.select_pattern(next(i for i, p in enumerate(PATTERNS) if p.key == "peak-white"))
        win.set_probe(1170.0)
        win.accept_measurement()
        self.assertAlmostEqual(win._declared_for(pattern_view.pattern_by_key("peak-white")),
                               1015.24, places=2)
