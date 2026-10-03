"""The app's JSON files: written atomically, read without ever losing what was there.

Every path is handed in by app.py, which owns where the files live; the window tests
redirect those paths into a temporary folder, and nothing here could find the real ones
on its own (tests/test_app_split_guard.py holds it to that).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from vhdr_color.model import ApplicationState


def write_json_atomic(path: Path, payload: object) -> bool:
    """Write JSON via a temporary file so a crash cannot leave a truncated file.

    The watchdog polls gamma_hotkeys.json continuously; a half-written file
    would be parsed as corrupt and silently ignored. The app's own files are
    read back at the next start, where a truncated one costs the whole state.

    On Windows the rename fails with a PermissionError whenever another
    process has the destination open without FILE_SHARE_DELETE — which the
    watchdog does on every poll. Retrying briefly covers that window; the
    temporary file is cleaned up rather than left behind, and the caller is
    told whether the publish actually landed.
    """
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return False
    for attempt in range(5):
        try:
            temporary.replace(path)
            return True
        except PermissionError:
            # The watchdog reads these files roughly every 800ms; a few short
            # retries clear a collision without blocking the GUI meaningfully.
            time.sleep(0.04 * (attempt + 1))
        except OSError:
            break
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def load_state(path: Path) -> tuple[ApplicationState, str, bool]:
    """The saved editor state, a problem to show the user ("" if none), and whether the
    file was there but could not be opened -- in which case nothing may be saved over it."""
    if not path.is_file():
        return ApplicationState.neutral(), "", False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        # Unreadable right now is not the same as corrupt -- another process can
        # simply have it open -- so the file is left exactly where it is. That
        # includes not saving the defaults over it later in this session.
        return ApplicationState.neutral(), (
            f"{path.name} could not be opened, so the app started from defaults "
            "and will not save over it. Close whatever has it open, then restart the app."
        ), True
    except ValueError:
        # Not UTF-8, or not JSON. JSONDecodeError is a ValueError.
        payload = None
    if isinstance(payload, dict):
        try:
            return ApplicationState.from_dict(payload), "", False
        except (ValueError, TypeError, AttributeError, KeyError):
            pass
    # Falling back to defaults used to be silent, and the first save afterwards wrote
    # them over the file: every display binding and measured correction gone, with
    # nothing on screen to say so. Keep what was there, and say it.
    aside = path.with_name(path.stem + ".unreadable.json")
    try:
        path.replace(aside)
        kept = f" The unreadable file was kept as {aside.name}."
    except OSError:
        kept = ""
    return ApplicationState.neutral(), (
        "The saved settings could not be read, so the app started from defaults." + kept
    ), False


def load_live_registry(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, dict):
                profile_name = str(value.get("profile_name", ""))
                if profile_name:
                    result[key] = {"profile_name": profile_name}
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def load_original_profiles(path: Path) -> dict[str, dict[str, object]] | None:
    """None when the file is there but cannot be opened right now."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return None
    except ValueError:
        payload = None
    displays = payload.get("displays") if isinstance(payload, dict) else None
    if isinstance(displays, dict):
        return {
            key: dict(value)
            for key, value in displays.items()
            if isinstance(key, str) and key and isinstance(value, dict)
        }
    # The same rule as the settings file: an unreadable record is kept rather than
    # written over, because it may be the only note of what Windows had.
    try:
        path.replace(path.with_name(path.stem + ".unreadable.json"))
    except OSError:
        pass
    return {}
