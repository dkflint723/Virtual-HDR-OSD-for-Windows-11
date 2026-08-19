"""Reading a display's HDR declaration from its own EDID.

Windows' DXGI_OUTPUT_DESC1 is the obvious source and gets two of the three figures wrong.
Measured against a real panel declaring 1015.24 peak, 265.05 maximum frame-average and
0.0002 minimum, DXGI reported 1010.40, 1010.40 and 0.1956 -- peak roughly right,
frame-average replaced by peak, minimum four hundred times too high.
"""

from __future__ import annotations

import unittest

from sdr_hdr_profile_creator.edid import (
    PanelMetadata,
    parse_hdr_static_metadata,
    read_panel_metadata,
)


def cta_edid(payload: bytes) -> bytes:
    """A minimal two-block EDID carrying one CTA data block."""
    base = bytes(128)
    block = bytes([(7 << 5) | len(payload)]) + payload
    # Byte 2 is the offset of the first detailed timing descriptor; the data block
    # collection runs from byte 4 up to it.
    extension = bytearray(128)
    extension[0] = 0x02
    extension[1] = 0x03
    extension[2] = 4 + len(block)
    extension[4:4 + len(block)] = block
    return base + bytes(extension)


def hdr_block(eotf=0x05, descriptor=0x00, peak=139, frame_average=77, minimum=1) -> bytes:
    return bytes([0x06, eotf, descriptor, peak, frame_average, minimum])


class LuminanceDecodingTests(unittest.TestCase):
    """CTA-861-G section 7.5.13: codes are logarithmic, minimum is a fraction of maximum."""

    def parsed(self, **overrides) -> PanelMetadata:
        result = parse_hdr_static_metadata(cta_edid(hdr_block(**overrides)))
        self.assertIsNotNone(result)
        return result

    def test_the_real_panels_codes_decode_to_its_real_figures(self):
        """Codes 139 and 77 are what one PG32UCDM declares."""
        panel = self.parsed()
        self.assertAlmostEqual(panel.peak_nits, 1015.24, places=1)
        self.assertAlmostEqual(panel.max_frame_average_nits, 265.05, places=1)

    def test_code_zero_is_the_fifty_nit_base(self):
        self.assertAlmostEqual(self.parsed(peak=0).peak_nits, 50.0, places=4)

    def test_thirty_two_codes_is_a_doubling(self):
        self.assertAlmostEqual(self.parsed(peak=32).peak_nits, 100.0, places=4)
        self.assertAlmostEqual(self.parsed(peak=64).peak_nits, 200.0, places=4)

    def test_minimum_is_a_squared_fraction_of_maximum(self):
        panel = self.parsed(peak=139, minimum=1)
        self.assertAlmostEqual(panel.min_nits, 1015.24 * (1 / 255) ** 2 / 100, places=6)

    def test_pq_support_is_read_from_the_eotf_bits(self):
        self.assertTrue(self.parsed(eotf=0x05).supports_pq)
        self.assertFalse(self.parsed(eotf=0x01).supports_pq)


class BlockShapeTests(unittest.TestCase):
    def test_an_edid_with_no_hdr_block_reports_nothing(self):
        other = bytes([0x05, 0x00, 0x00])   # an extended block that is not HDR metadata
        self.assertIsNone(parse_hdr_static_metadata(cta_edid(other)))

    def test_an_edid_with_no_extension_reports_nothing(self):
        self.assertIsNone(parse_hdr_static_metadata(bytes(128)))

    def test_a_truncated_edid_reports_nothing_rather_than_raising(self):
        self.assertIsNone(parse_hdr_static_metadata(b"\x00" * 40))

    def test_a_block_carrying_only_a_maximum_still_reads(self):
        """Only the maximum is mandatory once the block is present."""
        panel = parse_hdr_static_metadata(cta_edid(bytes([0x06, 0x05, 0x00, 139])))
        self.assertIsNotNone(panel)
        self.assertAlmostEqual(panel.peak_nits, 1015.24, places=1)
        self.assertEqual(panel.max_frame_average_nits, 0.0)
        self.assertEqual(panel.min_nits, 0.0)

    def test_a_block_too_short_to_be_hdr_metadata_is_skipped(self):
        self.assertIsNone(parse_hdr_static_metadata(cta_edid(bytes([0x06, 0x05]))))


class CredibilityTests(unittest.TestCase):
    """A declaration is only worth preferring over DXGI if it actually answered."""

    def test_a_real_declaration_is_credible(self):
        self.assertTrue(parse_hdr_static_metadata(cta_edid(hdr_block())).credible)

    def test_a_panel_claiming_no_pq_is_not(self):
        self.assertFalse(parse_hdr_static_metadata(cta_edid(hdr_block(eotf=0x01))).credible)

    def test_an_absurd_peak_is_not(self):
        panel = PanelMetadata(peak_nits=90000.0, max_frame_average_nits=100.0,
                              min_nits=0.0, supports_pq=True)
        self.assertFalse(panel.credible)

    def test_a_peak_below_any_real_display_is_not(self):
        panel = PanelMetadata(peak_nits=10.0, max_frame_average_nits=5.0,
                              min_nits=0.0, supports_pq=True)
        self.assertFalse(panel.credible)


class DevicePathTests(unittest.TestCase):
    """Every failure is just "no answer"; the caller's fallback is the same in each case."""

    def test_a_path_that_is_not_a_monitor_reports_nothing(self):
        self.assertIsNone(read_panel_metadata("not a device path"))

    def test_an_empty_path_reports_nothing(self):
        self.assertIsNone(read_panel_metadata(""))

    def test_an_unknown_monitor_reports_nothing_rather_than_raising(self):
        self.assertIsNone(read_panel_metadata(
            r"\\?\DISPLAY#NOSUCH000#1&00000000&0&UID0#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"))


class FrameAverageTests(unittest.TestCase):
    """The figure DXGI discards, and the reason this module exists."""

    def test_frame_average_is_read_separately_from_peak(self):
        panel = parse_hdr_static_metadata(cta_edid(hdr_block(peak=139, frame_average=77)))
        self.assertLess(panel.max_frame_average_nits, panel.peak_nits / 3,
                        "frame-average came out near peak, which is what DXGI does wrong")

    def test_an_emissive_panels_gap_survives_the_round_trip(self):
        """1015 peak against 265 sustained is the whole point: a tool that treats peak as
        the full-screen figure asks for four times the light the panel can hold."""
        panel = parse_hdr_static_metadata(cta_edid(hdr_block()))
        self.assertAlmostEqual(panel.peak_nits / panel.max_frame_average_nits, 3.83, places=1)


if __name__ == "__main__":
    unittest.main()
