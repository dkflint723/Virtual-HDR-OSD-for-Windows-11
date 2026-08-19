"""Turning meter readings into profile values, and refusing when they are wrong.

The rejection rules carry more weight than the arithmetic. A meter that is
unplugged, aimed at the wrong part of the screen, or reading through a closed
diffuser still returns numbers, and those reach the profile with nothing
downstream able to tell them from real measurements.
"""

from __future__ import annotations

import unittest

from sdr_hdr_profile_creator.measure import (
    D65_XY,
    MAX_CHANNEL_TRIM,
    Aborted,
    Calibration,
    MeasurementError,
    derive,
    plan,
    run,
    validate,
    white_balance_gains,
)
from sdr_hdr_profile_creator.meter import MeterError, Reading


def reading(Y: float, x: float, y: float) -> Reading:
    """A reading with a plausible XYZ for the given Y and chromaticity."""
    if y <= 0.0:
        return Reading(X=0.0, Y=Y, Z=0.0, x=x, y=y)
    return Reading(X=(x / y) * Y, Y=Y, Z=((1.0 - x - y) / y) * Y, x=x, y=y)


def xy_of(X: float, Y: float, Z: float) -> tuple[float, float]:
    total = X + Y + Z
    return (X / total, Y / total)


def combine(channels: dict[str, Reading]) -> Reading:
    """The white three channels add up to, so a set is additive by construction."""
    X = sum(channels[c].X for c in ("red", "green", "blue"))
    Y = sum(channels[c].Y for c in ("red", "green", "blue"))
    Z = sum(channels[c].Z for c in ("red", "green", "blue"))
    x, y = xy_of(X, Y, Z)
    return Reading(X=X, Y=Y, Z=Z, x=x, y=y)


PEAK = 454.252093
BALANCE = 100.0

# The balance patches are shown well below peak so the panel is linear there, and
# peak white is dimmed by the limiter relative to the sum of the channels -- which
# is exactly the situation that made an earlier version refuse every real run.
DIMMED_PEAK = reading(PEAK, 0.3270, 0.3295)

# BT.709 channels weighted so they sum to exactly D65 -- a display needing no
# correction at all.
NEUTRAL_CHANNELS = {
    "red": reading(0.2126 * BALANCE, 0.640, 0.330),
    "green": reading(0.7152 * BALANCE, 0.300, 0.600),
    "blue": reading(0.0722 * BALANCE, 0.150, 0.060),
}
NEUTRAL = dict(
    NEUTRAL_CHANNELS,
    black=reading(0.0, 0.3130, 0.3290),
    white=DIMMED_PEAK,
    **{"balance-white": combine(NEUTRAL_CHANNELS)},
)

# The chromaticities actually measured on the development panel, with red raised
# so white lands warm, as it did there.
WARM_CHANNELS = {
    "red": reading(0.2126 * BALANCE * 1.13, 0.6486, 0.3312),
    "green": reading(0.7152 * BALANCE * 0.99, 0.3141, 0.5892),
    "blue": reading(0.0722 * BALANCE * 0.97, 0.1524, 0.0596),
}
WARM = dict(
    WARM_CHANNELS,
    black=reading(0.0, 0.3130, 0.3290),
    white=DIMMED_PEAK,
    **{"balance-white": combine(WARM_CHANNELS)},
)


def changed(base=None, **replacements) -> dict[str, Reading]:
    updated = dict(NEUTRAL if base is None else base)
    updated.update(replacements)
    return updated


def without(key: str) -> dict[str, Reading]:
    updated = dict(NEUTRAL)
    updated.pop(key, None)
    return updated


