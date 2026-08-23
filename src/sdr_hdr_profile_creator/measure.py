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

from .gamma_correction import pq_eotf, pq_inverse_eotf
from .greyscale import PanelResponse
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
    #: How much of the screen this patch covers. Per-patch because peak is not specified
    #: at the same size as everything else -- see ``PEAK_WINDOW_FRACTION``.
    window_fraction: float = WINDOW_AREA_FRACTION


#: What the instrument must see before the run starts by itself. The target is a green
#: patch at PLACEMENT_NITS, and these say "that is what I am looking at" rather than
#: "I am looking at a dark screen, or at the room".
#:
#: Deliberately loose. This is a yes/no about placement, not a measurement: the meter
#: may be at an angle, the panel may be dimming the patch under its own limiter, and a
#: cheap colorimeter's green primary can sit some way from the panel's. Anything tight
#: enough to be a real chromaticity check would reject a meter that is correctly placed.
PLACEMENT_MIN_NITS = 12.0
PLACEMENT_MIN_GREEN_Y = 0.40


def sees_placement_target(reading: Reading) -> bool:
    """Whether this reading is of the green placement target.

    Two conditions, and both are needed. Luminance alone would accept a bright room or a
    meter still sitting on the desk under a lamp. Chromaticity alone would accept almost
    anything at near-zero luminance, where xy is numerically unstable and a
    black-screen reading can land anywhere on the diagram.

    ``y > x`` and a high ``y`` is what green looks like in CIE 1931 and nothing else
    does: the display's green primary sits near 0.24, 0.71 on the panel this was built
    against, room light near 0.44, 0.40, and a dark screen produces noise. The gap is
    wide enough that this needs no calibration for a particular meter or panel.
    """
    if reading.Y < PLACEMENT_MIN_NITS:
        return False
    return reading.y > reading.x and reading.y >= PLACEMENT_MIN_GREEN_Y


#: Points on the greyscale ramp. More than Calman's usual 21 because this is HDR: the
#: interesting part of the range is the bottom two stops, where a 5% step in signal is
#: an enormous step in luminance, and a ramp coarse enough to miss it would miss the
#: only part most displays get wrong.
GREYSCALE_STEPS = 33

#: The window peak is measured on. Smaller than everything else on purpose.
#:
#: An emissive panel's peak is a small-highlight figure: the limiter responds to total
#: output, so the largest number the display reaches is the one it only has to hold over
#: a few percent of the screen. That is what the EDID reports and what a manufacturer
#: means by "1000 nits". Measuring it on the 10% window the rest of the run uses answers
#: a different question -- a PG32UCDM gives 456 there against a declared 1015, and both
#: are correct.
#:
#: A smaller window is not a more accurate measurement, it is a different operating
#: point: the instrument's accuracy does not change, the panel's limiter does. Which
#: figure belongs in the profile is a judgement, because the peak reaches Windows as
#: tone-mapping metadata. Declare the 10% number and a 900-nit highlight is mapped down
#: to 456 even where the panel could have shown it; declare the small-window number and
#: small highlights land where they were graded while large bright areas are left to the
#: panel's own limiter. The second is how HDR peak is normally specified.
#:
#: 3% because it was measured, not because it is conventional. Asking a PG32UCDM for full
#: drive at every size, with the meter proved to be on the smallest patch first, gives
#: 1014, 1014 and 1019 nits at 1%, 2% and 3% -- within 0.6% of each other, because the
#: limiter does not engage at all down there -- then 773 at 5%, 464 at 10%, and 243 full
#: screen. So 3% is the largest window that still reaches peak *and* the highest reading
#: of the three, which makes it the one to use: the most light for the instrument, and no
#: limiting. Going smaller costs signal and buys nothing.
#:
#: It also settles the declared figure: 1019 against a declared 1015 is 100.4%, so the
#: EDID number is a 3%-or-smaller rating and the panel really does reach it.
#:
#: An earlier pass of the same sweep read 969/974/978 and was written up as the meter
#: giving up 4% on quantum-dot primaries. It was placement: a 1% patch is a third the
#: width of a 3% one, and an instrument centred well enough for the larger reads part
#: black on the smaller. Prove the meter is on the smallest patch before comparing sizes,
#: or the small windows read low and the limiter gets the blame.
#:
#: ``window_peak_nits`` records the 10% figure alongside so the gap is visible rather
#: than inferred.
PEAK_WINDOW_FRACTION = 0.03

