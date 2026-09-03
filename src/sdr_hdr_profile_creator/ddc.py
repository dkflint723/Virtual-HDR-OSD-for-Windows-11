"""Talking to the monitor's own controls over DDC/CI.

Everything in this project so far corrects the signal *before* it reaches the display.
That is the only option for tone response, but it is the wrong place for white balance:
the MHC2 matrix can only carry +-25% of channel trim, and on the display this was
developed against red hit that clamp with white still off. Moving the same correction
into the panel's RGB gain fixes the source instead of spending profile precision
compensating downstream, and it is what a hardware calibration workflow does first.

**What this display actually exposes**, read from an ASUS PG32UCDM with HDR on:
brightness (0x10), contrast (0x12), colour preset (0x14), RGB gain (0x16/0x18/0x1A), RGB
black level (0x6C/0x6E/0x70), gamma (0x72) and picture mode (0xDC), 25 codes in total.
Writes take effect while HDR is on -- confirmed by writing red gain 86 -> 87, reading it
back, and restoring it.

**Reads fail intermittently and mean nothing on one attempt.** The first probe of this
hardware reported brightness, contrast and red and green gain as unsupported; a second
pass over the whole range found all four, stable. A single failed read is a cold link,
not an absent feature, so every read here retries before concluding anything. Getting
this wrong the other way is worse than useless: it would report a monitor as
uncalibratable when it is not.

The Windows entry points live behind a small object so the tuning logic can be exercised
against a fake. None of the arithmetic in tune.py needs a monitor.
"""

from __future__ import annotations

import ctypes
import platform
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator, Protocol

IS_WINDOWS = platform.system() == "Windows"

#: MCCS 2.2 codes this project has a use for. Names as the spec gives them.
LUMINANCE = 0x10
CONTRAST = 0x12
COLOUR_PRESET = 0x14
RED_GAIN = 0x16
GREEN_GAIN = 0x18
BLUE_GAIN = 0x1A
RED_BLACK = 0x6C
GREEN_BLACK = 0x6E
BLUE_BLACK = 0x70
GAMMA = 0x72
PICTURE_MODE = 0xDC

GAINS = (RED_GAIN, GREEN_GAIN, BLUE_GAIN)

#: How many times to ask before believing a code is unsupported, and how long to wait
#: between attempts. Five at 120 ms is well inside the time a patch needs to settle
#: anyway, so a retry costs nothing the measurement was not already spending.
READ_ATTEMPTS = 5
RETRY_PAUSE_SECONDS = 0.12


@dataclass(frozen=True, slots=True)
class Control:
    """One VCP feature as the monitor currently reports it."""

    code: int
    current: int
    maximum: int


class Link(Protocol):
    """The two RAW operations a monitor link provides, each a single attempt.

    Single attempt on purpose. Retrying belongs in :func:`read_control` and
    :func:`write_control`, where a fake can exercise it -- 29% of single reads fail on
    the hardware this was written against, so the retry policy is the part most worth
    testing and the part a real-monitor-only implementation would leave uncovered.
    """

    def read(self, code: int) -> Control | None: ...

    def set(self, code: int, value: int) -> bool: ...


