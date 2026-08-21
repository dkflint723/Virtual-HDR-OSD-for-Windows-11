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
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from .edid import PanelMetadata
from .gamma_correction import pq_eotf, pq_inverse_eotf
from .hdr_display import PQ_MAX_NITS, DisplayCapability, HdrDisplayError, HdrSurface
from .patterns import (
    GUIDED_SEQUENCE,
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

# The finished screen is read, not measured against, so it does not need to hide at 12 nits.
SUMMARY_NITS = 40.0

# Identifies the probe-level track among the control tracks when hit-testing.
PROBE_TRACK_KEY = "probe-level"


@dataclass(frozen=True)
class ControlBinding:
    """One editor control the pattern view can drive without knowing what it is.

    ``minimum`` and ``maximum`` are what let the view draw a position rather than only a
    number: a figure with no range around it says nothing about how far there is left to
    go in either direction. ``write`` sets an absolute value, for typed entry -- stepping
    from 2.200 to 1.700 an arrow at a time is not a control anyone would use.
    """

    key: str
    label: str
    read: Callable[[], float]
    nudge: Callable[[float], None]
    step: float = 0.01
    suffix: str = ""
    minimum: float = 0.0
    maximum: float = 1.0
    write: Callable[[float], None] | None = None

    def formatted(self) -> str:
        return f"{self.read():.3f}{self.suffix}"

    def fraction(self) -> float:
        """Where the current value sits in its range, 0 to 1."""
        span = self.maximum - self.minimum
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self.read() - self.minimum) / span))

    def set_value(self, value: float) -> bool:
        if self.write is None:
            return False
        self.write(max(self.minimum, min(self.maximum, float(value))))
        return True