#: How far a ramp may go backwards before no curve can correct it. A 1-D LUT inverts
#: the display's transfer function, and a function that is not monotonic has no inverse:
#: there is no single drive that produces a level the display reaches twice. Flattening
#: the reversal and carrying on -- which is what an inverse built from a running maximum
#: quietly does -- produces a curve through exactly the range the reversal ruined.
#:
#: Measured on a PG32UCDM in one of its HDR presets: asked for 47.5 nits it emitted
#: 106.6, and asked for 58.5 it emitted 61.9. Switching the monitor to DisplayHDR True
#: Black 400 removed it entirely. So this is worth detecting and naming rather than
#: working around -- it is a setting on the display, and no profile can substitute for
#: changing it.
MAX_RAMP_REVERSAL = 0.05

#: Reversals below this level are ignored. The ramp floor is half a nit, where the
#: instrument's own noise is a large fraction of the reading, and a step backwards there
#: says nothing about the display.
REVERSAL_FLOOR_NITS = 1.0

#: The lowest level worth putting on the ramp. Below this the instrument is reading its
#: own noise floor on an emissive panel, and a point that is mostly noise does not
#: become useful for being included.
GREYSCALE_FLOOR_NITS = 0.5

#: Saturation levels swept for each hue, as Calman does. 100% alone says where a primary
#: lands and nothing about the path taken to get there, which is where a display's
#: colour management actually goes wrong.
SATURATIONS = (0.20, 0.40, 0.60, 0.80, 1.00)

#: The six hues at the corners of the RGB cube. Each entry is which channels stay at
#: full drive; the others fall away as saturation rises.
HUES = (
    ("red", (1.0, 0.0, 0.0)),
    ("yellow", (1.0, 1.0, 0.0)),
    ("green", (0.0, 1.0, 0.0)),
    ("cyan", (0.0, 1.0, 1.0)),
    ("blue", (0.0, 0.0, 1.0)),
    ("magenta", (1.0, 0.0, 1.0)),
)


def _saturated(mask: tuple[float, float, float], saturation: float) -> tuple[float, float, float]:
    """Drive for one hue at one saturation.

    Saturation is a path from white to the primary, not a scaling of the primary: at
    0% every channel is at full and the patch is white, at 100% the channels outside
    the hue are off. Scaling instead would change luminance along with colour and make
    the sweep a measurement of two things at once.
    """
    saturation = max(0.0, min(1.0, float(saturation)))
    return tuple(1.0 - saturation * (1.0 - channel) for channel in mask)


def greyscale_levels(peak_nits: float, steps: int = GREYSCALE_STEPS) -> tuple[float, ...]:
    """Ramp levels, spaced evenly in PQ rather than in nits.

    Even spacing in nits would put almost every point in the highlights: half the
    samples above half peak, where the eye can barely tell two levels apart, and three
    or four in the whole of the shadows, where it can. PQ is designed to be
    perceptually uniform, so even steps in it are even steps in what a viewer sees --
    and the same reasoning the pattern viewer already uses for its probe.
    """
    target = max(80.0, min(10000.0, float(peak_nits)))
    steps = max(2, int(steps))
    low, high = pq_inverse_eotf(GREYSCALE_FLOOR_NITS), pq_inverse_eotf(target)
    span = high - low
    return tuple(
        pq_eotf(low + span * (index / (steps - 1))) for index in range(steps)
    )