class UnavailableLink:
    """What you get on a machine with no DDC/CI. Reads nothing, writes nothing."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def read(self, code: int) -> Control | None:  # noqa: ARG002
        return None

    def set(self, code: int, value: int) -> bool:  # noqa: ARG002
        return False


if IS_WINDOWS:
    _dxva2 = ctypes.windll.dxva2
    _user32 = ctypes.windll.user32

    class _PhysicalMonitor(ctypes.Structure):
        _fields_ = [
            ("handle", wintypes.HANDLE),
            ("description", wintypes.WCHAR * 128),
        ]

    _MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )


class MonitorLink:
    """A real monitor, addressed through dxva2."""

    def __init__(self, handle: int, description: str) -> None:
        self._handle = handle
        self.description = description

    def read(self, code: int) -> Control | None:
        """One attempt. The retrying is in :func:`read_control`, so that the policy is
        one tested thing rather than something only real hardware exercises."""
        current = wintypes.DWORD()
        maximum = wintypes.DWORD()
        ok = _dxva2.GetVCPFeatureAndVCPFeatureReply(
            wintypes.HANDLE(self._handle), ctypes.c_ubyte(code), None,
            ctypes.byref(current), ctypes.byref(maximum),
        )
        if not ok:
            return None
        return Control(code=code, current=int(current.value), maximum=int(maximum.value))

    def set(self, code: int, value: int) -> bool:
        """One attempt. See :func:`write_control` for the retry and the read-back."""
        return bool(_dxva2.SetVCPFeature(
            wintypes.HANDLE(self._handle), ctypes.c_ubyte(code), wintypes.DWORD(value)
        ))


def monitors() -> Iterator[MonitorLink]:
    """Every physical monitor the DDC/CI layer can address."""
    if not IS_WINDOWS:
        return

    handles: list[int] = []

    def collect(monitor, _hdc, _rect, _data):
        handles.append(monitor)
        return True

    _user32.EnumDisplayMonitors(None, None, _MONITOR_ENUM_PROC(collect), 0)

    for handle in handles:
        count = wintypes.DWORD()
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(handle, ctypes.byref(count)):
            continue
        if count.value == 0:
            continue
        physical = (_PhysicalMonitor * count.value)()
        if not _dxva2.GetPhysicalMonitorsFromHMONITOR(handle, count.value, physical):
            continue
        for item in physical:
            yield MonitorLink(int(item.handle), item.description)


def open_link(friendly_name: str = "") -> Link:
    """The monitor matching ``friendly_name``, or the only one, or an unavailable link.

    Matching is loose because the DDC/CI description and the Windows friendly name come
    from different places and agree only most of the time. With one monitor attached the
    name is not consulted at all.
    """
    if not IS_WINDOWS:
        return UnavailableLink("DDC/CI is a Windows interface")

    try:
        found = list(monitors())
    except OSError as error:
        return UnavailableLink(f"the display could not be opened: {error}")

    if not found:
        return UnavailableLink("no monitor answered; DDC/CI may be off in its own menu")
    if len(found) == 1:
        return found[0]

    wanted = friendly_name.strip().casefold()
    for link in found:
        if wanted and wanted in link.description.casefold():
            return link
    return UnavailableLink(
        f"{len(found)} monitors answered and none matched {friendly_name!r}"
    )


def read_control(
    link: Link,
    code: int,
    *,
    attempts: int = READ_ATTEMPTS,
    pause: float = RETRY_PAUSE_SECONDS,
) -> Control | None:
    """Ask for a control until it answers, or conclude it is not there.

    The retry is the whole point. Probing this project's own hardware once per code
    reported brightness, contrast, red gain and green gain as unsupported; a second pass
    over the full range found all four, stable across two reads. A single failure is a
    cold link, and treating it as an absent feature would tell someone their perfectly
    calibratable monitor cannot be calibrated.
    """
    for attempt in range(max(1, attempts)):
        control = link.read(code)
        if control is not None:
            return control
        if attempt + 1 < attempts:
            time.sleep(pause)
    return None


def write_control(
    link: Link,
    code: int,
    value: int,
    *,
    attempts: int = READ_ATTEMPTS,
    pause: float = RETRY_PAUSE_SECONDS,
) -> str:
    """Set a control and confirm it took. Empty string on success, else the reason.

    Two failures wear the same face at the API and mean opposite things, so they are
    told apart here rather than reported as one:

    * The call itself fails. On the hardware this was written against 29% of single
      DDC/CI operations fail, so one refusal says nothing at all -- it is a cold link,
      and the answer is to ask again.
    * The call succeeds and the value does not move. That is the display accepting a
      write and ignoring it, which is what many do to their controls while HDR is on.
      No number of retries fixes that, and it is the one worth telling the user about.

    Reporting the first as the second would tell someone their monitor's controls are
    locked when they are simply busy.
    """
    for attempt in range(max(1, attempts)):
        if link.set(code, value):
            time.sleep(pause)
            seen = read_control(link, code, attempts=attempts, pause=pause)
            if seen is None:
                return "the monitor stopped answering after the change was sent"
            if seen.current == value:
                return ""
            return "the monitor accepted the change and did not apply it"
        if attempt + 1 < attempts:
            time.sleep(pause)
    return "the monitor did not accept the change"


def read_gains(link: Link) -> dict[int, Control] | None:
    """The three RGB gains, or None if any of them cannot be read.

    All or nothing: tuning two channels against a third whose value is unknown would
    move white somewhere nobody asked for.
    """
    found: dict[int, Control] = {}
    for code in GAINS:
        control = read_control(link, code)
        if control is None:
            return None
        found[code] = control
    return found
