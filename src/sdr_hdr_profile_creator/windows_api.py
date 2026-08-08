from __future__ import annotations

import ctypes
import os
import platform
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"


@dataclass(slots=True)
class DisplayInfo:
    key: str
    friendly_name: str
    gdi_name: str
    device_path: str
    adapter_low: int
    adapter_high: int
    source_id: int
    target_id: int
    advanced_color_supported: bool
    advanced_color_enabled: bool
    bits_per_color_channel: int
    advanced_color_kind: str = "SDR"

    @property
    def current_mode(self) -> str:
        return "HDR" if self.advanced_color_kind == "HDR" else "SDR"

    @property
    def acm_enabled(self) -> bool:
        """True when Windows is in SDR wide-gamut Auto Color Management mode."""
        return self.advanced_color_kind == "WCG"

    @property
    def label(self) -> str:
        support = "HDR" if self.advanced_color_supported else "SDR only"
        mode = "ACM/WCG" if self.acm_enabled else self.current_mode
        return f"{self.friendly_name}  ·  {self.gdi_name}  ·  {support}  ·  {mode}"


class WindowsColorError(RuntimeError):
    pass


if IS_WINDOWS:
    ULONG = wintypes.ULONG
    UINT32 = wintypes.UINT
    LONG = wintypes.LONG
    BOOL = wintypes.BOOL
    WCHAR = wintypes.WCHAR

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", LONG)]

    class POINTL(ctypes.Structure):
        _fields_ = [("x", LONG), ("y", LONG)]

    class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", UINT32), ("Denominator", UINT32)]

    class DISPLAYCONFIG_2DREGION(ctypes.Structure):
        _fields_ = [("cx", UINT32), ("cy", UINT32)]

    class DISPLAYCONFIG_VIDEO_SIGNAL_INFO(ctypes.Structure):
        _fields_ = [
            ("pixelRate", ctypes.c_uint64),
            ("hSyncFreq", DISPLAYCONFIG_RATIONAL),
            ("vSyncFreq", DISPLAYCONFIG_RATIONAL),
            ("activeSize", DISPLAYCONFIG_2DREGION),
            ("totalSize", DISPLAYCONFIG_2DREGION),
            ("videoStandard", UINT32),
            ("scanLineOrdering", UINT32),
        ]

    class DISPLAYCONFIG_TARGET_MODE(ctypes.Structure):
        _fields_ = [("targetVideoSignalInfo", DISPLAYCONFIG_VIDEO_SIGNAL_INFO)]

    class DISPLAYCONFIG_SOURCE_MODE(ctypes.Structure):
        _fields_ = [
            ("width", UINT32),
            ("height", UINT32),
            ("pixelFormat", UINT32),
            ("position", POINTL),
        ]

    class DISPLAYCONFIG_DESKTOP_IMAGE_INFO(ctypes.Structure):
        _fields_ = [
            ("PathSourceSize", POINTL),
            ("DesktopImageRegion", wintypes.RECT),
            ("DesktopImageClip", wintypes.RECT),
        ]

    class DISPLAYCONFIG_MODE_INFO_UNION(ctypes.Union):
        _fields_ = [
            ("targetMode", DISPLAYCONFIG_TARGET_MODE),
            ("sourceMode", DISPLAYCONFIG_SOURCE_MODE),
            ("desktopImageInfo", DISPLAYCONFIG_DESKTOP_IMAGE_INFO),
        ]

    class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
        _anonymous_ = ("mode",)
        _fields_ = [
            ("infoType", UINT32),
            ("id", UINT32),
            ("adapterId", LUID),
            ("mode", DISPLAYCONFIG_MODE_INFO_UNION),
        ]

    class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", UINT32),
            ("modeInfoIdx", UINT32),
            ("statusFlags", UINT32),
        ]

    class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", UINT32),
            ("outputTechnology", UINT32),
            ("rotation", UINT32),
            ("scaling", UINT32),
            ("refreshRate", DISPLAYCONFIG_RATIONAL),
            ("scanLineOrdering", UINT32),
            ("targetAvailable", BOOL),
            ("statusFlags", UINT32),
        ]

    class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
        _fields_ = [
            ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
            ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
            ("flags", UINT32),
        ]

    class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
        _fields_ = [
            ("type", UINT32),
            ("size", UINT32),
            ("adapterId", LUID),
            ("id", UINT32),
        ]

    class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("viewGdiDeviceName", WCHAR * 32),
        ]

    class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("flags", UINT32),
            ("outputTechnology", UINT32),
            ("edidManufactureId", wintypes.WORD),
            ("edidProductCodeId", wintypes.WORD),
            ("connectorInstance", UINT32),
            ("monitorFriendlyDeviceName", WCHAR * 64),
            ("monitorDevicePath", WCHAR * 128),
        ]

    class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("value", UINT32),
            ("colorEncoding", UINT32),
            ("bitsPerColorChannel", UINT32),
        ]

    class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("value", UINT32),
            ("colorEncoding", UINT32),
            ("bitsPerColorChannel", UINT32),
            ("activeColorMode", UINT32),
        ]

    class DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("SDRWhiteLevel", ULONG),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    mscms = ctypes.WinDLL("mscms", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetDisplayConfigBufferSizes.argtypes = [UINT32, ctypes.POINTER(UINT32), ctypes.POINTER(UINT32)]
    user32.GetDisplayConfigBufferSizes.restype = LONG
    user32.QueryDisplayConfig.argtypes = [
        UINT32,
        ctypes.POINTER(UINT32),
        ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
        ctypes.POINTER(UINT32),
        ctypes.POINTER(DISPLAYCONFIG_MODE_INFO),
        ctypes.c_void_p,
    ]
    user32.QueryDisplayConfig.restype = LONG
    user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
    user32.DisplayConfigGetDeviceInfo.restype = LONG

    mscms.InstallColorProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    mscms.InstallColorProfileW.restype = BOOL
    mscms.UninstallColorProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, BOOL]
    mscms.UninstallColorProfileW.restype = BOOL
    mscms.GetColorDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    mscms.GetColorDirectoryW.restype = BOOL
    if hasattr(mscms, "WcsGetCalibrationManagementState"):
        mscms.WcsGetCalibrationManagementState.argtypes = [ctypes.POINTER(BOOL)]
        mscms.WcsGetCalibrationManagementState.restype = BOOL
    if hasattr(mscms, "WcsSetCalibrationManagementState"):
        mscms.WcsSetCalibrationManagementState.argtypes = [BOOL]
        mscms.WcsSetCalibrationManagementState.restype = BOOL

    if hasattr(mscms, "ColorProfileAddDisplayAssociation"):
        mscms.ColorProfileAddDisplayAssociation.argtypes = [
            UINT32,
            wintypes.LPCWSTR,
            LUID,
            UINT32,
            BOOL,
            BOOL,
        ]
        mscms.ColorProfileAddDisplayAssociation.restype = LONG
    if hasattr(mscms, "ColorProfileSetDisplayDefaultAssociation"):
        mscms.ColorProfileSetDisplayDefaultAssociation.argtypes = [
            UINT32,
            wintypes.LPCWSTR,
            UINT32,
            UINT32,
            LUID,
            UINT32,
        ]
        mscms.ColorProfileSetDisplayDefaultAssociation.restype = LONG
    if hasattr(mscms, "ColorProfileRemoveDisplayAssociation"):
        mscms.ColorProfileRemoveDisplayAssociation.argtypes = [
            UINT32,
            wintypes.LPCWSTR,
            LUID,
            UINT32,
            BOOL,
        ]
        mscms.ColorProfileRemoveDisplayAssociation.restype = LONG

    if hasattr(mscms, "ColorProfileGetDisplayDefault"):
        mscms.ColorProfileGetDisplayDefault.argtypes = [
            UINT32,
            LUID,
            UINT32,
            UINT32,
            UINT32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        mscms.ColorProfileGetDisplayDefault.restype = LONG
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p


QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 = 15
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11
DISPLAYCONFIG_ADVANCED_COLOR_MODE_SDR = 0
DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG = 1
DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR = 2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER = 1
CPT_ICC = 0
CPST_STANDARD_DISPLAY_COLOR_MODE = 7
CPST_EXTENDED_DISPLAY_COLOR_MODE = 8


def _format_windows_error(prefix: str) -> WindowsColorError:
    error = ctypes.get_last_error()
    detail = f"{prefix}: {ctypes.FormatError(error).strip()} (Win32 {error})"
    if error == 5:
        detail += ". Windows denied profile installation; restart Run.bat as administrator and retry"
    return WindowsColorError(detail)


def _hresult_error(prefix: str, result: int) -> WindowsColorError:
    unsigned = ctypes.c_uint32(result).value
    return WindowsColorError(f"{prefix}: HRESULT 0x{unsigned:08X}")


def _device_info(packet: ctypes.Structure) -> int:
    return int(user32.DisplayConfigGetDeviceInfo(ctypes.byref(packet.header)))


def enumerate_displays() -> list[DisplayInfo]:
    if not IS_WINDOWS:
        return []

    for _attempt in range(4):
        path_count = UINT32()
        mode_count = UINT32()
        result = user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count)
        )
        if result != ERROR_SUCCESS:
            raise _hresult_error("GetDisplayConfigBufferSizes failed", result)

        paths = (DISPLAYCONFIG_PATH_INFO * max(1, path_count.value))()
        modes = (DISPLAYCONFIG_MODE_INFO * max(1, mode_count.value))()
        result = user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(path_count),
            paths,
            ctypes.byref(mode_count),
            modes,
            None,
        )
        if result == ERROR_INSUFFICIENT_BUFFER:
            continue
        if result != ERROR_SUCCESS:
            raise _hresult_error("QueryDisplayConfig failed", result)
        break
    else:
        raise WindowsColorError("Display topology kept changing while it was queried")

    displays: list[DisplayInfo] = []
    seen: set[str] = set()
    for index in range(path_count.value):
        path = paths[index]

        source = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        source.header.size = ctypes.sizeof(source)
        source.header.adapterId = path.sourceInfo.adapterId
        source.header.id = path.sourceInfo.id
        source_result = _device_info(source)
        gdi_name = source.viewGdiDeviceName if source_result == ERROR_SUCCESS else f"DISPLAY {index + 1}"

        target = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        target.header.size = ctypes.sizeof(target)
        target.header.adapterId = path.targetInfo.adapterId
        target.header.id = path.targetInfo.id
        target_result = _device_info(target)
        friendly_name = target.monitorFriendlyDeviceName if target_result == ERROR_SUCCESS else "Display"
        device_path = target.monitorDevicePath if target_result == ERROR_SUCCESS else ""
        if not friendly_name:
            friendly_name = gdi_name

        # Windows 11 exposes a second-generation query that distinguishes HDR
        # from WCG. Prefer it so "current mode" means actual active HDR, then
        # fall back to the older Advanced Color boolean on earlier builds.
        advanced2 = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2()
        advanced2.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2
        advanced2.header.size = ctypes.sizeof(advanced2)
        advanced2.header.adapterId = path.targetInfo.adapterId
        advanced2.header.id = path.targetInfo.id
        advanced2_result = _device_info(advanced2)
        if advanced2_result == ERROR_SUCCESS:
            value = advanced2.value
            supported = bool(value & (1 << 4))  # highDynamicRangeSupported
            active_mode = int(advanced2.activeColorMode)
            kind = {
                DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG: "WCG",
                DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR: "HDR",
            }.get(active_mode, "SDR")
            enabled = kind == "HDR"
            bits = int(advanced2.bitsPerColorChannel)
        else:
            advanced = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
            advanced.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
            advanced.header.size = ctypes.sizeof(advanced)
            advanced.header.adapterId = path.targetInfo.adapterId
            advanced.header.id = path.targetInfo.id
            advanced_result = _device_info(advanced)
            value = advanced.value if advanced_result == ERROR_SUCCESS else 0
            supported = bool(value & 0x1)
            enabled = bool(value & 0x2)
            kind = "HDR" if enabled else "SDR"
            bits = int(advanced.bitsPerColorChannel) if advanced_result == ERROR_SUCCESS else 0

        key = (
            f"{path.targetInfo.adapterId.HighPart & 0xFFFFFFFF:08X}:"
            f"{path.targetInfo.adapterId.LowPart:08X}:{path.sourceInfo.id}:{path.targetInfo.id}"
        )
        if key in seen:
            continue
        seen.add(key)
        displays.append(
            DisplayInfo(
                key=key,
                friendly_name=friendly_name,
                gdi_name=gdi_name,
                device_path=device_path,
                adapter_low=int(path.targetInfo.adapterId.LowPart),
                adapter_high=int(path.targetInfo.adapterId.HighPart),
                source_id=int(path.sourceInfo.id),
                target_id=int(path.targetInfo.id),
                advanced_color_supported=supported,
                advanced_color_enabled=enabled,
                bits_per_color_channel=bits,
                advanced_color_kind=kind,
            )
        )
    return displays