def plan(peak_nits: float, *, full: bool = True) -> tuple[MeasurementStep, ...]:
    """The patches to measure, in the order they should be shown.

    Black comes first while the panel is still cool: on an emissive display a
    long bright sequence warms it, and the black floor is the reading most
    disturbed by that. The primaries follow white at the same drive, because a
    channel measured at some other level samples a different point on the
    display's response and cannot be combined with the others.

    ``full`` adds what a Calman-style run measures and these six patches cannot: a
    greyscale ramp, which is the only way to see RGB balance and tone response *across*
    the range rather than at one point, and a saturation sweep per hue, which is where
    a display's own colour handling shows itself. The six core patches stay first and
    keep their keys, because everything the profile is built from is derived from those
    and a longer run must not change what a short one would have produced.

    Set ``full=False`` for the original six. It is the same measurement, just blind to
    everything between black and white.
    """
    target = max(80.0, min(10000.0, float(peak_nits)))
    # Never ask for a balance level above half the peak, so a dim display is not
    # measured for balance at a level its own limiter is already fighting.
    balance = min(BALANCE_NITS, target / 2.0)
    core = (
        MeasurementStep("black", "Black level", BLACK, 0.0, settle_seconds=3.0),
        MeasurementStep(
            "white", "Peak white", WHITE, target, settle_seconds=2.0,
            window_fraction=PEAK_WINDOW_FRACTION,
        ),
        # The same drive on the window everything else uses. Not what the profile
        # declares, but measured so the gap between the two is a number someone can look
        # at rather than a discrepancy they have to work out for themselves.
        MeasurementStep("window-white", "Peak white on the measurement window", WHITE, target),
        MeasurementStep("balance-white", "Reference white", WHITE, balance),
        MeasurementStep("red", "Red channel", (1.0, 0.0, 0.0), balance),
        MeasurementStep("green", "Green channel", (0.0, 1.0, 0.0), balance),
        MeasurementStep("blue", "Blue channel", (0.0, 0.0, 1.0), balance),
    )
    if not full:
        return core

    levels = greyscale_levels(target)
    grey = tuple(
        MeasurementStep(
            f"grey-{index:02d}",
            f"Grey {index + 1} of {len(levels)} ({nits:.4g} nits)",
            WHITE,
            nits,
            # A ramp climbs, so each patch is a smaller change than the jump from black
            # to peak the core patches make, and needs less time to settle.
            settle_seconds=1.0,
        )
        for index, nits in enumerate(levels)
    )
    colour = tuple(
        MeasurementStep(
            f"colour-{name}-{int(round(saturation * 100)):03d}",
            f"{name.capitalize()} at {int(round(saturation * 100))}%",
            _saturated(mask, saturation),
            balance,
            settle_seconds=1.0,
        )
        for name, mask in HUES
        for saturation in SATURATIONS
    )
    return core + grey + colour


#: What one read costs beyond its settle time: spotread starts a process, opens the
#: instrument and integrates. Measured at roughly this on an i1 Display Pro; it is used
#: only to tell the user how long a run will take, so being approximate is fine and
#: being absent is not -- a four minute wait nobody was warned about reads as a hang.
SECONDS_PER_READING = 2.5


def estimated_seconds(steps: tuple[MeasurementStep, ...]) -> float:
    """Roughly how long a run of these patches takes, settle plus integration."""
    return sum(step.settle_seconds for step in steps) + len(steps) * SECONDS_PER_READING


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

    return problems


def balance_problems(readings: dict[str, Reading]) -> list[str]:
    """What stops a *white balance* being solved, as distinct from what ruins a run.

    Both checks below are about the three channel patches agreeing with the white
    measured beside them, which is the one thing ``white_balance_gains`` assumes and the
    only thing it needs. Neither says anything about peak, black, or the greyscale ramp:
    those are read directly, or derived from ratios that survive a scale error on the
    primaries, so refusing the whole run over either used to throw away four minutes of
    good measurements to avoid one bad correction.

    Only one thing does, now. Channels that are the same colour cannot be told apart at
    all, and no amount of solving recovers three directions from one.

    Channels that do not *add up* used to refuse a run too, and no longer do.
    :func:`channel_contributions` recovers each channel's magnitude from the white
    measured beside it instead of from the primary patches, so additivity holds by
    construction rather than by assumption. On a PG32UCDM whose channels sum to 2.11x
    their white -- repeatably, on both of the instrument's calibration tables, and at
    every level from 100 nits to 350 -- that recovers R -20.9%, G 0.0%, B -0.4%, which
    is within half a percent of what the same display returned on the rare runs that
    did satisfy the old check. The departure is still worth reporting, because it says
    the display is doing something unusual; it is no longer worth refusing over.
    """
    problems: list[str] = []
    if [key for key in REQUIRED if key not in readings]:
        return problems

    separation = _channel_separation(readings)
    if separation < MIN_CHANNEL_SEPARATION:
        problems.append(
            f"The red, green and blue readings are nearly the same colour (area "
            f"{separation:.4f}). The patch cannot have changed between them, so there "
            "is no white balance to solve."
        )
    if channel_contributions(readings) is None and not problems:
        problems.append(
            "The reference white sits outside the triangle its own primaries make, so "
            "there is no combination of them that produces it. One of the four patches "
            "was misread."
        )
    return problems


