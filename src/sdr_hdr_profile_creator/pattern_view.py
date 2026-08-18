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
    MEASUREMENT_SEQUENCE,
    pattern_by_key,
    OVERLAY_NITS,
    PATTERNS,
    WINDOW_AREA_FRACTION,
    Pattern,
    PatternContext,
    compose,
    window_size,
)

# The guidance strip is sized against this reference width and scaled up from there. A
# fixed pixel size shrinks as resolution grows: at 460 px it is comfortable on 1080p and
# barely legible across a 32 inch 4K panel, which is the size of display this tool is for.
OVERLAY_REFERENCE_WIDTH = 1920
OVERLAY_WIDTH_FRACTION = 0.24
OVERLAY_MIN_WIDTH = 460

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
    accepted: dict[str, float] | None = None,
    scale: float = 1.0,
    step: int | None = None,
    total: int = 0,
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
        def points(size: int) -> int:
            return max(6, round(size * scale))

        def spacing(size: int) -> int:
            return max(1, round(size * scale))

        margin = spacing(18)
        y = margin

        if step is not None:
            # Where you are, before what you are looking at. A user who cannot tell how
            # many steps remain has no way to know whether they are nearly done.
            painter.setPen(QColor(120, 190, 255, 255))
            small = QFont()
            small.setPointSize(points(10))
            small.setBold(True)
            painter.setFont(small)
            painter.drawText(margin, y + spacing(14), f"STEP {step} OF {total}")
            y += spacing(30)

        heading = QFont()
        heading.setPointSize(points(15))
        heading.setBold(True)
        painter.setFont(heading)
        painter.setPen(QColor(255, 255, 255, 255))
        painter.drawText(margin, y + spacing(20), pattern.title)
        y += spacing(40)

        body = QFont()
        body.setPointSize(points(10))
        # The criterion goes first and brightest. It is the thing being checked against,
        # and a target buried in a paragraph is a pattern that gets stared at, not read.
        criterion_font = QFont()
        criterion_font.setPointSize(points(11))
        criterion_font.setBold(True)
        painter.setFont(criterion_font)
        painter.setPen(QColor(255, 255, 255, 255))
        box = painter.boundingRect(
            margin, y, width - margin * 2, spacing(200),
            int(Qt.TextFlag.TextWordWrap), pattern.criterion,
        )
        painter.drawText(box, int(Qt.TextFlag.TextWordWrap), pattern.criterion)
        y = box.bottom() + spacing(18)

        painter.setFont(body)
        painter.setPen(QColor(200, 200, 200, 255))
        box = painter.boundingRect(
            margin, y, width - margin * 2, spacing(400),
            int(Qt.TextFlag.TextWordWrap), pattern.instructions,
        )
        painter.drawText(box, int(Qt.TextFlag.TextWordWrap), pattern.instructions)
        y = box.bottom() + spacing(26)

        painter.setPen(QColor(150, 150, 150, 255))
        if context.absolute:
            scale_note = f"Peak {context.peak_nits:.0f} nits"
            if assumed_peak:
                scale_note += " (assumed; the panel reports nothing usable)"
        else:
            scale_note = "SDR: levels are relative to reference white, not nits"
        painter.drawText(margin, y, scale_note)
        y += spacing(30)

        if pattern.level_driven:
            # The display holds still and the pattern moves, so the sliders are not what
            # the arrows touch here. Showing them would say the opposite.
            painter.setPen(QColor(255, 255, 255, 255))
            reading = (f"{context.probe_nits:.4g} nits" if context.absolute
                       else f"{context.probe_nits:.4g} of white")
            painter.drawText(margin, y, f"Level  {reading}")
            y += spacing(22)
            recorded = accepted.get(pattern.key) if accepted else None
            if recorded is not None:
                painter.setPen(QColor(150, 220, 150, 255))
                unit = " nits" if context.absolute else ""
                painter.drawText(margin, y, f"recorded {recorded:.4g}{unit}")
            else:
                painter.setPen(QColor(150, 150, 150, 255))
                nxt = "Enter records it and moves on" if step is not None else "Enter records this level"
                painter.drawText(margin, y, nxt)
            y += spacing(30)
        else:
            for index, control in enumerate(controls):
                selected = index == active
                painter.setPen(QColor(255, 255, 255, 255) if selected
                               else QColor(140, 140, 140, 255))
                painter.drawText(margin, y, f"{'▸ ' if selected else '  '}{control.label}")
                painter.drawText(margin, y + spacing(18), f"   {control.formatted()}")
                y += spacing(44)

        y += spacing(10)
        painter.setPen(QColor(130, 130, 130, 255))
        adjust = "← →  move the level" if pattern.level_driven else "← →  adjust"
        keys = "1-9, 0" if len(PATTERNS) > 9 else f"1-{len(PATTERNS)}"
        lines = [f"{keys}   pattern"]
        if not pattern.level_driven and controls:
            lines.append("Tab   next control")
        lines += [adjust]
        if pattern.level_driven:
            lines.append("Enter  record it")
        if step is not None and step >= total:
            lines.append("then Apply Edits back in the app")
        lines += ["H     move this panel", "Esc   exit"]
        for line in lines:
            painter.drawText(margin, y, line)
            y += spacing(20)
    finally:
        painter.end()

    return (bytes(image.constBits()), width, height)