def find_display(display_key: str) -> DisplayInfo | None:
    displays = enumerate_displays()
    if not displays:
        return None
    for display in displays:
        if display.key == display_key:
            return display
    return displays[0]


def _luid(display: DisplayInfo) -> "LUID":
    value = LUID()
    value.LowPart = display.adapter_low
    value.HighPart = display.adapter_high
    return value


def get_color_directory() -> Path:
    if not IS_WINDOWS:
        raise WindowsColorError("Windows color directory is unavailable outside Windows")
    size = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    mscms.GetColorDirectoryW(None, None, ctypes.byref(size))
    if not size.value:
        error = ctypes.get_last_error()
        raise WindowsColorError(f"GetColorDirectoryW size query failed (Win32 {error})")
    buffer = ctypes.create_unicode_buffer(size.value + 2)
    if not mscms.GetColorDirectoryW(None, buffer, ctypes.byref(size)):
        raise _format_windows_error("GetColorDirectoryW failed")
    return Path(buffer.value)


def resolve_profile_path(profile_name: str) -> Path:
    candidate = Path(profile_name)
    if candidate.is_file():
        return candidate
    return get_color_directory() / candidate.name


def get_default_profile_path(display: DisplayInfo, mode: str) -> Path:
    return resolve_profile_path(get_default_profile(display, mode))