@dataclass(frozen=True, slots=True)
class GreyPoint:
    """One point on the greyscale ramp: what was asked for, and what came back.

    ``target_nits`` is copied from the plan rather than recomputed, so there is still
    only one place the number is decided. It has to be here because on its own a
    reading says nothing -- the whole of the correction is the difference between the
    two, and a caller that had to rebuild the plan to find the pair would need the peak
    the plan was built with, which is not the peak that was then measured.

    Zero means the point could not be paired with a plan step, which is how a
    ``derive`` called without one deserialises. The correction refuses those rather
    than treating "asked for nothing" as a measurement.
    """

    index: int
    target_nits: float
    measured_nits: float
    x: float
    y: float

    @property
    def xyz(self) -> tuple[float, float, float]:
        """The reading as XYZ. Chromaticity plus luminance carries the same content."""
        if self.y <= 0.0:
            return (0.0, max(0.0, self.measured_nits), 0.0)
        return (
            (self.x / self.y) * self.measured_nits,
            self.measured_nits,
            ((1.0 - self.x - self.y) / self.y) * self.measured_nits,
        )


@dataclass(frozen=True, slots=True)
class ColourPoint:
    """One patch from a saturation sweep."""

    hue: str
    saturation: float
    measured_nits: float
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Calibration:
    """What a completed measurement run contributes to a profile."""

    peak_nits: float
    black_nits: float
    white_xy: tuple[float, float]
    channel_gains: tuple[float, float, float]
    #: Peak on the window the rest of the run uses, when it was measured. Zero means it
    #: was not. At or below ``peak_nits`` on any display with a brightness limiter, and
    #: far below it on an emissive one.
    window_peak_nits: float = 0.0
    window_fraction: float = WINDOW_AREA_FRACTION
    #: The window ``peak_nits`` came from, which is deliberately not ``window_fraction``.
    peak_window_fraction: float = PEAK_WINDOW_FRACTION
    #: Why no white balance was solved, if none was. Non-empty means ``channel_gains``
    #: is (1, 1, 1) because the readings could not support a correction -- not because
    #: the display measured neutral. The two look identical in the numbers and must not
    #: look identical to the caller.
    balance_refused: tuple[str, ...] = ()
    #: The largest step backwards in the measured ramp, as a fraction of the level
    #: already reached. Anything above ``MAX_RAMP_REVERSAL`` means the display's
    #: transfer function has no inverse and no curve can correct it.
    ramp_reversal: float = 0.0
    #: How far the three channels were from summing to the white beside them. No longer
    #: a reason to refuse -- see ``channel_contributions`` -- but a large value means the
    #: primaries' own luminances were discarded, and a display that does that is worth
    #: telling someone about rather than quietly working around.
    additivity_error: float = 0.0
    #: The greyscale ramp, if one was measured. Empty from a six-patch run.
    greyscale: tuple[GreyPoint, ...] = ()
    #: Measured XYZ of the red, green and blue patches, in that order. The
    #: white-balance solve already needs these; they are carried so a caller can
    #: apportion a neutral reading among the channels without measuring again.
    channel_xyz: tuple[tuple[float, float, float], ...] = ()
    #: How the reference white's luminance divides between red, green and blue, summing
    #: to 1. This is what "neutral" means for this display and this signal path, and it
    #: is the target grey is held to at every other level. Empty if the channels did not
    #: span a colour space, which ``validate`` would already have refused.
    white_weights: tuple[float, ...] = ()
    #: The saturation sweeps, if measured. Empty from a six-patch run.
    colours: tuple[ColourPoint, ...] = ()

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


