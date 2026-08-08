from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentWidget,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SimpleCardWidget,
    StrongBodyLabel,
    SwitchButton,
    Theme,
    TitleLabel,
    setTheme,
    setThemeColor,
)

from .controls import Card, ControlSpec, PathField, SliderControl
from .curves import build_transform
from .icc import build_profile, import_profile
from .model import ApplicationState, DisplayMode, ModeState
from .windows_api import (
    DisplayInfo,
    WindowsColorError,
    enumerate_displays,
    install_and_associate_profile,
    get_color_directory,
    open_windows_display_settings,
    open_windows_color_profile_directory,
    get_default_profile,
    reapply_existing_default_profile,
    remove_profile,
    send_hdr_toggle_shortcut,
)

LOCAL_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Virtual_HDR_OSD_for_Windows"
STATE_PATH = LOCAL_ROOT / "last_gui_state.json"
LIVE_ROOT = LOCAL_ROOT / "live_profiles"
LIVE_REGISTRY_PATH = LOCAL_ROOT / "live_registry.json"
PACKAGE_ROOT = Path(__file__).resolve().parent


class MainWindow(FluentWidget):
    def __init__(self) -> None:
        setTheme(Theme.AUTO)
        setThemeColor(QColor("#4f8cff"))
        super().__init__()

        for directory in (
            LOCAL_ROOT,
            LIVE_ROOT,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.state = self._load_last_state()
        self.state.current_mode = "HDR"
        self.state.live_mode = False
        self._loading_controls = False
        self._loading_library = False
        self._last_detected_mode: DisplayMode | None = None
        self._current_display_snapshot: DisplayInfo | None = None
        self._live_profiles: dict[tuple[str, DisplayMode], tuple[str, Path, DisplayInfo]] = {}
        self._persisted_live_registry = self._load_live_registry()
        self._remembered_sdr_profiles: dict[str, str | None] = {}
        self.control_widgets: dict[str, SliderControl] = {}

        self.setWindowTitle("Virtual HDR OSD for Windows")
        self.setMinimumSize(1080, 720)
        self.resize(1380, 860)
        try:
            self.setMicaEffectEnabled(True)
            self.setCustomBackgroundColor(QColor(246, 248, 252), QColor(18, 22, 30))
        except Exception:
            pass

        self.live_timer = QTimer(self)
        self.live_timer.setSingleShot(True)
        self.live_timer.setInterval(420)
        self.live_timer.timeout.connect(self._apply_live_edit)

        self.mode_timer = QTimer(self)
        self.mode_timer.setInterval(900)
        self.mode_timer.timeout.connect(self._poll_windows_mode)

        self._build_ui()
        self._load_mode_into_controls(self.state.current_mode)
        self._refresh_displays(initial=True)
        self.mode_timer.start()

    # ----------------------------------------------------------------------------------
    # State persistence

    def _load_last_state(self) -> ApplicationState:
        if not STATE_PATH.is_file():
            return ApplicationState.neutral()
        try:
            return ApplicationState.from_dict(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ApplicationState.neutral()

    def _load_live_registry(self) -> dict[str, dict[str, str]]:
        if not LIVE_REGISTRY_PATH.is_file():
            return {}
        try:
            data = json.loads(LIVE_REGISTRY_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            result: dict[str, dict[str, str]] = {}
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, dict):
                    profile_name = str(value.get("profile_name", ""))
                    profile_path = str(value.get("profile_path", ""))
                    if profile_name:
                        result[key] = {"profile_name": profile_name, "profile_path": profile_path}
            return result
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_live_registry(self) -> None:
        try:
            temporary = LIVE_REGISTRY_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._persisted_live_registry, indent=2), encoding="utf-8")
            temporary.replace(LIVE_REGISTRY_PATH)
        except OSError:
            pass

    # ----------------------------------------------------------------------------------
    # Fluent UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, self.titleBar.height() + 10, 22, 16)
        root.setSpacing(14)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.addWidget(TitleLabel("Virtual HDR OSD for Windows", self))
        title_box.addWidget(CaptionLabel("A lightweight HDR pseudo-calibration OSD for Windows 11", self))
        heading.addLayout(title_box)
        heading.addStretch(1)
        help_button = PushButton("Help & Usage Guide", self)
        help_button.setToolTip("Open the complete usage guide, recommended calibration workflow, and safety notes.")
        help_button.clicked.connect(self._show_help)
        heading.addWidget(help_button)
        self.mode_badge = StrongBodyLabel("Windows mode: detecting…", self)
        self.mode_badge.setMinimumWidth(220)
        self.mode_badge.setToolTip("Shows the HDR/SDR state currently reported by Windows for the selected display.")
        self.mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.mode_badge)
        root.addLayout(heading)
        root.addWidget(self._build_global_bar())

        editor = QWidget(self)
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(10)
        self._editor_pages: dict[str, QWidget] = {}
        self.tab_selector = SegmentedWidget(self)
        self.page_stack = QStackedWidget(self)
        self._add_editor_page("tonePage", "Tone & Brightness", self._build_tone_tab())
        self._add_editor_page("colorPage", "Color & White Balance", self._build_color_tab())
        self.tab_selector.setToolTip("Switch between tone/luminance adjustments and color/white-balance adjustments.")
        self.tab_selector.currentItemChanged.connect(self._show_editor_page)
        self.page_stack.currentChanged.connect(self._page_changed)
        self._show_editor_page("tonePage")
        editor_layout.addWidget(self.tab_selector, 0, Qt.AlignmentFlag.AlignLeft)
        editor_layout.addWidget(self.page_stack, 1)
        root.addWidget(editor, 1)

        self.status_card = SimpleCardWidget(self)
        self.status_card.setBorderRadius(8)
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(16, 10, 16, 10)
        self.status_label = CaptionLabel("Ready", self.status_card)
        self.status_label.setToolTip("Current application status, Windows profile operation result, or error details.")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label, 1)
        root.addWidget(self.status_card)

    def _add_editor_page(self, route_key: str, text: str, page: QWidget) -> None:
        page.setObjectName(route_key)
        self._editor_pages[route_key] = page
        self.page_stack.addWidget(page)
        self.tab_selector.addItem(
            routeKey=route_key,
            text=text,
            onClick=lambda _checked=False, key=route_key: self._show_editor_page(key),
        )

    def _show_editor_page(self, route_key: object) -> None:
        """Switch the editor stack from a Fluent segmented-navigation route.

        QFluentWidgets invokes item callbacks with a click argument on some
        releases, while ``currentItemChanged`` emits the route key. Keeping the
        route lookup here makes both paths deterministic.
        """
        if isinstance(route_key, int):
            if 0 <= route_key < self.page_stack.count():
                self.page_stack.setCurrentIndex(route_key)
            return
        key = str(route_key)
        page = self._editor_pages.get(key)
        if page is None:
            return
        if self.page_stack.currentWidget() is not page:
            self.page_stack.setCurrentWidget(page)
        if self.tab_selector.currentRouteKey() != key:
            self.tab_selector.setCurrentItem(key)

    def _page_changed(self, index: int) -> None:
        widget = self.page_stack.widget(index)
        if widget is not None and self.tab_selector.currentRouteKey() != widget.objectName():
            self.tab_selector.setCurrentItem(widget.objectName())

    def _build_global_bar(self) -> QWidget:
        bar = SimpleCardWidget(self)
        bar.setBorderRadius(12)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        display_row = QHBoxLayout()
        display_row.setSpacing(9)
        display_label = StrongBodyLabel("Target Display", bar)
        display_label.setToolTip("The physical display whose Windows HDR profile will be edited and applied.")
        display_row.addWidget(display_label)
        self.display_combo = ComboBox(bar)
        self.display_combo.setMinimumWidth(290)
        self.display_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.display_combo.setToolTip("Select the active Windows display to target. HDR state and profile association are tracked per display.")
        self.display_combo.currentIndexChanged.connect(self._display_selected)
        display_row.addWidget(self.display_combo, 1)
        refresh_displays = PushButton("Refresh Displays", bar)
        refresh_displays.setToolTip("Rescan active Windows displays and refresh the selected display information.")
        refresh_displays.clicked.connect(self._refresh_displays)
        display_row.addWidget(refresh_displays)
        toggle_button = PushButton("Compare SDR / HDR", bar)
        toggle_button.setToolTip("Send Windows Win + Alt + B to switch HDR on or off for visual comparison. SDR profile associations are never modified by this app.")
        toggle_button.clicked.connect(self._toggle_windows_mode)
        display_row.addWidget(toggle_button)
        display_settings = PushButton("Windows Display Settings", bar)
        display_settings.setToolTip("Open the main Windows display settings page.")
        display_settings.clicked.connect(open_windows_display_settings)
        display_row.addWidget(display_settings)
        color_profiles = PushButton("Windows Color Profile Folder", bar)
        color_profiles.setToolTip(r"Open the Windows ICC/ICM profile folder (System32\spool\drivers\color)")
        color_profiles.clicked.connect(open_windows_color_profile_directory)
        display_row.addWidget(color_profiles)
        layout.addLayout(display_row)

        runtime_row = QHBoxLayout()
        runtime_row.setSpacing(12)
        runtime_label = StrongBodyLabel("HDR Profile Application", bar)
        runtime_label.setToolTip("Controls how the generated HDR profile is applied and reapplied while Windows HDR is active.")
        runtime_row.addWidget(runtime_label)
        self.refresh_profile_button = PushButton("Reapply Profile", bar)
        self.refresh_profile_button.setToolTip("Regenerate and reapply the current HDR profile immediately. Useful if Windows drops the HDR association or after a display-mode transition.")
        self.refresh_profile_button.clicked.connect(lambda: self._apply_mode_profile("HDR", "Manual refresh"))
        runtime_row.addWidget(self.refresh_profile_button)
        self.live_checkbox = SwitchButton(bar)
        self.live_checkbox.setOffText("Live Apply")
        self.live_checkbox.setOnText("Live Apply")
        self.live_checkbox.setToolTip("Automatically regenerate and apply the HDR profile after slider changes. Disable it when you want to make several edits before applying them manually.")
        self.live_checkbox.checkedChanged.connect(self._live_mode_toggled)
        runtime_row.addWidget(self.live_checkbox)
        self.automatic_mode_checkbox = SwitchButton(bar)
        self.automatic_mode_checkbox.setOffText("Automatic Mode Switching")
        self.automatic_mode_checkbox.setOnText("Automatic Mode Switching")
        self.automatic_mode_checkbox.setToolTip("Automatically follow Windows SDR/HDR transitions. HDR → SDR restores only the previously associated Windows STANDARD profile; SDR → HDR reapplies the active HDR profile. No neutral SDR profile is created.")
        automatic_enabled = self.state.follow_windows_mode and self.state.auto_refresh_after_mode_change
        self.automatic_mode_checkbox.setChecked(automatic_enabled)
        self.automatic_mode_checkbox.checkedChanged.connect(self._automatic_mode_switching_toggled)
        runtime_row.addWidget(self.automatic_mode_checkbox)
        runtime_row.addStretch(1)
        layout.addLayout(runtime_row)

        profile_row = QHBoxLayout()
        profile_row.setSpacing(8)
        profile_label = StrongBodyLabel("HDR Calibration Profile", bar)
        profile_label.setToolTip("The HDR ICC/ICM profile used as the editable base. Windows HDR Calibration profiles are recommended.")
        profile_row.addWidget(profile_label)
        self.profile_path_edit = PathField(bar)
        self.profile_path_edit.setMinimumWidth(300)
        self.profile_path_edit.setToolTip("Path of the currently loaded HDR ICC/ICM profile. The field is read-only; use Import HDR Profile to choose another file.")
        profile_row.addWidget(self.profile_path_edit, 1)
        import_profile = PushButton("Import HDR Profile", bar)
        import_profile.setToolTip("Import an HDR .icm or .icc profile. The file picker opens in the Windows system color-profile folder.")
        import_profile.clicked.connect(self._import_profile)
        profile_row.addWidget(import_profile)
        export_profile = PushButton("Export Edited HDR Profile", bar)
        export_profile.setToolTip("Export the current HDR profile with all slider corrections embedded in the generated ICC/ICM data.")
        export_profile.clicked.connect(self._export_profile)
        profile_row.addWidget(export_profile)
        self.apply_profile_button = PrimaryPushButton("Apply HDR Profile Now", bar)
        self.apply_profile_button.setToolTip("Generate, install, associate, and apply the current HDR profile immediately to the selected display.")
        self.apply_profile_button.clicked.connect(self._apply_selected_profile)
        profile_row.addWidget(self.apply_profile_button)
        layout.addLayout(profile_row)
        return bar

    def _scroll_page(self, content_layout: QVBoxLayout | QGridLayout) -> ScrollArea:
        content = QWidget(self)
        content.setLayout(content_layout)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        return scroll

    def _build_tone_tab(self) -> ScrollArea:
        page = QVBoxLayout()
        page.setContentsMargins(0, 4, 8, 8)
        page.setSpacing(14)

        gamma_card = Card(
            "HDR Tone & Luminance",
            "Traditional HDR tone trims. Gamma 2.200, Midtone Brightness 0% and Contrast 0% are neutral.",
        )
        gamma_card.add_widget(self._make_control(ControlSpec(
            "gamma", "Gamma / Midtone Response", 1.600, 3.000, 2.200, 0.005, "", 3, "2.200",
            "Adjust the traditional power-law tone response around the midtones. 2.200 is neutral; lower values brighten midtones and higher values darken them."
        )))
        gamma_card.add_widget(self._make_control(ControlSpec(
            "brightness_trim", "Midtone Brightness", -30.0, 30.0, 0.0, 0.05, "%", 2, "0.00%",
            "Raise or lower perceived HDR midtone brightness while keeping black and peak white anchored."
        )))
        gamma_card.add_widget(self._make_control(ControlSpec(
            "contrast", "Contrast / Tonal Separation", -30.0, 30.0, 0.0, 0.05, "%", 2, "0.00%",
            "Increase or reduce tonal separation around the midrange while preserving black and peak-white endpoints."
        )))
        page.addWidget(gamma_card)
        page.addStretch(1)
        return self._scroll_page(page)

    def _build_color_tab(self) -> ScrollArea:
        page = QVBoxLayout()
        page.setContentsMargins(0, 4, 8, 8)
        page.setSpacing(14)

        balance_card = Card(
            "White Balance & Color Intensity",
            "Fine colorimetric corrections. Temperature follows the D65/Planckian axis; Tint moves orthogonally toward green or magenta instead of adding a crude RGB cast.",
        )
        for spec in (
            ControlSpec(
                "temperature", "White Balance Temperature", -3000.0, 3000.0, 0.0, 5.0, " K", 0, "0 K",
                "Fine D65 white-point offset. Positive values warm the image; negative values cool it."
            ),
            ControlSpec(
                "tint", "Green–Magenta Tint", -25.0, 25.0, 0.0, 0.05, "", 2, "0.00",
                "Fine green/magenta white-point trim in a direction perpendicular to the local color-temperature locus."
            ),
            ControlSpec(
                "saturation", "Color Saturation", -50.0, 50.0, 0.0, 0.10, "%", 1, "0.0%",
                "Global Rec.2020 chroma trim with luminance-preserving coefficients."
            ),
        ):
            balance_card.add_widget(self._make_control(spec))
        page.addWidget(balance_card)

        channels_card = Card(
            "RGB Channel Fine Balance",
            "Small per-channel trims are composed in linear Rec.2020 and normalized to preserve neutral luminance, reducing the obvious single-channel dominance of simple RGB gain filters.",
        )
        for spec in (
            ControlSpec("red_channel", "Red Fine Balance", -25.0, 25.0, 0.0, 0.05, "%", 2, "0.00%", "Fine red-channel balance in linear Rec.2020. Use small values to correct a residual cyan/red cast after Temperature and Tint."),
            ControlSpec("green_channel", "Green Fine Balance", -25.0, 25.0, 0.0, 0.05, "%", 2, "0.00%", "Fine green-channel balance in linear Rec.2020. Use small values to correct a residual magenta/green cast after Temperature and Tint."),
            ControlSpec("blue_channel", "Blue Fine Balance", -25.0, 25.0, 0.0, 0.05, "%", 2, "0.00%", "Fine blue-channel balance in linear Rec.2020. Use small values to correct a residual yellow/blue cast after Temperature and Tint."),
        ):
            channels_card.add_widget(self._make_control(spec))
        page.addWidget(channels_card)
        page.addStretch(1)
        return self._scroll_page(page)

    # ----------------------------------------------------------------------------------
    # Editor controls

    def _make_control(self, spec: ControlSpec) -> SliderControl:
        control = SliderControl(spec)
        control.valueChanged.connect(lambda value, field=spec.key: self._control_changed(field, value))
        self.control_widgets[spec.key] = control
        return control

    def _control_changed(self, field: str, value: float) -> None:
        if self._loading_controls:
            return
        setattr(self.state.hdr, field, value)
        if field == "gamma":
            self._set_status(f"Traditional Gamma = {value:.3f} (2.200 is neutral).", "ok")
        else:
            self._set_status(f"HDR fine correction: {field.replace('_', ' ')} = {value:g}", "ok")
        self._queue_live_apply()

    def _metadata_changed(self, field: str, value: float) -> None:
        if self._loading_controls:
            return
        state = self.state.hdr
        setattr(state, field, float(value))
        if field == "peak_luminance_nits":
            state.full_frame_luminance_nits = min(state.full_frame_luminance_nits, state.peak_luminance_nits)
            state.minimum_luminance_nits = min(state.minimum_luminance_nits, state.peak_luminance_nits)
        elif field == "full_frame_luminance_nits":
            state.full_frame_luminance_nits = min(state.full_frame_luminance_nits, state.peak_luminance_nits)
        elif field == "minimum_luminance_nits":
            state.minimum_luminance_nits = min(state.minimum_luminance_nits, state.peak_luminance_nits)
        self._load_mode_into_controls(self.state.current_mode)
        self._queue_live_apply()

    def _profile_name_changed(self, text: str) -> None:
        if self._loading_controls:
            return
        self.state.hdr.profile_name = text.strip() or "Virtual HDR OSD"
        self._queue_live_apply()

    def _load_mode_into_controls(self, mode: DisplayMode) -> None:
        state = self.state.hdr
        self._loading_controls = True
        try:
            for key, control in self.control_widgets.items():
                control.set_value(float(getattr(state, key)), emit=False)
        finally:
            self._loading_controls = False

    def _automatic_mode_switching_toggled(self, checked: bool) -> None:
        # These legacy state fields are kept synchronized for backward-compatible
        # state/profile deserialization, but the GUI exposes one unambiguous control.
        self.state.follow_windows_mode = checked
        self.state.auto_refresh_after_mode_change = checked
        if checked:
            self._set_status("Automatic Mode Switching enabled. SDR/HDR transitions will restore the existing SDR association or reapply the active HDR profile as appropriate.", "ok")
        else:
            self._set_status("Automatic Mode Switching disabled. Windows mode changes will be detected for status only; no profile will be reapplied automatically.", "warning")

    def _live_mode_toggled(self, checked: bool) -> None:
        self.state.live_mode = checked
        if checked:
            self._set_status("Live Mode enabled. Changes are debounced before profile replacement.", "warning")
            self.live_timer.start(120)
        else:
            self.live_timer.stop()
            self._set_status("Live Mode disabled. The currently selected Windows profile remains active.", "ok")

    def _queue_live_apply(self) -> None:
        if self.state.live_mode:
            self.live_timer.start()

    def _apply_live_edit(self) -> None:
        if not self.state.live_mode:
            return
        self._apply_mode_profile("HDR", "Live update")

    def _switch_editor_mode(self, mode: DisplayMode) -> None:
        # The final editor is HDR-only. SDR exists solely as a Windows comparison mode.
        if mode == "HDR":
            self.state.current_mode = "HDR"
            self._load_mode_into_controls("HDR")

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "Virtual HDR OSD for Windows — Help",
            "Most monitors lock many OSD controls when HDR is enabled. Virtual HDR OSD provides a software pseudo-calibration layer so you can make small subjective color and tone corrections while staying in HDR.\n\n"
            "RECOMMENDED WORKFLOW\n"
            "1. First run the Windows HDR Calibration app from the Microsoft Store and create an HDR profile for your display.\n"
            "2. Import that HDR .ICM profile here. Import opens directly in Windows' system color-profile directory.\n"
            "3. Enable Live Mode and make SMALL corrections. White Balance Temperature / Green–Magenta Tint use a smooth white-point adaptation, RGB Fine Balance uses luminance-normalized channel balance, and Color Saturation uses a luminance-preserving Rec.2020 chroma matrix.\n"
            "4. Gamma / Midtone Response is a traditional control only: 2.200 is neutral. Midtone Brightness and Contrast / Tonal Separation are profile corrections with anchored black/white endpoints.\n"
            "5. Use Toggle SDR / HDR to compare subjective white balance and color against SDR. Virtual HDR OSD never installs, replaces or associates an SDR profile.\n"
            "6. Export the resulting HDR ICM when satisfied.\n\n"
            "IMPORTANT\n"
            "This app does not replace a colorimeter, spectrophotometer or professional calibration software. It intentionally relies on visual judgement and is designed for small personal corrections, including matching HDR white balance and perceived color to the display's SDR appearance."
        )

    # ----------------------------------------------------------------------------------
    # Display detection and application

    def _refresh_displays(self, _checked: bool = False, initial: bool = False) -> None:
        selected_key = self.state.selected_display_key
        try:
            displays = enumerate_displays()
        except Exception as exc:
            self.display_combo.clear()
            self._current_display_snapshot = None
            self._set_status(f"Display detection failed: {exc}", "error")
            return

        with QSignalBlocker(self.display_combo):
            self.display_combo.clear()
            for display in displays:
                self.display_combo.addItem(display.label, userData=display)
            selected_index = 0
            for index, display in enumerate(displays):
                if display.key == selected_key:
                    selected_index = index
                    break
            if displays:
                self.display_combo.setCurrentIndex(selected_index)

        if not displays:
            self._current_display_snapshot = None
            self._set_status("No active Windows displays were detected.", "error")
            return

        selected = self.display_combo.currentData()
        if isinstance(selected, DisplayInfo):
            self.state.selected_display_key = selected.key
            self._current_display_snapshot = selected
            self._update_mode_badge(selected)
            if initial:
                self._last_detected_mode = selected.current_mode  # type: ignore[assignment]
            self._set_status(f"Detected {len(displays)} active display(s). Selected {selected.friendly_name}.", "ok")

    def _display_selected(self, _index: int) -> None:
        selected = self.display_combo.currentData()
        if not isinstance(selected, DisplayInfo):
            return
        self.state.selected_display_key = selected.key
        self._current_display_snapshot = selected
        self._last_detected_mode = None
        self._remember_current_sdr_profile(selected)
        self._update_mode_badge(selected)

    def _selected_display(self) -> DisplayInfo | None:
        selected = self.display_combo.currentData()
        if isinstance(selected, DisplayInfo):
            if self._current_display_snapshot and self._current_display_snapshot.key == selected.key:
                return self._current_display_snapshot
            return selected
        return None

    def _poll_windows_mode(self) -> None:
        if self.display_combo.count() == 0:
            return
        key = self.state.selected_display_key
        try:
            displays = enumerate_displays()
        except Exception:
            return
        selected = next((display for display in displays if display.key == key), displays[0] if displays else None)
        if selected is None:
            return
        self._current_display_snapshot = selected
        self._update_mode_badge(selected)
        detected: DisplayMode = "HDR" if selected.advanced_color_enabled else "SDR"
        previous = self._last_detected_mode
        self._last_detected_mode = detected
        if previous is None:
            return
        if previous == detected:
            return

        self._set_status(f"Windows mode changed from {previous} to {detected}.", "warning")
        if detected == "SDR":
            if self.state.follow_windows_mode and self.state.auto_refresh_after_mode_change:
                QTimer.singleShot(650, lambda d=selected: self._restore_remembered_sdr_profile(d, "Automatic Mode Switching"))
            else:
                self._set_status("Windows switched to SDR. Automatic Mode Switching is disabled; Windows keeps its current SDR association.", "ok")
            return
        # While HDR is active the STANDARD association can be queried safely and remembered
        # without changing it. This keeps the watchdog aligned with the user's actual SDR profile.
        self._remember_current_sdr_profile(selected)
        if self.state.follow_windows_mode and self.state.auto_refresh_after_mode_change:
            QTimer.singleShot(650, lambda: self._apply_mode_profile("HDR", "Automatic Mode Switching"))

    def _remember_current_sdr_profile(self, display: DisplayInfo) -> None:
        """Remember Windows' existing STANDARD profile without modifying any association."""
        try:
            profile_name = get_default_profile(display, "SDR")
        except Exception:
            profile_name = None
        self._remembered_sdr_profiles[display.key] = profile_name or None

    def _restore_remembered_sdr_profile(self, display: DisplayInfo, reason: str) -> None:
        """Reapply only the SDR profile that Windows already had; never create a neutral fallback."""
        profile_name = self._remembered_sdr_profiles.get(display.key)
        if not profile_name:
            self._set_status(
                f"{reason}: no existing SDR profile was remembered for this display, so nothing was applied.",
                "ok",
            )
            return
        try:
            active = reapply_existing_default_profile(display, "SDR", profile_name)
            self._set_status(f"{reason}: restored SDR profile {active}.", "ok")
        except Exception as exc:
            self._set_status(f"{reason}: could not restore SDR profile {profile_name}: {exc}", "error")

    def _update_mode_badge(self, display: DisplayInfo) -> None:
        kind = display.advanced_color_kind
        mode_text = "HDR" if kind == "HDR" else ("SDR · ACM/WCG" if kind == "WCG" else "SDR")
        bit_text = f" · {display.bits_per_color_channel}-bit" if display.bits_per_color_channel else ""
        support_text = "" if display.advanced_color_supported else " · HDR unsupported"
        self.mode_badge.setText(f"Windows mode: {mode_text}{bit_text}{support_text}")
        if kind == "HDR":
            background = "background: rgba(206, 145, 45, 0.22);"
        elif kind == "WCG":
            background = "background: rgba(104, 88, 210, 0.24);"
        else:
            background = "background: rgba(45, 132, 196, 0.22);"
        self.mode_badge.setStyleSheet(
            "StrongBodyLabel { padding: 7px 12px; border-radius: 8px; " + background + " }"
        )


    def _toggle_windows_mode(self) -> None:
        try:
            display = self._selected_display()
            if display is not None and display.current_mode == "HDR":
                self._remember_current_sdr_profile(display)
            send_hdr_toggle_shortcut()
            self._set_status("Sent Win + Alt + B. Waiting for Windows to settle…", "warning")
        except Exception as exc:
            self._set_status(f"Could not toggle SDR/HDR: {exc}", "error")

    def _apply_selected_profile(self) -> None:
        path_text = self.profile_path_edit.text().strip()
        if path_text and Path(path_text).is_file():
            self._load_profile_from_path(Path(path_text))
        self._apply_mode_profile("HDR", "Apply Profile")

    @staticmethod
    def _safe_stem(text: str, fallback: str) -> str:
        cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in text.strip())
        return cleaned.strip("._")[:100] or fallback

    def _apply_mode_profile(self, mode: DisplayMode, reason: str) -> None:
        if mode != "HDR":
            self._set_status(
                "SDR profile management is intentionally disabled. Windows keeps its existing SDR association.",
                "ok",
            )
            return
        display = self._selected_display()
        if display is None:
            self._set_status("Select a detected display before applying a profile.", "error")
            return
        state = self.state.hdr if mode == "HDR" else self.state.sdr
        profile_path: Path | None = None
        try:
            transform = build_transform(state, hdr=mode == "HDR")
            profile_data = build_profile(mode, state, transform)
            digest = hashlib.sha256(profile_data).hexdigest()[:12]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"VirtualHDR_OSD_Live_{mode}_{stamp}_{digest}.icm"
            profile_path = LIVE_ROOT / filename
            profile_path.write_bytes(profile_data)
            installed_name = install_and_associate_profile(profile_path, display, mode)
        except Exception as exc:
            if profile_path is not None:
                try: profile_path.unlink(missing_ok=True)
                except OSError: pass
            if reason == "Live update":
                self.state.live_mode = False
                self.live_timer.stop()
                with QSignalBlocker(self.live_checkbox):
                    self.live_checkbox.setChecked(False)
            self._set_status(f"{reason} failed for {mode}: {exc}", "error")
            return

        key = f"{display.key}|{mode}"
        old = self._live_profiles.get((display.key, mode))
        if old is None:
            persisted = self._persisted_live_registry.get(key, {})
            if persisted.get("profile_name"):
                old = (persisted["profile_name"], Path(persisted.get("profile_path", "")), display)
        cleanup = ""
        if old and old[0] != installed_name:
            removed, detail = remove_profile(old[0], old[2], mode)
            cleanup = f" Previous live profile: {'removed' if removed else 'retained'} ({detail})."
            try: old[1].unlink(missing_ok=True)
            except OSError: pass
        self._live_profiles[(display.key, mode)] = (installed_name, profile_path, display)
        self._persisted_live_registry[key] = {"profile_name": installed_name, "profile_path": str(profile_path)}
        self._save_live_registry()
        self._set_status(f"{reason}: activated {installed_name} for {display.friendly_name} in {mode} mode.{cleanup}", "ok")

    def _load_profile_from_path(self, source: Path) -> None:
        try:
            imported = import_profile(source, "HDR")
        except Exception as exc:
            QMessageBox.critical(self, "Load Profile", f"Could not load the profile:\n\n{exc}")
            return
        target_mode: DisplayMode = "HDR"
        imported.state.imported_profile = str(source)
        if not imported.state.base_profile:
            imported.state.base_profile = str(source)
            imported.state.base_profile_name = imported.description
        self.state.set_mode_state(target_mode, imported.state)
        self._switch_editor_mode(target_mode)
        self._load_mode_into_controls(target_mode)
        self.profile_path_edit.setText(str(source))
        message = (
            f"Loaded {imported.description} into {target_mode}. Exact slider state recovered."
            if imported.exact_state
            else f"Loaded {imported.description} into {target_mode}. " + " ".join(imported.warnings)
        )
        self._set_status(message, "warning" if imported.warnings else "ok")
        self._queue_live_apply()

    @staticmethod
    def _windows_color_directory_or_home() -> str:
        try:
            return str(get_color_directory())
        except Exception:
            return str(Path.home())

    def _import_profile(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Import HDR ICC / ICM Profile",
            self._windows_color_directory_or_home(),
            "Color Profiles (*.icc *.icm);;All Files (*)",
        )
        if not filename:
            return
        source = Path(filename)
        self._load_profile_from_path(source)

    def _export_profile(self) -> None:
        state = self.state.hdr
        default_name = f"{self._safe_stem(state.profile_name, 'Virtual_HDR_OSD')}_HDR.icm"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export ICC / ICM Profile",
            str(Path.home() / default_name),
            "Color Profiles (*.icm *.icc)",
        )
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() not in (".icc", ".icm"):
            path = path.with_suffix(".icm")
        try:
            transform = build_transform(state, hdr=True)
            path.write_bytes(build_profile("HDR", state, transform))
            self.profile_path_edit.setText(str(path))
            self._set_status(f"Exported HDR profile to {path}", "ok")
        except Exception as exc:
            QMessageBox.critical(self, "Export Profile", f"Could not export the profile:\n\n{exc}")

    # ----------------------------------------------------------------------------------

    def _set_status(self, text: str, level: str) -> None:
        prefix = {"ok": "Ready", "warning": "Attention", "error": "Error"}.get(level, "Status")
        self.status_label.setText(f"{prefix} · {text}")
        colors = {
            "ok": "rgba(50, 170, 110, 0.14)",
            "warning": "rgba(220, 154, 45, 0.16)",
            "error": "rgba(220, 70, 88, 0.17)",
        }
        self.status_card.setStyleSheet(f"SimpleCardWidget {{ background: {colors.get(level, colors['ok'])}; }}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.live_timer.stop()
        self.mode_timer.stop()
        try:
            STATE_PATH.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass
        super().closeEvent(event)
