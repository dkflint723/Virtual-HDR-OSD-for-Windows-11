from __future__ import annotations

import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from sdr_hdr_profile_creator.curves import build_transform
from sdr_hdr_profile_creator.gamma_correction import pq_inverse_eotf, resolve_white_level, transform_piecewise_srgb_to_gamma22
from unittest import mock

from sdr_hdr_profile_creator import icc as icc_module
from sdr_hdr_profile_creator.icc import _parse_mhc2, _read_tags, build_profile, content_digest, import_profile
from sdr_hdr_profile_creator.model import ApplicationState, ModeState, normalize_primaries


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


class ProfileStructureTests(unittest.TestCase):
    """The generated bytes have to satisfy ICC.1 before Windows will honour them."""

    @staticmethod
    def profile(mode="HDR") -> bytes:
        state = ModeState.neutral(mode)
        return build_profile(mode, state, build_transform(state, hdr=mode == "HDR"))

    def test_chad_tag_is_a_nine_element_matrix(self):
        """A short s15Fixed16Array here is a malformed chromaticAdaptationTag."""
        for mode in ("HDR", "SDR"):
            with self.subTest(mode=mode):
                payload = _read_tags(self.profile(mode))[b"chad"]
                self.assertEqual(payload[:4], b"sf32")
                self.assertEqual(len(payload) - 8, 9 * 4, "chad must hold exactly nine values")

    def test_hdr_chad_is_identity(self):
        """HDR profiles carry physical D65 colorimetry, so no adaptation applies."""
        payload = _read_tags(self.profile("HDR"))[b"chad"]
        values = [struct.unpack_from(">i", payload, 8 + index * 4)[0] / 65536.0 for index in range(9)]
        self.assertEqual(values, [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])

    def test_header_size_matches_the_actual_length(self):
        blob = self.profile()
        self.assertEqual(struct.unpack_from(">I", blob, 0)[0], len(blob))
        self.assertEqual(blob[36:40], b"acsp")

    def test_every_tag_is_aligned_and_inside_the_file(self):
        blob = self.profile()
        count = struct.unpack_from(">I", blob, 128)[0]
        self.assertGreater(count, 0)
        for index in range(count):
            offset = 132 + index * 12
            signature = blob[offset : offset + 4]
            payload_offset, payload_size = struct.unpack_from(">II", blob, offset + 4)
            with self.subTest(tag=signature):
                self.assertEqual(payload_offset % 4, 0, "tag data must be 4-byte aligned")
                self.assertLessEqual(payload_offset + payload_size, len(blob))

    def test_profile_id_is_the_specified_md5(self):
        blob = self.profile()
        mutable = bytearray(blob)
        mutable[44:48] = b"\0" * 4      # flags
        mutable[64:68] = b"\0" * 4      # rendering intent
        mutable[84:100] = b"\0" * 16    # the id field itself
        self.assertEqual(blob[84:100], hashlib.md5(bytes(mutable)).digest())

    def test_mhc2_payload_reparses_to_the_curve_that_was_written(self):
        state = ModeState.neutral("HDR")
        state.gamma = 2.4
        transform = build_transform(state, hdr=True)
        parsed = _parse_mhc2(_read_tags(build_profile("HDR", state, transform))[b"MHC2"])
        self.assertIsNotNone(parsed)
        _minimum, _peak, _matrix, red, green, blue = parsed
        self.assertEqual(len(red), len(transform.red))
        for index in (0, len(red) // 2, len(red) - 1):
            self.assertAlmostEqual(red[index], transform.red[index], places=4)
        self.assertEqual(red, green)
        self.assertEqual(red, blue)

    def test_content_digest_ignores_when_the_profile_was_generated(self):
        """Identical settings must fingerprint identically at any clock time.

        The header carries a creation timestamp and the profile id is an MD5
        over it, so raw bytes differ every second. If the installed-profile
        cache keyed on raw bytes, it would reinstall for no reason.
        """
        blob = self.profile()
        aged = bytearray(blob)
        aged[24:36] = struct.pack(">6H", 1999, 1, 2, 3, 4, 5)
        aged[84:100] = b"\xab" * 16
        self.assertNotEqual(bytes(aged), blob)
        self.assertEqual(content_digest(bytes(aged)), content_digest(blob))

    def test_content_digest_still_separates_different_calibrations(self):
        neutral = ModeState.neutral("HDR")
        adjusted = ModeState.neutral("HDR")
        adjusted.gamma = 2.35
        self.assertNotEqual(
            content_digest(build_profile("HDR", neutral, build_transform(neutral, hdr=True))),
            content_digest(build_profile("HDR", adjusted, build_transform(adjusted, hdr=True))),
        )

    def test_luminance_metadata_round_trips_through_mhc2(self):
        state = ModeState.neutral("HDR")
        state.minimum_luminance_nits = 0.005
        state.peak_luminance_nits = 1450.0
        parsed = _parse_mhc2(
            _read_tags(build_profile("HDR", state, build_transform(state, hdr=True)))[b"MHC2"]
        )
        self.assertIsNotNone(parsed)
        minimum, peak = parsed[0], parsed[1]
        self.assertAlmostEqual(minimum, 0.005, places=4)
        self.assertAlmostEqual(peak, 1450.0, places=3)


class BaseProfileNamingTests(unittest.TestCase):
    """base_profile_name must be a filename Windows can resolve.

    It is handed straight back to Windows as a default association, and the
    standalone watchdog checks it against the colour directory to avoid adopting
    an app-managed working profile as its HDR fallback. Windows HDR Calibration
    writes descriptions like "HDR Calibrated Profile 8/14/2026 132247" while
    naming the file "...8-14-2026 132247.icc", so a description used as a name is
    not merely wrong: its slashes make it an invalid path, and every consumer
    then fails silently.
    """

    @staticmethod
    def third_party_profile(directory: Path, filename: str, description: str) -> Path:
        """An ICC with no embedded app state, as any external profile would be.

        Built by generating one and blanking the private 'sdhs' tag signature, so
        import falls through to the same path a vendor profile takes.
        """
        state = ModeState.neutral("HDR")
        state.profile_name = description
        blob = bytearray(build_profile("HDR", state, build_transform(state, hdr=True)))
        count = struct.unpack_from(">I", blob, 128)[0]
        for index in range(count):
            offset = 132 + index * 12
            if blob[offset : offset + 4] == b"sdhs":
                blob[offset : offset + 4] = b"zzzz"
                break
        else:
            raise AssertionError("generated profile had no sdhs tag to blank")
        path = directory / filename
        path.write_bytes(bytes(blob))
        return path

    def test_import_records_the_filename_not_the_description(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.third_party_profile(
                Path(directory), "Calibrated 1-2-2026 30405.icm", "Calibrated 1/2/2026 3:4:5"
            )
            imported = import_profile(path, "HDR")

        self.assertFalse(imported.exact_state, "fixture still carried embedded state")
        self.assertIn("/", imported.description, "fixture description should differ from the name")
        self.assertEqual(imported.state.base_profile_name, path.name)
        self.assertNotIn("/", imported.state.base_profile_name)

    def test_the_recorded_name_resolves_next_to_the_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.third_party_profile(
                Path(directory), "Some Display 9-9-2026.icc", "Some Display 9/9/2026"
            )
            imported = import_profile(path, "HDR")
            self.assertTrue((path.parent / imported.state.base_profile_name).is_file())


class BaseNameMigrationTests(unittest.TestCase):
    """State written by earlier versions stored a description where a name belongs."""

    def test_loading_repairs_a_description_stored_as_the_name(self):
        state = ModeState.from_dict(
            {
                "base_profile": r"C:\Windows\System32\spool\drivers\color\HDR Calibrated Profile 8-14-2026 132247.icc",
                "base_profile_name": "HDR Calibrated Profile 8/14/2026 132247",
            },
            "HDR",
        )
        self.assertEqual(state.base_profile_name, "HDR Calibrated Profile 8-14-2026 132247.icc")
        self.assertNotIn("/", state.base_profile_name)

    def test_a_correct_name_is_left_alone(self):
        state = ModeState.from_dict(
            {"base_profile": r"C:\dir\Vendor.icm", "base_profile_name": "Vendor.icm"}, "HDR"
        )
        self.assertEqual(state.base_profile_name, "Vendor.icm")

    def test_no_base_profile_leaves_the_name_untouched(self):
        state = ModeState.from_dict({"base_profile": "", "base_profile_name": "Whatever"}, "HDR")
        self.assertEqual(state.base_profile_name, "Whatever")

    def test_migration_is_idempotent(self):
        first = ModeState.from_dict(
            {"base_profile": r"C:\d\A B.icc", "base_profile_name": "A/B"}, "HDR"
        )
        second = ModeState.from_dict(first.to_dict(), "HDR")
        self.assertEqual(first.base_profile_name, second.base_profile_name)
        self.assertEqual(second.base_profile_name, "A B.icc")


class PanelGamutAgreementTests(unittest.TestCase):
    """A monitor's gamut mode lives in its own OSD, invisible to the PC.

    Switching a display from DCI-P3 to sRGB leaves every HDR profile describing a panel
    that no longer exists, with no error raised anywhere. Comparing what the profile
    claims against what DXGI reports is the only way to notice.
    """

    P3 = ((0.6746, 0.3144), (0.2698, 0.6859), (0.1512, 0.0609))
    BT709 = ((0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600))

    def test_primaries_are_reported_undapted_not_in_the_d50_pcs(self):
        """ICC stores colorants adapted to D50; raw tag values are not the primaries.

        A generated profile is built from D65 primaries, so reading it back must return
        something near D65 sRGB rather than the D50-adapted numbers actually on disk.
        """
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.icc import build_profile, profile_primaries_xy
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("SDR")
        data = build_profile("SDR", state, build_transform(state, hdr=False))
        result = profile_primaries_xy(data)
        self.assertIsNotNone(result)
        red, _green, _blue, white = result
        self.assertAlmostEqual(white[0], 0.3127, places=2)
        self.assertAlmostEqual(white[1], 0.3290, places=2)
        self.assertGreater(red[0], 0.55, "red x collapsed; the D50 adaptation was not undone")

    def test_a_profile_without_colorants_reports_nothing(self):
        from sdr_hdr_profile_creator.icc import profile_primaries_xy

        self.assertIsNone(profile_primaries_xy(b"not an icc profile at all"))

    def test_matching_profile_and_panel_agree(self):
        from sdr_hdr_profile_creator.icc import primaries_disagree

        self.assertEqual(primaries_disagree(self.P3, self.P3), 0.0)

    def test_measurement_noise_does_not_trip_the_check(self):
        """A real matching pair agrees to about 0.00005 xy; that must not alarm."""
        from sdr_hdr_profile_creator.icc import primaries_disagree

        nudged = tuple((x + 0.00005, y - 0.00005) for x, y in self.P3)
        self.assertEqual(primaries_disagree(self.P3, nudged), 0.0)

    def test_the_smallest_real_gamut_change_is_caught(self):
        """DCI-P3 to BT.709 is the mildest switch a monitor OSD offers."""
        from sdr_hdr_profile_creator.icc import primaries_disagree

        worst = primaries_disagree(self.P3, self.BT709)
        self.assertGreater(worst, 0.03)

    def test_the_threshold_sits_between_noise_and_real_change(self):
        from sdr_hdr_profile_creator.icc import PRIMARY_MISMATCH_THRESHOLD_XY

        self.assertGreater(PRIMARY_MISMATCH_THRESHOLD_XY, 0.0005, "would fire on noise")
        self.assertLess(PRIMARY_MISMATCH_THRESHOLD_XY, 0.03, "would miss a P3 to BT.709 switch")


class TemplateMergeTests(unittest.TestCase):
    """A base profile is an ICC tag template; half of one is worse than none."""

    @staticmethod
    def curve_kind(payload: bytes | None) -> str:
        if not payload or payload[:4] != b"curv":
            return "absent"
        count = struct.unpack_from(">I", payload, 8)[0]
        if count == 0:
            return "linear"
        if count == 1:
            return f"gamma{struct.unpack_from('>H', payload, 12)[0] / 256:.2f}"
        return f"table{count}"

    def build_with_base(self, base_bytes: bytes) -> dict[bytes, bytes]:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.icm"
            base.write_bytes(base_bytes)
            state = ModeState.neutral("HDR")
            state.base_profile = str(base)
            return _read_tags(build_profile("HDR", state, build_transform(state, hdr=True)))

    @staticmethod
    def table_curve(bend: float = 2.4, entries: int = 256) -> bytes:
        """A real 256-entry curv table, the shape a vendor profile actually ships.

        The point is that it cannot be confused with a generated default. A gamma
        curve is a curv of count 1; the defaults here are gamma 1.00 for HDR. A base
        whose curves are themselves generated is byte-identical to the defaults, so
        splicing and defaulting produce the same output and no test can tell them
        apart -- which is why the guard below used to pass with the prune loop
        removed entirely.
        """
        import struct as _struct

        values = [
            min(65535, int(round(((index / (entries - 1)) ** bend) * 65535)))
            for index in range(entries)
        ]
        return (
            b"curv" + b"\x00" * 4
            + _struct.pack(">I", entries)
            + b"".join(_struct.pack(">H", value) for value in values)
        )

    def vendor_like_profile(self) -> bytes:
        """A profile blob, for the tests that need real bytes on disk."""
        state = ModeState.neutral("HDR")
        blob = bytearray(build_profile("HDR", state, build_transform(state, hdr=True)))
        return bytes(blob)

    def vendor_like_tags(self) -> dict:
        """The same, as a tag dict carrying genuine table curves."""
        tags = dict(_read_tags(self.vendor_like_profile()))
        for signature in (b"rTRC", b"gTRC", b"bTRC"):
            tags[signature] = self.table_curve()
        return tags

    def test_truncated_base_is_refused_rather_than_half_inherited(self):
        full = self.vendor_like_profile()
        with self.assertRaises(ValueError):
            _read_tags(full[: len(full) // 2], strict=True)
        # Non-strict stays lenient so importing an odd profile still works.
        self.assertIsInstance(_read_tags(full[: len(full) // 2]), dict)

    def build_from_tags(self, tags: dict) -> dict[bytes, bytes]:
        """Build an HDR profile on a base whose tags are exactly `tags`."""
        state = ModeState.neutral("HDR")
        with mock.patch.object(icc_module, "_read_tags") as fake:
            fake.return_value = tags
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / "base.icm"
                base.write_bytes(self.vendor_like_profile())
                state.base_profile = str(base)
                blob = build_profile("HDR", state, build_transform(state, hdr=True))
        return _read_tags(blob)

    def test_a_base_missing_one_trc_contributes_no_trc_at_all(self):
        """Otherwise the output mixes a real curve with synthesised neighbours.

        The base carries genuine 256-entry tables, so the two outcomes are
        distinguishable: pruned gives three generated gamma curves, unpruned gives
        table256 beside gamma1.00 -- the exact colour cast the production comment
        says the prune loop prevents. With a generated base, as this had, both
        outcomes are byte-identical and the loop could be deleted with 62 tests green.
        """
        tags = self.vendor_like_tags()
        self.assertEqual("table256", self.curve_kind(tags[b"gTRC"]), "fixture is not table-based")
        del tags[b"gTRC"]

        out = self.build_from_tags(tags)
        kinds = {sig: self.curve_kind(out.get(sig)) for sig in (b"rTRC", b"gTRC", b"bTRC")}
        self.assertEqual(
            len(set(kinds.values())), 1,
            f"channel curves came from different sources: {kinds}",
        )
        self.assertNotIn(
            "table256", set(kinds.values()),
            "an incomplete table set was spliced in rather than dropped",
        )

    def test_a_complete_table_set_is_inherited_whole(self):
        """The other side of the rule: pruning must not throw away a base that is
        actually complete, or every vendor profile would lose its curves."""
        out = self.build_from_tags(self.vendor_like_tags())
        for signature in (b"rTRC", b"gTRC", b"bTRC"):
            with self.subTest(tag=signature):
                self.assertEqual("table256", self.curve_kind(out.get(signature)))

    def test_an_intact_base_still_supplies_its_tags(self):
        blob = self.vendor_like_profile()
        out = self.build_with_base(blob)
        for signature in (b"rXYZ", b"gXYZ", b"bXYZ", b"wtpt", b"chad"):
            with self.subTest(tag=signature):
                self.assertIn(signature, out)

    def test_coupled_groups_are_declared_for_every_multi_tag_set(self):
        """The loop alone is vacuous: emptying COUPLED_TAG_GROUPS runs no
        assertions and passes, which is exactly the change that would silently
        re-enable splicing half-inherited colorant and TRC sets."""
        self.assertTrue(icc_module.COUPLED_TAG_GROUPS, "no coupled groups declared")
        for group in icc_module.COUPLED_TAG_GROUPS:
            with self.subTest(group=group):
                self.assertGreaterEqual(len(group), 2)

        # The sets that must never be spliced apart, named rather than counted,
        # so removing one is a failure rather than a smaller loop.
        declared = {frozenset(group) for group in icc_module.COUPLED_TAG_GROUPS}
        for required in (
            {b"rXYZ", b"gXYZ", b"bXYZ"},
            {b"rTRC", b"gTRC", b"bTRC"},
        ):
            with self.subTest(group=sorted(required)):
                self.assertIn(frozenset(required), declared)


@unittest.skipUnless(sys.platform == "win32", "DisplayConfig structs are Windows-only")
class DisplayConfigLayoutTests(unittest.TestCase):
    """Every struct handed to DisplayConfig must match the Windows SDK exactly.

    Neither QueryDisplayConfig nor DisplayConfigGetDeviceInfo validates the
    caller's layout: the first takes an element count, the second trusts the size
    written into the header. A wrong layout is therefore never rejected — it
    silently overruns the buffer and misparses every element after the first.
    """

    def test_all_struct_sizes_match_the_sdk(self):
        import ctypes

        from sdr_hdr_profile_creator import windows_api as wa

        expected = {
            "DISPLAYCONFIG_PATH_SOURCE_INFO": 20,
            "DISPLAYCONFIG_PATH_TARGET_INFO": 48,
            "DISPLAYCONFIG_PATH_INFO": 72,
            "DISPLAYCONFIG_VIDEO_SIGNAL_INFO": 48,
            "DISPLAYCONFIG_SOURCE_MODE": 20,
            "DISPLAYCONFIG_TARGET_MODE": 48,
            "DISPLAYCONFIG_DESKTOP_IMAGE_INFO": 40,
            "DISPLAYCONFIG_MODE_INFO": 64,
            "DISPLAYCONFIG_DEVICE_INFO_HEADER": 20,
            "DISPLAYCONFIG_SOURCE_DEVICE_NAME": 84,
            "DISPLAYCONFIG_TARGET_DEVICE_NAME": 420,
            "DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO": 32,
            "DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2": 36,
            "DISPLAYCONFIG_SDR_WHITE_LEVEL": 24,
            "DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE": 24,
        }
        for name, size in expected.items():
            with self.subTest(struct=name):
                self.assertEqual(ctypes.sizeof(getattr(wa, name)), size)

    def test_the_target_info_union_member_is_present(self):
        """Its omission was a real out-of-bounds write; pin the field itself."""
        from sdr_hdr_profile_creator import windows_api as wa

        names = [n for n, _ in wa.DISPLAYCONFIG_PATH_TARGET_INFO._fields_]
        self.assertIn("modeInfoIdx", names)
        self.assertEqual(wa.DISPLAYCONFIG_PATH_TARGET_INFO.modeInfoIdx.offset, 12)
        self.assertEqual(wa.DISPLAYCONFIG_PATH_TARGET_INFO.outputTechnology.offset, 16)


class ChadlessBaseTests(unittest.TestCase):
    """A base profile that never says what white its colorants were adapted to.

    ICC requires a display profile's rXYZ/gXYZ/bXYZ to be D50-adapted, and a v2 file
    that omits chad relies on exactly that convention. The (wtpt, chad) coupled group
    is dropped when chad is missing and replaced with this profile's own D65 white and
    an identity chad -- so the inherited D50 colorants were then declared to be D65
    ones. A consumer honouring that identity chad read green at 0.3212,0.5979 instead
    of 0.3000,0.6000: 0.021 off, four times the module's own mismatch threshold.

    8 of the 27 profiles installed on the machine this was found on are chad-less and
    all 8 are offered in the HDR base picker.
    """

    # sRGB primaries in XYZ, relative to D65. What the base is really describing.
    RED = (0.4124, 0.2126, 0.0193)
    GREEN = (0.3576, 0.7152, 0.1192)
    BLUE = (0.1805, 0.0722, 0.9505)
    TRUE_GREEN_XY = (0.3000, 0.6000)

    def setUp(self):
        import tempfile
        from pathlib import Path

        from sdr_hdr_profile_creator import icc

        self.icc = icc
        self.base = Path(tempfile.mkdtemp(prefix="vhdr-base-")) / "base.icm"
        # Only its existence matters; the tag read is intercepted below.
        self.base.write_bytes(b"\0" * 200)

    def base_tags(self, with_chad: bool) -> dict:
        """The two conventions that actually occur on disk.

        With chad: colorants D50-adapted, wtpt is the D50 PCS illuminant.
        Without:   colorants D50-adapted, wtpt is the real media white.
        """
        icc = self.icc
        matrix = icc.D65_TO_D50_CHAD
        tags = {
            b"rXYZ": icc._xyz_type(icc._matrix_vector(matrix, self.RED)),
            b"gXYZ": icc._xyz_type(icc._matrix_vector(matrix, self.GREEN)),
            b"bXYZ": icc._xyz_type(icc._matrix_vector(matrix, self.BLUE)),
            b"wtpt": icc._xyz_type(icc.D50_XYZ if with_chad else icc.D65_XYZ),
            b"rTRC": icc._curve_gamma_type(2.2),
            b"gTRC": icc._curve_gamma_type(2.2),
            b"bTRC": icc._curve_gamma_type(2.2),
            b"desc": icc._mluc_type("base"),
            b"cprt": icc._mluc_type("test"),
        }
        if with_chad:
            tags[b"chad"] = icc._sf32_type(matrix)
        return tags

    def build_on(self, with_chad: bool) -> bytes:
        from unittest import mock

        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.base_profile = str(self.base)
        transform = build_transform(state, True)
        tags = self.base_tags(with_chad)
        with mock.patch.object(self.icc, "_read_tags", lambda *_a, **_k: tags):
            return self.icc.build_profile("HDR", state, transform)

    def green_of(self, blob: bytes) -> tuple[float, float]:
        primaries = self.icc.profile_primaries_xy(blob)
        self.assertIsNotNone(primaries, "the built profile has no colorant tags")
        return primaries[1]

    def test_a_chadless_base_still_describes_the_right_green(self):
        green = self.green_of(self.build_on(False))
        self.assertAlmostEqual(self.TRUE_GREEN_XY[0], green[0], places=3)
        self.assertAlmostEqual(self.TRUE_GREEN_XY[1], green[1], places=3)

    def test_a_base_that_carries_chad_is_unaffected(self):
        """The adaptation must apply only where the white is unstated; doing it to a
        profile that already declares its adaptation would double-apply it."""
        green = self.green_of(self.build_on(True))
        self.assertAlmostEqual(self.TRUE_GREEN_XY[0], green[0], places=3)
        self.assertAlmostEqual(self.TRUE_GREEN_XY[1], green[1], places=3)

    def test_reading_a_chadless_profile_back_gives_the_panels_real_primaries(self):
        """The read side of the same assumption.

        profile_primaries_xy took a missing chad to mean "unadapted already". ICC
        colorants are D50-relative whether the file says so or not, so reading them raw
        put the primaries 0.021-0.025 off across the eight chad-less profiles installed
        on the machine this was found on -- several times the 0.005 mismatch threshold,
        so a base describing its panel perfectly still reported a gamut change.
        """
        from unittest import mock

        # A v2-style file: D50-adapted colorants, the real media white, and no chad --
        # read through the same seam profile_primaries_xy uses, since there is no
        # profile assembler to hand.
        tags = self.base_tags(with_chad=False)
        with mock.patch.object(self.icc, "_read_tags", lambda *_a, **_k: tags):
            described = self.icc.profile_primaries_xy(b"any bytes; the tags are supplied")

        self.assertIsNotNone(described)
        for expected, actual in zip(
            [(0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600)], described[:3]
        ):
            self.assertAlmostEqual(expected[0], actual[0], places=3)
            self.assertAlmostEqual(expected[1], actual[1], places=3)

    def test_a_chadless_profile_no_longer_reports_a_false_gamut_change(self):
        """The user-visible consequence: one wrong sentence in the status bar."""
        from unittest import mock

        tags = self.base_tags(with_chad=False)
        with mock.patch.object(self.icc, "_read_tags", lambda *_a, **_k: tags):
            described = self.icc.profile_primaries_xy(b"x")
        truth = ((0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600))
        self.assertFalse(
            self.icc.primaries_disagree(described[:3], truth),
            "a profile describing its panel exactly still reported a mismatch",
        )

    def test_the_d65_marker_is_absent_when_the_colorants_are_not_d65(self):
        """MSCA is Windows HDR Calibration's "{'D65Adapted':True}" marker. A base that
        brings its own chad through leaves the colorants in the D50 PCS, so stamping it
        there tells a reader the opposite of what the chad says."""
        tags = self.icc._read_tags(self.build_on(True))
        self.assertNotIn(b"MSCA", tags, "claimed D65 adaptation beside a non-identity chad")

    def test_the_d65_marker_is_present_on_the_generated_form(self):
        """The chad-less path ends up genuinely D65-adapted with an identity chad, which
        is exactly what the marker is for -- so removing it everywhere is not the fix."""
        tags = self.icc._read_tags(self.build_on(False))
        self.assertIn(b"MSCA", tags)

    def test_the_colorants_add_up_to_the_declared_white(self):
        """The self-consistency check: a matrix profile's three colorants must sum to
        its own white, or it describes a display that cannot exist."""
        for with_chad in (True, False):
            with self.subTest(chad=with_chad):
                blob = self.build_on(with_chad)
                tags = self.icc._read_tags(blob)
                parts = [self.icc._parse_xyz(tags[s]) for s in (b"rXYZ", b"gXYZ", b"bXYZ")]
                white = self.icc._parse_xyz(tags[b"wtpt"])
                chad = self.icc._parse_chad(tags.get(b"chad", b""))
                total = tuple(sum(part[i] for part in parts) for i in range(3))
                if chad is not None:
                    # Colorants live in the PCS; undo the adaptation before comparing.
                    total = self.icc._matrix_vector(self.icc._inverse3(chad), total)
                    white = self.icc._matrix_vector(self.icc._inverse3(chad), white)
                delta = max(abs(total[i] - white[i]) for i in range(3))
                self.assertLess(delta, 0.01, f"colorants miss the white by {delta:.5f}")


class OverwriteInstalledProfileTests(unittest.TestCase):
    """Writing a profile's bytes in place, for the case Windows will not handle.

    InstallColorProfileW returns TRUE without copying when the destination already
    exists, and the uninstall that would clear the way fails on a file owned by
    BUILTIN\\Administrators from an earlier elevated run. The ACL still allows
    FILE_WRITE_DATA there, so the bytes can go straight in -- but not through
    open(path, "wb"), which asks for GENERIC_WRITE and is refused.
    """

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        from sdr_hdr_profile_creator import windows_api

        self.windows_api = windows_api
        self.color_dir = Path(tempfile.mkdtemp(prefix="vhdr-color-"))
        self.addCleanup(shutil.rmtree, self.color_dir, True)
        patcher = mock.patch.object(
            windows_api, "get_color_directory", lambda: self.color_dir
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def installed(self, name="probe.icm", data=b"A" * 120):
        (self.color_dir / name).write_bytes(data)
        return name

    def test_a_payload_of_the_same_length_replaces_the_content(self):
        name = self.installed()
        self.assertTrue(self.windows_api.overwrite_installed_profile(name, b"B" * 120))
        self.assertEqual(b"B" * 120, (self.color_dir / name).read_bytes())

    def test_a_longer_payload_extends_the_file(self):
        name = self.installed()
        self.assertTrue(self.windows_api.overwrite_installed_profile(name, b"C" * 200))
        self.assertEqual(b"C" * 200, (self.color_dir / name).read_bytes())

    def test_a_shorter_payload_truncates_rather_than_leaving_a_tail(self):
        """A short write without truncation leaves the end of the old profile behind --
        a file that still parses, because the ICC header carries the size, and describes
        the wrong display.

        This was refused outright until the truncation was measured: SetEndOfFile
        succeeds on a FILE_WRITE_DATA handle against the files this actually writes. The
        refusal was costing every apply once the embedded state grew a measured
        greyscale response, because the JSON for 198 floats is not the same length twice
        running and a profile 48 bytes shorter than the installed one is ordinary.
        """
        name = self.installed()
        self.assertTrue(self.windows_api.overwrite_installed_profile(name, b"D" * 20))
        self.assertEqual(b"D" * 20, (self.color_dir / name).read_bytes(),
                         "the tail of the old profile is still there")

    def test_a_shorter_payload_leaves_nothing_of_the_old_file(self):
        """Byte length as well as content: a tail is exactly what would not show up in a
        comparison that only checked the prefix."""
        name = self.installed()
        self.windows_api.overwrite_installed_profile(name, b"E" * 7)
        self.assertEqual(7, (self.color_dir / name).stat().st_size)

    def test_a_missing_destination_is_false_rather_than_an_exception(self):
        self.assertFalse(
            self.windows_api.overwrite_installed_profile("absent.icm", b"E" * 10)
        )

    def test_the_answer_is_a_read_back_not_the_write_call(self):
        """Every other step in this story reported success while changing nothing, so
        the return value has to come from reading the file again."""
        import inspect

        source = inspect.getsource(self.windows_api.overwrite_installed_profile)
        self.assertIn("target.read_bytes() == payload", source)


class CorrectionTargetGammaTests(unittest.TestCase):
    """The Gamma slider sets the correction's target rather than applying a second power.

    The correction's defining promise is that everything above diffuse SDR white is left
    at exact identity, because native HDR content lives there and does not want it. A
    power applied to PQ code afterwards lifts that range too, so moving one slider used to
    silently rebrighten HDR highlights the correction never touches.
    """

    @staticmethod
    def output_nits(state, nits, white=200.0):
        from sdr_hdr_profile_creator.curves import build_transform
        from sdr_hdr_profile_creator.gamma_correction import pq_eotf, pq_inverse_eotf

        transform = build_transform(state, hdr=True, sdr_white_nits=white)
        position = pq_inverse_eotf(nits) * (len(transform.red) - 1)
        low = int(position)
        high = min(len(transform.red) - 1, low + 1)
        value = transform.red[low] + (transform.red[high] - transform.red[low]) * (position - low)
        return pq_eotf(value)

    def corrected(self, gamma):
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.sdr_gamma_correction = "200 nits / Brightness 30"
        state.gamma = gamma
        return state

    def test_highlights_are_untouched_at_every_gamma_setting(self):
        for gamma in (1.8, 2.0, 2.2, 2.4, 2.6):
            for nits in (400.0, 1000.0, 4000.0):
                with self.subTest(gamma=gamma, nits=nits):
                    self.assertAlmostEqual(
                        self.output_nits(self.corrected(gamma), nits), nits, delta=nits * 0.01,
                        msg="the correction stopped being identity above diffuse white",
                    )

    def test_diffuse_white_itself_stays_put(self):
        for gamma in (1.8, 2.2, 2.6):
            with self.subTest(gamma=gamma):
                self.assertAlmostEqual(
                    self.output_nits(self.corrected(gamma), 200.0), 200.0, delta=2.0)

    def test_the_slider_does_move_the_sdr_range(self):
        """It must still do something, or it would be a control that does nothing."""
        dark = self.output_nits(self.corrected(2.4), 50.0)
        bright = self.output_nits(self.corrected(2.0), 50.0)
        self.assertGreater(bright, dark * 1.05)

    def test_the_target_matches_the_reference_transform(self):
        """Cross-checked against the correction evaluated directly at that target."""
        from sdr_hdr_profile_creator.gamma_correction import (
            pq_eotf,
            pq_inverse_eotf,
            transform_piecewise_srgb_to_gamma,
        )

        for nits in (5.0, 50.0, 100.0):
            with self.subTest(nits=nits):
                expected = pq_eotf(
                    transform_piecewise_srgb_to_gamma(pq_inverse_eotf(nits), 200.0, 2.0))
                self.assertAlmostEqual(
                    self.output_nits(self.corrected(2.0), nits), expected, delta=expected * 0.02)

    def test_gamma_still_works_as_a_plain_power_with_the_correction_off(self):
        """With nothing to fold a target into, it has to stay an independent control."""
        from sdr_hdr_profile_creator.model import ModeState

        state = ModeState.neutral("HDR")
        state.sdr_gamma_correction = "Off"
        state.gamma = 2.0
        raised = self.output_nits(state, 100.0)
        state.gamma = 2.4
        lowered = self.output_nits(state, 100.0)
        self.assertGreater(raised, lowered)


class RetiredCorrectionOptionTests(unittest.TestCase):
    """"Unspecified" and "SDR" were filenames in the upstream download list, not settings
    anyone would choose. Removing them from the dropdown must not change what a profile
    already built against one of them resolves to."""

    def test_they_are_no_longer_offered(self):
        from sdr_hdr_profile_creator.gamma_correction import CORRECTION_OPTIONS

        for name in ("Unspecified", "SDR"):
            with self.subTest(option=name):
                self.assertNotIn(name, CORRECTION_OPTIONS)

    def test_a_saved_state_naming_one_still_resolves_the_same(self):
        """Otherwise reopening an old profile silently applies a different correction."""
        self.assertEqual(resolve_white_level("SDR", None), 80.0)
        self.assertEqual(resolve_white_level("Unspecified", None), 200.0)

    def test_the_offered_options_all_resolve(self):
        from sdr_hdr_profile_creator.gamma_correction import CORRECTION_OPTIONS

        for option in CORRECTION_OPTIONS:
            with self.subTest(option=option):
                resolved = resolve_white_level(option, 240.0)
                if option == "Off":
                    self.assertIsNone(resolved)
                else:
                    self.assertGreater(resolved, 0.0)

    def test_an_unknown_name_falls_back_rather_than_raising(self):
        self.assertEqual(resolve_white_level("something else entirely", None), 200.0)


class PanelPrimariesTests(unittest.TestCase):
    """Profiles built with no base profile must describe the real panel.

    Without a base to inherit rXYZ/gXYZ/bXYZ from, the fallback is the generic
    per-mode table, and its HDR entry is BT.2020 -- a gamut no shipping display
    covers. On the panel this was developed against the green primary alone was
    out by 0.11 in xy, which misplaces every saturated colour.
    """

    PANEL = (0.674586, 0.314418, 0.269814, 0.685949, 0.151222, 0.060916, 0.313786, 0.329268)

    def _profile(self, primaries):
        state = ModeState.neutral("HDR")
        state.panel_primaries = primaries
        return build_profile("HDR", state, build_transform(state, hdr=True))

    def test_panel_primaries_survive_the_round_trip(self):
        described = icc_module.profile_primaries_xy(self._profile(self.PANEL))
        flat = [value for pair in described[:3] for value in pair]
        for index, expected in enumerate(self.PANEL[:6]):
            # s15Fixed16 quantisation is the only permitted difference.
            self.assertAlmostEqual(flat[index], expected, places=4)

    def test_without_panel_primaries_the_generic_table_is_used(self):
        described = icc_module.profile_primaries_xy(self._profile(()))
        self.assertAlmostEqual(described[0][0], icc_module.PRIMARIES["HDR"][0], places=4)
        self.assertAlmostEqual(described[1][1], icc_module.PRIMARIES["HDR"][3], places=4)

    def test_primaries_are_scaled_to_the_declared_white(self):
        """rXYZ+gXYZ+bXYZ must agree with wtpt, or the profile contradicts itself.

        The panel's native white sits slightly off D65. Scaling the primaries to
        it while wtpt still declares D65 would leave the two disagreeing.
        """
        tags = _read_tags(self._profile(self.PANEL))
        channels = [icc_module._parse_xyz(tags[key]) for key in (b"rXYZ", b"gXYZ", b"bXYZ")]
        white = icc_module._parse_xyz(tags[b"wtpt"])
        for axis in range(3):
            self.assertAlmostEqual(sum(c[axis] for c in channels), white[axis], places=3)

    def test_generated_profile_carries_every_tag_windows_writes(self):
        """Microsoft's HDR Calibration output is the compatibility reference."""
        required = {b"MHC2", b"MSCA", b"bTRC", b"bXYZ", b"chad", b"cprt",
                    b"desc", b"gTRC", b"gXYZ", b"lumi", b"rTRC", b"rXYZ", b"wtpt"}
        self.assertLessEqual(required, set(_read_tags(self._profile(self.PANEL))))

    def test_degenerate_primaries_fall_back_rather_than_raise(self):
        """Range-valid coordinates can still be collinear and have no inverse."""
        state = ModeState.neutral("HDR")
        object.__setattr__(state, "panel_primaries", (0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3127, 0.329))
        described = icc_module.profile_primaries_xy(
            build_profile("HDR", state, build_transform(state, hdr=True))
        )
        self.assertAlmostEqual(described[0][0], icc_module.PRIMARIES["HDR"][0], places=4)


class NormalizePrimariesTests(unittest.TestCase):
    """Primaries arrive from display drivers and from profiles written by older
    builds, so neither their length nor their values can be taken on trust."""

    def test_accepts_a_plausible_set(self):
        values = (0.674, 0.314, 0.269, 0.685, 0.151, 0.060, 0.3127, 0.329)
        self.assertEqual(normalize_primaries(values), values)

    def test_rejects_wrong_length(self):
        self.assertEqual(normalize_primaries((0.1, 0.2)), ())

    def test_rejects_zero_y_which_would_divide_by_zero(self):
        self.assertEqual(normalize_primaries((0.6, 0.0, 0.3, 0.6, 0.15, 0.06, 0.31, 0.33)), ())

    def test_rejects_nan(self):
        self.assertEqual(normalize_primaries((float("nan"),) * 8), ())

    def test_rejects_out_of_range(self):
        self.assertEqual(normalize_primaries((1.4, 0.3, 0.3, 0.6, 0.15, 0.06, 0.31, 0.33)), ())

    def test_rejects_non_numeric(self):
        self.assertEqual(normalize_primaries(None), ())
        self.assertEqual(normalize_primaries("abcdefgh"), ())

    def test_round_trips_through_state_serialization(self):
        values = (0.674, 0.314, 0.269, 0.685, 0.151, 0.060, 0.3127, 0.329)
        state = ModeState.neutral("HDR")
        state.panel_primaries = values
        self.assertEqual(ModeState.from_dict(state.to_dict(), "HDR").panel_primaries, values)

    def test_corrupt_serialized_primaries_are_dropped_on_load(self):
        state = ModeState.neutral("HDR")
        payload = state.to_dict()
        payload["panel_primaries"] = [0.5, 0.5]
        self.assertEqual(ModeState.from_dict(payload, "HDR").panel_primaries, ())


class InPlaceProfileRetryTests(unittest.TestCase):
    """A collision is not a permissions failure.

    The watchdog re-asserts the profile association every few seconds and Windows opens
    the file to do it, so an apply landing in that window is refused with a sharing
    violation. Without a retry that was reported as "installed by an earlier elevated
    run, press Run as Admin" -- useless advice for something that clears in
    milliseconds, and it switched Live Apply off as well. _write_json_atomic already
    retried for exactly this reason; this did not.
    """

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        from sdr_hdr_profile_creator import windows_api

        self.windows_api = windows_api
        self.color_dir = Path(tempfile.mkdtemp(prefix="vhdr-retry-"))
        self.addCleanup(shutil.rmtree, self.color_dir, True)
        patcher = mock.patch.object(windows_api, "get_color_directory", lambda: self.color_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.color_dir / "probe.icm").write_bytes(b"A" * 120)

    def hold_open_without_sharing_writes(self, name, seconds):
        """Do to the file exactly what Windows does while re-asserting a profile.

        Opened with FILE_SHARE_READ only, so any other writer is refused with a
        sharing violation until this handle closes. Released on a timer, which is what
        makes the retry observable rather than merely asserted.
        """
        import ctypes
        import threading
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x00000001, 3
        invalid = wintypes.HANDLE(-1).value

        handle = kernel32.CreateFileW(
            str(self.color_dir / name), GENERIC_READ, FILE_SHARE_READ,
            None, OPEN_EXISTING, 0, None,
        )
        self.assertNotEqual(invalid, handle, "could not hold the file open")
        released = threading.Timer(seconds, lambda: kernel32.CloseHandle(handle))
        released.daemon = True
        released.start()
        self.addCleanup(lambda: kernel32.CloseHandle(handle))
        return handle

    @unittest.skipUnless(sys.platform == "win32", "sharing semantics are a Windows thing")
    def test_a_write_blocked_by_another_process_succeeds_once_it_lets_go(self):
        """The owner hit this for real: a Live Apply edit collided with the watchdog
        re-asserting the association, and the failure was reported as a permissions
        problem telling them to elevate."""
        self.hold_open_without_sharing_writes("probe.icm", seconds=0.10)
        self.assertTrue(
            self.windows_api.overwrite_installed_profile("probe.icm", b"B" * 120),
            "a transient sharing violation was reported as a permanent failure",
        )
        self.assertEqual(b"B" * 120, (self.color_dir / "probe.icm").read_bytes())

    @unittest.skipUnless(sys.platform == "win32", "sharing semantics are a Windows thing")
    def test_a_write_blocked_for_longer_than_the_retries_still_fails(self):
        """The retry is a collision window, not a guarantee; a file genuinely held
        open must still be reported rather than waited on indefinitely."""
        self.hold_open_without_sharing_writes("probe.icm", seconds=30.0)
        self.assertFalse(self.windows_api.overwrite_installed_profile("probe.icm", b"C" * 120))
        self.assertEqual(b"A" * 120, (self.color_dir / "probe.icm").read_bytes(),
                         "a refused write must not have modified anything")

    def test_the_retry_only_covers_sharing_violations(self):
        """A genuine permissions failure must fail fast rather than stalling the UI
        for a fifth of a second on every apply."""
        import inspect

        source = inspect.getsource(self.windows_api.overwrite_installed_profile)
        self.assertIn("ERROR_SHARING_VIOLATION", source)
        self.assertIn("if ctypes.get_last_error() != ERROR_SHARING_VIOLATION:", source)
        self.assertIn("return False", source)

    def test_the_message_leads_with_the_likely_cause(self):
        """Telling the user to elevate first is wrong for the common case, and
        elevating is not something to advise lightly."""
        # Anchored on this file, not on the working directory. As a relative path it
        # read app.py only when the suite happened to be started from the repository
        # root, and raised FileNotFoundError from anywhere else -- an error rather than
        # a failure, so it read as the suite being broken rather than this test being
        # wrong about where it lives.
        app_source = (
            Path(__file__).resolve().parents[1] / "src/sdr_hdr_profile_creator/app.py"
        ).read_text(encoding="utf-8")
        start = app_source.index("Windows kept the previous")
        message = app_source[start:start + 420]
        self.assertLess(
            message.index("Try applying again"), message.index("Run as Admin"),
            "the transient cause should be offered before the drastic one",
        )


if __name__ == "__main__":
    unittest.main()