def context_for(
    capability: DisplayCapability | None,
    sdr_white_nits: float,
    panel: PanelMetadata | None = None,
) -> PatternContext:
    """Build a pattern context from what the panel reports, with sane fallbacks.

    The panel's own EDID outranks DXGI where both answer. DXGI gets maximum frame-average
    wrong -- it repeats peak -- and on the display this was measured against that is 1010
    nits where the panel declares 265. Starting the full-frame step four times too high
    means every press of the arrow key is spent climbing back down.

    A display with no credible metadata still gets patterns; it gets them against a
    conservative peak instead of a fabricated one, and the view says the figure is assumed.
    """
    if capability is None:
        return PatternContext(is_hdr=False, sdr_white_nits=sdr_white_nits)
    peak = capability.max_nits if capability.luminance_is_credible else 400.0
    full_frame = capability.max_full_frame_nits if capability.luminance_is_credible else 250.0
    if panel is not None and panel.credible:
        peak = panel.peak_nits
        if panel.max_frame_average_nits > 0.0:
            full_frame = panel.max_frame_average_nits
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
    editing: str | None = None,
    tracks: list[tuple[str, int, int, int, int]] | None = None,
    probe_moved: bool = True,
    declared: float | None = None,
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

        def draw_track(top: int, fraction: float, active: bool, key: str = "") -> None:
            """A bar showing where a value sits in its range.

            A number on its own says nothing about how much travel is left in either
            direction, which is most of what someone adjusting by eye wants to know.
            """
            track_width = width - margin * 2
            height_px = spacing(6)
            painter.fillRect(margin, top, track_width, height_px,
                             QColor(70, 70, 70, 255) if active else QColor(45, 45, 45, 255))
            filled = int(track_width * max(0.0, min(1.0, fraction)))
            painter.fillRect(margin, top, filled, height_px,
                             QColor(210, 210, 210, 255) if active else QColor(110, 110, 110, 255))
            handle = spacing(3)
            painter.fillRect(max(margin, margin + filled - handle), top - spacing(3),
                             handle * 2, height_px + spacing(6),
                             QColor(255, 255, 255, 255) if active else QColor(150, 150, 150, 255))
            if tracks is not None and key:
                # Reported rather than recomputed by the caller: two copies of this layout
                # would drift the moment either side changed.
                tracks.append((key, margin, top - spacing(8), track_width, height_px + spacing(16)))

        if pattern.level_driven:
            # The display holds still and the pattern moves, so the sliders are not what
            # the arrows touch here. Showing them would say the opposite.
            painter.setPen(QColor(255, 255, 255, 255))
            shown = editing if editing is not None else (
                f"{context.probe_nits:.4g} nits" if context.absolute
                else f"{context.probe_nits:.4g} of white")
            painter.drawText(margin, y, f"Level  {shown}" + ("_" if editing is not None else ""))
            y += spacing(10)
            if declared is not None:
                # The panel's own claim, beside the reading rather than instead of it.
                # Agreement is reassurance; a gap is the more interesting result, and
                # either way the user should not have to remember the number.
                painter.setPen(QColor(150, 150, 150, 255))
                painter.drawText(margin, y + spacing(18), f"       panel declares {declared:.4g}")
                y += spacing(22)
            # PQ, not nits: a linear bar would spend nearly all its length on highlights
            # and show no movement at all through the range where thresholds are found.
            draw_track(y, pq_inverse_eotf(context.probe_nits), True, key=PROBE_TRACK_KEY)
            y += spacing(22)
            recorded = accepted.get(pattern.key) if accepted else None
            if not probe_moved and recorded is None:
                painter.setPen(QColor(230, 190, 130, 255))
                painter.drawText(margin, y, "Move the level before recording")
            elif recorded is not None:
                painter.setPen(QColor(150, 220, 150, 255))
                unit = " nits" if context.absolute else ""
                painter.drawText(margin, y, f"recorded {recorded:.4g}{unit}")
            elif not pattern.records:
                # accept_measurement returns before repainting for these, so promising
                # that Enter does something described a key that is a silent no-op.
                painter.setPen(QColor(150, 150, 150, 255))
                painter.drawText(margin, y, "reference only — nothing is recorded here")
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
                shown = (editing + "_") if (selected and editing is not None) else control.formatted()
                painter.drawText(margin, y + spacing(18), f"   {shown}")
                draw_track(y + spacing(30), control.fraction(), selected, key=control.key)
                y += spacing(56)

        y += spacing(10)
        painter.setPen(QColor(130, 130, 130, 255))
        adjust = "← →  move the level" if pattern.level_driven else "← →  adjust"
        walk = "↑ ↓   next level" if pattern.levels is not None else ""
        last = step is not None and step >= total
        if pattern.level_driven and pattern.records:
            confirm = "Enter  record it and finish" if last else "Enter  record it"
        else:
            confirm = "Enter  finish" if last else "Enter  done, next step"
        keys = "1-9, 0" if len(PATTERNS) > 9 else f"1-{len(PATTERNS)}"
        lines = [f"{keys}   pattern"]
        if not pattern.level_driven and controls:
            lines.append("Tab   next control")
        lines += [adjust]
        if walk:
            lines.append(walk)
        # A level-driven pattern that records nothing has no Enter to offer, unless a
        # guided run needs it to move on.
        if (pattern.level_driven and pattern.records) or step is not None:
            lines.append(confirm)
        if any(control.write for control in controls) or pattern.level_driven:
            lines.append("E     type a value")
        lines += ["H     move this panel", "Esc   exit"]
        for line in lines:
            painter.drawText(margin, y, line)
            y += spacing(20)

        if last:
            # This line used to send the user out of the view to apply by hand, written
            # before the finished screen could apply anything itself. Left in place
            # afterwards, it named the one key that discards every measurement and skips
            # the screen offering to save them -- and it was followed, because it said to.
            y += spacing(16)
            painter.setPen(QColor(255, 255, 255, 255))
            painter.drawText(margin, y, "Last step. Enter shows the results.")
    finally:
        painter.end()

    return (bytes(image.constBits()), width, height)


