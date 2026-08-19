"""Turning meter readings into the figures a profile is built from.

Kept free of both the instrument and the display so the arithmetic can be tested
without either. ``meter`` talks to spotread; ``patterns.compose`` puts a patch on
screen; this decides which patches to show and what their readings mean.

The checks in ``validate`` matter more than the arithmetic. A meter that is
unplugged, pointed at the wrong part of the screen, or reading through a closed
diffuser does not fail -- it returns numbers. Those numbers reach the profile as
peak luminance and display primaries, where nothing downstream can tell them from
measurements, and the profile then describes a display that does not exist. Every
rule below exists to reject a reading that is physically impossible rather than
merely surprising, because a surprising reading may well be the panel.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .meter import MeterError, Reading

# The measurement window is a tenth of the screen, centred, on black -- see
# patterns.compose. On an emissive panel the surround is part of the measurement,
# because the brightness limiter responds to total output.
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class MeasurementStep:
    """One patch to display and read.

    ``rgb`` is relative channel drive and ``nits`` the absolute level the white
    point of that patch is asked for, because patterns work in absolute
    luminance rather than code values.
    """

    key: str
    label: str
    rgb: tuple[float, float, float]
    nits: float
    settle_seconds: float = 1.5


def plan(peak_nits: float) -> tuple[MeasurementStep, ...]:
    """The patches to measure, in the order they should be shown.

    Black comes first while the panel is still cool: on an emissive display a
    long bright sequence warms it, and the black floor is the reading most
    disturbed by that. White follows, then the three primaries at the same drive
    so their chromaticities are comparable with each other.
    """
    target = max(80.0, min(10000.0, float(peak_nits)))
    return (
        MeasurementStep("black", "Black level", BLACK, 0.0, settle_seconds=3.0),
        MeasurementStep("white", "Peak white", WHITE, target),
        MeasurementStep("red", "Red primary", (1.0, 0.0, 0.0), target),
        MeasurementStep("green", "Green primary", (0.0, 1.0, 0.0), target),
        MeasurementStep("blue", "Blue primary", (0.0, 0.0, 1.0), target),
    )


REQUIRED = ("black", "white", "red", "green", "blue")

# Widest range the Windows HDR calibration flow admits, so an unusual but real
# panel is never rejected for being unusual.
MIN_CREDIBLE_PEAK = 40.0
MAX_CREDIBLE_PEAK = 10000.0

# A display whose measured black is a meaningful fraction of its white is not a
# display; it is a meter reading room light, or a patch that never went black.
MAX_BLACK_FRACTION = 0.02

# Areas of the RGB triangle in xy, computed rather than estimated:
#   BT.709 0.1120   Adobe RGB 0.1512   DCI-P3 0.1520   BT.2020 0.2119
# The ceiling has to clear BT.2020, whose primaries sit on the spectral locus and
# so bound anything a display can actually reproduce. An earlier guess of 0.15
# would have rejected BT.2020 outright, and sat 1% above the wide-gamut panel
# this was developed on -- a threshold that fails on real hardware is worse than
# none, because it refuses the readings it was meant to protect.
MAX_GAMUT_AREA = 0.30
# A tenth of BT.709. Below this the three readings are effectively the same
# colour, which is what a meter measures when the patch never changed.
MIN_GAMUT_AREA = 0.0112


class MeasurementError(ValueError):
    """Readings that must not be allowed to reach a profile."""


def _triangle_area(primaries: tuple[float, ...]) -> float:
    """Area of the RGB triangle in the xy plane."""
    rx, ry, gx, gy, bx, by = primaries[:6]
    return abs((gx - rx) * (by - ry) - (bx - rx) * (gy - ry)) / 2.0


def validate(readings: dict[str, Reading]) -> list[str]:
    """Everything wrong with a set of readings, in plain language.

    Returns problems rather than raising so a caller can show all of them at
    once; ``derive`` raises on the first.
    """
    problems: list[str] = []

    missing = [key for key in REQUIRED if key not in readings]
    if missing:
        problems.append(f"Missing readings for: {', '.join(missing)}.")
        return problems

    white = readings["white"].Y
    black = readings["black"].Y

    if not MIN_CREDIBLE_PEAK <= white <= MAX_CREDIBLE_PEAK:
        problems.append(
            f"Measured peak white of {white:.1f} nits is outside anything a display "
            f"produces ({MIN_CREDIBLE_PEAK:g}-{MAX_CREDIBLE_PEAK:g}). The meter was "
            "probably not against the screen."
        )
    if black < 0.0:
        problems.append(f"Measured black of {black:.4f} nits is negative.")
    elif white > 0.0 and black > white * MAX_BLACK_FRACTION:
        problems.append(
            f"Measured black of {black:.4f} nits is {black / white:.1%} of white. That is "
            "room light reaching the sensor, or a patch that never went black."
        )
    if white <= black:
        problems.append("Peak white measured no brighter than black.")

    primaries = measured_primaries(readings)
    for name, (x, y) in zip(
        ("red", "green", "blue", "white"),
        [(primaries[i], primaries[i + 1]) for i in (0, 2, 4, 6)],
    ):
        if not (0.0 < x < 1.0 and 0.0 < y < 1.0):
            problems.append(f"The {name} reading has an impossible chromaticity ({x:.4f}, {y:.4f}).")

    area = _triangle_area(primaries)
    if area > MAX_GAMUT_AREA:
        problems.append(
            f"The measured primaries span an impossible gamut (area {area:.4f}). "
            "The patches and the readings are probably out of step."
        )
    elif area < MIN_GAMUT_AREA:
        problems.append(
            f"The red, green and blue readings are nearly the same colour (area {area:.4f}). "
            "The patch may not have changed between readings."
        )
    return problems


def measured_primaries(readings: dict[str, Reading]) -> tuple[float, ...]:
    """The eight xy coordinates a profile describes a display with.

    Ordered rx, ry, gx, gy, bx, by, wx, wy to match ``ModeState.panel_primaries``.
    """
    return (
        readings["red"].x,
        readings["red"].y,
        readings["green"].x,
        readings["green"].y,
        readings["blue"].x,
        readings["blue"].y,
        readings["white"].x,
        readings["white"].y,
    )


@dataclass(frozen=True, slots=True)
class Calibration:
    """What a completed measurement run contributes to a profile."""

    peak_nits: float
    black_nits: float
    primaries: tuple[float, ...]

    @property
    def contrast(self) -> float:
        """Measured contrast ratio, or infinity for a true black."""
        if self.black_nits <= 0.0:
            return float("inf")
        return self.peak_nits / self.black_nits


def derive(readings: dict[str, Reading]) -> Calibration:
    """Reduce a set of readings to profile values, or refuse.

    Refusing is the point. Everything this returns goes into a profile as fact,
    and the alternative to refusing is a display description that is confidently
    wrong.
    """
    problems = validate(readings)
    if problems:
        raise MeasurementError(" ".join(problems))
    return Calibration(
        peak_nits=readings["white"].Y,
        black_nits=max(0.0, readings["black"].Y),
        primaries=measured_primaries(readings),
    )


class Aborted(Exception):
    """The user stopped the run. Not an error, and never partially applied."""


class Display(Protocol):
    """Whatever can put a patch on screen and keep it there."""

    def show(self, step: MeasurementStep) -> None: ...


def run(
    display: Display,
    read: Callable[[], Reading],
    *,
    peak_nits: float,
    on_progress: Callable[[MeasurementStep, int, int], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Calibration:
    """Show each patch, read it, and reduce the set to profile values.

    The display and the instrument are both injected, so the sequence -- which
    patch, in what order, how long to settle, what to do when one fails -- can be
    tested without either.

    A failed reading ends the run rather than being skipped. Deriving a
    calibration from four of five patches would quietly change what the readings
    mean: primaries taken without their matching white are not comparable, and a
    peak carried over from a previous attempt is not a measurement of anything.
    """
    steps = plan(peak_nits)
    readings: dict[str, Reading] = {}

    for index, step in enumerate(steps):
        if should_abort is not None and should_abort():
            raise Aborted()
        if on_progress is not None:
            on_progress(step, index, len(steps))

        display.show(step)
        # The panel needs time to reach the level it was asked for, and an
        # emissive one settles more slowly out of black than into it.
        sleep(step.settle_seconds)

        if should_abort is not None and should_abort():
            raise Aborted()
        try:
            readings[step.key] = read()
        except MeterError as exc:
            raise MeasurementError(f"{step.label}: {exc}") from exc

    return derive(readings)