class PlanTests(unittest.TestCase):
    def test_measures_black_peak_and_a_balance_set(self):
        self.assertEqual(
            [step.key for step in plan(1000.0)],
            ["black", "white", "balance-white", "red", "green", "blue"],
        )

    def test_black_is_measured_first_while_the_panel_is_still_cool(self):
        """A long bright sequence warms an emissive panel, and the black floor
        is the reading most disturbed by that."""
        self.assertEqual(plan(1000.0)[0].key, "black")

    def test_black_is_given_longer_to_settle(self):
        steps = {step.key: step for step in plan(1000.0)}
        self.assertGreater(steps["black"].settle_seconds, steps["white"].settle_seconds)

    def test_channels_are_driven_at_the_same_level_as_the_reference_white(self):
        """A channel measured at another level samples a different point on the
        display's response and cannot be combined with the others."""
        steps = {step.key: step for step in plan(1000.0)}
        for key in ("red", "green", "blue"):
            self.assertEqual(steps[key].nits, steps["balance-white"].nits)

    def test_the_balance_set_sits_well_below_peak(self):
        """White at peak asks for about three times the power of one channel, so
        the limiter dims it much harder and the channels no longer add up to it.
        An earlier version measured balance at peak and refused every real run."""
        steps = {step.key: step for step in plan(1000.0)}
        self.assertLess(steps["balance-white"].nits, steps["white"].nits / 2.0)

    def test_a_dim_display_is_not_asked_for_balance_above_half_its_peak(self):
        steps = {step.key: step for step in plan(120.0)}
        self.assertLessEqual(steps["balance-white"].nits, steps["white"].nits / 2.0)

    def test_an_absurd_peak_is_clamped_into_range(self):
        self.assertLessEqual(plan(99000.0)[1].nits, 10000.0)
        self.assertGreaterEqual(plan(0.0)[1].nits, 80.0)


class WhiteBalanceTests(unittest.TestCase):
    """The gains that move measured white onto D65."""

    def corrected_white(self, readings, gains):
        axes = []
        for axis in ("X", "Y", "Z"):
            axes.append(sum(
                gain * getattr(readings[channel], axis)
                for gain, channel in zip(gains, ("red", "green", "blue"))
            ))
        return xy_of(*axes), axes[1]

    def test_a_neutral_display_needs_no_correction(self):
        gains = white_balance_gains(NEUTRAL)
        for gain in gains:
            self.assertAlmostEqual(gain, 1.0, places=3)

    def test_a_warm_display_has_its_red_pulled_down(self):
        red, green, blue = white_balance_gains(WARM)
        self.assertLess(red, 0.95)
        self.assertLessEqual(max(red, green, blue), 1.0 + 1e-9)

    def test_the_correction_actually_lands_on_d65(self):
        """The whole point. Anything else is a plausible-looking number."""
        gains = white_balance_gains(WARM)
        (x, y), _ = self.corrected_white(WARM, gains)
        self.assertAlmostEqual(x, D65_XY[0], places=4)
        self.assertAlmostEqual(y, D65_XY[1], places=4)

    def test_nothing_is_ever_boosted_above_unity(self):
        """A display cannot be asked for light it has already run out of, so the
        excess channels come down to meet the weakest."""
        for readings in (NEUTRAL, WARM):
            self.assertLessEqual(max(white_balance_gains(readings)), 1.0 + 1e-9)

    def test_correcting_white_costs_luminance_rather_than_clipping(self):
        gains = white_balance_gains(WARM)
        _, corrected_Y = self.corrected_white(WARM, gains)
        self.assertLess(corrected_Y, WARM["white"].Y)

    def test_degenerate_channels_give_a_neutral_answer_rather_than_raising(self):
        same = reading(150.0, 0.3127, 0.3290)
        readings = changed(red=same, green=same, blue=same)
        self.assertEqual(white_balance_gains(readings), (1.0, 1.0, 1.0))


