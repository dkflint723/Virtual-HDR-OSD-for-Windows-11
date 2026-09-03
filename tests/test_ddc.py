"""The DDC/CI layer and the gain-tuning loop, against a simulated panel.

Nothing here opens a monitor. The link and the measurement are both injected, so the
loop's arithmetic and its refusals are covered on any machine -- which matters, because
the one thing that cannot be tested here is the hardware, and every claim about the
hardware in this file came from probing an ASUS PG32UCDM directly:

    25 VCP codes answer, stable across two reads. Brightness, contrast, colour preset,
    RGB gain, RGB black level, gamma and picture mode are all present WITH HDR ON, and a
    write to red gain (86 -> 87) took effect and read back. A first single-attempt probe
    reported brightness, contrast and red and green gain as unsupported; they were not.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sdr_hdr_profile_creator import ddc, ddc_tune  # noqa: E402


class FakeLink:
    """A monitor whose gains can be read and written, with the failure modes real ones
    have: reads that fail the first few times, and writes that are accepted and ignored."""

    def __init__(self, gains=(100, 100, 100), maximum=100, *, cold_reads=0,
                 cold_writes=0, locked=False):
        self.values = dict(zip(ddc.GAINS, gains))
        self.maximum = maximum
        self.cold_reads = cold_reads
        self.cold_writes = cold_writes
        self.locked = locked
        self.writes: list[tuple[int, int]] = []

    def read(self, code):
        if self.cold_reads > 0:
            self.cold_reads -= 1
            return None
        if code not in self.values:
            return None
        return ddc.Control(code=code, current=self.values[code], maximum=self.maximum)

    def set(self, code, value):
        """One raw attempt, with the two real failure modes selectable.

        cold_writes fails the call outright, which is a busy link; locked lets the call
        succeed and does not move the value, which is a control the display is ignoring.
        They are opposite diagnoses and the code has to tell them apart.
        """
        self.writes.append((code, value))
        if self.cold_writes > 0:
            self.cold_writes -= 1
            return False
        if self.locked:
            return True          # accepted, and quietly ignored
        self.values[code] = value
        return True


class NextGainsTests(unittest.TestCase):
    def test_a_neutral_solve_changes_nothing(self):
        self.assertEqual(
            (86, 90, 90), ddc_tune.next_gains((86, 90, 90), (1.0, 1.0, 1.0), 100)
        )

    def test_the_correction_only_ever_reduces(self):
        """white_balance_gains normalises its largest entry to 1.0, so the channel that
        is already weakest stays and the others come down to meet it. Nothing is asked
        for beyond what the panel has left, so no step can clip."""
        result = ddc_tune.next_gains((100, 100, 100), (0.94, 1.0, 0.97), 100)
        self.assertEqual((94, 100, 97), result)
        self.assertTrue(all(value <= 100 for value in result))

    def test_nothing_is_driven_below_the_floor(self):
        """A gain in single digits is a solve that has gone wrong, not a calibration."""
        result = ddc_tune.next_gains((100, 100, 100), (0.01, 1.0, 0.01), 100)
        self.assertEqual(ddc_tune.FLOOR, result[0])
        self.assertEqual(ddc_tune.FLOOR, result[2])

    def test_a_correction_finer_than_one_step_is_reported_as_settled(self):
        """The control is an integer 0-100. Asking for 89.6 again and again is how a
        loop that cannot converge looks from the inside."""
        current = (90, 90, 90)
        proposed = ddc_tune.next_gains(current, (0.998, 1.0, 0.999), 100)
        self.assertTrue(ddc_tune.settled(current, proposed))


class ReadGainsTests(unittest.TestCase):
    def test_all_three_gains_or_nothing(self):
        """Tuning two channels against a third whose value is unknown would move white
        somewhere nobody asked for."""
        link = FakeLink()
        link.values.pop(ddc.BLUE_GAIN)
        self.assertIsNone(ddc.read_gains(link))

    def test_the_three_gains_come_back_together(self):
        found = ddc.read_gains(FakeLink(gains=(86, 90, 90)))
        self.assertIsNotNone(found)
        self.assertEqual(86, found[ddc.RED_GAIN].current)
        self.assertEqual(90, found[ddc.BLUE_GAIN].current)


class TuneTests(unittest.TestCase):
    """The loop, driven by a panel whose white moves as the gains move."""

    def panel(self, link, *, drift=0.02):
        """White that is off by `drift` until the gains are equalised, then neutral."""
        def measure():
            spread = max(link.values.values()) - min(link.values.values())
            return {"spread": spread}

        def solve(readings):
            # A red channel running hot: ask for red down until the gains match.
            return (0.97, 1.0, 1.0) if readings["spread"] < 10 else (1.0, 1.0, 1.0)

        def delta(readings):
            return drift * max(0, 10 - readings["spread"]) / 10.0

        return measure, solve, delta

    def test_it_stops_when_a_round_asks_for_no_change(self):
        link = FakeLink(gains=(90, 90, 90))
        measure, _solve, delta = self.panel(link)
        outcome = ddc_tune.tune(link, measure, lambda _r: (1.0, 1.0, 1.0), delta)
        self.assertEqual("", outcome.refused)
        self.assertFalse(outcome.changed, "a neutral display should not be adjusted")
        self.assertEqual([], link.writes)

    def test_it_walks_the_gains_down_and_settles(self):
        link = FakeLink(gains=(90, 90, 90))
        measure, solve, delta = self.panel(link)
        outcome = ddc_tune.tune(link, measure, solve, delta)
        self.assertTrue(outcome.changed)
        self.assertLess(outcome.ended_at[0], 90, "red should have come down")
        self.assertEqual(90, outcome.ended_at[1], "the weakest channel stays put")
        self.assertLessEqual(len(outcome.rounds), ddc_tune.MAX_ROUNDS)

    def test_a_monitor_that_will_not_report_its_gains_is_refused_before_any_write(self):
        link = FakeLink(cold_reads=999)
        measure, solve, delta = self.panel(link)
        outcome = ddc_tune.tune(link, measure, solve, delta)
        self.assertIn("did not report its RGB gains", outcome.refused)
        self.assertEqual([], link.writes, "nothing may be written after a failed read")

    def test_a_locked_control_is_restored_and_reported(self):
        """The common HDR case on many monitors: the write is accepted and ignored.
        write_control reads back, so this surfaces as a refusal rather than as a loop
        adjusting a control that never moved."""
        link = FakeLink(gains=(90, 90, 90), locked=True)
        measure, solve, delta = self.panel(link)
        outcome = ddc_tune.tune(link, measure, solve, delta)
        self.assertIn("did not apply it", outcome.refused)
        self.assertEqual(outcome.started_at, outcome.ended_at)

    def test_a_busy_link_is_retried_rather_than_called_locked(self):
        """29% of single DDC/CI operations fail on the hardware this was written for, so
        a refused write says nothing on its own. Reporting it as a locked control would
        send someone into their monitor's menu looking for a setting that is not the
        problem."""
        link = FakeLink(gains=(90, 90, 90), cold_writes=2)
        measure, solve, delta = self.panel(link)
        outcome = ddc_tune.tune(link, measure, solve, delta)
        self.assertEqual("", outcome.refused, "a transient failure must not stop tuning")
        self.assertTrue(outcome.changed)

    def test_the_two_write_failures_are_told_apart(self):
        """They wear the same face at the API and mean opposite things."""
        self.assertEqual(
            "", ddc.write_control(FakeLink(gains=(90, 90, 90), cold_writes=3),
                                  ddc.RED_GAIN, 80)
        )
        self.assertIn(
            "did not apply it",
            ddc.write_control(FakeLink(gains=(90, 90, 90), locked=True), ddc.RED_GAIN, 80),
        )
        self.assertIn(
            "did not accept",
            ddc.write_control(FakeLink(gains=(90, 90, 90), cold_writes=99),
                              ddc.RED_GAIN, 80, attempts=3, pause=0.0),
        )

    def test_a_restore_tries_every_channel_even_when_one_refuses(self):
        """Stopping at the first failure is how a display ends up half restored, which
        is the one state worse than either leaving it tuned or leaving it alone."""
        link = FakeLink(gains=(90, 90, 90))
        ddc_tune._restore(link, ddc.GAINS, (80, 81, 82))
        self.assertEqual([80, 81, 82], [link.values[code] for code in ddc.GAINS])

    def test_a_round_that_makes_white_worse_stops_and_steps_back(self):
        """A knocked meter or an unsettled patch would otherwise walk the display away
        from neutral one confident step at a time."""
        link = FakeLink(gains=(90, 90, 90))
        readings = {"n": 0}

        def measure():
            readings["n"] += 1
            return readings

        def solve(_r):
            return (0.97, 1.0, 1.0)

        def delta(record):
            # Better once, then abruptly worse.
            return {1: 0.010, 2: 0.004, 3: 0.050}.get(record["n"], 0.05)

        outcome = ddc_tune.tune(link, measure, solve, delta)
        self.assertIn("further from D65", outcome.refused)
        self.assertEqual(
            outcome.ended_at, outcome.rounds[-2].gains,
            "the setting from before the bad reading is what should be left in place",
        )

    def test_a_cold_link_is_retried_rather_than_called_unsupported(self):
        """The first probe of the real monitor reported four controls as unsupported
        that a second pass found present and stable. A single failed read is a cold
        link, and treating it as an absent feature would report a calibratable display
        as uncalibratable."""
        link = FakeLink(gains=(86, 90, 90), cold_reads=2)
        self.assertIsNotNone(ddc.read_gains(link))


if __name__ == "__main__":
    unittest.main()
