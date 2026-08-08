from __future__ import annotations

import math
from typing import Final

CORRECTION_OPTIONS: Final[tuple[str, ...]] = (
    "Off",
    "Auto (Recommended)",
    "100 nits / Brightness 5",
    "200 nits / Brightness 30",
    "300 nits / Brightness 55",
    "400 nits / Brightness 80",
    "Unspecified",
    "SDR",
)

# Manual presets published by dylanraga. Auto is an app extension that reads the
# active Windows SDR reference white instead of exposing a duplicate brightness slider.
_PRESET_WHITE_NITS: Final[dict[str, float]] = {
    "100 nits / Brightness 5": 100.0,
    "200 nits / Brightness 30": 200.0,
    "300 nits / Brightness 55": 300.0,
    "400 nits / Brightness 80": 400.0,
    # Compatibility entries from the upstream download list. The SDR profile is
    # based on the traditional 80-nit SDR reference. "Unspecified" uses the web
    # generator's 200-nit default when Windows readback is unavailable.
    "SDR": 80.0,
    "Unspecified": 200.0,
}

M1 = 0.1593017578125
M2 = 78.84375
C1 = 0.8359375
C2 = 18.8515625
C3 = 18.6875
SRGB_LINEAR_CUTOFF = 0.00313066844250063


def pq_eotf(value: float) -> float:
    """ST 2084 code value -> absolute luminance in nits."""
    v = max(0.0, min(1.0, float(value)))
    p = v ** (1.0 / M2)
    return 10000.0 * (max(p - C1, 0.0) / (C2 - C3 * p)) ** (1.0 / M1)


def pq_inverse_eotf(luminance_nits: float) -> float:
    """Absolute luminance in nits -> ST 2084 code value."""
    l = max(0.0, float(luminance_nits)) / 10000.0
    return ((C1 + C2 * l**M1) / (1.0 + C3 * l**M1)) ** M2


def srgb_inverse_eotf(linear: float) -> float:
    """Linear light -> piecewise sRGB signal value (upstream srgbInvEotf)."""
    x = max(0.0, float(linear))
    return x * 12.92 if x <= SRGB_LINEAR_CUTOFF else 1.055 * x ** (1.0 / 2.4) - 0.055


def resolve_white_level(option: str, windows_white_nits: float | None) -> float | None:
    if option == "Off":
        return None
    if option == "Auto (Recommended)":
        if windows_white_nits is not None and math.isfinite(windows_white_nits):
            return max(80.0, min(480.0, float(windows_white_nits)))
        return 200.0
    return _PRESET_WHITE_NITS.get(option, 200.0)


def transform_piecewise_srgb_to_gamma22(pq_input: float, white_level_nits: float) -> float:
    """Port of dylanraga's current NVIDIA LUT generator direction.

    PQ input -> absolute luminance -> piecewise-sRGB signal relative to SDR white
    -> reinterpret that signal through pure gamma 2.2 -> absolute luminance -> PQ.
    Values above diffuse SDR white are left untouched.
    """
    x = max(0.0, min(1.0, float(pq_input)))
    if x <= 0.0:
        return 0.0
    white = max(1.0, float(white_level_nits))
    luminance = pq_eotf(x)
    if luminance > white:
        return x
    srgb_signal = srgb_inverse_eotf(luminance / white)
    gamma_luminance = white * srgb_signal**2.2
    return max(0.0, min(1.0, pq_inverse_eotf(gamma_luminance)))
