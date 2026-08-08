from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sdr_hdr_profile_creator.curves import build_transform
from sdr_hdr_profile_creator.gamma_correction import pq_inverse_eotf, resolve_white_level, transform_piecewise_srgb_to_gamma22
from sdr_hdr_profile_creator.icc import _read_tags, build_profile, import_profile
from sdr_hdr_profile_creator.model import ApplicationState, ModeState


class CoreTests(unittest.TestCase):
    def test_final_state_defaults_to_hdr(self):
        self.assertEqual(ApplicationState.neutral().current_mode, "HDR")

    def test_removed_controls_are_neutralized(self):
        state = ModeState.from_dict(
            {
                "exposure": 3,
                "low_lights": 50,
                "mid_lights": -50,
                "high_lights": 70,
                "gamma_conversion": "sRGB Piecewise → Gamma 2.4",
                "gamma_fix_enabled": True,
            },
            "HDR",
        )
        self.assertEqual(state.exposure, 0.0)
        self.assertEqual(state.low_lights, 0.0)
        self.assertEqual(state.mid_lights, 0.0)
        self.assertEqual(state.high_lights, 0.0)
        self.assertEqual(state.gamma_conversion, "None")
        self.assertFalse(hasattr(state, "gamma_fix_enabled"))

    def test_sdr_comparison_transform_is_identity(self):
        state = ModeState.neutral("SDR")
        state.temperature = 500
        state.red_channel = 4
        state.gamma = 2.4
        state.brightness_trim = 5
        state.contrast = 5
        transform = build_transform(state, hdr=False)
        identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        self.assertEqual(transform.matrix, identity)
        for index in (0, 1, len(transform.red) // 2, len(transform.red) - 2, len(transform.red) - 1):
            self.assertAlmostEqual(transform.red[index], index / (len(transform.red) - 1), places=7)

    def test_hdr_neutral_is_exact_identity(self):
        state = ModeState.neutral("HDR")
        transform = build_transform(state, hdr=True)
        identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        self.assertEqual(transform.matrix, identity)
        for index in (0, 1, len(transform.red) // 4, len(transform.red) // 2, len(transform.red) - 2, len(transform.red) - 1):
            x = index / (len(transform.red) - 1)
            self.assertAlmostEqual(transform.red[index], x, places=7)
            self.assertAlmostEqual(transform.green[index], x, places=7)
            self.assertAlmostEqual(transform.blue[index], x, places=7)

    def test_traditional_gamma_uses_220_as_identity(self):
        neutral = ModeState.neutral("HDR")
        darker = ModeState.neutral("HDR")
        darker.gamma = 2.35
        t0 = build_transform(neutral, hdr=True)
        t1 = build_transform(darker, hdr=True)
        index = len(t0.red) // 3
        x = index / (len(t0.red) - 1)
        self.assertAlmostEqual(t0.red[index], x, places=6)
        self.assertLess(t1.red[index], x)

    def test_brightness_and_contrast_are_fine_endpoint_preserving_controls(self):
        state = ModeState.neutral("HDR")
        state.brightness_trim = 8.0
        state.contrast = 8.0
        transform = build_transform(state, hdr=True)
        self.assertEqual(transform.red[0], 0.0)
        self.assertEqual(transform.red[-1], 1.0)
        mid = len(transform.red) // 2
        self.assertNotAlmostEqual(transform.red[mid], mid / (len(transform.red) - 1), places=5)
        self.assertTrue(all(a <= b for a, b in zip(transform.red, transform.red[1:])))

    def test_chromatic_controls_use_matrix_not_per_channel_luts(self):
        state = ModeState.neutral("HDR")
        state.temperature = 400
        state.tint = 1.5
        state.red_channel = 1.25
        state.green_channel = -0.5
        state.blue_channel = -0.75
        state.saturation = 4.0
        transform = build_transform(state, hdr=True)
        self.assertEqual(transform.red, transform.green)
        self.assertEqual(transform.red, transform.blue)
        identity_matrix = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        self.assertNotEqual(transform.matrix, identity_matrix)

    def test_temperature_and_tint_are_smooth_and_symmetric_near_zero(self):
        warm = ModeState.neutral("HDR")
        cool = ModeState.neutral("HDR")
        magenta = ModeState.neutral("HDR")
        green = ModeState.neutral("HDR")
        warm.temperature = 5
        cool.temperature = -5
        magenta.tint = 0.05
        green.tint = -0.05
        mw = build_transform(warm, True).matrix
        mc = build_transform(cool, True).matrix
        mm = build_transform(magenta, True).matrix
        mg = build_transform(green, True).matrix
        # Fine-step changes should be tiny rather than large channel casts.
        self.assertLess(max(abs(v - i) for v, i in zip(mw, (1,0,0,0,0,1,0,0,0,0,1,0))), 0.01)
        self.assertLess(max(abs(v - i) for v, i in zip(mm, (1,0,0,0,0,1,0,0,0,0,1,0))), 0.01)
        self.assertNotEqual(mw, mc)
        self.assertNotEqual(mm, mg)

    def test_hdr_roundtrip_restores_new_visible_controls(self):
        state = ModeState.neutral("HDR")
        state.temperature = 235
        state.tint = -1.35
        state.red_channel = 0.65
        state.green_channel = -0.20
        state.blue_channel = -0.45
        state.gamma = 2.315
        state.saturation = 3.4
        state.brightness_trim = 1.25
        state.contrast = -2.10
        state.brightness = 55
        blob = build_profile("HDR", state, build_transform(state, hdr=True))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.icm"
            path.write_bytes(blob)
            imported = import_profile(path, "HDR")
        self.assertTrue(imported.exact_state)
        self.assertEqual(imported.state.temperature, 235)
        self.assertAlmostEqual(imported.state.tint, -1.35)
        self.assertAlmostEqual(imported.state.red_channel, 0.65)
        self.assertAlmostEqual(imported.state.gamma, 2.315)
        self.assertAlmostEqual(imported.state.saturation, 3.4)
        self.assertAlmostEqual(imported.state.brightness_trim, 1.25)
        self.assertAlmostEqual(imported.state.contrast, -2.10)
        self.assertEqual(imported.state.brightness, 55)

    def test_hdr_profile_has_mhc2_and_no_vcgt(self):
        state = ModeState.neutral("HDR")
        blob = build_profile("HDR", state, build_transform(state, hdr=True))
        tags = _read_tags(blob)
        self.assertIn(b"MHC2", tags)
        self.assertNotIn(b"vcgt", tags)
        self.assertIn(b"sdhs", tags)

    def test_dylan_direction_darkens_sdr_midtones_and_leaves_hdr_above_white_untouched(self):
        white = 200.0
        mid_input = pq_inverse_eotf(10.0)
        corrected = transform_piecewise_srgb_to_gamma22(mid_input, white)
        self.assertLess(corrected, mid_input)
        hdr_input = pq_inverse_eotf(500.0)
        self.assertAlmostEqual(transform_piecewise_srgb_to_gamma22(hdr_input, white), hdr_input, places=12)

    def test_auto_white_resolution_uses_windows_readback(self):
        self.assertEqual(resolve_white_level("Auto (Recommended)", 312.5), 312.5)
        self.assertEqual(resolve_white_level("Auto (Recommended)", None), 200.0)
        self.assertEqual(resolve_white_level("100 nits / Brightness 5", 300.0), 100.0)

    def test_gamma_correction_is_independent_from_traditional_gamma(self):
        off = ModeState.neutral("HDR")
        on = ModeState.neutral("HDR")
        on.sdr_gamma_correction = "200 nits / Brightness 30"
        t_off = build_transform(off, True)
        t_on = build_transform(on, True)
        self.assertAlmostEqual(t_off.red[len(t_off.red)//4], (len(t_off.red)//4)/(len(t_off.red)-1), places=6)
        self.assertNotEqual(t_off.red, t_on.red)
        darker = ModeState.neutral("HDR")
        darker.sdr_gamma_correction = "200 nits / Brightness 30"
        darker.gamma = 2.35
        self.assertNotEqual(build_transform(darker, True).red, t_on.red)


if __name__ == "__main__":
    unittest.main()
