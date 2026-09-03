"""Bringing white onto D65 with the monitor's own RGB gains, measured in a loop.

The profile can carry +-25% of channel trim and no more, and on the display this was
developed against red reached that clamp with white still visibly off. The app's own
advice at that point is "check the monitor's own colour temperature setting first" --
this is that advice, performed rather than printed.

Doing it at the panel is better than doing it in the profile for a reason beyond the
clamp: a gain applied before the panel's own processing costs no signal precision, while
the same correction in a LUT spends code values to achieve it. The profile correction
then starts from a display that is already close, and has range left for the tone curve.

**The solve is the one already used for the matrix.** ``white_balance_gains`` returns
per-channel gains normalised so the largest is exactly 1.0, worked out without assuming
the channels add up -- which they do not on this panel. Using a second method here would
mean the two halves of the correction were aimed at different whites.

That normalisation also decides the shape of this loop: every correction reduces two
channels rather than raising one, so nothing is ever asked for beyond what the panel has
left, and no step can clip. It costs luminance, which is the honest price of a neutral
white and the same trade a hardware calibration makes.

Nothing here talks to a monitor. The link and the measurement both arrive as arguments,
so the whole loop runs against a fake panel in the tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ddc

#: How many measure-and-adjust rounds before giving up. Each one costs a meter reading of
#: four patches, so this is minutes rather than seconds; a display that has not settled
#: by the fourth is not going to.
MAX_ROUNDS = 6

#: A gain is an integer 0-100, so a correction smaller than one step cannot be applied and
#: asking for it again would loop forever.
MIN_STEP = 1

#: Never drive a channel below this. A gain in single digits is not a calibration, it is a
#: solve that has gone wrong, and it leaves the panel with nothing to work with.
FLOOR = 40


@dataclass(frozen=True, slots=True)
class Round:
    """One measure-and-adjust pass, kept so the caller can show its work."""

    gains: tuple[int, int, int]
    delta_uv: float
    solved: tuple[float, float, float]
    applied: tuple[int, int, int] | None


@dataclass(slots=True)
class Outcome:
    """What the loop did, and whether it helped."""

    started_at: tuple[int, int, int] | None = None
    ended_at: tuple[int, int, int] | None = None
    before_delta_uv: float = 0.0
    after_delta_uv: float = 0.0
    rounds: list[Round] = field(default_factory=list)
    refused: str = ""

    @property
    def improved(self) -> bool:
        return bool(self.rounds) and self.after_delta_uv < self.before_delta_uv

    @property
    def changed(self) -> bool:
        return (
            self.started_at is not None
            and self.ended_at is not None
            and self.started_at != self.ended_at
        )


def next_gains(
    current: tuple[int, int, int],
    solved: tuple[float, float, float],
    maximum: int,
    *,
    floor: int = FLOOR,
) -> tuple[int, int, int]:
    """The gains to send, given where they are and what the solve asked for.

    ``solved`` is normalised so its largest entry is 1.0, so this only ever reduces --
    the channel that is already weakest stays put and the others come down to meet it.
    Rounding is to the nearest integer because that is the resolution a VCP gain has;
    the caller stops when a round asks for no change, which is what convergence means
    when the control is this coarse.
    """
    wanted = []
    for value, scale in zip(current, solved):
        target = int(round(value * max(0.0, scale)))
        wanted.append(max(floor, min(maximum, target)))
    return (wanted[0], wanted[1], wanted[2])


def settled(current: tuple[int, int, int], proposed: tuple[int, int, int]) -> bool:
    """Whether a further round could change anything."""
    return all(abs(a - b) < MIN_STEP for a, b in zip(current, proposed))


def tune(
    link: ddc.Link,
    measure_white,
    solve_gains,
    delta_from_d65,
    *,
    max_rounds: int = MAX_ROUNDS,
) -> Outcome:
    """Adjust the monitor's RGB gains until measured white stops improving.

    ``measure_white`` returns the readings a white-balance solve needs; ``solve_gains``
    turns those into per-channel gains; ``delta_from_d65`` scores the white. All three
    are injected so this runs against a simulated panel.

    Restores the gains it started with on any refusal after the first write, because a
    half-applied balance is worse than the one the user had: it is neither the factory
    setting they could reason about nor a calibration.
    """
    outcome = Outcome()

    controls = ddc.read_gains(link)
    if controls is None:
        outcome.refused = (
            "The monitor did not report its RGB gains over DDC/CI. It may have DDC/CI "
            "turned off in its own menu, or be connected through an adapter that does "
            "not carry it."
        )
        return outcome

    maximum = min(control.maximum for control in controls.values())
    start = tuple(controls[code].current for code in ddc.GAINS)
    outcome.started_at = start  # type: ignore[assignment]
    outcome.ended_at = start  # type: ignore[assignment]
    current = start

    for index in range(max_rounds):
        readings = measure_white()
        if not readings:
            outcome.refused = "The meter did not return a usable white reading."
            break

        delta = delta_from_d65(readings)
        solved = solve_gains(readings)
        if index == 0:
            outcome.before_delta_uv = delta
        outcome.after_delta_uv = delta

        proposed = next_gains(current, solved, maximum)  # type: ignore[arg-type]
        if settled(current, proposed):  # type: ignore[arg-type]
            outcome.rounds.append(Round(current, delta, solved, None))  # type: ignore[arg-type]
            break

        # A round that made white worse is a reading that cannot be trusted -- a meter
        # knocked, a patch caught before it settled -- and acting on it would walk the
        # display away from neutral one confident step at a time. Stop on the first one
        # and keep what the previous round achieved.
        if outcome.rounds and delta > outcome.rounds[-1].delta_uv + 1e-6:
            outcome.rounds.append(Round(current, delta, solved, None))  # type: ignore[arg-type]
            outcome.refused = (
                "White moved further from D65 after the last adjustment, so tuning "
                "stopped and the previous setting was kept. Check the meter is still "
                "flush against the screen."
            )
            _restore(link, ddc.GAINS, outcome.rounds[-2].gains)
            outcome.ended_at = outcome.rounds[-2].gains
            break

        problem = _apply(link, proposed)
        if problem:
            # The reason comes from write_control, which separates a busy link from a
            # locked control. Saying "your controls are locked" for what was actually a
            # cold DDC/CI link would send someone into their monitor's menu for nothing.
            outcome.refused = (
                f"The monitor's red, green and blue gains could not be set: {problem}. "
                "The previous setting has been put back."
            )
            _restore(link, ddc.GAINS, start)  # type: ignore[arg-type]
            outcome.ended_at = start  # type: ignore[assignment]
            break

        outcome.rounds.append(Round(current, delta, solved, proposed))  # type: ignore[arg-type]
        current = proposed
        outcome.ended_at = proposed

    # Gains that were written, read back, and changed nothing.
    #
    # This is the failure mode read-back cannot see, and it is not hypothetical on the
    # hardware this was built for: a meter-verified test on 2026-08-22 wrote red gain to
    # 70, read back 70, and moved measured white by 0.0001 in xy -- noise. The monitor
    # accepts the value, stores it, reports it, and does not apply it while HDR is on.
    #
    # So the only honest confirmation is the meter, and if white did not move there is
    # nothing to keep. Leaving the gains where the loop put them would be the worst
    # outcome available: a monitor whose settings no longer match its factory state, in
    # exchange for no change on screen and no way for the user to know that.
    if outcome.changed and not outcome.improved and not outcome.refused:
        _restore(link, ddc.GAINS, outcome.started_at)
        outcome.ended_at = outcome.started_at
        outcome.refused = (
            "Adjusting the monitor's RGB gains did not move measured white, so the "
            "original settings were put back. The gains read back correctly, which "
            "means the monitor is storing them and not applying them -- many panels "
            "ignore these controls while HDR is on."
        )

    return outcome


def _apply(link: ddc.Link, gains: tuple[int, int, int]) -> str:
    """Set all three gains. Empty string on success, else the first reason."""
    for code, value in zip(ddc.GAINS, gains):
        problem = ddc.write_control(link, code, value)
        if problem:
            return problem
    return ""


def _restore(link: ddc.Link, codes, gains) -> None:
    """Put the gains back, trying every channel even if one refuses.

    Stopping at the first failure is how a display ends up half restored, which is the
    one state worse than either leaving it tuned or leaving it alone.
    """
    for code, value in zip(codes, gains):
        ddc.write_control(link, code, value)
