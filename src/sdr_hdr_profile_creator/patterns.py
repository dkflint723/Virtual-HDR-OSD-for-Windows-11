"""Calibration test patterns, rendered as scRGB frames for :mod:`hdr_display`.

Every pattern is specified in **absolute nits** and encoded at render time, because what
a value means depends on the display:

* On an HDR output, scRGB is scene-referred and 1.0 is 80 nits, so a luminance maps
  directly and patterns can address the full ST.2084 range.
* On an SDR output, scRGB is display-referred and 1.0 is that display's reference white.
  Absolute luminance is simply not addressable, so the same pattern is rendered relative
  to reference white and anything above it clips. :attr:`PatternContext.absolute` says
  which of the two happened, so the UI can label a reading as real nits or as a ratio
  rather than quietly implying precision it does not have.

scRGB is linear light, which matters for more than encoding: the gamma-match pattern
below relies on two interleaved lines averaging optically to their arithmetic mean, and
that is only true in a linear signal.

Rendering happens straight into a swapchain buffer sized in *device* pixels. That is not
an incidental detail -- a display at 125% scaling would resample a one-pixel line drawn
through Qt, and a resampled gamma-match pattern reports a gamma that is simply wrong.
"""

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

    @property
    def absolute(self) -> bool:
        """Whether values shown are real luminance rather than a ratio to white."""
        return self.is_hdr

    @property
    def ceiling_nits(self) -> float:
        """The brightest luminance worth asking for on this display.

        Above the panel's peak everything is rolled off or clipped, so a pattern that
        keeps climbing past it invites chasing a difference that cannot exist.
        """
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
    """Split ``width`` into ``count`` spans summing to exactly ``width``.

    Giving the remainder to the last cell looks fine until the surface is narrower than
    the cell count, at which point that cell gets a negative width, silently renders as
    nothing, and the frame comes out the wrong size. Cells are dropped instead, and the
    remainder is spread over the leading cells so no single one is visibly wider.
    """
    count = max(1, min(int(count), max(1, int(width))))
    base, remainder = divmod(int(width), count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


@dataclass(frozen=True)
class Pattern:
    """One test pattern, plus the guidance needed to act on what it shows."""

    key: str
    title: str
    purpose: str
    instructions: str
    render: Callable[[int, int, PatternContext], bytes]


# ---------------------------------------------------------------------------------
# Individual patterns. Each returns width*height*8 bytes of scRGB half floats.


def _render_gamma_match(width: int, height: int, context: PatternContext) -> bytes:
    """Interleaved lines beside solid patches: the honest way to read actual gamma.

    Alternating full and black device-pixel lines average optically to half the line
    luminance, and because scRGB is linear that average is exactly arithmetic. Whichever
    solid patch blends into the surrounding lines identifies the luminance the display is
    really producing for that signal, which is a measurement of the transfer function
    rather than an impression of it.

    The lines must land on real device pixels. Any resampling averages them together and
    the pattern then reports a gamma that was never on screen.
    """
    line_nits = min(context.ceiling_nits, REFERENCE_WHITE_NITS)
    lit = _row(width, context.encode(line_nits))
    dark = _row(width, 0.0)

    # Candidate patches bracket the true half-luminance so a mismatch reads as a
    # direction, not just a failure to match.
    ideal = line_nits / 2.0
    candidates = [ideal * factor for factor in (0.70, 0.85, 1.00, 1.18, 1.40)]
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
    """Even perceptual steps from black to peak: crush, clipping and banding at a glance.

    Steps are spaced evenly in PQ rather than in luminance, because PQ is roughly
    perceptually uniform; evenly spaced *nits* would waste almost every step on
    highlights and show nothing where the eye is sensitive.
    """
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
    """Shadow patches on black, to find where detail stops being distinguishable.

    The SDR-in-HDR correction takes 0.5 nits to roughly 0.1, so this is the range it
    changes most and the range where a wrong black level is most visible.
    """
    levels = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
    levels = [nits for nits in levels if nits <= context.ceiling_nits] or [context.ceiling_nits]
    spans = _spans(width, len(levels))
    margin = height // 4

    patch_row = b"".join(
        _pixel(context.encode(level)) * span for level, span in zip(levels, spans)
    )
    black = _row(width, 0.0)
    return b"".join(black if y < margin or y >= height - margin else patch_row for y in range(height))


def _render_neutral_ramp(width: int, height: int, context: PatternContext) -> bytes:
    """A smooth grey sweep. Absolute white balance cannot be judged by eye, but a tint
    that *drifts* across the range -- green shadows into magenta highlights -- is obvious,
    and that is exactly what the per-channel trims exist to correct."""
    top = pq_inverse_eotf(context.ceiling_nits)
    row = b"".join(
        _pixel(context.encode(pq_eotf(top * (x / max(1, width - 1)))))
        for x in range(width)
    )
    return row * height


def _render_peak_window(width: int, height: int, context: PatternContext) -> bytes:
    """A small bright window on black, which is the only honest way to show peak.

    Filling the screen with white does not measure peak luminance on any panel worth
    calibrating. An OLED's automatic brightness limiter drops full-field output to a
    fraction of what a small window sustains, and the reading also drifts for seconds
    after the patch appears. A tenth of the screen area is the conventional compromise:
    small enough that the limiter barely engages, large enough to fill a meter's aperture
    and to judge by eye.

    The surround is true black rather than dark grey, because on an emissive panel a lit
    surround is itself part of what triggers the limiter.
    """
    fraction = 0.10
    side = fraction ** 0.5
    window_width = max(1, min(width, round(width * side)))
    window_height = max(1, min(height, round(height * side)))
    left = (width - window_width) // 2
    top = (height - window_height) // 2

    black = _row(width, 0.0)
    lit = (
        black[: left * 8]
        + _pixel(context.encode(context.ceiling_nits)) * window_width
        + black[(left + window_width) * 8:]
    )
    return b"".join(
        lit if top <= y < top + window_height else black for y in range(height)
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
    """Saturation and hue sanity, held at diffuse white rather than at peak.

    Judging colour at peak luminance mostly measures the panel's highlight rolloff; at
    reference white it measures the colour itself.
    """
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
        instructions=(
            "Step back until the fine lines blend into a solid tone. The patch that "
            "disappears into its surroundings is the one at true half luminance. If a "
            "brighter patch matches, the display is dark in the midtones; lower Gamma. "
            "If a darker patch matches, raise it."
        ),
        render=_render_gamma_match,
    ),
    Pattern(
        key="grey-staircase",
        title="Grey staircase",
        purpose="Shows crush, clipping and banding across the whole range at once.",
        instructions=(
            "Every step should be distinguishable from its neighbours. Steps merging at "
            "the dark end is crush; merging at the bright end is clipping."
        ),
        render=_render_grey_staircase,
    ),
    Pattern(
        key="near-black",
        title="Near black",
        purpose="Finds where shadow detail stops being visible.",
        instructions=(
            "In a dark room, note the darkest patch you can still separate from the "
            "background. Everything below it is being crushed."
        ),
        render=_render_near_black,
    ),
    Pattern(
        key="neutral-ramp",
        title="Neutral ramp",
        purpose="Exposes tint that drifts across the luminance range.",
        instructions=(
            "Look for colour creeping in at one end and not the other. A uniform cast is "
            "hard to judge without a reference; a drifting one is not, and it is what the "
            "per-channel Fine Balance trims correct."
        ),
        render=_render_neutral_ramp,
    ),
    Pattern(
        key="peak-window",
        title="Peak window",
        purpose="Shows peak luminance without the panel dimming itself to produce it.",
        instructions=(
            "A tenth of the screen, on black. Give it several seconds to settle before "
            "judging: emissive panels drift after a bright patch appears. If a filled "
            "white screen looks dimmer than this window, that is automatic brightness "
            "limiting, not a fault, and it is why peak is never measured full-field."
        ),
        render=_render_peak_window,
    ),
    Pattern(
        key="colour-patches",
        title="Colour patches",
        purpose="Hue and saturation sanity, including memory colours.",
        instructions=(
            "Skin, sky and foliage are the colours the eye judges hardest. Check these "
            "after the greyscale is right, never before -- white balance moves them all."
        ),
        render=_render_colour_patches,
    ),
)


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
