"""Behaviour of the calibration patterns.

These are measuring instruments, so the properties worth asserting are the ones that make
a reading trustworthy: that a value means the luminance it claims, that the gamma-match
lines land on individual device pixels, and that nothing asks the panel for light it
cannot produce.
"""

from __future__ import annotations

import struct
import unittest

from sdr_hdr_profile_creator.gamma_correction import pq_eotf, pq_inverse_eotf
from sdr_hdr_profile_creator.patterns import (
    PATTERNS,
    REFERENCE_WHITE_NITS,
    PatternContext,
    compose,
    pattern_by_key,
    render,
    window_size,
)

HDR = PatternContext(is_hdr=True, sdr_white_nits=240.0, peak_nits=1080.0, max_full_frame_nits=1080.0)
SDR = PatternContext(is_hdr=False, sdr_white_nits=240.0)


def pixel_at(frame: bytes, width: int, x: int, y: int) -> tuple[float, float, float, float]:
    return struct.unpack_from("<4e", frame, (y * width + x) * 8)


def channels(frame: bytes) -> list[float]:
    count = len(frame) // 2
    values = struct.unpack(f"<{count}e", frame)
    return [values[i] for i in range(0, count, 4)]  # red channel of every pixel


class FrameShapeTests(unittest.TestCase):
    def test_every_pattern_fills_the_surface_exactly(self):
        """A frame of the wrong length is rejected by the swapchain, not tolerated."""
        for pattern in PATTERNS:
            for width, height in ((1, 1), (17, 5), (640, 360), (1921, 1081)):
                with self.subTest(pattern=pattern.key, size=(width, height)):
                    frame = render(pattern, width, height, HDR)
                    self.assertEqual(len(frame), width * height * 8)

    def test_odd_sizes_do_not_lose_a_column(self):
        """Integer division across columns must give the remainder to the last one."""
        width, height = 1003, 7
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern.key):
                self.assertEqual(len(render(pattern, width, height, HDR)), width * height * 8)

    def test_keys_are_unique_and_resolvable(self):
        keys = [pattern.key for pattern in PATTERNS]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIsNotNone(pattern_by_key(key))
        self.assertIsNone(pattern_by_key("no-such-pattern"))


class LuminanceEncodingTests(unittest.TestCase):
    def test_hdr_values_are_absolute_luminance(self):
        """scRGB is scene-referred on HDR: 1.0 is 80 nits, so 203 nits is 2.5375."""
        self.assertTrue(HDR.absolute)
        self.assertAlmostEqual(HDR.encode(203.0), 2.5375, places=4)
        self.assertAlmostEqual(HDR.encode(80.0), 1.0, places=6)

    def test_sdr_values_are_relative_to_reference_white(self):
        """scRGB is display-referred on SDR, so absolute nits are not addressable."""
        self.assertFalse(SDR.absolute)
        self.assertAlmostEqual(SDR.encode(240.0), 1.0, places=6)
        self.assertAlmostEqual(SDR.encode(120.0), 0.5, places=6)

    def test_sdr_cannot_exceed_its_own_white(self):
        self.assertEqual(SDR.encode(5000.0), 1.0)

    def test_negative_luminance_never_encodes_negative(self):
        for context in (HDR, SDR):
            with self.subTest(hdr=context.is_hdr):
                self.assertEqual(context.encode(-10.0), 0.0)

    def test_the_ceiling_follows_the_display(self):
        self.assertEqual(HDR.ceiling_nits, 1080.0)
        self.assertEqual(SDR.ceiling_nits, 240.0)