def _unit_xyz(reading: Reading) -> tuple[float, float, float] | None:
    """A reading's chromaticity as an XYZ vector of luminance exactly 1."""
    if reading.y <= 0.0:
        return None
    return (reading.x / reading.y, 1.0, (1.0 - reading.x - reading.y) / reading.y)


def channel_contributions(
    readings: dict[str, Reading],
) -> tuple[tuple[float, float, float], tuple[float, ...]] | None:
    """What each channel contributes to the reference white, and the matrix that says so.

    Solved from the primaries' *chromaticities* and the white measured beside them,
    rather than read off the primaries' own luminances. The two are the same thing on a
    display whose channels add up: solving ``a R + b G + c B = W`` for unit-luminance
    primaries returns exactly the luminances that were measured, so nothing changes for
    a well-behaved panel.

    They stop being the same thing on a panel that boosts saturated colour. A PG32UCDM
    reads its primaries at 2.30x, 2.26x and 2.04x their share of the white beside them,
    so a matrix built from those luminances describes a display that emits twice the
    light it does, and a white balance solved through it is solved for a fiction. Their
    chromaticities are steady and plausible -- repeatable to 1%, and close to BT.709,
    which is what the patches ask for -- so the direction of each primary survives even
    though its magnitude does not.

    This keeps the directions and recovers the magnitudes from the one patch the boost
    cannot touch: white is not a saturated colour, so there is nothing there to boost.
    Additivity then holds by construction rather than by assumption, which is what makes
    a white balance solvable on a display that cannot satisfy the check at all.

    ``None`` if the primaries do not span a colour space, which is the one case no
    amount of solving fixes.
    """
    units = [_unit_xyz(readings[channel]) for channel in ("red", "green", "blue")]
    if any(unit is None for unit in units):
        return None
    span = tuple(units[column][row] for row in range(3) for column in range(3))  # type: ignore[index]
    inverse = _inverse3(span)
    if inverse is None:
        return None

    contributions = _matvec3(inverse, _xyz(readings["balance-white"]))
    if any(value <= 0.0 for value in contributions):
        # A negative contribution means the white measured outside the triangle its own
        # primaries make. Nothing physical does that; something was misread.
        return None

    # The columns scaled to the luminance each channel actually puts into the white.
    matrix = tuple(
        units[column][row] * contributions[column]  # type: ignore[index]
        for row in range(3)
        for column in range(3)
    )
    return contributions, matrix


def white_balance_gains(readings: dict[str, Reading]) -> tuple[float, float, float]:
    """Per-channel gains that move measured white onto D65.

    Solves ``M g = T``, where the columns of ``M`` are each channel's contribution to
    the reference white and ``T`` is D65 at the same luminance. If the display were
    already neutral the answer would be (1, 1, 1).

    ``M`` comes from :func:`channel_contributions` rather than straight from the primary
    patches, so this works on a display whose channels do not add up -- see there for
    why that matters and why it changes nothing on a display whose channels do.

    The result is scaled so the largest gain is exactly 1.0. Correcting white by
    boosting a channel would ask for output the panel has already run out of, so
    the excess channels come down to meet the weakest instead. That costs
    luminance, which is the honest price of a neutral white.
    """
    solved = channel_contributions(readings)
    if solved is None:
        # Three channels that do not span a colour space; balance_problems() will have
        # said why, and a neutral answer is the only safe one.
        return (1.0, 1.0, 1.0)
    _contributions, matrix = solved
    inverse = _inverse3(matrix)
    if inverse is None:
        return (1.0, 1.0, 1.0)

    luminance = max(readings["balance-white"].Y, 1e-6)
    x, y = D65_XY
    target = ((x / y) * luminance, luminance, ((1.0 - x - y) / y) * luminance)

    gains = _matvec3(inverse, target)
    largest = max(gains)
    if largest <= 0.0:
        return (1.0, 1.0, 1.0)
    return tuple(max(0.0, gain / largest) for gain in gains)


