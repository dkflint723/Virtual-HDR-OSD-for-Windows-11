"""The fullscreen measurement surface and the run that drives it.

The swapchain needs real hardware, so the surface is faked and what is checked is
everything around it: that a frame is the size the swapchain demands, that it is
sized in device pixels rather than Qt's logical units, and that a run reports
exactly one outcome whatever happens inside it.
"""

from __future__ import annotations

import os
import struct
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from sdr_hdr_profile_creator import measure_view
    from sdr_hdr_profile_creator.measure import Calibration, MeasurementStep
    from sdr_hdr_profile_creator.meter import MeterError, Reading

    GUI_AVAILABLE = True
    GUI_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - environment without Qt
    GUI_AVAILABLE = False
    GUI_IMPORT_ERROR = str(exc)


def reading(Y, x, y):
    if y <= 0.0:
        return Reading(X=0.0, Y=Y, Z=0.0, x=x, y=y)
    return Reading(X=(x / y) * Y, Y=Y, Z=((1.0 - x - y) / y) * Y, x=x, y=y)


GOOD_ORDER = [
    reading(0.00016, 0.3130, 0.3290),      # black
    reading(1015.24, 0.3127, 0.3290),      # white
    reading(215.0, 0.674586, 0.314418),    # red
    reading(710.0, 0.269814, 0.685949),    # green
    reading(90.0, 0.151222, 0.060916),     # blue
]


class FakeSurface:
    def __init__(self, *_args, **_kwargs):
        self.frames = []
        self.closed = False
        self.size = (0, 0)

    def present(self, pixels, vsync=True):
        self.frames.append(pixels)

    def resize(self, width, height):
        self.size = (width, height)

    def close(self):
        self.closed = True


def hdr_capability():
    """An output reporting HDR with credible luminance.

    context_for falls back to a non-HDR, display-referred context when there is
    no capability at all, which clamps every patch to the SDR white level. That
    is right for an unknown display and wrong for measuring one."""
    from sdr_hdr_profile_creator.hdr_display import DisplayCapability

    return DisplayCapability(
        device_name=r"\\.\DISPLAY1",
        left=0, top=0, right=3840, bottom=2160,
        bits_per_color=10,
        # DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
        color_space=12,
        min_nits=0.0, max_nits=1015.24, max_full_frame_nits=265.05,
        red_primary=(0.674586, 0.314418),
        green_primary=(0.269814, 0.685949),
        blue_primary=(0.151222, 0.060916),
        white_point=(0.3127, 0.3290),
    )