def get_default_profile(display: DisplayInfo, mode: str) -> str:
    if not IS_WINDOWS or not hasattr(mscms, "ColorProfileGetDisplayDefault"):
        raise WindowsColorError("Default-profile read-back is unavailable on this Windows build")
    subtype = CPST_EXTENDED_DISPLAY_COLOR_MODE if mode == "HDR" else CPST_STANDARD_DISPLAY_COLOR_MODE
    allocated = ctypes.c_void_p()
    result = mscms.ColorProfileGetDisplayDefault(
        WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
        _luid(display),
        display.source_id,
        CPT_ICC,
        subtype,
        ctypes.byref(allocated),
    )
    if result < 0:
        raise _hresult_error("ColorProfileGetDisplayDefault failed", result)
    if not allocated.value:
        raise WindowsColorError("Windows returned an empty default profile name")
    try:
        return ctypes.wstring_at(allocated.value)
    finally:
        kernel32.LocalFree(allocated.value)




def reapply_existing_default_profile(display: DisplayInfo, mode: str, profile_name: str) -> str:
    """Re-set an already-associated Windows display profile as default.

    This intentionally does not install a profile and does not invent a fallback. It is
    used by the SDR watchdog to restore the STANDARD association that Windows already had.
    """
    if not IS_WINDOWS or not hasattr(mscms, "ColorProfileSetDisplayDefaultAssociation"):
        raise WindowsColorError("Default-profile association API is unavailable on this Windows build")
    if not profile_name:
        raise WindowsColorError("No profile name was supplied")
    # Only reapply profiles that still resolve inside Windows' color directory (or an
    # explicit existing path). This prevents the watchdog from manufacturing associations.
    resolved = resolve_profile_path(profile_name)
    if not resolved.is_file():
        raise WindowsColorError(f"Remembered profile is no longer installed: {profile_name}")
    subtype = CPST_EXTENDED_DISPLAY_COLOR_MODE if mode == "HDR" else CPST_STANDARD_DISPLAY_COLOR_MODE
    result = mscms.ColorProfileSetDisplayDefaultAssociation(
        WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
        Path(profile_name).name,
        CPT_ICC,
        subtype,
        _luid(display),
        display.source_id,
    )
    if result < 0:
        raise _hresult_error("ColorProfileSetDisplayDefaultAssociation failed", result)
    active = get_default_profile(display, mode)
    if Path(active).name.casefold() != Path(profile_name).name.casefold():
        raise WindowsColorError(
            f"Windows read-back mismatch: expected {Path(profile_name).name}, received {active or '<empty>'}"
        )
    return active