def derive(
    readings: dict[str, Reading], steps: tuple[MeasurementStep, ...] = ()
) -> Calibration:
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
    # Solved only if it can be. Everything else here is read directly or derived from
    # ratios, so a display whose channels do not add up still yields a usable peak,
    # black, white point and ramp -- and refusing all of that to avoid one bad
    # correction discards far more than it protects.
    refused = tuple(balance_problems(readings))
    gains = (1.0, 1.0, 1.0) if refused else white_balance_gains(readings)

    reference = readings["balance-white"]
    return Calibration(
        peak_nits=readings["white"].Y,
        window_peak_nits=readings["window-white"].Y if "window-white" in readings else 0.0,
        black_nits=max(0.0, readings["black"].Y),
        white_xy=(reference.x, reference.y),
        channel_gains=gains,
        balance_refused=refused,
        additivity_error=_additivity_error(readings),
        ramp_reversal=_ramp_reversal(_greyscale_points(readings, steps)),
        greyscale=_greyscale_points(readings, steps),
        colours=_colour_points(readings),
        channel_xyz=tuple(_xyz(readings[channel]) for channel in ("red", "green", "blue")),
        white_weights=_channel_weights(readings),
    )


def _channel_matrix(readings: dict[str, Reading]) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """The channel-to-XYZ matrix and its inverse, or ``None`` if it does not invert."""
    columns = [_xyz(readings[channel]) for channel in ("red", "green", "blue")]
    matrix = tuple(columns[column][row] for row in range(3) for column in range(3))
    inverse = _inverse3(matrix)
    return None if inverse is None else (matrix, inverse)


