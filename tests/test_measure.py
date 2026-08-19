"""Turning meter readings into profile values, and refusing when they are wrong.

The rejection rules carry more weight than the arithmetic. A meter that is
unplugged, aimed at the wrong part of the screen, or reading through a closed
diffuser still returns numbers, and those numbers reach the profile as peak
luminance and display primaries where nothing downstream can tell them from real
measurements. Each test below stands for a way that has actually been observed
to happen.
"""

from __future__ import annotations

import unittest

from sdr_hdr_profile_creator.measure import (
    MAX_GAMUT_AREA,
    MIN_GAMUT_AREA,
    Aborted,
    Calibration,
    MeasurementError,
    derive,
    measured_primaries,
    plan,
    run,
    validate,
)
from sdr_hdr_profile_creator.meter import MeterError, Reading


def reading(Y: float, x: float, y: float) -> Reading:
    """A reading with a plausible XYZ for the given Y and chromaticity."""
    if y <= 0.0:
        return Reading(X=0.0, Y=Y, Z=0.0, x=x, y=y)
    X = (x / y) * Y
    Z = ((1.0 - x - y) / y) * Y
    return Reading(X=X, Y=Y, Z=Z, x=x, y=y)


# Measured figures for the wide-gamut OLED this was developed against: EDID
# declares 1015.24 nits peak and 0.000156 black, and DXGI reports these primaries.
GOOD = {
    "black": reading(0.00016, 0.3130, 0.3290),
    "white": reading(1015.24, 0.3127, 0.3290),
    "red": reading(215.0, 0.674586, 0.314418),
    "green": reading(710.0, 0.269814, 0.685949),
    "blue": reading(90.0, 0.151222, 0.060916),
}


def without(key: str, **replacements) -> dict[str, Reading]:
    updated = dict(GOOD)
    updated.pop(key, None)
    updated.update(replacements)
    return updated


def changed(**replacements) -> dict[str, Reading]:
    updated = dict(GOOD)
    updated.update(replacements)
    return updated


class PlanTests(unittest.TestCase):
    def test_measures_black_white_and_all_three_primaries(self):
        keys = [step.key for step in plan(1000.0)]
        self.assertEqual(keys, ["black", "white", "red", "green", "blue"])

    def test_black_is_measured_first_while_the_panel_is_still_cool(self):
        """A long bright sequence warms an emissive panel, and the black floor
        is the reading most disturbed by that."""
        self.assertEqual(plan(1000.0)[0].key, "black")

    def test_black_is_given_longer_to_settle(self):
        steps = {step.key: step for step in plan(1000.0)}
        self.assertGreater(steps["black"].settle_seconds, steps["white"].settle_seconds)

    def test_primaries_are_driven_at_the_same_level_as_white(self):
        """Their chromaticities are only comparable if the drive matches."""
        steps = {step.key: step for step in plan(1000.0)}
        for key in ("red", "green", "blue"):
            self.assertEqual(steps[key].nits, steps["white"].nits)

    def test_an_absurd_peak_is_clamped_into_range(self):
        self.assertLessEqual(plan(99000.0)[1].nits, 10000.0)
        self.assertGreaterEqual(plan(0.0)[1].nits, 80.0)


class DeriveTests(unittest.TestCase):
    def test_accepts_a_realistic_wide_gamut_oled(self):
        result = derive(GOOD)
        self.assertIsInstance(result, Calibration)
        self.assertAlmostEqual(result.peak_nits, 1015.24, places=2)
        self.assertAlmostEqual(result.black_nits, 0.00016, places=6)

    def test_primaries_are_ordered_to_match_the_profile_field(self):
        primaries = measured_primaries(GOOD)
        self.assertAlmostEqual(primaries[0], 0.674586)  # red x
        self.assertAlmostEqual(primaries[3], 0.685949)  # green y
        self.assertAlmostEqual(primaries[6], 0.3127)    # white x
        self.assertEqual(len(primaries), 8)

    def test_contrast_is_reported_for_a_measurable_black(self):
        self.assertGreater(derive(GOOD).contrast, 1_000_000)

    def test_a_true_black_gives_infinite_contrast_rather_than_dividing_by_zero(self):
        result = derive(changed(black=reading(0.0, 0.31, 0.33)))
        self.assertEqual(result.contrast, float("inf"))

    def test_refuses_rather_than_returning_something_wrong(self):
        with self.assertRaises(MeasurementError):
            derive(without("white"))


class ValidationTests(unittest.TestCase):
    def test_a_good_set_has_no_complaints(self):
        self.assertEqual(validate(GOOD), [])

    def test_missing_readings_are_named(self):
        problems = validate(without("green"))
        self.assertTrue(any("green" in problem for problem in problems))

    def test_rejects_a_peak_no_display_could_produce(self):
        """A meter off the screen entirely reads room light, not the panel."""
        problems = validate(changed(white=reading(3.0, 0.3127, 0.3290)))
        self.assertTrue(any("outside anything a display" in p for p in problems))

    def test_rejects_a_black_that_is_really_room_light(self):
        """A meter not seated against the screen reads a black patch as a
        substantial fraction of white, which no panel does."""
        problems = validate(changed(black=reading(80.0, 0.31, 0.33)))
        self.assertTrue(any("room light" in p for p in problems))

    def test_rejects_white_no_brighter_than_black(self):
        problems = validate(
            changed(white=reading(50.0, 0.3127, 0.3290), black=reading(60.0, 0.31, 0.33))
        )
        self.assertTrue(any("no brighter than black" in p for p in problems))

    def test_rejects_an_impossible_chromaticity(self):
        problems = validate(changed(red=reading(215.0, 1.4, 0.31)))
        self.assertTrue(any("impossible chromaticity" in p for p in problems))

    def test_rejects_three_readings_of_the_same_colour(self):
        """This is what a meter returns when the patch never changed between
        steps -- three identical readings that average to a plausible white."""
        same = reading(300.0, 0.3127, 0.3290)
        problems = validate(changed(red=same, green=same, blue=same))
        self.assertTrue(any("nearly the same colour" in p for p in problems))

    def test_rejects_a_gamut_larger_than_anything_real(self):
        problems = validate(
            changed(
                red=reading(215.0, 0.95, 0.04),
                green=reading(710.0, 0.02, 0.95),
                blue=reading(90.0, 0.02, 0.02),
            )
        )
        self.assertTrue(any("impossible gamut" in p for p in problems))


