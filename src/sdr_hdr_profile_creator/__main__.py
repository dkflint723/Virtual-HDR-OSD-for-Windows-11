from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path


def _run_app() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .app import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Virtual HDR OSD for Windows")
    app.setOrganizationName("Local Display Tools")
    window = MainWindow()
    window.show()
    return app.exec()


def _report_startup_failure() -> None:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Virtual_HDR_OSD_for_Windows"
    try:
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / "startup_error.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        details = f"The application could not start.\n\nDiagnostic log:\n{log_path}"
    except OSError:
        details = "The application could not start. Run Install.ps1 again and review its output."

    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                details,
                "Virtual HDR OSD for Windows",
                0x00000010,
            )
            return
        except Exception:
            pass
    print(details, file=sys.stderr)


def main() -> int:
    try:
        return _run_app()
    except Exception:
        _report_startup_failure()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