class DeriveTests(unittest.TestCase):
    def test_reports_peak_and_black(self):
        result = derive(NEUTRAL)
        self.assertIsInstance(result, Calibration)
        self.assertAlmostEqual(result.peak_nits, PEAK, places=2)
        self.assertEqual(result.black_nits, 0.0)

    def test_peak_comes_from_the_full_drive_patch_not_the_balance_one(self):
        """They are deliberately different levels, and mixing them up would
        report the balance level as the display's peak."""
        self.assertAlmostEqual(derive(NEUTRAL).peak_nits, PEAK, places=2)
        self.assertGreater(derive(NEUTRAL).peak_nits, BALANCE * 2)

    def test_the_white_point_comes_from_the_patch_the_channels_sat_beside(self):
        """That is the white the correction is solved against."""
        result = derive(WARM)
        self.assertAlmostEqual(result.white_xy[0], WARM["balance-white"].x, places=6)

    def test_records_the_window_the_peak_was_measured_on(self):
        """Peak luminance is meaningless without it. The development panel is
        rated 1015 nits but reads 454 on a tenth of the screen, because the
        brightness limiter responds to total output."""
        self.assertAlmostEqual(derive(NEUTRAL).window_fraction, 0.10, places=3)

    def test_an_unmeasurably_low_black_gives_infinite_contrast(self):
        """An OLED reads true black as 0.0000 on this class of instrument. That
        is a floor rather than a value, and must not divide by zero."""
        self.assertEqual(derive(NEUTRAL).contrast, float("inf"))

    def test_contrast_is_a_ratio_when_black_is_measurable(self):
        result = derive(changed(black=reading(0.05, 0.313, 0.329)))
        self.assertAlmostEqual(result.contrast, PEAK / 0.05, places=0)

    def test_white_error_is_reported_against_d65(self):
        dx, dy = derive(WARM).white_error
        self.assertGreater(dx, 0.0)
        # Against the reference white the channels sat beside, not peak white,
        # because that is the white the correction is solved against.
        self.assertAlmostEqual(dx, WARM["balance-white"].x - D65_XY[0], places=6)
        self.assertAlmostEqual(dy, WARM["balance-white"].y - D65_XY[1], places=6)

    def test_trims_are_the_gains_as_percentages_and_never_positive(self):
        trims = derive(WARM).channel_trims
        self.assertEqual(len(trims), 3)
        for trim in trims:
            self.assertLessEqual(trim, 0.0)

    def test_a_neutral_display_needs_no_trim(self):
        for trim in derive(NEUTRAL).channel_trims:
            self.assertAlmostEqual(trim, 0.0, places=1)

    def test_an_oversized_correction_is_flagged(self):
        """A profile can only carry so much, and clamping it silently would
        leave white visibly off with nothing said."""
        extreme = dict(WARM_CHANNELS)
        extreme["red"] = reading(0.2126 * BALANCE * 3.0, 0.6486, 0.3312)
        readings = dict(
            extreme,
            black=reading(0.0, 0.313, 0.329),
            white=DIMMED_PEAK,
            **{"balance-white": combine(extreme)},
        )
        self.assertTrue(derive(readings).trims_exceed_range)

    def test_a_modest_correction_is_not_flagged(self):
        self.assertFalse(derive(NEUTRAL).trims_exceed_range)
        self.assertLessEqual(MAX_CHANNEL_TRIM, 1.0)

    def test_refuses_rather_than_returning_something_wrong(self):
        with self.assertRaises(MeasurementError):
            derive(without("white"))


class GamutIsNotMeasuredTests(unittest.TestCase):
    """Why these readings never reach the profile's colorant tags.

    Measured on a P3 panel whose native green is (0.2698, 0.6859), the green
    patch read (0.3141, 0.5892) -- 0.0141 from BT.709 green and 0.0967 from the
    panel's own. scRGB is defined on BT.709, so the patch asks for BT.709 green
    and the display renders it; the reading describes the encoding, not the panel.
    """

    MEASURED_GREEN = (0.3141, 0.5892)
    BT709_GREEN = (0.300, 0.600)
    PANEL_GREEN = (0.269814, 0.685949)

    def distance(self, a, b):
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def test_the_reading_was_far_closer_to_bt709_than_to_the_panel(self):
        to_709 = self.distance(self.MEASURED_GREEN, self.BT709_GREEN)
        to_panel = self.distance(self.MEASURED_GREEN, self.PANEL_GREEN)
        self.assertLess(to_709, to_panel / 5)

    def test_a_calibration_offers_no_primaries_to_write(self):
        self.assertFalse(hasattr(derive(NEUTRAL), "primaries"))