class GamutThresholdTests(unittest.TestCase):
    """The thresholds must admit every gamut that actually ships.

    A first pass guessed a ceiling of 0.15, which rejected BT.2020 outright and
    sat 1% above the panel this was developed on. A limit that refuses real
    hardware is worse than no limit, because it blocks exactly the readings it
    was added to protect.
    """

    GAMUTS = {
        "BT.709": (0.640, 0.330, 0.300, 0.600, 0.150, 0.060),
        "Adobe RGB": (0.640, 0.330, 0.210, 0.710, 0.150, 0.060),
        "DCI-P3": (0.680, 0.320, 0.265, 0.690, 0.150, 0.060),
        "BT.2020": (0.708, 0.292, 0.170, 0.797, 0.131, 0.046),
        "PG32UCDM": (0.674586, 0.314418, 0.269814, 0.685949, 0.151222, 0.060916),
    }

    def test_every_real_gamut_is_accepted(self):
        for name, xy in self.GAMUTS.items():
            with self.subTest(gamut=name):
                readings = changed(
                    red=reading(215.0, xy[0], xy[1]),
                    green=reading(710.0, xy[2], xy[3]),
                    blue=reading(90.0, xy[4], xy[5]),
                )
                self.assertEqual(validate(readings), [], name)

    def test_the_ceiling_clears_bt2020_which_bounds_what_a_display_can_do(self):
        """BT.2020's primaries sit on the spectral locus, so nothing physical
        exceeds its area of 0.2119."""
        self.assertGreater(MAX_GAMUT_AREA, 0.2119)

    def test_the_floor_sits_below_the_narrowest_shipping_gamut(self):
        self.assertLess(MIN_GAMUT_AREA, 0.1120)


class FakeDisplay:
    """Records which patches were shown, in order."""

    def __init__(self):
        self.shown = []

    def show(self, step):
        self.shown.append(step.key)


class RunTests(unittest.TestCase):
    """Sequencing, with both the screen and the instrument injected."""

    def setUp(self):
        self.display = FakeDisplay()
        self.slept = []

    def run_sequence(self, reader, **kwargs):
        return run(
            self.display,
            reader,
            peak_nits=1015.24,
            sleep=self.slept.append,
            **kwargs,
        )

    def good_reader(self):
        order = iter([GOOD[key] for key in ('black', 'white', 'red', 'green', 'blue')])
        return lambda: next(order)

    def test_shows_every_patch_in_plan_order(self):
        self.run_sequence(self.good_reader())
        self.assertEqual(self.display.shown, ['black', 'white', 'red', 'green', 'blue'])

    def test_returns_a_calibration_from_the_readings(self):
        result = self.run_sequence(self.good_reader())
        self.assertAlmostEqual(result.peak_nits, 1015.24, places=2)

    def test_lets_the_panel_settle_before_each_reading(self):
        """A patch read the instant it appears is read mid-transition."""
        self.run_sequence(self.good_reader())
        self.assertEqual(len(self.slept), 5)
        self.assertTrue(all(delay > 0 for delay in self.slept))

    def test_reports_progress_for_each_step(self):
        seen = []
        self.run_sequence(
            self.good_reader(),
            on_progress=lambda step, i, total: seen.append((step.key, i, total)),
        )
        self.assertEqual([entry[0] for entry in seen],
                         ['black', 'white', 'red', 'green', 'blue'])
        self.assertEqual(seen[0][2], 5)

    def test_a_failed_reading_ends_the_run_rather_than_being_skipped(self):
        """Primaries taken without their matching white are not comparable,
        so a partial set must not be reduced to a calibration at all."""
        def reader():
            raise MeterError('sensor in the wrong position')

        with self.assertRaises(MeasurementError) as caught:
            self.run_sequence(reader)
        self.assertIn('wrong position', str(caught.exception))

    def test_the_failing_step_is_named(self):
        def reader():
            raise MeterError('boom')

        with self.assertRaises(MeasurementError) as caught:
            self.run_sequence(reader)
        self.assertIn('Black level', str(caught.exception))

    def test_aborting_stops_without_producing_a_calibration(self):
        with self.assertRaises(Aborted):
            self.run_sequence(self.good_reader(), should_abort=lambda: True)
        self.assertEqual(self.display.shown, [])

    def test_aborting_partway_leaves_nothing_applied(self):
        calls = {'n': 0}

        def abort():
            calls['n'] += 1
            return calls['n'] > 4

        with self.assertRaises(Aborted):
            self.run_sequence(self.good_reader(), should_abort=abort)

    def test_readings_that_do_not_survive_validation_are_refused(self):
        """The run completing is not the same as the readings being usable."""
        same = reading(300.0, 0.3127, 0.3290)
        order = iter([GOOD['black'], GOOD['white'], same, same, same])
        with self.assertRaises(MeasurementError):
            self.run_sequence(lambda: next(order))


if __name__ == "__main__":
    unittest.main()