class CeilingTests(unittest.TestCase):
    """Asking for light above the panel's peak invites chasing a difference that the
    display physically cannot show."""

    def test_no_fixed_pattern_exceeds_the_panels_peak(self):
        """Level-driven patterns are excluded: following the probe past peak is how they
        find peak in the first place."""
        dim = PatternContext(is_hdr=True, peak_nits=400.0, max_full_frame_nits=400.0)
        allowed = dim.encode(400.0) + 1e-3
        for pattern in PATTERNS:
            if pattern.level_driven:
                continue
            with self.subTest(pattern=pattern.key):
                brightest = max(channels(render(pattern, 200, 120, dim)))
                self.assertLessEqual(brightest, allowed)

    def test_a_dim_panel_still_gets_usable_shadow_patches(self):
        """Filtering the near-black ladder by the ceiling must not empty it."""
        tiny = PatternContext(is_hdr=True, peak_nits=45.0, max_full_frame_nits=45.0)
        frame = render(pattern_by_key("near-black"), 400, 200, tiny)
        self.assertGreater(len(set(round(v, 5) for v in channels(frame))), 1)


class GammaMatchTests(unittest.TestCase):
    """The pattern is only valid if its lines land on individual device pixels."""

    WIDTH, HEIGHT = 400, 200

    def setUp(self):
        self.frame = render(pattern_by_key("gamma-match"), self.WIDTH, self.HEIGHT, HDR)

    def test_lines_alternate_every_single_row(self):
        """One resampled line and the pattern reports a gamma that was never on screen."""
        edge = 2  # sample away from the centre patches
        lit = [pixel_at(self.frame, self.WIDTH, edge, y)[0] for y in range(0, 40, 2)]
        dark = [pixel_at(self.frame, self.WIDTH, edge, y)[0] for y in range(1, 41, 2)]
        self.assertTrue(all(value > 0.0 for value in lit), "even rows should be lit")
        self.assertTrue(all(value == 0.0 for value in dark), "odd rows should be black")

    def test_the_lit_line_is_at_reference_white(self):
        value = pixel_at(self.frame, self.WIDTH, 2, 0)[0]
        self.assertAlmostEqual(value, HDR.encode(REFERENCE_WHITE_NITS), places=3)

    def test_a_candidate_patch_sits_at_true_half_luminance(self):
        """scRGB is linear, so interleaved lines average to exactly half. One patch must
        be at that value or the pattern has no correct answer."""
        expected = HDR.encode(REFERENCE_WHITE_NITS / 2.0)
        centre = self.WIDTH // 2
        column = [pixel_at(self.frame, self.WIDTH, centre, y)[0] for y in range(self.HEIGHT)]
        self.assertTrue(
            any(abs(value - expected) < 1e-3 for value in column),
            f"no patch at {expected:.4f}; column held {sorted(set(round(v, 4) for v in column))}",
        )

    def test_patches_bracket_the_ideal_in_both_directions(self):
        """A mismatch should read as a direction, so candidates must straddle the answer."""
        ideal = HDR.encode(REFERENCE_WHITE_NITS / 2.0)
        centre = self.WIDTH // 2
        values = {round(pixel_at(self.frame, self.WIDTH, centre, y)[0], 5)
                  for y in range(self.HEIGHT)}
        patches = {v for v in values if v > 0.0}
        self.assertTrue(any(v < ideal - 1e-4 for v in patches), "no darker candidate")
        self.assertTrue(any(v > ideal + 1e-4 for v in patches), "no brighter candidate")


class StaircaseTests(unittest.TestCase):
    def test_steps_are_perceptually_spaced_not_linear_in_nits(self):
        """Evenly spaced nits would spend nearly every step on highlights.

        PQ spacing is the point: the gaps in luminance must grow towards the top.
        """
        frame = render(pattern_by_key("grey-staircase"), 1600, 4, HDR)
        levels = sorted({round(v, 6) for v in channels(frame)})
        nits = [value * 80.0 for value in levels]
        gaps = [b - a for a, b in zip(nits, nits[1:])]
        self.assertGreater(len(gaps), 4)
        self.assertGreater(gaps[-1], gaps[0] * 5,
                           "steps look linear in luminance, so PQ spacing was lost")

    def test_the_top_step_reaches_the_panel_ceiling(self):
        frame = render(pattern_by_key("grey-staircase"), 1600, 4, HDR)
        self.assertAlmostEqual(max(channels(frame)), HDR.encode(1080.0), places=3)

    def test_the_bottom_step_is_black(self):
        frame = render(pattern_by_key("grey-staircase"), 1600, 4, HDR)
        self.assertEqual(min(channels(frame)), 0.0)


