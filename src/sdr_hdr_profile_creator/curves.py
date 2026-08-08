from __future__ import annotations

import math
from dataclasses import dataclass

from .model import ModeState
from .gamma_correction import resolve_white_level, transform_piecewise_srgb_to_gamma22


@dataclass(slots=True)
class CalibrationTransform:
    matrix: tuple[float, ...]
    red: list[float]
    green: list[float]
    blue: list[float]


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def srgb_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> float:
    value = max(0.0, value)
    if value <= 0.0031308:
        return 12.92 * value
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def pq_eotf(code: float) -> float:
    """ST.2084 code value to absolute luminance in nits."""
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    p = max(code, 0.0) ** (1.0 / m2)
    numerator = max(p - c1, 0.0)
    denominator = c2 - c3 * p
    if denominator <= 0.0:
        return 10000.0
    return 10000.0 * (numerator / denominator) ** (1.0 / m1)


def pq_oetf(nits: float) -> float:
    """Absolute luminance in nits to ST.2084 code value."""
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    y = clamp(nits / 10000.0)
    p = y**m1
    return ((c1 + c2 * p) / (1.0 + c3 * p)) ** m2


# Rec.2020 / D65 matrix, appropriate for Windows HDR's BT.2020-oriented pipeline.
_REC2020_TO_XYZ = (
    0.6369580483, 0.1446169036, 0.1688809752,
    0.2627002120, 0.6779980715, 0.0593017165,
    0.0000000000, 0.0280726930, 1.0609850577,
)
_REC2020_LUMA = (0.2627, 0.6780, 0.0593)
_D65_XYZ = (0.95047, 1.0, 1.08883)

# Bradford chromatic-adaptation transform. Temperature/tint are expressed as a
# white-point adaptation in XYZ rather than crude per-channel LUT multipliers.
_BRADFORD = (
    0.8951, 0.2664, -0.1614,
    -0.7502, 1.7135, 0.0367,
    0.0389, -0.0685, 1.0296,
)


def _matmul3(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        sum(left[row * 3 + inner] * right[inner * 3 + column] for inner in range(3))
        for row in range(3)
        for column in range(3)
    )


