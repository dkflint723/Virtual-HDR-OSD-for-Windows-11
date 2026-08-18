"""Calibration test patterns, rendered as scRGB frames for :mod:`hdr_display`.\n\nEvery pattern is specified in **absolute nits** and encoded at render time, because what\na value means depends on the display:\n\n* On an HDR output, scRGB is scene-referred and 1.0 is 80 nits, so a luminance maps\ndirectly and patterns can address the full ST.2084 range.\n* On an SDR output, scRGB is display-referred and 1.0 is that display's reference white.\nAbsolute luminance is simply not addressable, so the same pattern is rendered relative\nto reference white and anything above it clips. :attr:`PatternContext.absolute` says\nwhich of the two happened, so the UI can label a reading as real nits or as a ratio\nrather than quietly implying precision it does not have.\n\nscRGB is linear light, which matters for more than encoding: the gamma-match pattern\nbelow relies on two interleaved lines averaging optically to their arithmetic mean, and\nthat is only true in a linear signal.\n\nRendering happens straight into a swapchain buffer sized in *device* pixels. That is not\nan incidental detail -- a display at 125% scaling would resample a one-pixel line drawn\nthrough Qt, and a resampled gamma-match pattern reports a gamma that is simply wrong.\n"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Sequence

from .gamma_correction import pq_eotf, pq_inverse_eotf
from .hdr_display import SCRGB_WHITE_NITS

# Diffuse white for HDR reference content. BT.2408 uses 203 nits for graphics white, and
# it is the level most HDR mastering treats as paper white.
REFERENCE_WHITE_NITS = 203.0


@dataclass(frozen=True)
class PatternContext:
    """Everything a pattern needs to know about where it is being shown."""

    is_hdr: bool
    sdr_white_nits: float = 240.0
    peak_nits: float = 1000.0
    max_full_frame_nits: float = 600.0
    # The level a threshold pattern is currently probing at. Windows HDR Calibration
    # works this way and it is the right way round: the eye cannot say what luminance a
    # patch is, but it can say whether a shape is visible, so the pattern moves until the
    # shape disappears and the level where that happens is the reading. It also means a
    # meter can drive exactly the same pattern by stepping this value.
    probe_nits: float = 0.05

    @property
    def absolute(self) -> bool:
        """Whether values shown are real luminance rather than a ratio to white."""
        return self.is_hdr

    @property
    def ceiling_nits(self) -> float:
        """The brightest luminance worth asking for on this display.\n\nAbove the panel's peak everything is rolled off or clipped, so a pattern that\nkeeps climbing past it invites chasing a difference that cannot exist.\n"""
        return self.peak_nits if self.is_hdr else self.sdr_white_nits

    def encode(self, nits: float) -> float:
        """Absolute luminance to an scRGB channel value for this display."""
        nits = max(0.0, float(nits))
        if self.is_hdr:
            return nits / SCRGB_WHITE_NITS
        # Display-referred: 1.0 is this display's white, and nothing exceeds it.
        return min(1.0, nits / max(1.0, self.sdr_white_nits))


def _pixel(value: float) -> bytes:
    return struct.pack("<4e", value, value, value, 1.0)


def _colour_pixel(red: float, green: float, blue: float) -> bytes:
    return struct.pack("<4e", red, green, blue, 1.0)


def _row(width: int, value: float) -> bytes:
    return _pixel(value) * width


def _spans(width: int, count: int) -> list[int]:
    """Split ``width`` into ``count`` spans summing to exactly ``width``.\n\nGiving the remainder to the last cell looks fine until the surface is narrower than\nthe cell count, at which point that cell gets a negative width, silently renders as\nnothing, and the frame comes out the wrong size. Cells are dropped instead, and the\nremainder is spread over the leading cells so no single one is visibly wider.\n"""
    count = max(1, min(int(count), max(1, int(width))))
    base, remainder = divmod(int(width), count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


@dataclass(frozen=True)
class Marker:
    """A label drawn into the pattern itself, positioned as a fraction of the window.\n\nPatterns without these are unusable by eye. A grey ramp shows what the display is\ndoing but says nothing about what it should be doing, and a viewer with no target is\nnot calibrating, they are looking at grey.\n"""

    text: str
    x: float
    y: float
    target: bool = False


@dataclass(frozen=True)
class Pattern:
    """One test pattern, plus everything needed to act on what it shows.\n\n``criterion`` is the single sentence describing what correct looks like. It is\nseparate from ``instructions`` because it is the thing a user checks against, and\nburying it in a paragraph is how a pattern ends up being stared at rather than read.\n\n``level_driven`` patterns are the ones where the *pattern* moves rather than the\ndisplay: the user drives ``PatternContext.probe_nits`` until a shape disappears, and\nthat level is the measurement. Fixed patterns are the ones where the display moves and\nthe pattern holds still.\n"""

    key: str
    title: str
    purpose: str
    criterion: str
    instructions: str
    render: Callable[[int, int, PatternContext], bytes]
    markers: Callable[[PatternContext], tuple[Marker, ...]] = lambda _context: ()
    level_driven: bool = False
    # Overrides the standard window. Only maximum full-frame luminance needs this: it is
    # defined as the whole screen lit, so measuring it in a tenth of one measures nothing.
    window_fraction: float | None = None


# Relative sizes of the candidate patches in the gamma-match pattern. The middle entry is
# the true half-luminance, so it is the answer; the others exist so a miss reads as a
# direction rather than merely a failure.
_NEAR_BLACK_LEVELS: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0)