def _apportion(
    inverse: tuple[float, ...],
    primaries: tuple[tuple[float, float, float], ...],
    xyz: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """How a neutral reading's luminance divides between the three channels.

    Ratios only, and deliberately. Solving for absolute channel amounts and adding them
    up would inherit the problem the module docstring describes at the top of the
    range: white asks for roughly three times the power of one channel, so the
    brightness limiter dims it much harder, and the three no longer sum to the white
    measured beside them. The split between them survives that; the sum does not. So
    the solve decides only the proportions and the measured luminance decides the
    total.
    """
    drives = _matvec3(inverse, xyz)
    contributions = [
        max(0.0, drive) * primaries[channel][1] for channel, drive in enumerate(drives)
    ]
    total = sum(contributions)
    if total <= 0.0:
        return None
    return tuple(value / total for value in contributions)  # type: ignore[return-value]


def _channel_weights(readings: dict[str, Reading]) -> tuple[float, ...]:
    """The reference white's luminance split. See ``Calibration.white_weights``.

    Solved the same way the gains are, and for the same reason: taking each channel's
    magnitude from its own patch inherits whatever the display did to that patch, and on
    a panel that boosts saturated colour the three do not add up to the white they are
    supposed to divide. Splitting the white itself cannot disagree with the white.

    Using one method for the gains and another for the weights would also mean the two
    halves of the correction were solved against different displays -- the matrix
    balancing towards one white and the curves holding grey to another.
    """
    solved = channel_contributions(readings)
    if solved is None:
        return ()
    contributions, _matrix = solved
    total = sum(contributions)
    if total <= 0.0:
        return ()
    return tuple(value / total for value in contributions)


def _ramp_reversal(points: tuple[GreyPoint, ...]) -> float:
    """The largest step backwards in a measured ramp, as a fraction of the peak so far.

    Zero for a ramp that only ever climbs, which is every display that can be corrected.
    """
    worst = 0.0
    highest = 0.0
    for point in sorted(points, key=lambda p: p.target_nits):
        if point.target_nits <= 0.0 or point.measured_nits < REVERSAL_FLOOR_NITS:
            continue
        if highest > 0.0 and point.measured_nits < highest:
            worst = max(worst, (highest - point.measured_nits) / highest)
        highest = max(highest, point.measured_nits)
    return worst


#: Sustained luminance is what the display holds with the whole screen lit, so it is
#: measured on the whole screen. Nothing smaller answers the question.
SUSTAINED_WINDOW_FRACTION = 1.0

#: Before the first reading, and between the ones after it.
SUSTAINED_SETTLE_SECONDS = 3.0
SUSTAINED_INTERVAL_SECONDS = 4.0

#: Two consecutive readings within this of each other means the limiter has stopped
#: working and the number has settled. A fixed timer cannot know that: it is either long
#: enough for the worst panel or too short for some, and a peak read before it settles is
#: not a peak, it is a number on the way down.
SUSTAINED_TOLERANCE = 0.02

#: A hard ceiling on how long full-screen white stays up, whatever the readings do. This
#: is the one patch in the whole app that lights every pixel at once, which is the exact
#: stress case for an emissive panel, so "keep reading until it settles" needs an end
#: even when it never does. Eight reads is about fifty seconds.
SUSTAINED_MAX_READS = 8


@dataclass(frozen=True, slots=True)
class Sustained:
    """What the display holds with the whole screen lit."""

    nits: float
    #: Every reading taken, in order, so a panel that never settled can be seen to have
    #: been falling rather than merely reported as a number.
    readings: tuple[float, ...]
    #: Whether two consecutive readings agreed before the cap was reached.
    settled: bool

    @property
    def fell_by(self) -> float:
        """How far it dropped from the first reading to the last, as a fraction."""
        if not self.readings or self.readings[0] <= 0.0:
            return 0.0
        return max(0.0, (self.readings[0] - self.readings[-1]) / self.readings[0])


def sustained(
    display: Display,
    read: Callable[[], Reading],
    *,
    peak_nits: float,
    sleep: Callable[[float], None] = time.sleep,
    should_abort: Callable[[], bool] | None = None,
    on_reading: Callable[[int, float], None] | None = None,
) -> Sustained:
    """Hold full-screen white and read until it stops falling.

    Asks for ``peak_nits`` and lets the display's limiter decide, because the answer is
    what the panel does rather than what it was told. Stops when two consecutive
    readings agree, which is what "sustained" means, or at ``SUSTAINED_MAX_READS``.

    Separate from ``run`` on purpose. It is slow, it does not change between runs the way
    the greyscale does, and it is the only thing here that lights the whole panel at
    once -- none of which belongs in a sweep somebody repeats to check their work.
    """
    target = max(80.0, min(10000.0, float(peak_nits)))
    step = MeasurementStep(
        "sustained", "Sustained luminance", WHITE, target,
        settle_seconds=SUSTAINED_SETTLE_SECONDS,
        window_fraction=SUSTAINED_WINDOW_FRACTION,
    )

    values: list[float] = []
    for index in range(SUSTAINED_MAX_READS):
        if should_abort is not None and should_abort():
            raise Aborted()
        display.show(step)
        sleep(step.settle_seconds if index == 0 else SUSTAINED_INTERVAL_SECONDS)
        if should_abort is not None and should_abort():
            raise Aborted()
        try:
            reading = read()
        except MeterError as exc:
            raise MeasurementError(f"{step.label}: {exc}") from exc
        values.append(max(0.0, reading.Y))
        if on_reading is not None:
            on_reading(index, values[-1])
        if len(values) >= 2:
            latest, previous = values[-1], values[-2]
            if previous > 0.0 and abs(latest - previous) / previous <= SUSTAINED_TOLERANCE:
                return Sustained(latest, tuple(values), True)

    if not values:
        raise MeasurementError("The sustained measurement took no readings.")
    return Sustained(values[-1], tuple(values), False)


def panel_response(
    calibration: Calibration, sent: Callable[[float], tuple[float, float, float]]
) -> PanelResponse | None:
    """Pair each channel's delivered luminance with the code it was actually sent.

    ``sent`` maps a requested level to the three codes the pipeline put on the wire for
    it -- which is the LUT in force at measurement time, not the identity, and is why
    this describes the panel rather than the correction currently sitting on top of it.
    A later pass therefore replaces the result instead of composing with it.

    ``None`` when the run cannot support a correction: a short ramp, a plan that was
    never paired with the readings, or channels that do not span a colour space.

    **How much of this survives a display whose channels do not add up.** The luminance
    half is untouched: every ramp point is a neutral patch and its total ``Y`` is read
    directly, so EOTF tracking is as good as the instrument. The balance half is only as
    good as the channel matrix, which comes from the three primary patches -- and on a
    panel that boosts saturated colour those are wrong by different amounts (measured
    2.30, 2.26 and 2.04 on a PG32UCDM). The reference white and the ramp are apportioned
    through the same matrix, so the *drift* between them still means something, but the
    weights it is measured against can be off by the spread between those factors.

    That is why this is computed even when ``balance_problems`` refuses the white
    balance. A skewed grey-tracking target is worth having; a white balance solved from
    channels that sum to twice their white is not.
    """
    if len(calibration.channel_xyz) != 3 or len(calibration.white_weights) != 3:
        return None
    if calibration.ramp_reversal > MAX_RAMP_REVERSAL:
        # No inverse exists. Building one anyway is worse than building none: the
        # flattening is invisible, and the curve it produces is wrong precisely where
        # the ramp puts most of its points.
        return None
    matrix = tuple(
        calibration.channel_xyz[column][row] for row in range(3) for column in range(3)
    )
    inverse = _inverse3(matrix)
    if inverse is None:
        return None

    red: list[tuple[float, float]] = []
    green: list[tuple[float, float]] = []
    blue: list[tuple[float, float]] = []
    for point in calibration.greyscale:
        # target_nits is 0 when the readings were never paired with a plan. A point
        # whose request is unknown says nothing about the panel's transfer function.
        if point.target_nits <= 0.0 or point.measured_nits <= 0.0:
            continue
        shares = _apportion(inverse, calibration.channel_xyz, point.xyz)
        if shares is None:
            continue
        codes = sent(point.target_nits)
        if len(codes) != 3:
            continue
        for channel, bucket in enumerate((red, green, blue)):
            bucket.append((codes[channel], shares[channel] * point.measured_nits))

    response = PanelResponse(
        tuple(red), tuple(green), tuple(blue), tuple(calibration.white_weights)
    )
    return response if response.usable else None


def _greyscale_points(
    readings: dict[str, Reading], steps: tuple[MeasurementStep, ...] = ()
) -> tuple[GreyPoint, ...]:
    """The ramp, in the order it was measured.

    Absent from a six-patch run, and that is not a failure -- everything the profile is
    built from comes from the core patches, and the ramp is what makes the *report*
    worth reading. Anything here that is missing is simply not shown.
    """
    targets = {step.key: step.nits for step in steps}
    points = []
    for key in sorted(k for k in readings if k.startswith("grey-")):
        reading = readings[key]
        points.append(
            GreyPoint(
                index=int(key.split("-", 1)[1]),
                target_nits=targets.get(key, 0.0),
                measured_nits=reading.Y,
                x=reading.x,
                y=reading.y,
            )
        )
    return tuple(points)


def _colour_points(readings: dict[str, Reading]) -> tuple[ColourPoint, ...]:
    """The saturation sweeps, grouped by nothing -- the caller decides how to read them."""
    points = []
    for key in sorted(k for k in readings if k.startswith("colour-")):
        _prefix, hue, percent = key.split("-", 2)
        reading = readings[key]
        points.append(
            ColourPoint(
                hue=hue,
                saturation=int(percent) / 100.0,
                measured_nits=reading.Y,
                x=reading.x,
                y=reading.y,
            )
        )
    return tuple(points)


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
    full: bool = True,
) -> Calibration:
    """Show each patch, read it, and reduce the set to profile values.

    The display and the instrument are both injected, so the sequence -- which
    patch, in what order, how long to settle, what to do when one fails -- can be
    tested without either.

    A failed reading ends the run rather than being skipped. Channels measured
    without their matching white cannot be combined, and a peak carried over from
    a previous attempt is not a measurement of anything.
    """
    steps = plan(peak_nits, full=full)
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

    return derive(readings, steps)