class NeutralRampTests(unittest.TestCase):
    def test_the_ramp_rises_monotonically(self):
        frame = render(pattern_by_key("neutral-ramp"), 512, 2, HDR)
        row = [pixel_at(frame, 512, x, 0)[0] for x in range(512)]
        self.assertEqual(row, sorted(row))

    def test_the_ramp_is_neutral_at_every_point(self):
        """Any channel imbalance here would be mistaken for display tint."""
        frame = render(pattern_by_key("neutral-ramp"), 256, 2, HDR)
        for x in range(0, 256, 16):
            red, green, blue, _ = pixel_at(frame, 256, x, 0)
            with self.subTest(x=x):
                self.assertEqual((red, green), (red, blue))

    def test_the_ramp_is_evenly_spaced_in_pq(self):
        frame = render(pattern_by_key("neutral-ramp"), 256, 1, HDR)
        row = [pixel_at(frame, 256, x, 0)[0] * 80.0 for x in range(256)]
        codes = [pq_inverse_eotf(value) for value in row]
        gaps = [b - a for a, b in zip(codes, codes[1:])]
        self.assertLess(max(gaps) - min(gaps), 0.01, "ramp is not uniform in PQ")


class ColourPatchTests(unittest.TestCase):
    def test_patches_are_held_at_diffuse_white_not_peak(self):
        """At peak this would mostly measure highlight rolloff, not colour."""
        frame = render(pattern_by_key("colour-patches"), 500, 200, HDR)
        self.assertAlmostEqual(max(channels(frame)), HDR.encode(REFERENCE_WHITE_NITS), places=3)

    def test_the_first_patch_is_pure_red(self):
        frame = render(pattern_by_key("colour-patches"), 500, 200, HDR)
        red, green, blue, _ = pixel_at(frame, 500, 2, 2)
        self.assertGreater(red, 0.0)
        self.assertEqual((green, blue), (0.0, 0.0))


