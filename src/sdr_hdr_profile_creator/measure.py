"""Turning meter readings into the figures a profile is built from.

Kept free of both the instrument and the display so the arithmetic can be tested
without either. ``meter`` talks to spotread; ``patterns.measurement_frame`` puts a
patch on screen; this decides which patches to show and what their readings mean.

The checks in ``validate`` matter more than the arithmetic. A meter that is
unplugged, pointed at the wrong part of the screen, or reading through a closed
diffuser does not fail -- it returns numbers, and those reach the profile with
nothing downstream able to tell them from measurements. Every rule below rejects
a reading that is physically impossible rather than merely surprising, because a
surprising reading may well be the panel.

**What the red, green and blue patches are, and are not.** They are presented in
scRGB, which is defined on BT.709 primaries, so asking for ``(1, 0, 0)`` asks for
BT.709 red and Windows renders it inside whatever the panel can do. Measured on a
P3 panel whose native green is (0.2698, 0.6859), the reading came back
(0.3141, 0.5892) -- 0.0141 from BT.709 green and 0.0967 from the panel's own. So
they are useless as a description of the display's gamut, and writing them to a
profile's colorant tags replaced correct figures with a narrower, wrong gamut.

They are exactly right for the other job. A white-balance correction acts on the
signal this app sends, so what it needs to know is how the display responds to
*that* signal -- which is what these patches measure. The panel's own primaries
are read from its EDID instead, because DXGI reports the applied profile back.

**Why the balance patches are dim.** The correction is solved from red plus green
plus blue equalling the white they make, which only holds where the panel is
linear. At peak drive it is not: white asks for about three times the power of a
single channel, so the brightness limiter dims white much harder than it dims
red, and the channels then sum far above the white measured beside them. Peak is
therefore measured at full drive, where the limiter is the point, and the balance
set well below it, where the limiter is not running.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .meter import MeterError, Reading

WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)

# The patch covers this fraction of screen area -- see patterns.WINDOW_AREA_FRACTION.
# Peak luminance is meaningless without it: an emissive panel's brightness limiter
# responds to total output, and the display this was developed against is rated
# 1015 nits but reads 454 on a tenth of the screen.
WINDOW_AREA_FRACTION = 0.10

# CIE xy of D65, the white every HDR profile here is built around.
D65_XY = (0.3127, 0.3290)

# White within this distance of D65 in u'v' counts as neutral. 0.003 is the
# threshold a calibrated display is normally held to, and comfortably below where
# a tint becomes visible against a reference.
VERIFIED_DELTA_UV = 0.003

# The level the balance patches are shown at, in nits.
#
# White balance is solved from red plus green plus blue equalling the white they
# make, and that only holds where the panel is linear. At full drive it is not:
# white asks for roughly three times the power of any single channel, so an
# emissive panel's brightness limiter dims white far harder than it dims red, and
# the three channels then sum to well above the white actually measured. On the
# development panel a mere 5% of such dimming already exceeds the additivity
# check, and a limiter does much more than 5%.
#
# So peak is measured at full drive, where the limiter is the thing being
# measured, and the balance set is measured here, low enough that it never
# engages and high enough to sit far above the instrument's noise.
BALANCE_NITS = 100.0


def to_uv(x: float, y: float) -> tuple[float, float]:
    """CIE 1976 u'v', where equal distances are roughly equally visible.

    xy is not perceptually uniform: the same numeric error is glaring in one
    part of the diagram and invisible in another, which makes a dx of 0.017
    impossible to interpret on its own.
    """
    denominator = -2.0 * x + 12.0 * y + 3.0
    if abs(denominator) < 1e-9:
        return (0.0, 0.0)
    return (4.0 * x / denominator, 9.0 * y / denominator)


def delta_uv(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Distance between two chromaticities in u'v'."""
    u1, v1 = to_uv(*first)
    u2, v2 = to_uv(*second)
    return ((u1 - u2) ** 2 + (v1 - v2) ** 2) ** 0.5


