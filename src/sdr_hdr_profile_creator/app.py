from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QApplication,
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
from .gamma_correction import CORRECTION_OPTIONS, resolve_white_level
from .hotkeys import GammaHotkeyListener
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
    get_sdr_white_level_nits,
    reapply_existing_default_profile,
    remove_profile,
    send_hdr_toggle_shortcut,
)

LOCAL_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Virtual_HDR_OSD_for_Windows"
STATE_PATH = LOCAL_ROOT / "last_gui_state.json"
LIVE_ROOT = LOCAL_ROOT / "live_profiles"
LIVE_REGISTRY_PATH = LOCAL_ROOT / "live_registry.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / "resources"
GAMMA_HOTKEY_STATE_PATH = LOCAL_ROOT / "gamma_hotkeys.json"
GAMMA_PROFILE_ROOT = LOCAL_ROOT / "gamma_hotkey_profiles"


class MainWindow(FluentWidget):
    def __init__(self) -> None:
        setTheme(Theme.AUTO)
        setThemeColor(QColor("#4f8cff"))
        super().__init__()

        for directory in (
            LOCAL_ROOT,
            LIVE_ROOT,
            GAMMA_PROFILE_ROOT,
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
        self._base_hdr_profiles: dict[str, dict[str, str]] = {}
        self.control_widgets: dict[str, SliderControl] = {}
        self._last_enabled_gamma_correction = self.state.hdr.sdr_gamma_correction if self.state.hdr.sdr_gamma_correction != "Off" else "Auto (Recommended)"
        self._hotkey_listener: GammaHotkeyListener | None = None

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

        self.gamma_companion_timer = QTimer(self)
        self.gamma_companion_timer.setSingleShot(True)
        self.gamma_companion_timer.setInterval(1400)

        self.mode_timer = QTimer(self)
        self.mode_timer.setInterval(900)
        self.mode_timer.timeout.connect(self._poll_windows_mode)

        self._build_ui()
        self._load_mode_into_controls(self.state.current_mode)
        self._hotkey_listener = GammaHotkeyListener(self._gamma_hotkey_disable, self._gamma_hotkey_enable)

        # If the standalone watchdog already owns Alt+1 / Alt+2, RegisterHotKey
        # intentionally fails here. In that case the GUI follows the shared runtime
        # state written by the watchdog, avoiding two processes fighting for one hotkey.
        self.gamma_runtime_timer = QTimer(self)
        self.gamma_runtime_timer.setInterval(450)
        self.gamma_runtime_timer.timeout.connect(self._sync_external_gamma_hotkey_state)
        self.gamma_runtime_timer.start()

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
                        result[key] = {
                            "profile_name": profile_name,
                            "profile_path": profile_path,
                            "base_profile_name": str(value.get("base_profile_name", "")),
                            "base_profile_path": str(value.get("base_profile_path", "")),
                        }
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
        watchdog_button = PushButton("Watchdog Settings…", self)
        watchdog_button.setToolTip("Install or remove the independent profile-association watchdog and persistent Alt+1 / Alt+2 gamma-correction hotkeys.")
        watchdog_button.clicked.connect(self._show_watchdog_settings)
        heading.addWidget(watchdog_button)
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
            "Traditional HDR tone trims plus an optional SDR-in-HDR piecewise-sRGB → gamma 2.2 correction based on dylanraga's documented MHC2 method.",
        )
        correction_row = QHBoxLayout()
        correction_label = StrongBodyLabel("SDR-in-HDR Gamma Correction", self)
        correction_label.setToolTip("Optional display-wide correction for Windows 11 SDR content composed inside HDR. Alt+1 disables it; Alt+2 restores the selected mode.")
        correction_row.addWidget(correction_label)
        self.gamma_correction_combo = ComboBox(self)
        self.gamma_correction_combo.addItems(list(CORRECTION_OPTIONS))
        index = self.gamma_correction_combo.findText(self.state.hdr.sdr_gamma_correction)
        self.gamma_correction_combo.setCurrentIndex(max(0, index))
        self.gamma_correction_combo.setMinimumWidth(270)
        self.gamma_correction_combo.setToolTip("Off; Auto reads Windows' current SDR reference white internally; the remaining choices mirror dylanraga's published profile options. The correction is display-wide, so disable it for native HDR content.")
        self.gamma_correction_combo.currentTextChanged.connect(self._gamma_correction_changed)
        correction_row.addWidget(self.gamma_correction_combo)
        correction_hint = CaptionLabel("Alt+1 Off  ·  Alt+2 On", self)
        correction_hint.setToolTip("Global hotkeys while the app runs. Installing the watchdog keeps the hotkeys available after the GUI closes.")
        correction_row.addWidget(correction_hint)
        correction_row.addStretch(1)
        gamma_card.add_layout(correction_row)
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

    def _save_state_now(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass

    def _gamma_correction_changed(self, text: str) -> None:
        if self._loading_controls:
            return
        option = text if text in CORRECTION_OPTIONS else "Off"
        self.state.hdr.sdr_gamma_correction = option
        if option != "Off":
            self._last_enabled_gamma_correction = option
        self._save_state_now()

        white = self._effective_sdr_white_nits()
        detail = f" · {white:.0f} nits" if white is not None else ""
        self._set_status(
            f"SDR-in-HDR gamma correction: {option}{detail}. Alt+1 disables; Alt+2 restores.",
            "warning" if option != "Off" else "ok",
        )

        # This dropdown is an explicit correction switch, not an editor trim. Apply it
        # immediately even when Live Apply is disabled so selecting Off can never leave
        # a previously corrected profile active.
        display = self._selected_display()
        if display is not None and display.current_mode == "HDR":
            self._publish_gamma_runtime_intent(display)
            self.live_timer.stop()
            self._apply_mode_profile("HDR", "Gamma correction changed")

    def _effective_sdr_white_nits(self) -> float | None:
        option = self.state.hdr.sdr_gamma_correction
        if option == "Off":
            return None
        detected: float | None = None
        if option == "Auto (Recommended)":
            display = self._selected_display()
            if display is not None:
                try:
                    detected = get_sdr_white_level_nits(display)
                except Exception:
                    detected = None
        return resolve_white_level(option, detected)

    def _gamma_hotkey_disable(self) -> None:
        if self.state.hdr.sdr_gamma_correction != "Off":
            self._last_enabled_gamma_correction = self.state.hdr.sdr_gamma_correction
        with QSignalBlocker(self.gamma_correction_combo):
            self.gamma_correction_combo.setCurrentText("Off")
        self.state.hdr.sdr_gamma_correction = "Off"
        self._save_state_now()
        self.live_timer.stop()
        self._set_status(
            "Alt+1: SDR-in-HDR gamma correction disabled. The uncorrected HDR profile is now authoritative.",
            "ok",
        )
        display = self._selected_display()
        if display is not None:
            self._publish_gamma_runtime_intent(display)
        self._apply_mode_profile("HDR", "Gamma hotkey OFF")

    def _gamma_hotkey_enable(self) -> None:
        target = self._last_enabled_gamma_correction if self._last_enabled_gamma_correction != "Off" else "Auto (Recommended)"
        with QSignalBlocker(self.gamma_correction_combo):
            self.gamma_correction_combo.setCurrentText(target)
        self.state.hdr.sdr_gamma_correction = target
        self._save_state_now()
        self.live_timer.stop()
        self._set_status(f"Alt+2: SDR-in-HDR gamma correction enabled ({target}).", "warning")
        display = self._selected_display()
        if display is not None:
            self._publish_gamma_runtime_intent(display)
        self._apply_mode_profile("HDR", "Gamma hotkey ON")

    def _sync_external_gamma_hotkey_state(self) -> None:
        """Follow hotkeys handled by the standalone watchdog while this GUI is open.

        RegisterHotKey is exclusive. If the watchdog already owns Alt+1/Alt+2, the
        GUI cannot register the same chords; the shared JSON state is therefore the
        single synchronization channel and no profile is reapplied here.
        """
        display = self._selected_display()
        if display is None or not GAMMA_HOTKEY_STATE_PATH.is_file():
            return
        try:
            payload = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))
            displays = payload.get("displays", {}) if isinstance(payload, dict) else {}
            if not isinstance(displays, dict):
                return
            entry = displays.get(display.key)
            if not isinstance(entry, dict):
                entry = next(
                    (item for item in displays.values() if isinstance(item, dict) and item.get("gdi_name") == display.gdi_name),
                    None,
                )
            if not isinstance(entry, dict):
                return
            enabled = bool(entry.get("enabled", False))
            selected = str(entry.get("selected") or self._last_enabled_gamma_correction or "Auto (Recommended)")
            if selected not in CORRECTION_OPTIONS or selected == "Off":
                selected = "Auto (Recommended)"
            desired = selected if enabled else "Off"
            if desired == self.state.hdr.sdr_gamma_correction:
                return
            if enabled:
                self._last_enabled_gamma_correction = selected
            self.state.hdr.sdr_gamma_correction = desired
            with QSignalBlocker(self.gamma_correction_combo):
                self.gamma_correction_combo.setCurrentText(desired)
            self._save_state_now()
            self._set_status(
                f"External watchdog hotkey synchronized: gamma correction {'ON (' + selected + ')' if enabled else 'OFF'}.",
                "warning" if enabled else "ok",
            )
        except Exception:
            pass

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
            if hasattr(self, "gamma_correction_combo"):
                self.gamma_correction_combo.setCurrentText(state.sdr_gamma_correction)
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
            "Most monitors lock many OSD controls when HDR is enabled. Virtual HDR OSD provides a software pseudo-calibration layer for small subjective corrections while HDR remains active.\n\n"
            "RECOMMENDED WORKFLOW\n"
            "1. Create the base HDR profile with Windows HDR Calibration.\n"
            "2. Import that HDR .ICM profile here.\n"
            "3. Use Live Apply and make small Temperature, Tint, RGB, Saturation, traditional Gamma, Midtone Brightness and Contrast corrections.\n"
            "4. Optional SDR-in-HDR Gamma Correction converts Windows' piecewise-sRGB SDR response toward pure gamma 2.2 using dylanraga's documented PQ-domain mapping. Auto reads Windows' SDR reference white internally; no SDR-brightness slider is duplicated here.\n"
            "5. This gamma correction is display-wide and can also affect native HDR10 / RTX HDR content. Use Alt+1 to disable it before native HDR content and Alt+2 to restore the selected correction for the SDR desktop.\n"
            "6. Install the independent watchdog from Watchdog Settings if you want profile-association recovery and Alt+1 / Alt+2 to remain available after closing this GUI.\n"
            "7. Export the resulting HDR ICM when satisfied.\n\n"
            "IMPORTANT\n"
            "This app does not replace a colorimeter, spectrophotometer or professional calibration workflow. It intentionally relies on visual judgement for small personal corrections."
        )

    def _show_watchdog_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Watchdog Settings")
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = StrongBodyLabel("Standalone Color Profile Mode Watchdog", dialog)
        layout.addWidget(title)
        description = BodyLabel(
            "The watchdog is independent of Virtual HDR OSD. It keeps the Windows STANDARD (SDR) and EXTENDED (HDR) profile associations stable across Win + Alt + B transitions. "
            "When this app has prepared gamma-correction profiles, the watchdog also keeps Alt+1 (correction OFF) and Alt+2 (correction ON) available after the GUI closes. It never creates a generic SDR fallback.",
            dialog,
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        hint = CaptionLabel("Install captures the current SDR/HDR associations. Re-run Install after intentionally changing either default profile.", dialog)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        install = PrimaryPushButton("Install Watchdog", dialog)
        uninstall = PushButton("Uninstall Watchdog", dialog)
        close = PushButton("Close", dialog)
        install.clicked.connect(lambda: self._run_watchdog_script("2- OPTIONAL - Install-Watchdog.bat"))
        uninstall.clicked.connect(lambda: self._run_watchdog_script("Uninstall-Watchdog.bat"))
        close.clicked.connect(dialog.accept)
        buttons.addWidget(install)
        buttons.addWidget(uninstall)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec()

    def _run_watchdog_script(self, name: str) -> None:
        if name.startswith("2-"):
            display = self._selected_display()
            if display is not None:
                self._prepare_gamma_hotkey_profiles(display)
        path = RESOURCE_ROOT / name
        if not path.is_file():
            # Source-tree fallback.
            candidate = PACKAGE_ROOT.parents[1] / name
            path = candidate if candidate.is_file() else path
        if not path.is_file():
            QMessageBox.critical(self, "Watchdog Settings", f"Watchdog script not found:\n{path}")
            return
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", str(path)], cwd=str(path.parent))
        except Exception as exc:
            QMessageBox.critical(self, "Watchdog Settings", f"Could not launch watchdog setup:\n\n{exc}")

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
        self._capture_current_hdr_base(selected, load_controls=True)
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


    def _capture_current_hdr_base(self, display: DisplayInfo, *, load_controls: bool = False) -> None:
        """Remember the real Windows HDR default as the immutable source profile.

        App-managed working profiles are never promoted to base profiles. This prevents
        repeated HDR transitions from recursively editing an already edited profile.
        """
        try:
            profile_name = get_default_profile(display, "HDR")
        except Exception:
            return
        if not profile_name or profile_name.startswith("Virtual_HDR_OSD_"):
            return
        profile_path = get_color_directory() / Path(profile_name).name
        if not profile_path.is_file():
            return
        self._base_hdr_profiles[display.key] = {"profile_name": profile_name, "profile_path": str(profile_path)}
        self.state.hdr.base_profile = str(profile_path)
        self.state.hdr.base_profile_name = profile_name
        self.state.hdr.imported_profile = str(profile_path)
        if load_controls:
            try:
                imported = import_profile(profile_path, "HDR")
                correction = self.state.hdr.sdr_gamma_correction
                self.state.hdr = imported.state
                self.state.hdr.sdr_gamma_correction = correction
                self.state.hdr.base_profile = str(profile_path)
                self.state.hdr.base_profile_name = profile_name
                self.state.hdr.imported_profile = str(profile_path)
                self._load_mode_into_controls("HDR")
                self.profile_path_edit.setText(str(profile_path))
            except Exception:
                pass
        self._save_state_now()

    def _cleanup_legacy_managed_profiles(self, display: DisplayInfo) -> None:
        """Remove only old Virtual HDR OSD timestamped/companion profiles.

        Profiles are removed by their installed filename, never by ICC description, so a
        user's original Windows HDR Calibration profile is not touched even when older
        app-generated copies inherited the same visible description.
        """
        names: set[str] = set()
        for entry in self._persisted_live_registry.values():
            if isinstance(entry, dict):
                name = str(entry.get("profile_name", ""))
                if name.startswith("VirtualHDR_OSD_") or name.startswith("Virtual_HDR_OSD_"):
                    names.add(name)
        if GAMMA_HOTKEY_STATE_PATH.is_file():
            try:
                payload = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))
                displays = payload.get("displays", {}) if isinstance(payload, dict) else {}
                for entry in displays.values() if isinstance(displays, dict) else ():
                    if not isinstance(entry, dict):
                        continue
                    profiles = entry.get("profiles", {})
                    if isinstance(profiles, dict):
                        for name in profiles.values():
                            if isinstance(name, str) and (name.startswith("VirtualHDR_OSD_") or name.startswith("Virtual_HDR_OSD_")):
                                names.add(name)
                    active = entry.get("active_profile")
                    if isinstance(active, str) and (active.startswith("VirtualHDR_OSD_") or active.startswith("Virtual_HDR_OSD_")):
                        names.add(active)
            except Exception:
                pass
        for name in names:
            try:
                remove_profile(name, display, "HDR")
            except Exception:
                pass

    def _working_profile_paths(self, display: DisplayInfo) -> tuple[Path, Path]:
        token = hashlib.sha256(display.key.encode("utf-8")).hexdigest()[:10]
        return (
            LIVE_ROOT / f"Virtual_HDR_OSD_{token}_Off.icm",
            LIVE_ROOT / f"Virtual_HDR_OSD_{token}_On.icm",
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

        # Capture the real Windows HDR profile only when it is not one of our two
        # stable working slots. All edits are always derived from this base.
        self._capture_current_hdr_base(display)
        state = self.state.hdr
        try:
            current_name = get_default_profile(display, "HDR")
        except Exception:
            current_name = ""
        if (current_name.startswith("VirtualHDR_OSD_") or current_name.startswith("Virtual_HDR_OSD_")) and state.base_profile_name:
            try:
                reapply_existing_default_profile(display, "HDR", state.base_profile_name)
            except Exception:
                pass
        self._cleanup_legacy_managed_profiles(display)
        off_path, on_path = self._working_profile_paths(display)
        selected_option = state.sdr_gamma_correction
        enabled = selected_option != "Off"

        try:
            variants: list[tuple[str, Path, ModeState, float | None]] = []

            off_state = ModeState.from_dict(state.to_dict(), "HDR")
            off_state.sdr_gamma_correction = "Off"
            off_state.profile_name = "Virtual HDR OSD - Working Profile (Correction Off)"
            variants.append(("Off", off_path, off_state, None))

            on_option = self._last_enabled_gamma_correction if self._last_enabled_gamma_correction != "Off" else "Auto (Recommended)"
            on_state = ModeState.from_dict(state.to_dict(), "HDR")
            on_state.sdr_gamma_correction = on_option
            on_state.profile_name = "Virtual HDR OSD - Working Profile (Correction On)"
            detected_white = None
            if on_option == "Auto (Recommended)":
                try:
                    detected_white = get_sdr_white_level_nits(display)
                except Exception:
                    detected_white = None
            on_white = resolve_white_level(on_option, detected_white)
            variants.append((on_option, on_path, on_state, on_white))

            installed: dict[str, tuple[str, Path]] = {}
            for label, path, variant, white in variants:
                # Stable filenames are intentionally reused. Remove the previous
                # app-owned copy before reinstalling it so Windows never accumulates
                # timestamped/numbered HDR profiles.
                try:
                    remove_profile(path.name, display, "HDR")
                except Exception:
                    pass
                transform = build_transform(variant, hdr=True, sdr_white_nits=white)
                path.write_bytes(build_profile("HDR", variant, transform))
                name = install_and_associate_profile(path, display, "HDR", make_default=False)
                installed[label] = (name, path)

            active_key = on_option if enabled else "Off"
            active_name, active_path = installed[active_key]
            reapply_existing_default_profile(display, "HDR", active_name)
        except Exception as exc:
            if reason == "Live update":
                self.state.live_mode = False
                self.live_timer.stop()
                with QSignalBlocker(self.live_checkbox):
                    self.live_checkbox.setChecked(False)
            self._set_status(f"{reason} failed for HDR: {exc}", "error")
            return

        key = f"{display.key}|HDR"
        self._live_profiles[(display.key, "HDR")] = (active_name, active_path, display)
        self._persisted_live_registry[key] = {
            "profile_name": active_name,
            "profile_path": str(active_path),
            "base_profile_name": self._base_hdr_profiles.get(display.key, {}).get("profile_name", state.base_profile_name),
            "base_profile_path": self._base_hdr_profiles.get(display.key, {}).get("profile_path", state.base_profile),
        }
        self._save_live_registry()
        self._write_gamma_runtime_state(display, installed, on_option, enabled, active_name, active_path)
        self._set_status(
            f"{reason}: updated the stable Virtual HDR OSD working profile for {display.friendly_name}. "
            f"Gamma correction is {'ON' if enabled else 'OFF'}; no timestamped profile was created.",
            "ok",
        )

    def _write_gamma_runtime_state(
        self,
        display: DisplayInfo,
        installed: dict[str, tuple[str, Path]],
        on_option: str,
        enabled: bool,
        active_name: str,
        active_path: Path,
    ) -> None:
        try:
            payload: dict[str, object] = {}
            if GAMMA_HOTKEY_STATE_PATH.is_file():
                try:
                    candidate = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = {}
            displays_state = payload.get("displays")
            if not isinstance(displays_state, dict):
                displays_state = {}
                payload["displays"] = displays_state
            base = self._base_hdr_profiles.get(display.key, {})
            off_name, off_path = installed["Off"]
            on_name, on_path = installed[on_option]
            displays_state[display.key] = {
                "display_name": display.friendly_name,
                "gdi_name": display.gdi_name,
                "selected": on_option,
                "enabled": enabled,
                "active_profile": active_name,
                "active_profile_path": str(active_path),
                "base_profile": base.get("profile_name", self.state.hdr.base_profile_name),
                "base_profile_path": base.get("profile_path", self.state.hdr.base_profile),
                "profiles": {"Off": off_name, "On": on_name},
                "paths": {"Off": str(off_path), "On": str(on_path)},
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            payload["schema"] = "virtual-hdr-osd-gamma-hotkeys-v2"
            GAMMA_HOTKEY_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _publish_gamma_runtime_intent(self, display: DisplayInfo) -> None:
        """Publish ON/OFF intent before profile generation to prevent watchdog races."""
        try:
            payload: dict[str, object] = {}
            if GAMMA_HOTKEY_STATE_PATH.is_file():
                try:
                    candidate = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = {}
            displays_state = payload.get("displays")
            if not isinstance(displays_state, dict):
                displays_state = {}
                payload["displays"] = displays_state
            entry = displays_state.get(display.key)
            if not isinstance(entry, dict):
                entry = {
                    "display_name": display.friendly_name,
                    "gdi_name": display.gdi_name,
                    "profiles": {},
                    "paths": {},
                }
                displays_state[display.key] = entry
            entry["selected"] = self._last_enabled_gamma_correction
            entry["enabled"] = self.state.hdr.sdr_gamma_correction != "Off"
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
            payload["schema"] = "virtual-hdr-osd-gamma-hotkeys"
            GAMMA_HOTKEY_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _update_gamma_runtime_state(self, display: DisplayInfo, profile_name: str, profile_path: Path) -> None:
        """Publish the exact profile currently active so the persistent watchdog never overrides the GUI.

        The standalone watchdog may be running while this application is open. Its periodic
        association recovery must follow the profile most recently applied by the GUI rather
        than restoring an older profile captured when the watchdog was installed.
        """
        try:
            payload: dict[str, object] = {}
            if GAMMA_HOTKEY_STATE_PATH.is_file():
                try:
                    candidate = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = {}
            displays_state = payload.get("displays")
            if not isinstance(displays_state, dict):
                displays_state = {}
                payload["displays"] = displays_state
            entry = displays_state.get(display.key)
            if not isinstance(entry, dict):
                entry = {
                    "display_name": display.friendly_name,
                    "gdi_name": display.gdi_name,
                    "profiles": {},
                    "paths": {},
                }
                displays_state[display.key] = entry
            entry["selected"] = self._last_enabled_gamma_correction
            entry["enabled"] = self.state.hdr.sdr_gamma_correction != "Off"
            entry["active_profile"] = profile_name
            entry["active_profile_path"] = str(profile_path)
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
            payload["schema"] = "virtual-hdr-osd-gamma-hotkeys"
            GAMMA_HOTKEY_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            # Runtime coordination is best-effort and must never prevent profile application.
            pass

    def _prepare_gamma_hotkey_profiles_for_current(self) -> None:
        # Legacy compatibility hook. Working OFF/ON profiles are now generated in
        # _apply_mode_profile using two stable filenames only.
        return

    def _prepare_gamma_hotkey_profiles(self, display: DisplayInfo) -> None:
        return

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
            transform = build_transform(state, hdr=True, sdr_white_nits=self._effective_sdr_white_nits())
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
        self.gamma_companion_timer.stop()
        self.mode_timer.stop()
        self.gamma_runtime_timer.stop()
        if self._hotkey_listener is not None:
            self._hotkey_listener.close()
        try:
            STATE_PATH.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass
        super().closeEvent(event)