def install_and_associate_profile(profile_path: Path, display: DisplayInfo, mode: str, make_default: bool = True) -> str:
    if not IS_WINDOWS:
        raise WindowsColorError("Windows profile APIs are only available on Windows")
    if not profile_path.is_file():
        raise WindowsColorError(f"Profile not found: {profile_path}")
    required = (
        "ColorProfileAddDisplayAssociation",
        "ColorProfileSetDisplayDefaultAssociation",
        "ColorProfileGetDisplayDefault",
    )
    if any(not hasattr(mscms, name) for name in required):
        raise WindowsColorError("Modern display profile association APIs are unavailable on this Windows build")

    absolute = str(profile_path.resolve())
    if not mscms.InstallColorProfileW(None, absolute):
        raise _format_windows_error("InstallColorProfileW failed")

    profile_name = profile_path.name
    advanced = mode == "HDR"
    subtype = CPST_EXTENDED_DISPLAY_COLOR_MODE if advanced else CPST_STANDARD_DISPLAY_COLOR_MODE
    try:
        result = mscms.ColorProfileAddDisplayAssociation(
            WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            profile_name,
            _luid(display),
            display.source_id,
            True,
            advanced,
        )
        if result < 0:
            raise _hresult_error("ColorProfileAddDisplayAssociation failed", result)

        if make_default:
            result = mscms.ColorProfileSetDisplayDefaultAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
                profile_name,
                CPT_ICC,
                subtype,
                _luid(display),
                display.source_id,
            )
            if result < 0:
                raise _hresult_error("ColorProfileSetDisplayDefaultAssociation failed", result)

            active = get_default_profile(display, mode)
            if Path(active).name.casefold() != profile_name.casefold():
                raise WindowsColorError(
                    f"Windows read-back mismatch: expected {profile_name}, received {active or '<empty>'}"
                )
        return profile_name
    except Exception:
        if hasattr(mscms, "ColorProfileRemoveDisplayAssociation"):
            mscms.ColorProfileRemoveDisplayAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
                profile_name,
                _luid(display),
                display.source_id,
                advanced,
            )
        mscms.UninstallColorProfileW(None, profile_name, True)
        raise