def _matvec3(matrix: tuple[float, ...], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(
        sum(matrix[row * 3 + column] * vector[column] for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def _inverse3(matrix: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f, g, h, i = matrix
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) < 1e-12:
        raise ValueError("Singular color matrix")
    return (
        (e * i - f * h) / determinant,
        (c * h - b * i) / determinant,
        (b * f - c * e) / determinant,
        (f * g - d * i) / determinant,
        (a * i - c * g) / determinant,
        (c * d - a * f) / determinant,
        (d * h - e * g) / determinant,
        (b * g - a * h) / determinant,
        (a * e - b * d) / determinant,
    )


def _diag3(a: float, b: float, c: float) -> tuple[float, ...]:
    return (a, 0.0, 0.0, 0.0, b, 0.0, 0.0, 0.0, c)


def _xy_to_xyz(x: float, y: float) -> tuple[float, float, float]:
    y = max(1e-8, y)
    return x / y, 1.0, max(0.0, (1.0 - x - y) / y)


def _xy_to_uvp(x: float, y: float) -> tuple[float, float]:
    denominator = -2.0 * x + 12.0 * y + 3.0
    if abs(denominator) < 1e-12:
        return 0.19783, 0.46832
    return 4.0 * x / denominator, 9.0 * y / denominator


def _uvp_to_xy(u: float, v: float) -> tuple[float, float]:
    denominator = 6.0 * u - 16.0 * v + 12.0
    if abs(denominator) < 1e-12:
        return 0.3127, 0.3290
    return 9.0 * u / denominator, 4.0 * v / denominator


def _cct_to_xy(kelvin: float) -> tuple[float, float]:
    """Approximate Planckian-locus xy for 1667..25000 K."""
    t = max(1667.0, min(25000.0, float(kelvin)))
    if t <= 4000.0:
        x = -0.2661239e9 / t**3 - 0.2343580e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390

    if t <= 2222.0:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000.0:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def _white_balance_matrix(state: ModeState) -> tuple[float, ...]:
    """Return a smooth XYZ->XYZ white-point adaptation.

    Temperature is a small offset around D65. Tint moves perpendicular to the
    local Planckian locus in CIE u'v', so it behaves like a true green/magenta
    trim instead of adding a generic RGB cast.
    """
    temperature = float(state.temperature)
    tint = float(state.tint)
    if abs(temperature) < 1e-12 and abs(tint) < 1e-12:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    # Positive temperature means visually warmer, therefore a lower CCT target.
    target_kelvin = max(3500.0, min(10000.0, 6504.0 - temperature))
    # D65 is not exactly on the black-body locus. Apply only the *delta* along
    # the local CCT locus to the true D65 chromaticity so crossing zero is smooth
    # and never jumps from D65 to a nearby Planckian white.
    d65_u, d65_v = _xy_to_uvp(0.3127, 0.3290)
    base_planck_u, base_planck_v = _xy_to_uvp(*_cct_to_xy(6504.0))
    target_planck_u, target_planck_v = _xy_to_uvp(*_cct_to_xy(target_kelvin))
    u = d65_u + (target_planck_u - base_planck_u)
    v = d65_v + (target_planck_v - base_planck_v)
    x, y = _uvp_to_xy(u, v)

    if abs(tint) > 1e-12:
        x_lo, y_lo = _cct_to_xy(max(3500.0, target_kelvin - 50.0))
        x_hi, y_hi = _cct_to_xy(min(10000.0, target_kelvin + 50.0))
        u_lo, v_lo = _xy_to_uvp(x_lo, y_lo)
        u_hi, v_hi = _xy_to_uvp(x_hi, y_hi)
        du = u_hi - u_lo
        dv = v_hi - v_lo
        length = math.hypot(du, dv) or 1.0
        # Perpendicular to CCT locus. Orient positive tint toward magenta (lower y).
        pu, pv = -dv / length, du / length
        test_x, test_y = _uvp_to_xy(u + pu * 0.001, v + pv * 0.001)
        if test_y > y:
            pu, pv = -pu, -pv
        # ±5 UI units ~= ±0.0025 u'v': deliberately a fine correction range.
        distance = tint * 0.0005
        x, y = _uvp_to_xy(u + pu * distance, v + pv * distance)

    target_xyz = _xy_to_xyz(x, y)
    bradford_inv = _inverse3(_BRADFORD)
    source_lms = _matvec3(_BRADFORD, _D65_XYZ)
    target_lms = _matvec3(_BRADFORD, target_xyz)
    scale = _diag3(
        target_lms[0] / max(1e-12, source_lms[0]),
        target_lms[1] / max(1e-12, source_lms[1]),
        target_lms[2] / max(1e-12, source_lms[2]),
    )
    return _matmul3(_matmul3(bradford_inv, scale), _BRADFORD)


def _rgb_color_matrix(state: ModeState) -> tuple[float, ...]:
    """Compose RGB fine trims and saturation while preserving neutral luminance."""
    gains = (
        1.0 + float(state.red_channel) / 100.0,
        1.0 + float(state.green_channel) / 100.0,
        1.0 + float(state.blue_channel) / 100.0,
    )
    gain_matrix = _diag3(*gains)

    saturation = max(0.50, min(1.50, 1.0 + float(state.saturation) / 100.0))
    lr, lg, lb = _REC2020_LUMA
    one_minus = 1.0 - saturation
    saturation_matrix = (
        lr * one_minus + saturation, lg * one_minus, lb * one_minus,
        lr * one_minus, lg * one_minus + saturation, lb * one_minus,
        lr * one_minus, lg * one_minus, lb * one_minus + saturation,
    )

    rgb_adjust = _matmul3(saturation_matrix, gain_matrix)

    # A white-balance channel trim should change chromaticity, not secretly act as
    # a brightness control. Normalize the Y contribution of neutral RGB white.
    white_rgb = _matvec3(rgb_adjust, (1.0, 1.0, 1.0))
    white_y = (
        _REC2020_LUMA[0] * white_rgb[0]
        + _REC2020_LUMA[1] * white_rgb[1]
        + _REC2020_LUMA[2] * white_rgb[2]
    )
    if white_y > 1e-9:
        rgb_adjust = tuple(value / white_y for value in rgb_adjust)

    xyz_adjust = _matmul3(_matmul3(_REC2020_TO_XYZ, rgb_adjust), _inverse3(_REC2020_TO_XYZ))
    return xyz_adjust


def _safe_color_matrix(state: ModeState, hdr: bool) -> tuple[float, ...]:
    identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    if not hdr:
        return identity
    if all(abs(float(value)) < 1e-12 for value in (
        state.temperature, state.tint, state.red_channel, state.green_channel,
        state.blue_channel, state.saturation,
    )):
        return identity

    # First make channel/saturation corrections in the HDR RGB working space,
    # then adapt the neutral axis. Both stages are XYZ->XYZ when serialized to MHC2.
    rgb_adjust = _rgb_color_matrix(state)
    white_balance = _white_balance_matrix(state)
    xyz_adjust = _matmul3(white_balance, rgb_adjust)
    return (
        xyz_adjust[0], xyz_adjust[1], xyz_adjust[2], 0.0,
        xyz_adjust[3], xyz_adjust[4], xyz_adjust[5], 0.0,
        xyz_adjust[6], xyz_adjust[7], xyz_adjust[8], 0.0,
    )


def _contrast_curve(value: float, amount_percent: float) -> float:
    """Symmetric, endpoint-preserving contrast around 50%."""
    x = clamp(value)
    exponent = max(0.50, min(1.50, 1.0 + amount_percent / 100.0))
    if x <= 0.5:
        return 0.5 * (2.0 * x) ** exponent
    return 1.0 - 0.5 * (2.0 * (1.0 - x)) ** exponent


def _shape_curve(value: float, state: ModeState, hdr: bool, sdr_white_nits: float | None = None) -> float:
    x = clamp(value)
    if not hdr:
        # SDR is comparison-only; Virtual HDR OSD never modifies its profile path.
        return x

    # Optional SDR-in-HDR correction follows dylanraga's documented direction.
    white_level = resolve_white_level(state.sdr_gamma_correction, sdr_white_nits)
    if white_level is not None:
        x = transform_piecewise_srgb_to_gamma22(x, white_level)

    # Traditional gamma remains an independent control. 2.20 is mathematically neutral.
    gamma_ratio = max(0.65, min(1.45, float(state.gamma) / 2.2))
    y = x**gamma_ratio

    # Contrast uses a smooth symmetric S-curve with fixed 0, 0.5 and 1 anchors.
    y = _contrast_curve(y, float(state.contrast))

    # Brightness is a restrained midtone lift/cut that preserves black and white.
    # The wider UI range remains endpoint-preserving; fine steps make small trims easy.
    brightness = max(-0.35, min(0.35, float(state.brightness_trim) / 100.0))
    y = y + brightness * y * (1.0 - y)
    return clamp(y)


def build_transform(state: ModeState, hdr: bool, sdr_white_nits: float | None = None) -> CalibrationTransform:
    entries = max(256, min(4096, int(state.lut_entries)))
    curve: list[float] = []
    for index in range(entries):
        x = index / (entries - 1)
        curve.append(_shape_curve(x, state, hdr, sdr_white_nits))

    curve[0] = 0.0
    curve[-1] = 1.0
    previous = 0.0
    for index, value in enumerate(curve):
        previous = max(previous, clamp(value))
        curve[index] = previous
    curve[-1] = 1.0

    # Chromatic corrections live in the MHC2 XYZ matrix. Keeping one common LUT
    # for R/G/B prevents channel-specific clipping and makes blends much smoother.
    return CalibrationTransform(
        matrix=_safe_color_matrix(state, hdr),
        red=list(curve),
        green=list(curve),
        blue=list(curve),
    )


def estimate_curve_gamma(curve: list[float]) -> float:
    if len(curve) < 3:
        return 1.0
    index = max(1, min(len(curve) - 2, round((len(curve) - 1) * 0.5)))
    x = index / (len(curve) - 1)
    y = clamp(curve[index], 1e-6, 1.0)
    try:
        return clamp(math.log(x) / math.log(y), 0.5, 3.0)
    except (ValueError, ZeroDivisionError):
        return 1.0