class CompositionTests(unittest.TestCase):
    """Every pattern is shown in one centred window on black, as display patches always
    have been. On an emissive panel it is also the only way readings stay comparable: a
    full-screen pattern engages the brightness limiter differently depending on how bright
    the pattern is, so two measurements are not measuring the same thing."""

    WIDTH, HEIGHT = 800, 600

    def frame(self, key="solid-patch", **kwargs):
        return compose(self.WIDTH, self.HEIGHT, pattern_by_key(key), HDR, **kwargs)

    def test_the_window_is_a_tenth_of_the_screen_area(self):
        width, height = window_size(3840, 2160)
        self.assertAlmostEqual((width * height) / (3840 * 2160), 0.10, places=3)

    def test_everything_outside_the_window_is_true_black(self):
        """A lit surround is itself part of what engages the limiter."""
        frame = self.frame()
        for x, y in ((0, 0), (self.WIDTH - 1, 0), (0, self.HEIGHT - 1),
                     (self.WIDTH - 1, self.HEIGHT - 1), (5, self.HEIGHT // 2)):
            with self.subTest(point=(x, y)):
                self.assertEqual(pixel_at(frame, self.WIDTH, x, y)[0], 0.0)

    def test_the_window_is_centred(self):
        frame = self.frame()
        row = [pixel_at(frame, self.WIDTH, x, self.HEIGHT // 2)[0] for x in range(self.WIDTH)]
        lit = [x for x, value in enumerate(row) if value > 0.0]
        self.assertTrue(lit)
        self.assertAlmostEqual((lit[0] + lit[-1]) / 2, (self.WIDTH - 1) / 2, delta=1.5)

    def test_every_pattern_composes_to_the_exact_surface_size(self):
        for pattern in PATTERNS:
            for width, height in ((1, 1), (37, 11), (800, 600)):
                with self.subTest(pattern=pattern.key, size=(width, height)):
                    frame = compose(width, height, pattern, HDR)
                    self.assertEqual(len(frame), width * height * 8)

    def test_the_solid_patch_sits_at_the_probe_level(self):
        """It is the patch a meter reads, so it must be exactly where it was put."""
        from dataclasses import replace

        context = replace(HDR, probe_nits=137.0)
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("solid-patch"), context)
        centre = pixel_at(frame, self.WIDTH, self.WIDTH // 2, self.HEIGHT // 2)[0]
        self.assertAlmostEqual(centre, context.encode(137.0), places=3)


class OverlayPlacementTests(unittest.TestCase):
    """Controls go hard against one edge. Anything near the patch contaminates both the
    reading and the viewer's dark adaptation."""

    WIDTH, HEIGHT = 800, 600
    OVERLAY = (bytes([255, 255, 255, 255]) * (60 * 200), 60, 200)

    def test_the_overlay_lands_on_the_right_when_asked(self):
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=self.OVERLAY, overlay_side="right")
        self.assertGreater(pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0], 0.0)
        self.assertEqual(pixel_at(frame, self.WIDTH, 2, self.HEIGHT // 2)[0], 0.0)

    def test_the_overlay_lands_on_the_left_when_asked(self):
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=self.OVERLAY, overlay_side="left")
        self.assertGreater(pixel_at(frame, self.WIDTH, 2, self.HEIGHT // 2)[0], 0.0)
        self.assertEqual(pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0], 0.0)

    def test_the_overlay_is_held_far_below_diffuse_white(self):
        """It shares the screen with the patch, so its light counts too."""
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=self.OVERLAY, overlay_side="right")
        nits = pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0] * 80.0
        self.assertLess(nits, 30.0, "an overlay this bright would engage the limiter")
        self.assertGreater(nits, 1.0, "an overlay this dim would be unreadable")

    def test_a_transparent_overlay_contributes_no_light(self):
        clear = (bytes([255, 255, 255, 0]) * (60 * 200), 60, 200)
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=clear, overlay_side="right")
        self.assertEqual(pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0], 0.0)

    def test_an_oversized_overlay_does_not_corrupt_the_frame(self):
        huge = (bytes([255, 255, 255, 255]) * (4000 * 4000), 4000, 4000)
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR, overlay=huge)
        self.assertEqual(len(frame), self.WIDTH * self.HEIGHT * 8)


class DeclaredMetadataTests(unittest.TestCase):
    """A panel quoting the same number for peak and full frame is quoting a spec."""

    def test_equal_peak_and_full_frame_is_flagged(self):
        from sdr_hdr_profile_creator.hdr_display import DisplayCapability

        from tests.test_hdr_display import capability

        panel = capability(max_nits=1080.0, max_full_frame_nits=1080.0)
        self.assertIsInstance(panel, DisplayCapability)
        self.assertTrue(panel.luminance_looks_declared)

    def test_a_panel_reporting_a_real_full_frame_figure_is_not_flagged(self):
        from tests.test_hdr_display import capability

        self.assertFalse(capability(max_nits=1080.0, max_full_frame_nits=250.0)
                         .luminance_looks_declared)

    def test_a_dim_panel_is_not_flagged(self):
        """Equal figures are plausible when there is no headroom to throttle."""
        from tests.test_hdr_display import capability

        self.assertFalse(capability(max_nits=350.0, max_full_frame_nits=350.0)
                         .luminance_looks_declared)


class GuidanceTests(unittest.TestCase):
    """A pattern nobody can act on is decoration."""

    def test_every_pattern_says_what_it_is_for_and_what_to_do(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern.key):
                self.assertTrue(pattern.title.strip())
                self.assertGreater(len(pattern.purpose), 20)
                self.assertGreater(len(pattern.instructions), 40)
                self.assertGreater(len(pattern.criterion), 20,
                                   "a pattern with no stated target cannot be aimed at")


class PqHelperSanityTests(unittest.TestCase):
    def test_pq_round_trips(self):
        for nits in (0.1, 1.0, 100.0, 203.0, 1000.0, 4000.0):
            with self.subTest(nits=nits):
                self.assertAlmostEqual(pq_eotf(pq_inverse_eotf(nits)), nits, places=2)


if __name__ == "__main__":
    unittest.main()