class FakeDisplay:
    def __init__(self):
        self.shown = []

    def show(self, step):
        self.shown.append(step.key)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class MeasureWindowTests(unittest.TestCase):
    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def build(self, width=640, height=400, ratio=1.0):
        window = measure_view.MeasureWindow(hdr_capability(), 240.0, None)
        window.resize(width, height)
        surface = FakeSurface()
        window._surface = surface
        patcher = mock.patch.object(
            type(window), "devicePixelRatioF", lambda _self: ratio
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return window, surface

    def test_qt_never_paints_into_the_d3d_surface(self):
        window, _ = self.build()
        self.assertIsNone(window.paintEngine())

    def test_frames_are_sized_in_device_pixels_not_qt_units(self):
        """At 125% scaling a fullscreen widget reports a smaller size than its
        client area, and handing that to the swapchain stretches the frame."""
        window, _ = self.build(width=1000, height=800, ratio=1.25)
        self.assertEqual(window.device_size(), (1250, 1000))

    def test_a_presented_frame_is_exactly_what_the_swapchain_expects(self):
        window, surface = self.build(width=320, height=200)
        window.show_patch(MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0))
        self.assertEqual(len(surface.frames[-1]), 320 * 200 * 8)

    def test_the_patch_carries_the_luminance_it_was_asked_for(self):
        window, surface = self.build(width=320, height=200)
        window.show_patch(MeasurementStep("white", "Peak white", (1.0, 1.0, 1.0), 800.0))
        frame = surface.frames[-1]
        centre = struct.unpack_from("<4e", frame, ((200 // 2) * 320 + 320 // 2) * 8)
        self.assertAlmostEqual(centre[0] * 80.0, 800.0, places=1)

    def test_the_screen_starts_black(self):
        """Nothing should be lit before the first patch is asked for."""
        window, surface = self.build()
        with mock.patch.object(measure_view, "HdrSurface", lambda *a, **k: surface):
            self.assertTrue(window.begin())
        self.assertTrue(surface.frames)
        self.assertEqual(set(surface.frames[0][:24]), {0, 0x3C})

    def test_a_surface_that_cannot_be_created_is_reported_not_raised(self):
        window, _ = self.build()
        window._surface = None
        with mock.patch.object(
            measure_view, "HdrSurface",
            side_effect=measure_view.HdrDisplayError("no HDR"),
        ):
            self.assertFalse(window.begin())
        self.assertIn("no HDR", window.failure)

    def test_closing_releases_the_surface(self):
        """A surface left open holds the swapchain, and the next run cannot
        create one for the same window."""
        from PySide6.QtGui import QCloseEvent

        window, surface = self.build()
        window.closeEvent(QCloseEvent())
        self.assertTrue(surface.closed)


@unittest.skipUnless(GUI_AVAILABLE, f"GUI dependencies unavailable: {GUI_IMPORT_ERROR}")
class MeasurementWorkerTests(unittest.TestCase):
    """Exactly one outcome, whatever happens inside the run."""

    qt_app = None

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def run_worker(self, reader, peak_nits=1015.24):
        display = FakeDisplay()
        # Real settling delays would charge these tests 24 seconds for nothing.
        worker = measure_view.MeasurementWorker(
            display, reader, peak_nits, sleep=lambda _seconds: None
        )
        outcomes = []
        worker.finished.connect(lambda result, message: outcomes.append((result, message)))
        return worker, display, outcomes

    def test_a_good_run_reports_a_calibration_and_no_message(self):
        order = iter(GOOD_ORDER)
        worker, display, outcomes = self.run_worker(lambda: next(order))
        worker.run()
        self.assertEqual(len(outcomes), 1)
        result, message = outcomes[0]
        self.assertIsInstance(result, Calibration)
        self.assertEqual(message, "")
        self.assertEqual(display.shown, ["black", "white", "red", "green", "blue"])

    def test_a_meter_failure_reports_a_message_and_no_calibration(self):
        def reader():
            raise MeterError("sensor in the wrong position")

        worker, _, outcomes = self.run_worker(reader)
        worker.run()
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertIn("wrong position", message)

    def test_cancelling_reports_neither_a_calibration_nor_an_error(self):
        """A cancelled run is not a failure, and must not look like one."""
        order = iter(GOOD_ORDER)
        worker, _, outcomes = self.run_worker(lambda: next(order))
        worker.cancel()
        worker.run()
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertEqual(message, "")

    def test_an_unexpected_error_still_produces_an_outcome(self):
        """A worker that dies silently leaves the window up with no explanation
        and no way to tell a hang from a crash."""
        def reader():
            raise ZeroDivisionError("something nobody predicted")

        worker, _, outcomes = self.run_worker(reader)
        worker.run()
        result, message = outcomes[0]
        self.assertIsNone(result)
        self.assertIn("Measurement failed", message)

    def test_progress_is_reported_for_every_patch(self):
        order = iter(GOOD_ORDER)
        worker, _, _ = self.run_worker(lambda: next(order))
        seen = []
        worker.progress.connect(lambda label, index, total: seen.append((label, index, total)))
        worker.run()
        self.assertEqual(len(seen), 5)
        self.assertEqual(seen[0][2], 5)
        self.assertEqual(seen[0][0], "Black level")


if __name__ == "__main__":
    unittest.main()