class ValidationTests(unittest.TestCase):
    def test_a_good_set_has_no_complaints(self):
        self.assertEqual(validate(NEUTRAL), [])
        self.assertEqual(validate(WARM), [])

    def test_missing_readings_are_named(self):
        self.assertTrue(any("white" in problem for problem in validate(without("white"))))

    def test_rejects_a_peak_no_display_could_produce(self):
        """A meter off the screen entirely reads room light, not the panel."""
        problems = validate(changed(white=reading(3.0, 0.3127, 0.3290)))
        self.assertTrue(any("outside anything a display" in p for p in problems))

    def test_rejects_a_black_that_is_really_room_light(self):
        problems = validate(changed(black=reading(80.0, 0.31, 0.33)))
        self.assertTrue(any("room light" in p for p in problems))

    def test_rejects_white_no_brighter_than_black(self):
        problems = validate(
            changed(white=reading(50.0, 0.3127, 0.3290), black=reading(60.0, 0.31, 0.33))
        )
        self.assertTrue(any("no brighter than black" in p for p in problems))

    def test_rejects_an_impossible_chromaticity(self):
        problems = validate(changed(red=reading(150.0, 1.4, 0.31)))
        self.assertTrue(any("impossible chromaticity" in p for p in problems))

    def test_a_true_black_of_zero_is_accepted(self):
        """It is what an OLED actually reads, and rejecting it would refuse the
        best display this could be pointed at."""
        self.assertEqual(validate(changed(black=reading(0.0, 0.31, 0.33))), [])

    def test_rejects_channels_that_do_not_add_up_to_the_reference_white(self):
        """Additivity is the assumption the correction rests on. If red plus
        green plus blue is not the white measured beside them, something in the
        path is not linear and gains derived from it would be wrong."""
        broken = reading(BALANCE * 1.6, 0.3127, 0.3290)
        problems = validate(changed(**{"balance-white": broken}))
        self.assertTrue(any("not linear" in p for p in problems), problems)

    def test_small_departures_from_additivity_are_tolerated(self):
        """Instrument noise on a dim blue channel should not fail a good run."""
        base = NEUTRAL["balance-white"]
        nudged = reading(base.Y * 1.03, base.x, base.y)
        self.assertEqual(validate(changed(**{"balance-white": nudged})), [])

    def test_a_peak_white_the_limiter_dimmed_is_not_an_additivity_failure(self):
        """This is the whole reason the balance set exists. Peak white is
        expected to fall well short of the channel sum."""
        self.assertEqual(validate(NEUTRAL), [])
        self.assertLess(NEUTRAL["balance-white"].Y, NEUTRAL["white"].Y)


class FakeDisplay:
    def __init__(self):
        self.shown = []

    def show(self, step):
        self.shown.append(step.key)


class RunTests(unittest.TestCase):
    """Sequencing, with both the screen and the instrument injected."""

    ORDER = ("black", "white", "balance-white", "red", "green", "blue")

    def setUp(self):
        self.display = FakeDisplay()
        self.slept = []

    def run_sequence(self, reader, **kwargs):
        return run(
            self.display, reader, peak_nits=1015.24, sleep=self.slept.append, **kwargs
        )

    def good_reader(self):
        order = iter([NEUTRAL[key] for key in self.ORDER])
        return lambda: next(order)

    def test_shows_every_patch_in_plan_order(self):
        self.run_sequence(self.good_reader())
        self.assertEqual(self.display.shown, list(self.ORDER))

    def test_returns_a_calibration_from_the_readings(self):
        self.assertAlmostEqual(self.run_sequence(self.good_reader()).peak_nits, PEAK, places=2)

    def test_lets_the_panel_settle_before_each_reading(self):
        """A patch read the instant it appears is read mid-transition."""
        self.run_sequence(self.good_reader())
        self.assertEqual(len(self.slept), 6)
        self.assertTrue(all(delay > 0 for delay in self.slept))

    def test_reports_progress_for_each_step(self):
        seen = []
        self.run_sequence(
            self.good_reader(),
            on_progress=lambda step, i, total: seen.append((step.key, i, total)),
        )
        self.assertEqual([entry[0] for entry in seen], list(self.ORDER))
        self.assertEqual(seen[0][2], 6)

    def test_a_failed_reading_ends_the_run_rather_than_being_skipped(self):
        def reader():
            raise MeterError("sensor in the wrong position")

        with self.assertRaises(MeasurementError) as caught:
            self.run_sequence(reader)
        self.assertIn("wrong position", str(caught.exception))

    def test_the_failing_step_is_named(self):
        def reader():
            raise MeterError("boom")

        with self.assertRaises(MeasurementError) as caught:
            self.run_sequence(reader)
        self.assertIn("Black level", str(caught.exception))

    def test_aborting_stops_without_producing_a_calibration(self):
        with self.assertRaises(Aborted):
            self.run_sequence(self.good_reader(), should_abort=lambda: True)
        self.assertEqual(self.display.shown, [])

    def test_readings_that_do_not_survive_validation_are_refused(self):
        """The run completing is not the same as the readings being usable."""
        same = reading(BALANCE / 3.0, 0.3127, 0.3290)
        order = iter([NEUTRAL["black"], NEUTRAL["white"],
                      reading(BALANCE, 0.3127, 0.3290), same, same, same])
        with self.assertRaises(MeasurementError):
            self.run_sequence(lambda: next(order))


if __name__ == "__main__":
    unittest.main()