GAMMA_CANDIDATES: tuple[tuple[float, str], ...] = (
    (0.70, "much darker"),
    (0.85, "darker"),
    (1.00, "TARGET"),
    (1.18, "brighter"),
    (1.40, "much brighter"),
)


def _disc_spans(width: int, height: int, diameter_fraction: float):
    """Per-row (start, span) of a centred disc, or None for rows it does not touch.\n\nA shape rather than a bar, because the question a threshold pattern asks is "can you
    see it", and a recognisable outline is far easier to answer that about than an edge\nwhich might be a gradient.\n"""
    centre_x, centre_y = (width - 1) / 2.0, (height - 1) / 2.0
    radius = min(width, height) * max(0.0, min(1.0, diameter_fraction)) / 2.0
    for y in range(height):
        offset = y - centre_y
        if radius <= 0.0 or abs(offset) > radius:
            yield None
            continue
        half = (radius * radius - offset * offset) ** 0.5
        start = max(0, int(round(centre_x - half)))
        end = min(width, int(round(centre_x + half)) + 1)
        yield (start, end - start) if end > start else None


def _shape_on_field(width: int, height: int, field: float, shape: float) -> bytes:
    background = _row(width, field)
    rows: list[bytes] = []
    for span in _disc_spans(width, height, 0.55):
        if span is None:
            rows.append(background)
            continue
        start, count = span
        rows.append(
            background[: start * 8] + _pixel(shape) * count + background[(start + count) * 8:]
        )
    return b"".join(rows)


# ---------------------------------------------------------------------------------
# Individual patterns. Each returns width*height*8 bytes of scRGB half floats.


def _render_black_level(width: int, height: int, context: PatternContext) -> bytes:
    """A shape barely above black. The level where it vanishes is the black threshold.\n\nThis is the pattern that moves rather than the display. The eye cannot say what\nluminance it is looking at, but it is very good at saying whether a shape is there,\nso lowering the probe until the shape disappears converts a judgement nobody can make\ninto one everybody can.\n"""
    return _shape_on_field(width, height, 0.0, context.encode(context.probe_nits))


def _render_peak_clip(width: int, height: int, context: PatternContext) -> bytes:
    """A shape slightly brighter than a bright surround, to find where the panel clips.\n\nRaising both together, the shape separates from its surround until the display runs\nout of range, at which point the two clip to the same output and the shape disappears.\nThat level is peak luminance, measured rather than taken from metadata that on this\nkind of panel is a specification.\n"""
    field = context.probe_nits
    return _shape_on_field(
        width, height, context.encode(field), context.encode(field * 1.20)
    )


