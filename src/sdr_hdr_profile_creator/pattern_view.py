"""The fullscreen calibration pattern view.

A Qt widget that owns an :class:`~.hdr_display.HdrSurface`. Qt keeps the window, focus,
keyboard and fullscreen handling; D3D owns the pixels. The interop recipe is
``WA_PaintOnScreen`` plus ``WA_NativeWindow`` plus a ``paintEngine`` that returns ``None``,
which stops Qt trying to draw into a surface it does not control.

The window is deliberately not a general-purpose UI. While a pattern is up, the screen is
a measuring instrument: everything except the patch and one dim strip of text is black,
and every control is a keystroke rather than a widget, because a widget would have to be
drawn somewhere and there is nowhere to draw it that does not contaminate the reading.

Controls are supplied by the caller as :class:`ControlBinding` values, so this module knows
nothing about the editor's state model and can be driven by a test with no display present.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QWidget

from .gamma_correction import pq_eotf, pq_inverse_eotf
from .hdr_display import PQ_MAX_NITS, DisplayCapability, HdrDisplayError, HdrSurface
from .patterns import (
    OVERLAY_NITS,
    PATTERNS,
    WINDOW_AREA_FRACTION,
    Pattern,
    PatternContext,
    compose,
    window_size,
)

OVERLAY_WIDTH = 460

# One arrow press moves the probe this far in PQ code. A fixed step in nits is hopeless at
# both ends: far too coarse near black where a threshold actually sits, and so fine near
# peak that crossing the range would take hundreds of presses.
PROBE_STEP_PQ = 0.004


@dataclass(frozen=True)
class ControlBinding:
    """One editor control the pattern view can drive without knowing what it is."""

    key: str
    label: str
    read: Callable[[], float]
    nudge: Callable[[float], None]
    step: float = 0.01
    suffix: str = ""

    def formatted(self) -> str:
        return f"{self.read():.3f}{self.suffix}"


def context_for(capability: DisplayCapability | None, sdr_white_nits: float) -> PatternContext:
    """Build a pattern context from what the panel reports, with sane fallbacks.

    A display with no credible metadata still gets patterns; it gets them against a
    conservative peak instead of a fabricated one, and the view says the figure is assumed.
    """
    if capability is None:
        return PatternContext(is_hdr=False, sdr_white_nits=sdr_white_nits)
    peak = capability.max_nits if capability.luminance_is_credible else 400.0
    full_frame = capability.max_full_frame_nits if capability.luminance_is_credible else 250.0
    return PatternContext(
        is_hdr=capability.is_hdr,
        sdr_white_nits=sdr_white_nits,
        peak_nits=peak,
        max_full_frame_nits=full_frame,
    )


def render_overlay(
    width: int,
    height: int,
    pattern: Pattern,
    context: PatternContext,
    controls: Sequence[ControlBinding],
    active: int,
    *,
    assumed_peak: bool = False,
) -> tuple[bytes, int, int]:
    """Paint the guidance strip with Qt and hand back raw RGBA8.

    Text layout is the one thing Qt is better at than anything hand-rolled, so it draws
    into an image and :func:`~.patterns.compose` converts and dims the result. Painting it
    straight to the screen would put it in sRGB at whatever luminance the desktop uses,
    which beside a near-black patch is precisely what must not happen.
    """
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(0, 0, 0, 0))

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        margin = 18
        y = margin

        heading = QFont()
        heading.setPointSize(15)
        heading.setBold(True)
        painter.setFont(heading)
        painter.setPen(QColor(255, 255, 255, 255))
        painter.drawText(margin, y + 20, pattern.title)
        y += 40

        body = QFont()
        body.setPointSize(10)
        # The criterion goes first and brightest. It is the thing being checked against,
        # and a target buried in a paragraph is a pattern that gets stared at, not read.
        criterion_font = QFont()
        criterion_font.setPointSize(11)
        criterion_font.setBold(True)
        painter.setFont(criterion_font)
        painter.setPen(QColor(255, 255, 255, 255))
        box = painter.boundingRect(
            margin, y, width - margin * 2, 200,
            int(Qt.TextFlag.TextWordWrap), pattern.criterion,
        )
        painter.drawText(box, int(Qt.TextFlag.TextWordWrap), pattern.criterion)
        y = box.bottom() + 18

        painter.setFont(body)
        painter.setPen(QColor(200, 200, 200, 255))
        box = painter.boundingRect(
            margin, y, width - margin * 2, 400,
            int(Qt.TextFlag.TextWordWrap), pattern.instructions,
        )
        painter.drawText(box, int(Qt.TextFlag.TextWordWrap), pattern.instructions)
        y = box.bottom() + 26

        painter.setPen(QColor(150, 150, 150, 255))
        if context.absolute:
            scale = f"Peak {context.peak_nits:.0f} nits"
            if assumed_peak:
                scale += " (assumed; the panel reports nothing usable)"
        else:
            scale = "SDR: levels are relative to reference white, not nits"
        painter.drawText(margin, y, scale)
        y += 30

        if pattern.level_driven:
            # The display holds still and the pattern moves, so the sliders are not what
            # the arrows touch here. Showing them would say the opposite.
            painter.setPen(QColor(255, 255, 255, 255))
            reading = (f"{context.probe_nits:.4g} nits" if context.absolute
                       else f"{context.probe_nits:.4g} of white")
            painter.drawText(margin, y, f"Level  {reading}")
            y += 40
        else:
            for index, control in enumerate(controls):
                selected = index == active
                painter.setPen(QColor(255, 255, 255, 255) if selected
                               else QColor(140, 140, 140, 255))
                painter.drawText(margin, y, f"{'▸ ' if selected else '  '}{control.label}")
                painter.drawText(margin, y + 18, f"   {control.formatted()}")
                y += 44

        y += 10
        painter.setPen(QColor(130, 130, 130, 255))
        adjust = "← →  move the level" if pattern.level_driven else "← →  adjust"
        lines = [f"1-{len(PATTERNS)}   pattern"]
        if not pattern.level_driven and controls:
            lines.append("Tab   next control")
        lines += [adjust, "H     move this panel", "Esc   exit"]
        for line in lines:
            painter.drawText(margin, y, line)
            y += 20
    finally:
        painter.end()

    return (bytes(image.constBits()), width, height)


def render_markers(
    width: int, height: int, pattern: Pattern, context: PatternContext
) -> tuple[bytes, int, int] | None:
    """Paint a pattern's target labels into a window-sized RGBA image.

    Without these a pattern shows what the display is doing but never what it should be
    doing, which leaves a viewer with nothing to aim at. The target label is drawn
    brighter than the rest so the answer is findable at a glance in a dark room.
    """
    markers = pattern.markers(context)
    if not markers:
        return None

    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        for marker in markers:
            font = QFont()
            font.setPointSize(11 if marker.target else 9)
            font.setBold(marker.target)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, 255) if marker.target
                           else QColor(190, 190, 190, 255))
            metrics = painter.fontMetrics()
            text = f"◀ {marker.text}" if marker.target else marker.text
            x = int(marker.x * width) - metrics.horizontalAdvance(text) // 2
            y = int(marker.y * height)
            painter.drawText(max(0, min(width - 1, x)), max(0, min(height - 1, y)), text)
    finally:
        painter.end()
    return (bytes(image.constBits()), width, height)


class PatternWindow(QWidget):
    """Fullscreen pattern display for one output."""

    def __init__(
        self,
        capability: DisplayCapability | None,
        sdr_white_nits: float,
        controls: Sequence[ControlBinding] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("Virtual HDR OSD - calibration patterns")
        self.setCursor(Qt.CursorShape.BlankCursor)

        self._capability = capability
        self._assumed_peak = capability is not None and not capability.luminance_is_credible
        self._context = context_for(capability, sdr_white_nits)
        self._controls = list(controls)
        self._active = 0
        self._pattern_index = 0
        self._overlay_side = "right"
        self._surface: HdrSurface | None = None
        self._frame: bytes = b""
        self.failure = ""

        # Re-presenting costs nothing next to composing, so a slow tick keeps the surface
        # populated across occlusion or a mode change without rebuilding the frame.
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(1000)
        self._keepalive.timeout.connect(self._present)

    # -- Qt interop ------------------------------------------------------------------

    def paintEngine(self):  # noqa: D102 - Qt must not paint into a D3D surface
        return None

    # -- lifecycle -------------------------------------------------------------------

    def begin(self) -> bool:
        """Create the swapchain and show the first pattern. False if HDR is unavailable."""
        try:
            self._surface = HdrSurface(int(self.winId()), max(1, self.width()), max(1, self.height()))
        except HdrDisplayError as exc:
            self.failure = str(exc)
            return False
        self.refresh()
        self._keepalive.start()
        return True

    def closeEvent(self, event):
        self._keepalive.stop()
        if self._surface is not None:
            self._surface.close()
            self._surface = None
        super().closeEvent(event)

    # -- state -----------------------------------------------------------------------

    @property
    def pattern(self) -> Pattern:
        return PATTERNS[self._pattern_index % len(PATTERNS)]

    @property
    def active_control(self) -> ControlBinding | None:
        if not self._controls:
            return None
        return self._controls[self._active % len(self._controls)]

    def select_pattern(self, index: int) -> None:
        self._pattern_index = index % len(PATTERNS)
        self.refresh()

    def next_control(self) -> None:
        if self._controls:
            self._active = (self._active + 1) % len(self._controls)
            self.refresh()

    def adjust(self, direction: int) -> None:
        """Move whatever this pattern is asking the user to move.

        On a threshold pattern the pattern is the variable and the display holds still,
        so the arrows drive the probe level; on the rest it is the other way round. Having
        one key do both is not overloading: in each case it moves the only thing there is
        to move, and a separate key would mean remembering which pattern you are on.
        """
        if self.pattern.level_driven:
            self.step_probe(direction)
            return
        control = self.active_control
        if control is None:
            return
        control.nudge(direction * control.step)
        self.refresh()

    def step_probe(self, direction: int) -> None:
        """Move the probe level by one perceptual step.

        Steps are taken in PQ rather than in nits. A fixed nit step is far too coarse near
        black, where a threshold is found, and far too fine near peak, where it would take
        hundreds of presses to cross the range.
        """
        code = pq_inverse_eotf(self._context.probe_nits) + direction * PROBE_STEP_PQ
        code = max(0.0, min(1.0, code))
        nits = min(pq_eotf(code), PQ_MAX_NITS)
        self._context = replace(self._context, probe_nits=nits)
        self.refresh()

    @property
    def probe_nits(self) -> float:
        return self._context.probe_nits

    def set_probe(self, nits: float) -> None:
        """Place the probe at an exact level, for a meter loop to drive the sequence."""
        self._context = replace(self._context, probe_nits=max(0.0, float(nits)))
        self.refresh()

    def toggle_side(self) -> None:
        self._overlay_side = "left" if self._overlay_side == "right" else "right"
        self.refresh()

    # -- rendering -------------------------------------------------------------------

    def build_frame(self) -> bytes:
        width, height = max(1, self.width()), max(1, self.height())
        pattern = self.pattern
        overlay = render_overlay(
            min(OVERLAY_WIDTH, width), min(int(height * 0.8), height),
            pattern, self._context, self._controls, self._active,
            assumed_peak=self._assumed_peak,
        )
        block_width, block_height = window_size(
            width, height,
            pattern.window_fraction if pattern.window_fraction is not None else WINDOW_AREA_FRACTION,
        )
        return compose(
            width, height, pattern, self._context,
            overlay=overlay, overlay_side=self._overlay_side, overlay_nits=OVERLAY_NITS,
            window_overlay=render_markers(block_width, block_height, pattern, self._context),
        )

    def refresh(self) -> None:
        self._frame = self.build_frame()
        self._present()

    def _present(self) -> None:
        if self._surface is None or not self._frame:
            return
        try:
            self._surface.present(self._frame)
        except HdrDisplayError as exc:
            self.failure = str(exc)
            self._keepalive.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._surface is not None:
            try:
                self._surface.resize(max(1, self.width()), max(1, self.height()))
            except HdrDisplayError as exc:
                self.failure = str(exc)
                return
            self.refresh()

    # -- input -----------------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            index = key - Qt.Key.Key_1
            if index < len(PATTERNS):
                self.select_pattern(index)
            return
        if key == Qt.Key.Key_Tab:
            self.next_control()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self.adjust(-1)
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self.adjust(1)
            return
        if key == Qt.Key.Key_H:
            self.toggle_side()
            return
        super().keyPressEvent(event)