def remove_profile(profile_name: str, display: DisplayInfo, mode: str) -> tuple[bool, str]:
    if not IS_WINDOWS or not profile_name:
        return False, "Not removed"
    advanced = mode == "HDR"
    messages: list[str] = []
    association_removed = False

    if hasattr(mscms, "ColorProfileRemoveDisplayAssociation"):
        result = mscms.ColorProfileRemoveDisplayAssociation(
            WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            profile_name,
            _luid(display),
            display.source_id,
            advanced,
        )
        if result >= 0:
            association_removed = True
        else:
            messages.append(f"association HRESULT 0x{ctypes.c_uint32(result).value:08X}")

    if mscms.UninstallColorProfileW(None, profile_name, True):
        messages.append("uninstalled")
        return True, ", ".join(messages)

    error = ctypes.get_last_error()
    messages.append(f"uninstall Win32 {error}")
    return association_removed, ", ".join(messages)


def ensure_calibration_management_enabled() -> tuple[bool, str]:
    """Best-effort enablement of the legacy VCGT calibration loader.

    MHC2 profiles are loaded automatically by modern Windows. VCGT/MS00
    calibration is managed by a separate system switch and enabling that
    switch can require elevation. A failure here must therefore never undo an
    already verified MHC2/default-profile activation.
    """
    if not IS_WINDOWS:
        return False, "unavailable outside Windows"
    if not (
        hasattr(mscms, "WcsGetCalibrationManagementState")
        and hasattr(mscms, "WcsSetCalibrationManagementState")
    ):
        return False, "legacy calibration-management API unavailable"

    enabled = BOOL()
    ctypes.set_last_error(0)
    if not mscms.WcsGetCalibrationManagementState(ctypes.byref(enabled)):
        error = ctypes.get_last_error()
        return False, f"could not query VCGT loader state (Win32 {error})"
    if bool(enabled.value):
        return True, "VCGT loader enabled"

    ctypes.set_last_error(0)
    if mscms.WcsSetCalibrationManagementState(True):
        return True, "VCGT loader enabled"
    error = ctypes.get_last_error()
    if error == 5:
        return False, "VCGT loader remains disabled; administrator elevation is required"
    return False, f"VCGT loader remains disabled (Win32 {error})"


