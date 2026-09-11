from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_NOREPEAT = 0x4000
VK_1 = 0x31
VK_2 = 0x32
HOTKEY_OFF = 0x5648_01
HOTKEY_ON = 0x5648_02
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


class GammaHotkeyListener(QObject):
    """Reliable global Alt+1 / Alt+2 hotkeys while the GUI is running.

    RegisterHotKey with ``hWnd = NULL`` posts WM_HOTKEY to the registering
    thread's message queue. A dedicated native message-loop thread is therefore
    used instead of relying on Qt's window-message filter. Signals are queued
    back to the GUI thread before any UI or profile state is changed.

    Registration is exclusive per chord, so it legitimately fails when the
    standalone watchdog already owns Alt+1 / Alt+2. ``registrationChanged``
    reports the outcome to the GUI so that a silent no-op hotkey is never
    mistaken for a broken application.
    """

    disableRequested = Signal()
    enableRequested = Signal()
    registrationChanged = Signal(bool, str)

    def __init__(
        self,
        on_disable: Callable[[], None],
        on_enable: Callable[[], None],
        on_registration: Callable[[bool, str], None] | None = None,
    ) -> None:
        super().__init__()
        self.disableRequested.connect(on_disable, Qt.ConnectionType.QueuedConnection)
        self.enableRequested.connect(on_enable, Qt.ConnectionType.QueuedConnection)
        # Connected before the worker starts. Connecting after construction would
        # race the thread and lose the one-shot registration result.
        if on_registration is not None:
            self.registrationChanged.connect(on_registration, Qt.ConnectionType.QueuedConnection)
        self.registered = False
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

        if os.name == "nt":
            self._thread = threading.Thread(
                target=self._message_loop,
                name="VirtualHDR-OSD-GammaHotkeys",
                daemon=True,
            )
            self._thread.start()
            self._ready.wait(timeout=1.5)

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL

        self._thread_id = int(kernel32.GetCurrentThreadId())
        modifiers = MOD_ALT | MOD_NOREPEAT
        ok_off = bool(user32.RegisterHotKey(None, HOTKEY_OFF, modifiers, VK_1))
        ok_on = bool(user32.RegisterHotKey(None, HOTKEY_ON, modifiers, VK_2))
        self.registered = ok_off and ok_on

        if not self.registered:
            last_error = int(kernel32.GetLastError())
            if ok_off:
                user32.UnregisterHotKey(None, HOTKEY_OFF)
            if ok_on:
                user32.UnregisterHotKey(None, HOTKEY_ON)
            if last_error == ERROR_HOTKEY_ALREADY_REGISTERED:
                reason = (
                    "another process already owns Alt+1 / Alt+2. If the standalone watchdog "
                    "is installed this is expected — it handles the hotkeys instead."
                )
            else:
                reason = f"Windows refused the hotkey registration (Win32 {last_error})."
            self.registrationChanged.emit(False, reason)
            self._ready.set()
            return

        self.registrationChanged.emit(True, "Alt+1 / Alt+2 are active while this window is open.")
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while True:
                result = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if result <= 0:
                    break
                if int(msg.message) != WM_HOTKEY:
                    continue
                hotkey_id = int(msg.wParam)
                if hotkey_id == HOTKEY_OFF:
                    self.disableRequested.emit()
                elif hotkey_id == HOTKEY_ON:
                    self.enableRequested.emit()
        finally:
            user32.UnregisterHotKey(None, HOTKEY_OFF)
            user32.UnregisterHotKey(None, HOTKEY_ON)
            self.registered = False

    def close(self) -> None:
        if os.name == "nt" and self._thread_id:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.registered = False
