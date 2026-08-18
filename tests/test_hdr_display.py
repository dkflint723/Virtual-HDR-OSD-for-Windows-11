"""Behaviour of the HDR presentation layer that does not need HDR hardware.

The swapchain itself cannot be exercised without a display, but everything that decides
*what* to send it -- luminance encoding, which output a window is on, and whether a
panel's own metadata is trustworthy -- is pure and is tested here.
"""

from __future__ import annotations

import struct
import unittest
from unittest import mock

from sdr_hdr_profile_creator import hdr_display
from sdr_hdr_profile_creator.hdr_display import (
    DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709,
    DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
    DisplayCapability,
    capability_for_rect,
    nits_to_scrgb,
    scrgb_pixel,
)


def capability(**overrides) -> DisplayCapability:
    base = dict(
        device_name="\\\\.\\DISPLAY1", left=0, top=0, right=3840, bottom=2160,
        bits_per_color=10, color_space=DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
        min_nits=0.0, max_nits=1080.0, max_full_frame_nits=1080.0,
        red_primary=(0.67, 0.31), green_primary=(0.27, 0.69),
        blue_primary=(0.15, 0.06), white_point=(0.3127, 0.329),
    )
    base.update(overrides)
    return DisplayCapability(**base)


class LuminanceEncodingTests(unittest.TestCase):
    def test_scrgb_one_is_eighty_nits(self):
        """The whole encoding hangs off this constant; Direct2D names it explicitly."""
        self.assertAlmostEqual(nits_to_scrgb(80.0), 1.0)

    def test_absolute_nits_scale_linearly(self):
        for nits, expected in ((0.0, 0.0), (203.0, 2.5375), (1000.0, 12.5), (4000.0, 50.0)):
            with self.subTest(nits=nits):
                self.assertAlmostEqual(nits_to_scrgb(nits), expected, places=6)

    def test_negative_luminance_is_clamped_not_wrapped(self):
        self.assertEqual(nits_to_scrgb(-5.0), 0.0)

    def test_a_pixel_is_four_half_floats(self):
        raw = scrgb_pixel(12.5, 12.5, 12.5)
        self.assertEqual(len(raw), 8, "R16G16B16A16_FLOAT is 8 bytes per pixel")
        self.assertEqual(struct.unpack("<4e", raw), (12.5, 12.5, 12.5, 1.0))

    def test_values_above_diffuse_white_survive_encoding(self):
        """The point of FP16: scRGB above 1.0 is legal and must not clip."""
        red, green, blue, _alpha = struct.unpack("<4e", scrgb_pixel(*(nits_to_scrgb(4000.0),) * 3))
        self.assertAlmostEqual(red, 50.0)
        self.assertEqual((red, green, blue), (red, red, red))


class DisplayKindTests(unittest.TestCase):
    def test_hdr10_output_reports_hdr(self):
        self.assertTrue(capability().is_hdr)

    def test_sdr_output_does_not_report_hdr(self):
        sdr = capability(color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709)
        self.assertFalse(sdr.is_hdr)

    def test_wcg_is_indistinguishable_from_sdr_here(self):
        """DXGI reports auto-colour-managed SDR displays as plain SDR.

        Recorded as behaviour so nobody later reads is_hdr as "not advanced colour".
        """
        wcg = capability(color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709, bits_per_color=10)
        self.assertFalse(wcg.is_hdr)


class MetadataCredibilityTests(unittest.TestCase):
    """Cheap panels report absent or nonsense luminance; calibrating to it would be worse
    than admitting we do not know."""

    def test_a_real_panel_is_credible(self):
        self.assertTrue(capability().luminance_is_credible)

    def test_zero_peak_is_not_credible(self):
        self.assertFalse(capability(max_nits=0.0).luminance_is_credible)

    def test_peak_beyond_st2084_is_not_credible(self):
        self.assertFalse(capability(max_nits=65535.0).luminance_is_credible)

    def test_missing_full_frame_is_not_credible(self):
        self.assertFalse(capability(max_full_frame_nits=0.0).luminance_is_credible)

    def test_sdr_mode_luminance_is_never_credible(self):
        """DXGI describes the mode the output is in, not the panel.

        Observed on a real display: HDR off reports 240/240 nits and BT.709 primaries,
        HDR on reports 1080/1080 and DCI-P3. 240 is the SDR reference white and passes
        any plain range check, so a calibration step would target it as if it were peak.
        """
        with_hdr_off = capability(
            color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709,
            max_nits=240.0, max_full_frame_nits=240.0,
        )
        self.assertFalse(with_hdr_off.luminance_is_credible)

    def test_the_same_panel_is_credible_once_hdr_is_on(self):
        self.assertTrue(capability(max_nits=1080.0, max_full_frame_nits=1080.0).luminance_is_credible)


class OutputSelectionTests(unittest.TestCase):
    """Microsoft's guidance is to pick the output by greatest overlap with the window,
    never via GetContainingOutput, which goes stale and blacks the screen if refreshed."""

    def setUp(self):
        self.left_monitor = capability(device_name="LEFT", left=0, right=1920, top=0, bottom=1080)
        self.right_monitor = capability(device_name="RIGHT", left=1920, right=3840, top=0, bottom=1080)
        patcher = mock.patch.object(
            hdr_display, "enumerate_display_capabilities",
            lambda: [self.left_monitor, self.right_monitor],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_window_wholly_on_one_monitor(self):
        self.assertEqual(capability_for_rect(2000, 100, 2600, 700).device_name, "RIGHT")

    def test_straddling_window_picks_the_greater_overlap(self):
        # 400px on the left monitor, 600px on the right.
        self.assertEqual(capability_for_rect(1520, 0, 2520, 1080).device_name, "RIGHT")
        # Mirrored: 600px left, 400px right.
        self.assertEqual(capability_for_rect(1320, 0, 2320, 1080).device_name, "LEFT")

    def test_window_off_every_monitor_still_returns_something(self):
        """A minimised or off-screen window must not crash the pattern view."""
        self.assertIsNotNone(capability_for_rect(-5000, -5000, -4900, -4900))

    def test_no_outputs_returns_none(self):
        with mock.patch.object(hdr_display, "enumerate_display_capabilities", lambda: []):
            self.assertIsNone(capability_for_rect(0, 0, 100, 100))


class IntersectionTests(unittest.TestCase):
    def test_disjoint_rectangles_have_no_area(self):
        self.assertEqual(capability(left=0, right=100, top=0, bottom=100)
                         .area_of_intersection(200, 200, 300, 300), 0)

    def test_contained_rectangle_area_is_its_own(self):
        self.assertEqual(capability(left=0, right=1000, top=0, bottom=1000)
                         .area_of_intersection(10, 10, 110, 60), 100 * 50)


if __name__ == "__main__":
    unittest.main()
