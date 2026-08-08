from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

from .windows_api import (
    enumerate_displays,
    get_default_profile,
    reapply_existing_default_profile,
)

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Virtual_HDR_OSD_for_Windows"
LOG_PATH = APP_DIR / "watchdog.log"
POLL_SECONDS = 0.8
SETTLE_SECONDS = 0.7


def _log(message: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def _single_instance() -> bool:
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\Virtual_HDR_OSD_SDR_Profile_Watchdog")
    if not handle:
        return False
    ERROR_ALREADY_EXISTS = 183
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def main() -> int:
    if os.name != "nt":
        return 0
    if not _single_instance():
        return 0

    remembered: dict[str, str | None] = {}
    previous_mode: dict[str, str] = {}
    _log("Watchdog started")

    while True:
        try:
            displays = enumerate_displays()
            live_keys = {d.key for d in displays}
            for key in list(previous_mode):
                if key not in live_keys:
                    previous_mode.pop(key, None)
                    remembered.pop(key, None)

            for display in displays:
                mode = "HDR" if display.advanced_color_enabled else "SDR"
                prev = previous_mode.get(display.key)

                # While HDR is active, remember the user's actual STANDARD profile.
                # Querying does not alter Windows color associations.
                if mode == "HDR":
                    try:
                        remembered[display.key] = get_default_profile(display, "SDR") or None
                    except Exception:
                        remembered.setdefault(display.key, None)

                if prev == "HDR" and mode == "SDR":
                    profile = remembered.get(display.key)
                    if profile:
                        time.sleep(SETTLE_SECONDS)
                        try:
                            active = reapply_existing_default_profile(display, "SDR", profile)
                            _log(f"{display.friendly_name}: restored SDR profile {active}")
                        except Exception as exc:
                            _log(f"{display.friendly_name}: restore failed for {profile}: {exc}")
                    else:
                        _log(f"{display.friendly_name}: switched to SDR; no remembered STANDARD profile, no action")

                # If the watchdog starts while already in SDR, remember what Windows has now
                # for future transitions, but do not write anything.
                if prev is None and mode == "SDR":
                    try:
                        remembered[display.key] = get_default_profile(display, "SDR") or None
                    except Exception:
                        remembered[display.key] = None

                previous_mode[display.key] = mode
        except Exception as exc:
            _log(f"poll error: {exc}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
