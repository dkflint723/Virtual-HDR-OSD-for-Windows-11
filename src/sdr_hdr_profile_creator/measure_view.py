"""The fullscreen surface a colorimeter reads, and the run that drives it.

Deliberately separate from ``pattern_view``. That window exists to be judged by
eye: it carries guidance text, adjustable controls, markers and a guided
sequence, all of which put light on screen and all of which a meter would
integrate along with the patch. Here the screen shows one patch on black and
nothing else.

Threading is the awkward part. A reading blocks for seconds -- spotread starts a
process, opens the instrument and integrates -- and Qt must keep presenting
frames meanwhile or Windows paints the window as not responding. So the sequence
runs on a worker thread while every frame is presented on the UI thread, with the
worker blocking until each patch is actually on screen. A patch reported as shown
before it has been presented would be read mid-transition.
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from . import measure
from .edid import PanelMetadata
from .hdr_display import DisplayCapability, HdrDisplayError, HdrSurface
from .measure import Calibration, MeasurementStep
from .meter import Reading
from .pattern_view import context_for, dim_cursor
from .patterns import measurement_frame


class MeasureWindow(QWidget):
    """Blacked-out fullscreen output showing one measurement patch at a time."""

    def __init__(
        self,
        capability: DisplayCapability | None,
        sdr_white_nits: float,
        panel: PanelMetadata | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("Virtual HDR OSD - measuring")
        # The pointer sits on the screen being measured, so it must not glow.
        self.setCursor(dim_cursor())

        self._context = context_for(capability, sdr_white_nits, panel)
        self._surface: HdrSurface | None = None
        self._frame: bytes = b""
        self.failure = ""

    def paintEngine(self):  # noqa: D102 - Qt must not paint into a D3D surface
        return None

    def device_size(self) -> tuple[int, int]:
        """Size in real pixels. Qt reports logical units, which on a scaled
        display are smaller, and a stretched frame is not the patch requested."""
        ratio = self.devicePixelRatioF() or 1.0
        return (max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))

    def begin(self) -> bool:
        """Create the swapchain and go black. False if HDR is unavailable."""
        width, height = self.device_size()
        try:
            self._surface = HdrSurface(int(self.winId()), width, height)
        except HdrDisplayError as exc:
            self.failure = str(exc)
            return False
        self.show_patch(MeasurementStep("blank", "", (0.0, 0.0, 0.0), 0.0, 0.0))
        return True

    @Slot(object)
    def show_patch(self, step: MeasurementStep) -> None:
        """Present one patch. Must run on the UI thread."""
        if self._surface is None:
            return
        width, height = self.device_size()
        self._frame = measurement_frame(width, height, step.rgb, step.nits, self._context)
        try:
            self._surface.present(self._frame)
        except HdrDisplayError as exc:
            self.failure = str(exc)

    def resizeEvent(self, event):  # noqa: D102
        super().resizeEvent(event)
        if self._surface is None:
            return
        width, height = self.device_size()
        try:
            self._surface.resize(width, height)
        except HdrDisplayError as exc:
            self.failure = str(exc)

    def closeEvent(self, event):  # noqa: D102
        if self._surface is not None:
            self._surface.close()
            self._surface = None
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: D102
        # Escape is the only control; everything else is automatic.
        if event.key() == Qt.Key.Key_Escape:
            self.close()


class _WindowDisplay:
    """Shows patches on a MeasureWindow from a worker thread.

    ``BlockingQueuedConnection`` is what makes this correct: the worker stops
    until the UI thread has actually presented the frame. Returning sooner would
    let the meter read a patch that is still on its way to the screen.
    """

    def __init__(self, window: MeasureWindow) -> None:
        self._window = window

    def show(self, step: MeasurementStep) -> None:
        QMetaObject.invokeMethod(
            self._window,
            "show_patch",
            Qt.ConnectionType.BlockingQueuedConnection,
            Q_ARG(object, step),
        )


class MeasurementWorker(QObject):
    """Runs the measurement sequence off the UI thread."""

    progress = Signal(str, int, int)
    finished = Signal(object, str)

    def __init__(
        self,
        display: measure.Display,
        read: Callable[[], Reading],
        peak_nits: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__()
        # A display rather than a window: the worker has no business knowing about
        # Qt widgets, and taking one made every outcome below untestable, because
        # a BlockingQueuedConnection issued from the thread that would service it
        # deadlocks rather than returning.
        self._display = display
        self._read = read
        self._peak_nits = peak_nits
        # Injected so tests are not charged the panel's settling time; the real
        # delays exist for the display, not for the code.
        self._sleep = sleep
        self._abort = False

    def cancel(self) -> None:
        self._abort = True

    @Slot()
    def run(self) -> None:
        """Measure, and report either a Calibration or a message. Never both."""
        try:
            result: Calibration | None = measure.run(
                self._display,
                self._read,
                peak_nits=self._peak_nits,
                on_progress=lambda step, index, total: self.progress.emit(
                    step.label, index, total
                ),
                should_abort=lambda: self._abort,
                sleep=self._sleep,
            )
        except measure.Aborted:
            self.finished.emit(None, "")
        except measure.MeasurementError as exc:
            self.finished.emit(None, str(exc))
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            self.finished.emit(None, f"Measurement failed: {exc}")
        else:
            self.finished.emit(result, "")


def start(
    window: MeasureWindow,
    read: Callable[[], Reading],
    peak_nits: float,
    on_progress: Callable[[str, int, int], None],
    on_finished: Callable[[Calibration | None, str], None],
) -> tuple[QThread, MeasurementWorker]:
    """Begin a run, returning the thread and worker so the caller can cancel.

    The caller keeps both alive: a QThread that goes out of scope while running
    takes the worker with it, and Qt warns about the destroyed thread rather than
    reporting anything useful about the measurement.
    """
    thread = QThread()
    worker = MeasurementWorker(_WindowDisplay(window), read, peak_nits)
    worker.moveToThread(thread)

    thread.started.connect(worker.run)
    worker.progress.connect(on_progress)

    def _done(result, message):
        on_finished(result, message)
        thread.quit()

    worker.finished.connect(_done)
    thread.start()
    return thread, worker
