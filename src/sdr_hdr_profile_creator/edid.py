"""Read a display's HDR capability from its own EDID.

Windows' ``DXGI_OUTPUT_DESC1`` is the obvious source and is wrong about two of the three
figures that matter. Measured against one panel whose EDID declares 1015.24 nits peak,
265.05 nits maximum frame-average and 0.0002 nits minimum, DXGI reported 1010.40, 1010.40
and 0.1956: peak roughly right, frame-average replaced by peak, and a minimum four hundred
times too high.

Maximum frame-average is the one that matters most and is the one DXGI discards. On an
emissive panel it is a small fraction of peak -- 265 against 1015 here -- because the
brightness limiter cannot sustain a full screen at peak. A tool that treats peak as the
full-screen figure asks the display for four times the light it can hold, and a profile
built on that describes a display nobody owns.

The EDID is stored by the Plug and Play enumerator, so this reads the panel's own
declaration rather than anything Windows derived from it. Encoding is CTA-861-G section
7.5.13: luminance codes are logarithmic, and minimum is a fraction of maximum.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None  # type: ignore[assignment]

_ENUM_ROOT = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"

# CTA-861 extension block, and the extended tag that carries HDR static metadata.
_CTA_EXTENSION_TAG = 0x02
_EXTENDED_TAG_BLOCK = 7
_HDR_STATIC_METADATA = 0x06

# EOTF support bits in the first payload byte.
_EOTF_PQ_BIT = 0x04


@dataclass(frozen=True)
class PanelMetadata:
    """What the display declares about itself, in nits."""

    peak_nits: float
    max_frame_average_nits: float
    min_nits: float
    supports_pq: bool

    @property
    def credible(self) -> bool:
        """Whether these figures are worth preferring over anything else.

        A panel that declares no PQ support, or a peak outside anything a display could
        produce, has not really answered the question.
        """
        return self.supports_pq and 40.0 <= self.peak_nits <= 10000.0


def _luminance_from_code(code: int) -> float:
    """CTA-861 desired-luminance code to nits: 50 * 2^(code/32)."""
    return 50.0 * (2.0 ** (code / 32.0))


def parse_hdr_static_metadata(edid: bytes) -> PanelMetadata | None:
    """Pull the HDR static metadata data block out of a raw EDID, if it carries one."""
    if len(edid) < 256:
        return None
    for extension in range(1, len(edid) // 128):
        base = extension * 128
        if edid[base] != _CTA_EXTENSION_TAG:
            continue
        # Byte 2 is the offset of the first detailed timing descriptor, so the data block
        # collection runs from byte 4 up to it.
        end = base + edid[base + 2]
        cursor = base + 4
        while cursor < end and cursor < len(edid):
            tag = edid[cursor] >> 5
            length = edid[cursor] & 0x1F
            payload = cursor + 1
            if (
                tag == _EXTENDED_TAG_BLOCK
                and length >= 3
                and payload < len(edid)
                and edid[payload] == _HDR_STATIC_METADATA
                and payload + length <= len(edid)
            ):
                eotf = edid[payload + 1]
                # Only the maximum is mandatory once the block is present; a panel may
                # stop after it, so each later field is read only if it is really there.
                peak = _luminance_from_code(edid[payload + 3]) if length >= 4 else 0.0
                frame_average = (
                    _luminance_from_code(edid[payload + 4]) if length >= 5 else 0.0
                )
                minimum = (
                    peak * ((edid[payload + 5] / 255.0) ** 2) / 100.0 if length >= 6 else 0.0
                )
                return PanelMetadata(
                    peak_nits=peak,
                    max_frame_average_nits=frame_average,
                    min_nits=minimum,
                    supports_pq=bool(eotf & _EOTF_PQ_BIT),
                )
            cursor += length + 1
    return None


def _registry_key_for(device_path: str) -> tuple[str, str] | None:
    """Split a monitor device path into the enumerator key it was registered under.

    ``\\\\?\\DISPLAY#AUS32F2#5&25649870&7&UID4357#{guid}`` is stored under
    ``DISPLAY\\AUS32F2\\5&25649870&7&UID4357``.
    """
    parts = device_path.split("#")
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def read_panel_metadata(device_path: str) -> PanelMetadata | None:
    """The HDR figures a display declares, or None if it declares none.

    Returns None rather than raising for every failure: a missing EDID, a panel with no
    HDR block, and a registry that cannot be read are all just "no answer", and the
    caller's fallback is the same in each case.
    """
    if winreg is None or not device_path:
        return None
    parsed = _registry_key_for(device_path)
    if parsed is None:
        return None
    hardware_id, instance = parsed
    path = f"{_ENUM_ROOT}\\{hardware_id}\\{instance}\\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            edid, _kind = winreg.QueryValueEx(key, "EDID")
    except OSError:
        return None
    if not isinstance(edid, (bytes, bytearray)):
        return None
    return parse_hdr_static_metadata(bytes(edid))