def _render_gamma_match(width: int, height: int, context: PatternContext) -> bytes:
    """Interleaved lines beside solid patches: the honest way to read actual gamma.\n\nAlternating full and black device-pixel lines average optically to half the line\nluminance, and because scRGB is linear that average is exactly arithmetic. Whichever\nsolid patch blends into the surrounding lines identifies the luminance the display is\nreally producing for that signal, which is a measurement of the transfer function\nrather than an impression of it.\n\nThe lines must land on real device pixels. Any resampling averages them together and\nthe pattern then reports a gamma that was never on screen.\n"""
    line_nits = min(context.ceiling_nits, REFERENCE_WHITE_NITS)
    lit = _row(width, context.encode(line_nits))
    dark = _row(width, 0.0)

    # Candidate patches bracket the true half-luminance so a mismatch reads as a
    # direction, not just a failure to match.
    ideal = line_nits / 2.0
    candidates = [ideal * factor for factor, _label in GAMMA_CANDIDATES]
    patch_width = max(1, width // (len(candidates) * 2))
    band = max(1, height // (len(candidates) + 1))

    rows: list[bytes] = []
    for y in range(height):
        index = (y - band // 2) // band
        base = lit if y % 2 == 0 else dark
        if 0 <= index < len(candidates) and (y - band // 2) % band < band * 2 // 3:
            left = (width - patch_width) // 2
            patch = _pixel(context.encode(candidates[index])) * patch_width
            rows.append(base[: left * 8] + patch + base[(left + patch_width) * 8:])
        else:
            rows.append(base)
    return b"".join(rows)


def _render_grey_staircase(width: int, height: int, context: PatternContext) -> bytes:
    """Even perceptual steps from black to peak: crush, clipping and banding at a glance.\n\nSteps are spaced evenly in PQ rather than in luminance, because PQ is roughly\nperceptually uniform; evenly spaced *nits* would waste almost every step on\nhighlights and show nothing where the eye is sensitive.\n"""
    steps = 16
    spans = _spans(width, steps)
    top = pq_inverse_eotf(context.ceiling_nits)
    divisor = max(1, len(spans) - 1)
    row = b"".join(
        _pixel(context.encode(pq_eotf(top * (index / divisor)))) * span
        for index, span in enumerate(spans)
    )
    return row * height


def _render_near_black(width: int, height: int, context: PatternContext) -> bytes:
    """Shadow patches on black, to find where detail stops being distinguishable.\n\nThe SDR-in-HDR correction takes 0.5 nits to roughly 0.1, so this is the range it\nchanges most and the range where a wrong black level is most visible.\n"""
    levels = [nits for nits in _NEAR_BLACK_LEVELS if nits <= context.ceiling_nits]
    levels = levels or [context.ceiling_nits]
    spans = _spans(width, len(levels))
    margin = height // 4

    patch_row = b"".join(
        _pixel(context.encode(level)) * span for level, span in zip(levels, spans)
    )
    black = _row(width, 0.0)
    return b"".join(black if y < margin or y >= height - margin else patch_row for y in range(height))


def _render_neutral_ramp(width: int, height: int, context: PatternContext) -> bytes:
    """A smooth grey sweep. Absolute white balance cannot be judged by eye, but a tint\nthat *drifts* across the range -- green shadows into magenta highlights -- is obvious,\nand that is exactly what the per-channel trims exist to correct."""
    top = pq_inverse_eotf(context.ceiling_nits)
    row = b"".join(
        _pixel(context.encode(pq_eotf(top * (x / max(1, width - 1)))))
        for x in range(width)
    )
    return row * height


def _render_solid_patch(width: int, height: int, context: PatternContext) -> bytes:
    """A single flat level filling the window: the patch a meter reads."""
    return _row(width, context.encode(context.probe_nits)) * height


# ---------------------------------------------------------------------------------
# Markers. Positions are fractions of the window, so they follow it at any size.


def _gamma_markers(_context: PatternContext) -> tuple[Marker, ...]:
    """Name every candidate patch, and say plainly which one is the answer."""
    count = len(GAMMA_CANDIDATES)
    band = 1.0 / (count + 1)
    return tuple(
        Marker(
            text=label,
            x=0.30,
            y=band * 0.5 + index * band + band * 0.33,
            target=(label == "TARGET"),
        )
        for index, (_factor, label) in enumerate(GAMMA_CANDIDATES)
    )


def _near_black_markers(context: PatternContext) -> tuple[Marker, ...]:
    levels = [nits for nits in _NEAR_BLACK_LEVELS if nits <= context.ceiling_nits]
    levels = levels or [context.ceiling_nits]
    step = 1.0 / len(levels)
    return tuple(
        Marker(text=f"{nits:g}", x=step * (index + 0.5), y=0.80)
        for index, nits in enumerate(levels)
    )


def _probe_markers(context: PatternContext) -> tuple[Marker, ...]:
    reading = f"{context.probe_nits:.4g} nits" if context.absolute else f"{context.probe_nits:.4g}"
    return (Marker(text=reading, x=0.5, y=0.92, target=True),)


def _staircase_markers(context: PatternContext) -> tuple[Marker, ...]:
    top = f"{context.ceiling_nits:.0f} nits" if context.absolute else "white"
    return (
        Marker(text="black", x=0.03, y=0.90),
        Marker(text=top, x=0.90, y=0.90),
    )


# Primaries, secondaries, and two memory colours the eye judges harshly.
_COLOUR_PATCHES: Sequence[tuple[str, tuple[float, float, float]]] = (
    ("red", (1.0, 0.0, 0.0)),
    ("green", (0.0, 1.0, 0.0)),
    ("blue", (0.0, 0.0, 1.0)),
    ("cyan", (0.0, 1.0, 1.0)),
    ("magenta", (1.0, 0.0, 1.0)),
    ("yellow", (1.0, 1.0, 0.0)),
    ("skin", (0.87, 0.66, 0.55)),
    ("sky", (0.35, 0.55, 0.85)),
    ("foliage", (0.35, 0.60, 0.30)),
    ("neutral", (1.0, 1.0, 1.0)),
)


def _render_colour_patches(width: int, height: int, context: PatternContext) -> bytes:
    """Saturation and hue sanity, held at diffuse white rather than at peak.\n\nJudging colour at peak luminance mostly measures the panel's highlight rolloff; at\nreference white it measures the colour itself.\n"""
    level = context.encode(min(context.ceiling_nits, REFERENCE_WHITE_NITS))
    column_spans = _spans(width, 5)
    band_count = (len(_COLOUR_PATCHES) + len(column_spans) - 1) // len(column_spans)
    band_spans = _spans(height, band_count)

    bands: list[bytes] = []
    for band_index, band_height in enumerate(band_spans):
        pieces: list[bytes] = []
        for column_index, span in enumerate(column_spans):
            index = band_index * len(column_spans) + column_index
            if index < len(_COLOUR_PATCHES):
                red, green, blue = _COLOUR_PATCHES[index][1]
                pieces.append(_colour_pixel(red * level, green * level, blue * level) * span)
            else:
                pieces.append(_pixel(0.0) * span)
        bands.append(b"".join(pieces) * band_height)
    return b"".join(bands)


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        key="gamma-match",
        title="Gamma match",
        purpose="Reads the transfer function the display is actually producing.",
        criterion="The patch labelled TARGET is the one that vanishes into the lines.",
        instructions=(
            "This one needs distance. At a desk the lines will not blend -- stand back "
            "about two metres, or squint until they turn into flat grey.\n\n"
            "1. Find the single patch that disappears into the background.\n"
            "2. If it sits above TARGET, press Left; if below, press Right.\n"
            "3. Repeat until TARGET is the one that disappears."
        ),
        render=_render_gamma_match,
        markers=_gamma_markers,
    ),
    Pattern(
        key="black-level",
        title="Black level",
        purpose="Measures the darkest luminance this display can still show.",
        criterion="Correct is the lowest level at which the shape is still just visible.",
        instructions=(
            "1. Turn the room lights off and wait a minute for your eyes to adjust.\n"
            "2. Hold Left until the circle disappears completely.\n"
            "3. Tap Right until you can just barely see it again.\n"
            "4. Press Enter."
        ),
        render=_render_black_level,
        markers=_probe_markers,
        level_driven=True,
    ),
    Pattern(
        key="peak-white",
        title="Peak white",
        purpose="Measures peak luminance instead of trusting the panel's own claim.",
        criterion="Correct is the highest level at which the shape still separates.",
        instructions=(
            "There is a circle here slightly brighter than the square around it.\n\n"
            "1. Hold Right until the circle vanishes into the square.\n"
            "2. Tap Left until you can just separate them again.\n"
            "3. Press Enter.\n\n"
            "Go slowly near the top: the panel takes a few seconds to settle."
        ),
        render=_render_peak_clip,
        markers=_probe_markers,
        level_driven=True,
    ),
    Pattern(
        key="full-frame-white",
        title="Full-frame white",
        purpose="Measures how bright the display sustains with the whole screen lit.",
        criterion="Correct is the highest level the whole screen holds without dimming.",
        instructions=(
            "This one floods the whole screen. It will be uncomfortable; that is normal.\n\n"
            "1. Hold Right and watch the brightness.\n"
            "2. Stop when it stops getting brighter, or starts dimming.\n"
            "3. Press Enter.\n\n"
            "Expect a number far below peak on an OLED. That gap is the brightness "
            "limiter doing its job, not a fault."
        ),
        render=_render_solid_patch,
        markers=_probe_markers,
        level_driven=True,
        window_fraction=1.0,
    ),
    Pattern(
        key="grey-staircase",
        title="Grey staircase",
        purpose="Shows crush, clipping and banding across the whole range at once.",
        criterion="Correct is every step distinguishable from both of its neighbours.",
        instructions=(
            "Steps merging at the dark end is crush; merging at the bright end is "
            "clipping. This is a check rather than an adjustment: fix it with Black level "
            "and Peak white, then come back and confirm."
        ),
        render=_render_grey_staircase,
        markers=_staircase_markers,
    ),
    Pattern(
        key="near-black",
        title="Shadow ladder",
        purpose="Shows how far into the shadows detail survives, in real luminance.",
        criterion="Correct is being able to separate every labelled patch from the one beside it.",
        instructions=(
            "Labels are nits. Note the darkest patch you can still separate from black; "
            "everything below it is being crushed. Use this to see what the SDR-in-HDR "
            "correction costs you: it takes 0.5 nits to roughly 0.1."
        ),
        render=_render_near_black,
        markers=_near_black_markers,
    ),
    Pattern(
        key="neutral-ramp",
        title="Neutral ramp",
        purpose="Exposes tint that drifts across the luminance range.",
        criterion="Correct is a sweep with no colour in it at any point.",
        instructions=(
            "Look for colour creeping in at one end and not the other. A uniform cast is "
            "hard to judge without a reference; a drifting one is not, and it is what the "
            "per-channel Fine Balance trims correct."
        ),
        render=_render_neutral_ramp,
    ),
    Pattern(
        key="solid-patch",
        title="Solid patch",
        purpose="One flat level, for a meter to read.",
        criterion="Nothing to judge by eye: this is the patch a meter measures.",
        instructions=(
            "Set the level, place the meter against the centre of the patch, and let it "
            "settle. Emissive panels drift for several seconds after a bright patch "
            "appears, and a reading taken too early is simply wrong."
        ),
        render=_render_solid_patch,
        markers=_probe_markers,
        level_driven=True,
    ),
    Pattern(
        key="colour-patches",
        title="Colour patches",
        purpose="Hue and saturation sanity, including memory colours.",
        criterion="Correct is skin, sky and foliage all looking unremarkable.",
        instructions=(
            "Those three are the colours the eye judges hardest, which is what makes them "
            "useful. Check these after the greyscale is right, never before -- white "
            "balance moves every one of them."
        ),
        render=_render_colour_patches,
    ),
)


# The measured steps, in the order they must be taken. Black first because a wrong black
# level changes what every later judgement looks like, and full-frame last because it is
# the only one that floods the screen and wrecks dark adaptation for anything after it.
MEASUREMENT_SEQUENCE: tuple[str, ...] = ("black-level", "peak-white", "full-frame-white")


def pattern_by_key(key: str) -> Pattern | None:
    for pattern in PATTERNS:
        if pattern.key == key:
            return pattern
    return None


def render(pattern: Pattern, width: int, height: int, context: PatternContext) -> bytes:
    """Render a pattern, guaranteeing the frame is exactly the size the surface wants."""
    width, height = max(1, int(width)), max(1, int(height))
    frame = pattern.render(width, height, context)
    expected = width * height * 8
    if len(frame) != expected:
        raise ValueError(
            f"{pattern.key} produced {len(frame)} bytes, expected {expected} "
            f"for {width}x{height}"
        )
    return frame


# ---------------------------------------------------------------------------------
# Composition: a black field, a centred window, and controls pushed to one edge.

# A tenth of the screen area is the long-standing convention for display patches. It is
# small enough that an emissive panel's brightness limiter barely engages and large enough
# to overfill a meter's aperture at a normal working distance.
WINDOW_AREA_FRACTION = 0.10

# The overlay is held far below diffuse white. It shares the screen with the patch, so its
# light counts towards what the panel is being asked to produce, and a bright strip of text
# beside a near-black patch would both engage the limiter and destroy dark adaptation.
OVERLAY_NITS = 12.0

# Markers sit on the patch itself, so they are dimmer again than the edge strip. A bright
# label beside a near-black shape raises local adaptation and moves the very threshold the
# pattern exists to find.
MARKER_NITS = 4.0


def window_size(width: int, height: int, fraction: float = WINDOW_AREA_FRACTION) -> tuple[int, int]:
    """Dimensions of a centred window covering ``fraction`` of the screen *area*."""
    side = max(0.0, min(1.0, float(fraction))) ** 0.5
    return (
        max(1, min(int(width), round(int(width) * side))),
        max(1, min(int(height), round(int(height) * side))),
    )


def _srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


# One entry per possible byte, so the sRGB decode is a lookup rather than a pow() per
# channel per pixel. Text overlays are the only thing that needs it and they are redrawn on
# every keystroke, so this is the difference between a responsive view and a sluggish one.
_SRGB_LINEAR_LUT: tuple[float, ...] = tuple(_srgb_to_linear(index / 255.0) for index in range(256))


def _blend_over(
    frame: bytearray,
    width: int,
    height: int,
    overlay: tuple[bytes, int, int],
    context: PatternContext,
    nits: float,
    *,
    left: int = 0,
    top: int = 0,
) -> None:
    """Alpha-blend RGBA8 text into a frame in place.\n\nText is overwhelmingly transparent, so rows with no coverage are skipped wholesale and\neach remaining row is walked only between its first and last opaque pixel. Converting\nthe empty space as well was costing 7 million pointless conversions per frame, which is\nhalf a second of lag on every keypress at 4K.\n"""
    pixels, source_width, source_height = overlay
    source_width, source_height = int(source_width), int(source_height)
    if source_width <= 0 or source_height <= 0:
        return

    alpha_plane = pixels[3::4]
    encode = context.encode
    for row in range(source_height):
        y = top + row
        if not 0 <= y < height:
            continue
        row_start = row * source_width
        row_alpha = alpha_plane[row_start:row_start + source_width]
        if row_alpha.count(0) == len(row_alpha):
            continue
        first = len(row_alpha) - len(row_alpha.lstrip(b"\x00"))
        last = len(row_alpha.rstrip(b"\x00"))
        for column in range(first, last):
            alpha = row_alpha[column]
            if not alpha:
                continue
            x = left + column
            if not 0 <= x < width:
                continue
            weight = alpha / 255.0
            source = (row_start + column) * 4
            destination = (y * width + x) * 8
            existing = struct.unpack_from("<4e", frame, destination)
            struct.pack_into(
                "<4e", frame, destination,
                *(
                    existing[channel] * (1.0 - weight)
                    + encode(_SRGB_LINEAR_LUT[pixels[source + channel]] * nits) * weight
                    for channel in range(3)
                ),
                1.0,
            )


def compose(
    width: int,
    height: int,
    pattern: Pattern,
    context: PatternContext,
    *,
    fraction: float = WINDOW_AREA_FRACTION,
    overlay: tuple[bytes, int, int] | None = None,
    overlay_side: str = "right",
    overlay_nits: float = OVERLAY_NITS,
    window_overlay: tuple[bytes, int, int] | None = None,
    marker_nits: float = MARKER_NITS,
) -> bytes:
    """Build a full frame: black everywhere, the pattern in a centred window.\n\nThis is how display patches have always been presented, and on an emissive panel it\nis the only way readings stay comparable. A pattern that fills the screen makes the\nbrightness limiter engage differently for a dark pattern than a bright one, so two\nmeasurements taken minutes apart are not measuring the same thing. Confining every\npattern to the same window area holds that variable still.\n\n``overlay`` is straight RGBA8, as produced by a Qt paint into a QImage, and is pinned\nto the far left or far right rather than floated over the middle: anything near the\npatch contaminates both the reading and the viewer's adaptation.\n"""
    width, height = max(1, int(width)), max(1, int(height))
    block_width, block_height = window_size(
        width, height, pattern.window_fraction if pattern.window_fraction is not None else fraction
    )
    block = render(pattern, block_width, block_height, context)

    left = (width - block_width) // 2
    top = (height - block_height) // 2
    black_row = _row(width, 0.0)

    rows: list[bytes] = []
    for y in range(height):
        if top <= y < top + block_height:
            offset = (y - top) * block_width * 8
            rows.append(
                black_row[: left * 8]
                + block[offset:offset + block_width * 8]
                + black_row[(left + block_width) * 8:]
            )
        else:
            rows.append(black_row)
    frame = bytearray(b"".join(rows))

    # Both overlays go through one routine, blended into the assembled frame. Markers land
    # on the window, guidance against an edge, and each is painted over whatever is beneath
    # so nothing is silently dropped where they meet.
    if window_overlay is not None:
        _blend_over(frame, width, height, window_overlay, context, marker_nits,
                    left=left, top=top)
    if overlay is not None:
        overlay_width = min(int(overlay[1]), width)
        overlay_height = min(int(overlay[2]), height)
        _blend_over(
            frame, width, height, overlay, context, overlay_nits,
            left=(width - overlay_width) if overlay_side == "right" else 0,
            top=max(0, (height - overlay_height) // 2),
        )
    return bytes(frame)