def dim_cursor(size: int = 20) -> QCursor:
    """A black pointer with a faint outline, for a screen being measured.

    The usual objection to a cursor here is the light it adds. This one is black with a
    grey edge, and the compositor draws it at the SDR white level, so the outline works
    out around nine nits over a few hundred pixels -- against a patch that can be five
    hundred nits across a million, which is nothing. It also lives over the guidance strip
    rather than the patch. Hiding the cursor entirely was the safer default while there
    was nothing to point at; now there is.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        arrow = [
            (1, 1), (1, size - 6), (int(size * 0.30), int(size * 0.68)),
            (int(size * 0.46), size - 2), (int(size * 0.62), int(size * 0.86)),
            (int(size * 0.46), int(size * 0.60)), (size - 6, int(size * 0.58)),
        ]
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QPolygon

        polygon = QPolygon([QPoint(x, y) for x, y in arrow])
        painter.setBrush(QColor(0, 0, 0, 255))
        painter.setPen(QPen(QColor(90, 90, 90, 255), 1))
        painter.drawPolygon(polygon)
    finally:
        painter.end()
    return QCursor(pixmap, 1, 1)


def _ink_extent(raw: bytes, width: int, height: int) -> int:
    """Lowest row with anything drawn on it, or -1 for an empty image."""
    alpha = raw[3::4]
    for y in range(height - 1, -1, -1):
        row = alpha[y * width:(y + 1) * width]
        if row.count(0) != len(row):
            return y
    return -1


def render_overlay_fitted(
    width: int,
    height: int,
    pattern: Pattern,
    context: PatternContext,
    controls: Sequence[ControlBinding],
    active: int,
    *,
    scale: float = 1.0,
    tracks: list[tuple[str, int, int, int, int]] | None = None,
    **kwargs,
) -> tuple[bytes, int, int]:
    """Render the guidance strip at the largest scale whose content actually fits.

    The text is not a fixed size: instructions differ per pattern, and the panel's own
    dimensions come from the display. Picking a scale from resolution alone silently
    clipped the controls off the bottom on a 1440p screen -- the sliders and key hints
    simply were not on the panel, with nothing to indicate anything was missing.

    Measured against a deliberately over-tall image first, so the extent is the content's
    own rather than whatever survived the crop.
    """
    probe_height = height * 3
    raw, _w, _h = render_overlay(width, probe_height, pattern, context, controls, active,
                                 scale=scale, **kwargs)
    needed = _ink_extent(raw, width, probe_height) + 1
    if 0 < needed > height:
        # A little under the exact ratio, since text does not shrink perfectly linearly.
        scale = max(0.55, scale * (height / needed) * 0.97)
    if tracks is not None:
        tracks.clear()
    return render_overlay(width, height, pattern, context, controls, active,
                          scale=scale, tracks=tracks, **kwargs)


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
        panel: PanelMetadata | None = None,
        parent: QWidget | None = None,
        measure: Callable[[str, float], None] | None = None,
        guided: bool = True,
        on_close: Callable[[], None] | None = None,
        apply: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("Virtual HDR OSD - calibration patterns")
        self.setCursor(dim_cursor())

        self._capability = capability
        self._assumed_peak = (
            capability is not None
            and not capability.luminance_is_credible
            and not (panel is not None and panel.credible)
        )
        self._panel = panel
        self._context = context_for(capability, sdr_white_nits, panel)
        self._controls = list(controls)
        self._active = 0
        self._pattern_index = 0
        self._overlay_side = "right"
        self._surface: HdrSurface | None = None
        self._frame: bytes = b""
        self._measure = measure
        self._on_close = on_close
        self._apply = apply
        self.applied = False
        #: Set when an apply was attempted and Windows refused it. The status bar that
        #: carries the reason is a child of the window this one covers, so without a
        #: line here the user is told nothing at all.
        self.apply_failed = False
        self._editing: str | None = None
        self._tracks: list[tuple[str, int, int, int, int]] = []
        self._overlay_origin = (0, 0)
        self._dragging: str | None = None
        # Guided by default. Nine patterns and a page of theory is not a procedure, and a
        # user who has to work out what to do first will do nothing.
        self._guided = bool(guided) and measure is not None
        self._step = 0
        self._complete = False
        self._showing_summary = False
        self._probe_moved = False
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
        if self._on_close is not None:
            self._on_close()
            self._on_close = None
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
        self._showing_summary = False
        self._pattern_index = index % len(PATTERNS)
        self._start_level()
        self.refresh()

    def _start_level(self) -> None:
        """Put a stepped pattern in the middle of its range.

        Neither end is a sensible place to arrive: the top blinds the viewer for the next
        minute and the bottom shows nothing until their eyes catch up. This runs for both
        ways in, since a pattern reached by number key needs it as much as a guided step
        and previously only the guided path did it.
        """
        levels = self.pattern.levels
        if levels is None:
            return
        available = levels(self._context)
        if available:
            self._context = replace(self._context, probe_nits=available[len(available) // 2])

    def step_level(self, direction: int) -> None:
        """Walk a stepped pattern's levels, settling the eye at each one."""
        levels = self.pattern.levels
        if levels is None:
            return
        available = levels(self._context)
        if not available:
            return
        current = min(range(len(available)),
                      key=lambda i: abs(available[i] - self._context.probe_nits))
        index = max(0, min(len(available) - 1, current + direction))
        self._context = replace(self._context, probe_nits=available[index])
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
        self._probe_moved = True
        self.refresh()

    @property
    def probe_nits(self) -> float:
        return self._context.probe_nits

    def set_probe(self, nits: float) -> None:
        """Place the probe at an exact level, for a meter loop to drive the sequence."""
        self._context = replace(self._context, probe_nits=max(0.0, float(nits)))
        self._probe_moved = True
        self.refresh()

    @property
    def guided_step(self) -> int | None:
        """Which step of the guided run this is, or None when browsing freely."""
        if not self._guided:
            return None
        return self._step + 1

    def _apply_guided_step(self) -> None:
        key = GUIDED_SEQUENCE[self._step]
        index = next((i for i, p in enumerate(PATTERNS) if p.key == key), 0)
        self._pattern_index = index
        # Each step starts near where its answer usually lies, so the first press moves
        # towards the answer instead of away from it. Starting every step at the same
        # level would mean holding an arrow for several seconds before anything happened.
        if self.pattern.levels is not None:
            self._start_level()
            self._probe_moved = False
            self.refresh()
            return
        start = {
            "black-level": 0.05,
            "peak-white": max(80.0, self._context.peak_nits * 0.6),
            "full-frame-white": max(80.0, self._context.max_full_frame_nits * 0.6),
        }.get(key, self._context.probe_nits)
        self._context = replace(self._context, probe_nits=start)
        self._probe_moved = False
        self.refresh()

    def advance(self) -> bool:
        """Move to the next guided step. False when the run is finished."""
        if not self._guided:
            return False
        if self._step + 1 >= len(GUIDED_SEQUENCE):
            self._guided = False
            # Ending the run by quietly dropping the step counter left the last pattern on
            # screen looking exactly as it did a moment earlier, so there was no way to
            # tell whether Enter had done anything. Say plainly that it is finished.
            self._complete = True
            self._showing_summary = True
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
            if self.applied:
                painter.setPen(QColor(150, 220, 150, 255))
                painter.drawText(margin, y, "Written into the profile. Esc to finish.")
            elif self.apply_failed:
                painter.setPen(QColor(240, 170, 120, 255))
                painter.drawText(margin, y, "Windows refused the profile — nothing was written.")
                y += round(30 * scale)
                painter.setPen(QColor(200, 200, 200, 255))
                painter.drawText(margin, y, "Esc     leave and read the reason; the readings are kept")
                y += round(30 * scale)
                painter.setPen(QColor(160, 160, 160, 255))
                painter.drawText(margin, y, "Enter   try again")
            else:
                painter.setPen(QColor(255, 255, 255, 255))
                painter.drawText(margin, y, "Enter   write these into the profile now")
                y += round(30 * scale)
                painter.setPen(QColor(160, 160, 160, 255))
                painter.drawText(margin, y, "Esc     leave; they stay in the editor")
            y += round(34 * scale)
            painter.setPen(QColor(130, 130, 130, 255))
            painter.drawText(margin, y, "1-9 views a pattern, S returns here")
        finally:
            painter.end()
        return (bytes(image.constBits()), width, height)

    # -- typed entry -----------------------------------------------------------------

    def begin_edit(self) -> bool:
        """Start typing a value for whatever the arrows would otherwise move.

        Stepping from 2.200 to 1.700 one arrow press at a time is not a control anyone
        would use, and on a level-driven pattern the range spans four orders of magnitude.
        """
        if self._showing_summary:
            return False
        if not self.pattern.level_driven:
            control = self.active_control
            if control is None or control.write is None:
                return False
        self._editing = ""
        self.refresh()
        return True

    def cancel_edit(self) -> None:
        self._editing = None
        self.refresh()

    def commit_edit(self) -> bool:
        """Apply the typed value, or discard it if it was not a number."""
        text = (self._editing or "").strip()
        self._editing = None
        try:
            value = float(text)
        except ValueError:
            self.refresh()
            return False
        if self.pattern.level_driven:
            self.set_probe(min(PQ_MAX_NITS, max(0.0, value)))
            return True
        control = self.active_control
        if control is None or not control.set_value(value):
            self.refresh()
            return False
        self.refresh()
        return True

    def _handle_edit_key(self, event) -> bool:
        """Consume a keystroke while typing. Digits would otherwise switch pattern."""
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit_edit()
            return True
        if key == Qt.Key.Key_Escape:
            self.cancel_edit()
            return True
        if key == Qt.Key.Key_Backspace:
            self._editing = (self._editing or "")[:-1]
            self.refresh()
            return True
        text = event.text()
        if text and (text.isdigit() or text in ".-"):
            self._editing = (self._editing or "") + text
            self.refresh()
            return True
        return True   # swallow everything else; half-typed input must not switch pattern

    def _fitted_summary(self, width: int, height: int, scale: float) -> tuple[bytes, int, int]:
        """The summary at the largest scale that fits, as the guidance strip already does.

        render_summary walks its own y with unconditional increments and QPainter clips at
        the image edge without complaint, so on a narrow or very wide panel the readings
        themselves could scroll off the bottom -- the results, silently absent, on the one
        screen whose entire job is showing them.
        """
        probe = self.render_summary(width, height * 3, scale)
        needed = _ink_extent(probe[0], width, height * 3) + 1
        if 0 < needed > height:
            scale = max(0.55, scale * (height / needed) * 0.97)
        return self.render_summary(width, height, scale)

    def _declared_for(self, pattern: Pattern) -> float | None:
        """What the panel claims for the figure this step measures, if it claims one."""
        if self._panel is None or not self._panel.credible:
            return None
        value = {
            "black-level": self._panel.min_nits,
            "peak-white": self._panel.peak_nits,
            "full-frame-white": self._panel.max_frame_average_nits,
        }.get(pattern.key)
        return value if value else None

    def show_summary(self) -> bool:
        """Return to the results after looking at a pattern."""
        if not self._complete:
            return False
        self._showing_summary = True
        self.refresh()
        return True

    def apply_measurements(self) -> bool:
        """Write everything measured into the profile, without leaving the patterns.

        Live Apply covers the sliders, but a measurement only reaches the editor state, so
        without this the readings sit there until the user thinks to press Apply Edits
        themselves. They are not lost by leaving -- _record_measurement writes and persists
        them at the moment of capture -- so nothing here may describe Esc as discarding
        anything.
        """
        if self._apply is None or self.applied:
            return False
        self.applied = bool(self._apply())
        # Latching on failure too would refuse every later Enter for the life of the
        # window, which is what happened while this returned True unconditionally.
        self.apply_failed = not self.applied
        self.refresh()
        return self.applied

    def confirm_step(self) -> bool:
        """Enter: record if this step measures something, then move on either way.

        A guided step that adjusts sliders has nothing to record, but Enter still has to
        mean "done, next" there or the run would stall on it with no way forward that the
        overlay mentions.
        """
        if self.pattern.level_driven:
            return self.accept_measurement()
        if self._guided:
            self.advance()
            return True
        return False

    def accept_measurement(self) -> bool:
        """Record the current level as this step's answer.

        Finding a threshold and then losing it is the whole calibration wasted, so the
        reading goes straight into the editor state and from there into the MHC2 header
        and lumi tag of the next generated profile. Nothing is measured that the profile
        does not then carry.
        """
        if not self.pattern.level_driven or not self.pattern.records or self._measure is None:
            return False
        if not self._probe_moved:
            # Every step opens somewhere plausible so the first press moves towards the
            # answer. That convenience makes an untouched step look exactly like a
            # completed one, and three starting values were once recorded and reported as
            # readings. A measurement nobody took is worse than no measurement.
            self.refresh()
            return False
        measured = self._context.probe_nits
        self.accepted[self.pattern.key] = measured
        self._measure(self.pattern.key, measured)
        # What the user just measured outranks what the panel claims, for every pattern
        # after this one. Continuing to show the EDID's figure would have the overlay
        # contradicting the reading taken thirty seconds earlier on the same screen.
        if self.pattern.key == "peak-white":
            self._context = replace(self._context, peak_nits=max(80.0, measured))
            self._assumed_peak = False
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
        if self._showing_summary:
            # Black, so the eyes recover, and nothing on screen that looks adjustable.
            overlay_width = min(width, max(OVERLAY_MIN_WIDTH, round(width * 0.34)))
            return compose(
                width, height, pattern_by_key("black-level") or PATTERNS[0],
                replace(self._context, probe_nits=0.0),
                fraction=0.0001,
                overlay=self._fitted_summary(overlay_width, min(height, round(height * 0.6)), scale),
                # Centred, and brighter than the measuring overlay: there is no patch left
                # to contaminate and no dark adaptation left to preserve.
                overlay_side="centre", overlay_nits=SUMMARY_NITS,
            )
        pattern = self.pattern
        # Text is sized against a reference width so it stays the same physical size as
        # resolution grows, rather than shrinking into illegibility on a 4K panel.
        overlay_width = min(width, max(OVERLAY_MIN_WIDTH, round(width * OVERLAY_WIDTH_FRACTION)))
        overlay_height = min(int(height * 0.85), height)
        overlay = render_overlay_fitted(
            overlay_width, overlay_height,
            pattern, self._context, self._controls, self._active,
            assumed_peak=self._assumed_peak, accepted=self.accepted, scale=scale,
            step=self.guided_step, total=len(GUIDED_SEQUENCE), editing=self._editing,
            tracks=self._tracks, probe_moved=self._probe_moved,
            declared=self._declared_for(pattern),
        )
        # Where compose will put the strip, so a click can be matched to a track.
        self._overlay_origin = (
            (width - overlay_width) if self._overlay_side == "right" else 0,
            max(0, (height - overlay_height) // 2),
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

    # -- mouse ------------------------------------------------------------------------

    def _track_at(self, x: float, y: float) -> str | None:
        """Which slider, if any, sits under a point given in Qt's logical coordinates."""
        ratio = self.devicePixelRatioF() or 1.0
        left, top = self._overlay_origin
        device_x, device_y = x * ratio - left, y * ratio - top
        for key, tx, ty, tw, th in self._tracks:
            if tx <= device_x <= tx + tw and ty <= device_y <= ty + th:
                return key
        return None

    def _set_from_track(self, key: str, x: float) -> None:
        ratio = self.devicePixelRatioF() or 1.0
        left, _top = self._overlay_origin
        for entry, tx, _ty, tw, _th in self._tracks:
            if entry != key or tw <= 0:
                continue
            fraction = max(0.0, min(1.0, ((x * ratio - left) - tx) / tw))
            if key == PROBE_TRACK_KEY:
                self.set_probe(min(PQ_MAX_NITS, pq_eotf(fraction)))
            else:
                control = next((c for c in self._controls if c.key == key), None)
                if control is not None:
                    control.set_value(control.minimum
                                      + fraction * (control.maximum - control.minimum))
                    self.refresh()
            return

    def mousePressEvent(self, event):
        position = event.position()
        key = self._track_at(position.x(), position.y())
        if key is None:
            return
        # Clicking a control's track also selects it, so the arrows and Enter that follow
        # act on the one just touched rather than whatever was selected before.
        for index, control in enumerate(self._controls):
            if control.key == key:
                self._active = index
                break
        self._dragging = key
        self._set_from_track(key, position.x())

    def mouseMoveEvent(self, event):
        if self._dragging is not None:
            self._set_from_track(self._dragging, event.position().x())

    def mouseReleaseEvent(self, _event):
        self._dragging = None

    def keyPressEvent(self, event):
        if self._editing is not None:
            self._handle_edit_key(event)
            return
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
        if self.pattern.levels is not None and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            # Up and Down walk the levels here; Left and Right keep driving the sliders,
            # so both jobs the step needs are reachable without a mode.
            self.step_level(1 if key == Qt.Key.Key_Up else -1)
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
        if key == Qt.Key.Key_E:
            self.begin_edit()
            return
        if key == Qt.Key.Key_S and self._complete:
            # The summary invites browsing the patterns, so there has to be a way back to
            # it. Without one, a single digit key destroyed the results screen for good.
            self.show_summary()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # On the finished screen there is no step left to confirm, so Enter is free
            # for the one thing still outstanding.
            if self._showing_summary:
                self.apply_measurements()
            else:
                self.confirm_step()
            return
        super().keyPressEvent(event)