def correlated_colour_temperature(x: float, y: float) -> float:
    """McCamy's approximation, in kelvin.

    Only meaningful near the blackbody locus, which is where a display's white
    should be; far from it the number is arithmetic rather than a temperature.
    """
    denominator = 0.1858 - y
    if abs(denominator) < 1e-9:
        return 0.0
    n = (x - 0.3320) / denominator
    return 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33


def compose_gains(
    existing: tuple[float, float, float], measured: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Fold a new correction into the one already in force.

    A measurement describes the display *as it is currently corrected*, so its
    gains are relative to that, not absolute. Replacing rather than composing
    makes a second pass undo the first: a display corrected to neutral measures
    neutral, solves gains of (1, 1, 1), and stores them -- throwing the
    correction away and returning the display to where it started.

    Composing also makes repeated passes converge. Where the first correction was
    imperfect the second measures only what is left, and the product is the
    total. A verified calibration therefore reports a composed result equal to
    the one it started with.
    """
    combined = tuple(max(0.0, a) * max(0.0, b) for a, b in zip(existing, measured))
    largest = max(combined)
    if largest <= 0.0:
        return (1.0, 1.0, 1.0)
    return tuple(value / largest for value in combined)


@dataclass(frozen=True, slots=True)
class MeasurementStep:
    """One patch to display and read.

    ``rgb`` is relative channel drive and ``nits`` the absolute level white is
    asked for, because patterns work in absolute luminance rather than code values.
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
    disturbed by that. The primaries follow white at the same drive, because a
    channel measured at some other level samples a different point on the
    display's response and cannot be combined with the others.
    """
    target = max(80.0, min(10000.0, float(peak_nits)))
    # Never ask for a balance level above half the peak, so a dim display is not
    # measured for balance at a level its own limiter is already fighting.
    balance = min(BALANCE_NITS, target / 2.0)
    return (
        MeasurementStep("black", "Black level", BLACK, 0.0, settle_seconds=3.0),
        MeasurementStep("white", "Peak white", WHITE, target, settle_seconds=2.0),
        MeasurementStep("balance-white", "Reference white", WHITE, balance),
        MeasurementStep("red", "Red channel", (1.0, 0.0, 0.0), balance),
        MeasurementStep("green", "Green channel", (0.0, 1.0, 0.0), balance),
        MeasurementStep("blue", "Blue channel", (0.0, 0.0, 1.0), balance),
    )


REQUIRED = ("black", "white", "balance-white", "red", "green", "blue")

# Widest range the Windows HDR calibration flow admits, so an unusual but real
# panel is never rejected for being unusual.
MIN_CREDIBLE_PEAK = 40.0
MAX_CREDIBLE_PEAK = 10000.0

# A display whose measured black is a meaningful fraction of its white is not a
# display; it is a meter reading room light, or a patch that never went black.
MAX_BLACK_FRACTION = 0.02

# How far the three channels added together may sit from the measured white before
# the set is refused. Additivity is the assumption the whole correction rests on:
# if red plus green plus blue does not make the white that was measured, something
# between the signal and the panel is not linear -- tone mapping, or a brightness
# limiter reacting to the different patches -- and gains derived from it would be
# confidently wrong. 8% is loose enough for instrument noise on a dim channel and
# tight enough to catch a limiter.
MAX_ADDITIVITY_ERROR = 0.08

# The per-channel trims a profile can carry, as a fraction. Matches the +/-25%
# range ModeState clamps red_channel, green_channel and blue_channel to.
MAX_CHANNEL_TRIM = 0.25

# The three channels must actually differ from one another, measured as the area
# of the triangle they span in xy. This is a degeneracy check and nothing more --
# these readings describe the encoding rather than the panel, so their absolute
# size says nothing about the display's gamut. But three readings of the same
# colour mean the patch never changed between them, and the gains solved from
# that are meaningless: the matrix is singular, and a set that adds up correctly
# can still be three identical greys. A tenth of the BT.709 area (0.1120) is far
# below any real set and far above measurement noise.
MIN_CHANNEL_SEPARATION = 0.0112


class MeasurementError(ValueError):
    """Readings that must not be allowed to reach a profile."""


def _xyz(reading: Reading) -> tuple[float, float, float]:
    return (reading.X, reading.Y, reading.Z)


def _inverse3(m: tuple[float, ...]) -> tuple[float, ...] | None:
    """Inverse of a row-major 3x3, or None when it is singular."""
    a, b, c, d, e, f, g, h, i = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    return (
        (e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det,
        (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det,
        (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det,
    )


def _matvec3(m: tuple[float, ...], v: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
        m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
        m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
    )


def _channel_separation(readings: dict[str, Reading]) -> float:
    """Area of the triangle the three channel readings span in xy."""
    rx, ry = readings["red"].x, readings["red"].y
    gx, gy = readings["green"].x, readings["green"].y
    bx, by = readings["blue"].x, readings["blue"].y
    return abs((gx - rx) * (by - ry) - (bx - rx) * (gy - ry)) / 2.0


def _additivity_error(readings: dict[str, Reading]) -> float:
    """How far red + green + blue sits from the reference white, relatively.

    Measured against the balance white rather than peak white, because the three
    channels were shown at the same level as it. Comparing them against a peak
    white the limiter has dimmed would fail on every emissive panel.
    """
    white = _xyz(readings["balance-white"])
    total = tuple(
        sum(_xyz(readings[channel])[axis] for channel in ("red", "green", "blue"))
        for axis in range(3)
    )
    scale = max(white[1], 1e-6)
    return max(abs(total[axis] - white[axis]) for axis in range(3)) / scale


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

    for name in ("white", "balance-white", "red", "green", "blue"):
        x, y = readings[name].x, readings[name].y
        if not (0.0 < x < 1.0 and 0.0 < y < 1.0):
            problems.append(
                f"The {name} reading has an impossible chromaticity ({x:.4f}, {y:.4f})."
            )

    if not problems:
        separation = _channel_separation(readings)
        if separation < MIN_CHANNEL_SEPARATION:
            problems.append(
                f"The red, green and blue readings are nearly the same colour (area "
                f"{separation:.4f}). The patch cannot have changed between them, so there "
                "is no white balance to solve."
            )
        error = _additivity_error(readings)
        if error > MAX_ADDITIVITY_ERROR:
            problems.append(
                f"Red, green and blue add up to {error:.0%} away from the measured white. "
                "Something between the signal and the panel is not linear, so a white "
                "balance derived from these would be wrong."
            )
    return problems


@dataclass(frozen=True, slots=True)
class Calibration:
    """What a completed measurement run contributes to a profile."""

    peak_nits: float
    black_nits: float
    white_xy: tuple[float, float]
    channel_gains: tuple[float, float, float]
    window_fraction: float = WINDOW_AREA_FRACTION

    @property
    def contrast(self) -> float:
        """Measured contrast ratio, or infinity when black is below the floor.

        An OLED reads a true black as 0.0000 on this class of instrument, which is
        a real result rather than a failed one -- but it is a floor, not a value,
        so an infinite ratio should be read as "unmeasurably low".
        """
        if self.black_nits <= 0.0:
            return float("inf")
        return self.peak_nits / self.black_nits

    @property
    def white_error(self) -> tuple[float, float]:
        """How far measured white sits from D65, as (dx, dy)."""
        return (self.white_xy[0] - D65_XY[0], self.white_xy[1] - D65_XY[1])

    @property
    def white_delta_uv(self) -> float:
        """White error in u'v', which is what decides whether it is visible.

        Roughly: below 0.001 is indistinguishable, 0.003 is the usual target for
        a calibrated display, and 0.005 is where a careful eye starts to see a
        tint against a known reference.
        """
        return delta_uv(self.white_xy, D65_XY)

    @property
    def white_cct(self) -> float:
        """Measured white as a colour temperature, which D65 puts at 6504K."""
        return correlated_colour_temperature(*self.white_xy)

    @property
    def verified(self) -> bool:
        """Whether the display was already neutral when this was measured.

        True means the run found nothing left to correct, which for a second pass
        over an applied calibration is the confirmation that it worked.
        """
        return self.white_delta_uv <= VERIFIED_DELTA_UV

    @property
    def channel_trims(self) -> tuple[float, float, float]:
        """The gains as the percentage trims a profile stores, red first.

        Always zero or negative. A display cannot be asked for light it does not
        have, so white is corrected by pulling the excess channels down to meet
        the weakest, never by pushing one up into clipping.
        """
        return tuple(round((gain - 1.0) * 100.0, 3) for gain in self.channel_gains)

    @property
    def trims_exceed_range(self) -> bool:
        """Whether the correction is larger than a profile can carry."""
        return any(abs(trim) > MAX_CHANNEL_TRIM * 100.0 for trim in self.channel_trims)


def white_balance_gains(readings: dict[str, Reading]) -> tuple[float, float, float]:
    """Per-channel gains that move measured white onto D65.

    Solves ``M g = T``, where the columns of ``M`` are the measured XYZ of the
    three channels at full drive and ``T`` is D65 at the same luminance. If the
    display were already neutral the answer would be (1, 1, 1).

    The result is scaled so the largest gain is exactly 1.0. Correcting white by
    boosting a channel would ask for output the panel has already run out of, so
    the excess channels come down to meet the weakest instead. That costs
    luminance, which is the honest price of a neutral white.
    """
    columns = [_xyz(readings[channel]) for channel in ("red", "green", "blue")]
    matrix = tuple(columns[column][row] for row in range(3) for column in range(3))
    inverse = _inverse3(matrix)
    if inverse is None:
        # Three channels that do not span a colour space; validate() will have
        # said why, and a neutral answer is the only safe one.
        return (1.0, 1.0, 1.0)

    luminance = max(readings["balance-white"].Y, 1e-6)
    x, y = D65_XY
    target = ((x / y) * luminance, luminance, ((1.0 - x - y) / y) * luminance)

    gains = _matvec3(inverse, target)
    largest = max(gains)
    if largest <= 0.0:
        return (1.0, 1.0, 1.0)
    return tuple(max(0.0, gain / largest) for gain in gains)


def derive(readings: dict[str, Reading]) -> Calibration:
    """Reduce a set of readings to profile values, or refuse.

    Refusing is the point. Everything this returns goes into a profile as fact,
    and the alternative to refusing is a display description that is confidently
    wrong.
    """
    problems = validate(readings)
    if problems:
        raise MeasurementError(" ".join(problems))
    # Peak comes from the patch driven to peak; the white point comes from the
    # one the channels were measured beside, which is the white the correction
    # is actually solved against.
    reference = readings["balance-white"]
    return Calibration(
        peak_nits=readings["white"].Y,
        black_nits=max(0.0, readings["black"].Y),
        white_xy=(reference.x, reference.y),
        channel_gains=white_balance_gains(readings),
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
    on_reading: Callable[[MeasurementStep, Reading], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Calibration:
    """Show each patch, read it, and reduce the set to profile values.

    The display and the instrument are both injected, so the sequence -- which
    patch, in what order, how long to settle, what to do when one fails -- can be
    tested without either.

    A failed reading ends the run rather than being skipped. Channels measured
    without their matching white cannot be combined, and a peak carried over from
    a previous attempt is not a measurement of anything.
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
            reading = read()
        except MeterError as exc:
            raise MeasurementError(f"{step.label}: {exc}") from exc
        readings[step.key] = reading
        if on_reading is not None:
            # Reported as it arrives rather than at the end, so a run that is
            # later refused still leaves the numbers that caused it.
            on_reading(step, reading)

    return derive(readings)