def get_sdr_white_level_nits(display: DisplayInfo) -> float:
    """Return Windows' current SDR reference white for an HDR display.

    This is the public DisplayConfig read API behind the SDR-content-brightness
    setting. Windows exposes the current white level, not a documented public
    setter for that Settings slider.
    """
    if not IS_WINDOWS:
        raise WindowsColorError("SDR white-level query is only available on Windows")
    packet = DISPLAYCONFIG_SDR_WHITE_LEVEL()
    packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL
    packet.header.size = ctypes.sizeof(packet)
    packet.header.adapterId = _luid(display)
    packet.header.id = display.target_id
    result = _device_info(packet)
    if result != ERROR_SUCCESS:
        raise _hresult_error("SDR white-level query failed", result)
    return float(packet.SDRWhiteLevel) / 1000.0 * 80.0


def estimate_sdr_brightness_slider(sdr_white_nits: float) -> float:
    """Invert the documented/reference white table to the Windows 0..100 slider."""
    points = ((0.0,80.0),(5.0,100.0),(10.0,120.0),(30.0,200.0),(55.0,300.0),(80.0,400.0),(100.0,480.0))
    n=max(80.0,min(480.0,float(sdr_white_nits)))
    for (x0,y0),(x1,y1) in zip(points,points[1:]):
        if n <= y1:
            t=(n-y0)/(y1-y0) if y1!=y0 else 0.0
            return x0+(x1-x0)*t
    return 100.0


def open_windows_hdr_settings() -> None:
    if not IS_WINDOWS:
        return
    os.startfile("ms-settings:display-advancedcolor")  # type: ignore[attr-defined]



def open_windows_display_settings() -> None:
    if not IS_WINDOWS:
        return
    os.startfile("ms-settings:display")  # type: ignore[attr-defined]

def open_windows_color_profile_directory() -> None:
    """Open Windows' canonical ICC/ICM profile directory in File Explorer."""
    if not IS_WINDOWS:
        return
    os.startfile(str(get_color_directory()))  # type: ignore[attr-defined]

def send_hdr_toggle_shortcut() -> None:
    if not IS_WINDOWS:
        raise WindowsColorError("The HDR shortcut is only available on Windows")
    VK_LWIN = 0x5B
    VK_MENU = 0x12
    VK_B = 0x42
    KEYEVENTF_KEYUP = 0x0002
    for key in (VK_LWIN, VK_MENU, VK_B):
        user32.keybd_event(key, 0, 0, 0)
    for key in (VK_B, VK_MENU, VK_LWIN):
        user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def open_windows_color_settings() -> None:
    """Backward-compatible alias for the HDR settings page."""
    open_windows_hdr_settings()