def render_markers(
    width: int, height: int, pattern: Pattern, context: PatternContext, scale: float = 1.0
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
            font.setPointSize(max(6, round((11 if marker.target else 9) * scale)))
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
        measure: Callable[[str, float], None] | None = None,
        guided: bool = True,
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
        self._measure = measure
        # Guided by default. Nine patterns and a page of theory is not a procedure, and a
        # user who has to work out what to do first will do nothing.
        self._guided = bool(guided) and measure is not None
        self._step = 0
        self._complete = False
        self.accepted: dict[str, float] = {}
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

    def device_size(self) -> tuple[int, int]:
        """The window's size in real pixels, which is not what Qt reports.

        Qt measures in logical units, so on this 125% display a fullscreen widget reports
        3206x1803 while its client area is 3840x2160. Handing the smaller figure to the
        swapchain makes DXGI stretch the buffer to fit, and a stretched frame resamples the
        gamma-match lines -- which is precisely the failure the whole D3D path exists to
        avoid. Everything here is therefore sized in device pixels.
        """
        ratio = self.devicePixelRatioF() or 1.0
        return (max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))

    def begin(self) -> bool:
        """Create the swapchain and show the first pattern. False if HDR is unavailable."""
        width, height = self.device_size()
        try:
            self._surface = HdrSurface(int(self.winId()), width, height)
        except HdrDisplayError as exc:
            self.failure = str(exc)
            return False
        if self._guided:
            self._apply_guided_step()
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
        # Choosing a pattern by hand means leaving the sequence; carrying on numbering the
        # steps afterwards would be a lie about where the user is.
        self._guided = False
        self._complete = False
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

    @property
    def guided_step(self) -> int | None:
        """Which step of the guided run this is, or None when browsing freely."""
        if not self._guided:
            return None
        return self._step + 1

    def _apply_guided_step(self) -> None:
        key = MEASUREMENT_SEQUENCE[self._step]
        index = next((i for i, p in enumerate(PATTERNS) if p.key == key), 0)
        self._pattern_index = index
        # Each step starts near where its answer usually lies, so the first press moves
        # towards the answer instead of away from it. Starting every step at the same
        # level would mean holding an arrow for several seconds before anything happened.
        start = {
            "black-level": 0.05,
            "peak-white": max(80.0, self._context.peak_nits * 0.6),
            "full-frame-white": max(80.0, self._context.max_full_frame_nits * 0.6),
        }.get(key, self._context.probe_nits)
        self._context = replace(self._context, probe_nits=start)
        self.refresh()

    def advance(self) -> bool:
        """Move to the next guided step. False when the run is finished."""
        if not self._guided:
            return False
        if self._step + 1 >= len(MEASUREMENT_SEQUENCE):
            self._guided = False
            # Ending the run by quietly dropping the step counter left the last pattern on
            # screen looking exactly as it did a moment earlier, so there was no way to
            # tell whether Enter had done anything. Say plainly that it is finished.
            self._complete = True
            self.refresh()
            return False
        self._step += 1
        self._apply_guided_step()
        return True

    def render_summary(self, width: int, height: int, scale: float) -> tuple[bytes, int, int]:
        """The finished screen: what was measured, and the one thing left to do."""
        image = QImage(width, height, QImage.Format.Format_RGBA8888)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            margin = max(1, round(28 * scale))
            y = margin + round(30 * scale)

            title = QFont()
            title.setPointSize(max(6, round(17 * scale)))
            title.setBold(True)
            painter.setFont(title)
            painter.setPen(QColor(255, 255, 255, 255))
            painter.drawText(margin, y, "Measurements complete")
            y += round(52 * scale)

            body = QFont()
            body.setPointSize(max(6, round(11 * scale)))
            painter.setFont(body)
            for key in MEASUREMENT_SEQUENCE:
                pattern = next((p for p in PATTERNS if p.key == key), None)
                value = self.accepted.get(key)
                painter.setPen(QColor(150, 220, 150, 255) if value is not None
                               else QColor(150, 150, 150, 255))
                label = pattern.title if pattern else key
                unit = " nits" if self._context.absolute else ""
                shown = f"{value:.4g}{unit}" if value is not None else "not measured"
                painter.drawText(margin, y, f"{label}:  {shown}")
                y += round(34 * scale)

            y += round(24 * scale)
            painter.setPen(QColor(210, 210, 210, 255))
            painter.drawText(margin, y, "Press Esc, then Apply Edits in the app")
            y += round(30 * scale)
            painter.setPen(QColor(130, 130, 130, 255))
            painter.drawText(margin, y, "1-9 to look at any pattern instead")
        finally:
            painter.end()
        return (bytes(image.constBits()), width, height)

    def accept_measurement(self) -> bool:
        """Record the current level as this step's answer.

        Finding a threshold and then losing it is the whole calibration wasted, so the
        reading goes straight into the editor state and from there into the MHC2 header
        and lumi tag of the next generated profile. Nothing is measured that the profile
        does not then carry.
        """
        if not self.pattern.level_driven or self._measure is None:
            return False
        self.accepted[self.pattern.key] = self._context.probe_nits
        self._measure(self.pattern.key, self._context.probe_nits)
        if self._guided and not self.advance():
            return True
        self.refresh()
        return True

    def toggle_side(self) -> None:
        self._overlay_side = "left" if self._overlay_side == "right" else "right"
        self.refresh()

    # -- rendering -------------------------------------------------------------------

    def build_frame(self) -> bytes:
        width, height = self.device_size()
        scale = max(1.0, min(3.0, width / OVERLAY_REFERENCE_WIDTH))
        if self._complete:
            # Black, so the eyes recover, and nothing on screen that looks adjustable.
            overlay_width = min(width, max(OVERLAY_MIN_WIDTH, round(width * 0.34)))
            return compose(
                width, height, pattern_by_key("black-level") or PATTERNS[0],
                replace(self._context, probe_nits=0.0),
                fraction=0.0001,
                overlay=self.render_summary(overlay_width, min(height, round(height * 0.6)), scale),
                overlay_side=self._overlay_side, overlay_nits=OVERLAY_NITS,
            )
        pattern = self.pattern
        # Text is sized against a reference width so it stays the same physical size as
        # resolution grows, rather than shrinking into illegibility on a 4K panel.
        overlay_width = min(width, max(OVERLAY_MIN_WIDTH, round(width * OVERLAY_WIDTH_FRACTION)))
        overlay = render_overlay(
            overlay_width, min(int(height * 0.85), height),
            pattern, self._context, self._controls, self._active,
            assumed_peak=self._assumed_peak, accepted=self.accepted, scale=scale,
            step=self.guided_step, total=len(MEASUREMENT_SEQUENCE),
        )
        block_width, block_height = window_size(
            width, height,
            pattern.window_fraction if pattern.window_fraction is not None else WINDOW_AREA_FRACTION,
        )
        return compose(
            width, height, pattern, self._context,
            overlay=overlay, overlay_side=self._overlay_side, overlay_nits=OVERLAY_NITS,
            window_overlay=render_markers(
                block_width, block_height, pattern, self._context, scale),
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
                self._surface.resize(*self.device_size())
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
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9 or key == Qt.Key.Key_0:
            # 0 is the tenth, so a list longer than nine is still fully reachable.
            index = 9 if key == Qt.Key.Key_0 else key - Qt.Key.Key_1
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
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept_measurement()
            return
        super().keyPressEvent(event)
