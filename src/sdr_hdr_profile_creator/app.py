from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QDialog,
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

from .controls import Card, ControlSpec, SliderControl
from .curves import build_transform
from .dialogs import GuideDialog, HelpDialog
from .gamma_correction import CORRECTION_OPTIONS, resolve_white_level
from .hotkeys import GammaHotkeyListener
from .hdr_display import capability_for_device_name
from .pattern_view import ControlBinding, PatternWindow
from .icc import (
    build_profile,
    content_digest,
    import_profile,
    is_app_generated,
    primaries_disagree,
    profile_primaries_xy,
)
from .model import ApplicationState, DisplayBinding, DisplayMode, ModeState
from .windows_api import (
    DisplayInfo,
    enumerate_displays,
    associate_profile,
    install_and_associate_profile,
    get_color_directory,
    open_windows_display_settings,
    open_windows_hdr_calibration_app,
    open_windows_color_profile_directory,
    get_default_profile,
    get_sdr_white_level_nits,
    list_installed_profiles,
    set_hdr_enabled,
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
# Where the standalone watchdog installs itself. Read only, to confirm that its
# installer actually ran; this app never writes there.
WATCHDOG_INSTALL_ROOT = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")
) / "ColorProfileModeWatchdog"
GAMMA_RUNTIME_SCHEMA = "virtual-hdr-osd-gamma-hotkeys-v2"

# SDR handling is deliberately opt-in. Third-party calibration suites (Calman,
# DisplayCAL, i1Profiler) install an SDR profile *and* run their own loader that
# re-asserts its VCGT; a second process re-associating profiles behind their back
# is how calibration silently breaks. AUTO reproduces the historical behaviour,
# UNMANAGED tells this app to keep its hands off SDR entirely.
SDR_AUTO = "Auto — restore whatever Windows had"
SDR_UNMANAGED = "Leave unmanaged (third-party calibration owns SDR)"

# Filename prefixes this app has ever used for its own managed HDR profiles.
# Cleanup matches on these only, so a user's own profile is never removed.
MANAGED_PREFIXES = ("VirtualHDR_OSD_", "Virtual_HDR_OSD_")


# Fields that change the generated profile bytes. Used for the unapplied-edits
# indicator; the authoritative check before reinstalling is a content digest.
EDIT_FIELDS = (
    "gamma", "brightness_trim", "contrast",
    "temperature", "tint", "saturation",
    "red_channel", "green_channel", "blue_channel",
    "sdr_gamma_correction", "base_profile",
    "minimum_luminance_nits", "peak_luminance_nits", "full_frame_luminance_nits",
)


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

        self._first_run = not STATE_PATH.is_file()
        self.state = self._load_last_state()
        self.state.current_mode = "HDR"
        self.state.live_mode = False
        self._loading_controls = False
        self._last_detected_mode: DisplayMode | None = None
        self._current_display_snapshot: DisplayInfo | None = None
        self._persisted_live_registry = self._load_live_registry()
        self._remembered_sdr_profiles: dict[str, str | None] = {}
        self._base_hdr_profiles: dict[str, dict[str, str]] = {}
        # name -> (mtime_ns, size, generated). Keyed by stat so a profile that is
        # reinstalled over the top of an old one is re-examined.
        self._generated_profile_cache: dict[str, tuple[int, int, bool]] = {}
        self._announced_base_divergence = ""
        self.control_widgets: dict[str, SliderControl] = {}
        self._pattern_window: "PatternWindow | None" = None
        self._last_enabled_gamma_correction = self.state.hdr.sdr_gamma_correction if self.state.hdr.sdr_gamma_correction != "Off" else "Auto (Recommended)"
        self._hotkey_listener: GammaHotkeyListener | None = None
        self._guide_dialog: GuideDialog | None = None

        # Installed-content cache. Keyed by profile filename, holding the digest of
        # the bytes Windows currently has, so an unchanged apply costs one
        # association call instead of an uninstall/reinstall round trip.
        self._installed_digests: dict[str, str] = {}
        self._legacy_cleaned: set[str] = set()
        self._applied_signature: str | None = None
        # True once the user picks a base with Import or Revert to Base. That choice
        # is the ICC tag template and must outlive an Apply.
        self._base_is_user_selected = False
        self._active_profile_name: str = ""

        self.setWindowTitle("Virtual HDR OSD for Windows")
        self.setMinimumSize(1080, 720)
        self.resize(1380, 880)
        try:
            self.setMicaEffectEnabled(True)
            self.setCustomBackgroundColor(QColor(246, 248, 252), QColor(18, 22, 30))
        except Exception:
            pass

        self.live_timer = QTimer(self)
        self.live_timer.setSingleShot(True)
        self.live_timer.setInterval(420)
        self.live_timer.timeout.connect(self._apply_live_edit)

        self.state_save_timer = QTimer(self)
        self.state_save_timer.setSingleShot(True)
        self.state_save_timer.setInterval(900)
        self.state_save_timer.timeout.connect(self._save_state_now)

        self.mode_timer = QTimer(self)
        self.mode_timer.setInterval(900)
        self.mode_timer.timeout.connect(self._poll_windows_mode)

        self._build_ui()
        self._load_mode_into_controls()

        self._hotkey_listener = GammaHotkeyListener(
            self._gamma_hotkey_disable,
            self._gamma_hotkey_enable,
            self._hotkey_registration_changed,
        )

        # If the standalone watchdog already owns Alt+1 / Alt+2, RegisterHotKey
        # intentionally fails here. In that case the GUI follows the shared runtime
        # state written by the watchdog, avoiding two processes fighting for one hotkey.
        self.gamma_runtime_timer = QTimer(self)
        self.gamma_runtime_timer.setInterval(450)
        self.gamma_runtime_timer.timeout.connect(self._sync_external_gamma_hotkey_state)
        self.gamma_runtime_timer.start()

        self._refresh_displays(initial=True)
        self.mode_timer.start()
        self._update_activity_bar()

        if self._first_run:
            QTimer.singleShot(400, self._show_guide)

    # ----------------------------------------------------------------------------------
    # State persistence

    def _load_last_state(self) -> ApplicationState:
        if not STATE_PATH.is_file():
            return ApplicationState.neutral()
        try:
            return ApplicationState.from_dict(json.loads(STATE_PATH.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ApplicationState.neutral()

    def _load_live_registry(self) -> dict[str, dict[str, str]]:
        if not LIVE_REGISTRY_PATH.is_file():
            return {}
        try:
            data = json.loads(LIVE_REGISTRY_PATH.read_text(encoding="utf-8-sig"))
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
        self._write_json_atomic(LIVE_REGISTRY_PATH, self._persisted_live_registry)

    @staticmethod
    def _write_json_atomic(path: Path, payload: object) -> bool:
        """Write JSON via a temporary file so a crash cannot leave a truncated file.

        The watchdog polls these files continuously; a half-written state file
        would be parsed as corrupt and silently ignored.

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

    def _save_state_now(self) -> None:
        self._write_json_atomic(STATE_PATH, self.state.to_dict())

    # ----------------------------------------------------------------------------------
    # Fluent UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, self.titleBar.height() + 10, 22, 16)
        root.setSpacing(12)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.addWidget(TitleLabel("Virtual HDR OSD for Windows", self))
        title_box.addWidget(CaptionLabel("A lightweight HDR pseudo-calibration OSD for Windows 11", self))
        heading.addLayout(title_box)
        heading.addStretch(1)
        guide_button = PrimaryPushButton("Getting Started", self)
        guide_button.setToolTip("Open the step-by-step walkthrough of the recommended calibration workflow.")
        guide_button.clicked.connect(self._show_guide)
        heading.addWidget(guide_button)
        watchdog_button = PushButton("Watchdog Settings…", self)
        watchdog_button.setToolTip("Install or remove the independent profile-association watchdog and persistent Alt+1 / Alt+2 gamma-correction hotkeys.")
        watchdog_button.clicked.connect(self._show_watchdog_settings)
        heading.addWidget(watchdog_button)
        help_button = PushButton("Help", self)
        help_button.setToolTip("Open the complete usage guide, control reference, and safety notes.")
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
        self._add_editor_page(
            "tonePage", "Tone & Brightness", self._build_tone_tab(),
            "Gamma, midtone brightness, contrast, and the optional SDR-in-HDR gamma correction.",
        )
        self._add_editor_page(
            "colorPage", "Color & White Balance", self._build_color_tab(),
            "White-point temperature, green/magenta tint, saturation, and per-channel RGB balance.",
        )
        self.tab_selector.setToolTip("Switch between tone/luminance adjustments and color/white-balance adjustments.")
        self.tab_selector.currentItemChanged.connect(self._show_editor_page)
        self.page_stack.currentChanged.connect(self._page_changed)
        self._show_editor_page("tonePage")
        editor_layout.addWidget(self.tab_selector, 0, Qt.AlignmentFlag.AlignLeft)
        editor_layout.addWidget(self.page_stack, 1)
        root.addWidget(editor, 1)

        root.addWidget(self._build_activity_bar())

        self.status_card = SimpleCardWidget(self)
        self.status_card.setBorderRadius(8)
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(16, 10, 16, 10)
        self.status_label = CaptionLabel("Ready", self.status_card)
        self.status_label.setToolTip("Current application status, Windows profile operation result, or error details.")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label, 1)
        root.addWidget(self.status_card)

    def _build_activity_bar(self) -> QWidget:
        """Persistent answer to 'what is actually active, and are my edits applied?'."""
        bar = SimpleCardWidget(self)
        bar.setBorderRadius(8)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(14)

        self.active_profile_label = CaptionLabel("Active HDR profile: not applied yet", bar)
        self.active_profile_label.setToolTip("The profile Windows currently has associated as the HDR default for the selected display.")
        self.active_profile_label.setWordWrap(True)
        layout.addWidget(self.active_profile_label, 1)

        self.hotkey_status_label = CaptionLabel("Hotkeys: checking…", bar)
        self.hotkey_status_label.setToolTip("Whether this window currently owns the global Alt+1 / Alt+2 gamma-correction hotkeys.")
        layout.addWidget(self.hotkey_status_label)

        self.dirty_label = StrongBodyLabel("No unapplied edits", bar)
        self.dirty_label.setToolTip("Whether the sliders differ from the profile currently installed in Windows.")
        layout.addWidget(self.dirty_label)
        return bar

    def _add_editor_page(self, route_key: str, text: str, page: QWidget, tooltip: str = "") -> None:
        page.setObjectName(route_key)
        self._editor_pages[route_key] = page
        self.page_stack.addWidget(page)
        self.tab_selector.addItem(
            routeKey=route_key,
            text=text,
            onClick=lambda _checked=False, key=route_key: self._show_editor_page(key),
        )
        if tooltip:
            # addItem returns None on some qfluentwidgets releases, so look the
            # freshly added tab up by route key instead of relying on a handle.
            item = self.tab_selector.items.get(route_key) if hasattr(self.tab_selector, "items") else None
            if item is not None:
                item.setToolTip(tooltip)

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
        """Three rows, grouped by consequence.

        Row 1 selects what is being edited, row 2 holds actions that touch only
        this app's own state, and row 3 holds the actions that write to Windows.
        Keeping that boundary visible is the point of the grouping.
        """
        bar = SimpleCardWidget(self)
        bar.setBorderRadius(12)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        display_row = QHBoxLayout()
        display_row.setSpacing(9)
        display_label = StrongBodyLabel("1 · Target Display", bar)
        display_label.setToolTip("The physical display whose Windows HDR profile will be edited and applied.")
        display_row.addWidget(display_label)
        self.display_combo = ComboBox(bar)
        self.display_combo.setMinimumWidth(290)
        self.display_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.display_combo.setToolTip("Select the active Windows display to target. HDR state and profile association are tracked per display.")
        self.display_combo.currentIndexChanged.connect(self._display_selected)
        display_row.addWidget(self.display_combo, 1)
        self.hdr_switch = SwitchButton(bar)
        self.hdr_switch.setOffText("HDR Off")
        self.hdr_switch.setOnText("HDR On")
        self.hdr_switch.setToolTip(
            "Turn Windows HDR on or off for the selected display, without leaving this app. "
            "Unlike Win + Alt + B this targets the display you picked above."
        )
        self.hdr_switch.checkedChanged.connect(self._hdr_switch_toggled)
        display_row.addWidget(self.hdr_switch)
        refresh_displays = PushButton("Refresh", bar)
        refresh_displays.setToolTip("Rescan active Windows displays and refresh the selected display information.")
        refresh_displays.clicked.connect(self._refresh_displays)
        display_row.addWidget(refresh_displays)
        display_settings = PushButton("Display Settings", bar)
        display_settings.setToolTip("Open the main Windows display settings page.")
        display_settings.clicked.connect(open_windows_display_settings)
        display_row.addWidget(display_settings)
        color_profiles = PushButton("Profile Folder", bar)
        color_profiles.setToolTip(r"Open the Windows ICC/ICM profile folder (System32\spool\drivers\color)")
        color_profiles.clicked.connect(open_windows_color_profile_directory)
        display_row.addWidget(color_profiles)
        layout.addLayout(display_row)

        profile_row = QHBoxLayout()
        profile_row.setSpacing(8)
        profile_label = StrongBodyLabel("2 · Profiles for this Display", bar)
        profile_label.setToolTip(
            "Pin which installed profile is this display's SDR profile and which is the HDR "
            "profile you want to edit. Both are remembered per monitor across restarts."
        )
        profile_row.addWidget(profile_label)

        sdr_label = BodyLabel("SDR", bar)
        sdr_label.setToolTip("The profile Windows should use when this display is in SDR mode.")
        profile_row.addWidget(sdr_label)
        self.sdr_profile_combo = ComboBox(bar)
        self.sdr_profile_combo.setMinimumWidth(210)
        self.sdr_profile_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.sdr_profile_combo.setToolTip(
            "The SDR profile for this display. Pinning it means the app can restore it on an "
            "HDR → SDR switch instead of hoping it observed the right one earlier. "
            "This app never edits your SDR profile."
        )
        self.sdr_profile_combo.currentTextChanged.connect(self._sdr_profile_chosen)
        profile_row.addWidget(self.sdr_profile_combo, 1)

        hdr_label = BodyLabel("HDR", bar)
        hdr_label.setToolTip("The HDR profile used as the editable base for the sliders.")
        profile_row.addWidget(hdr_label)
        self.hdr_profile_combo = ComboBox(bar)
        self.hdr_profile_combo.setMinimumWidth(210)
        self.hdr_profile_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hdr_profile_combo.setToolTip(
            "The HDR profile the sliders edit. Pick one and it loads immediately as the base — "
            "no import round trip. The app's own working profiles are excluded so your edits "
            "never compound on already-edited data."
        )
        self.hdr_profile_combo.currentTextChanged.connect(self._hdr_profile_chosen)
        profile_row.addWidget(self.hdr_profile_combo, 1)

        import_button = PushButton("Import…", bar)
        import_button.setToolTip("Use an HDR .icm or .icc file that is not installed in the Windows colour folder.")
        import_button.clicked.connect(self._import_profile)
        profile_row.addWidget(import_button)
        export_button = PushButton("Export Copy…", bar)
        export_button.setToolTip("Save the current HDR profile, with all slider corrections embedded, to a file of your choosing. Does not install anything.")
        export_button.clicked.connect(self._export_profile)
        profile_row.addWidget(export_button)
        layout.addLayout(profile_row)

        runtime_row = QHBoxLayout()
        runtime_row.setSpacing(12)
        runtime_label = StrongBodyLabel("3 · Edits & Apply", bar)
        runtime_label.setToolTip(
            "Manage your slider edits on the left; the two buttons on the right are the ones "
            "that install and associate the generated profile with Windows."
        )
        runtime_row.addWidget(runtime_label)
        self.live_checkbox = SwitchButton(bar)
        self.live_checkbox.setOffText("Live Apply")
        self.live_checkbox.setOnText("Live Apply")
        self.live_checkbox.setToolTip("Automatically regenerate and apply the HDR profile shortly after each slider change. Disable it when you want to make several edits before applying them manually.")
        self.live_checkbox.checkedChanged.connect(self._live_mode_toggled)
        runtime_row.addWidget(self.live_checkbox)
        self.automatic_mode_checkbox = SwitchButton(bar)
        self.automatic_mode_checkbox.setOffText("Automatic Mode Switching")
        self.automatic_mode_checkbox.setOnText("Automatic Mode Switching")
        self.automatic_mode_checkbox.setToolTip("Automatically follow Windows SDR/HDR transitions. On HDR → SDR the app restores the SDR profile pinned in row 2, or, on Auto, the one Windows previously had; with SDR set to Leave unmanaged it does nothing. On SDR → HDR it reapplies the active HDR profile. An SDR profile is never created, edited, or overwritten — only the association is set.")
        automatic_enabled = self.state.follow_windows_mode and self.state.auto_refresh_after_mode_change
        self.automatic_mode_checkbox.setChecked(automatic_enabled)
        self.automatic_mode_checkbox.checkedChanged.connect(self._automatic_mode_switching_toggled)
        runtime_row.addWidget(self.automatic_mode_checkbox)
        revert_button = PushButton("Revert to Base", bar)
        revert_button.setToolTip("Discard your slider edits and reload the selected HDR profile untouched.")
        revert_button.clicked.connect(self._revert_to_base)
        runtime_row.addWidget(revert_button)
        reset_button = PushButton("Reset All Sliders", bar)
        reset_button.setToolTip("Return every slider to its neutral default without changing which profile is selected.")
        reset_button.clicked.connect(self._reset_all_controls)
        runtime_row.addWidget(reset_button)
        self.patterns_button = PushButton("Test Patterns…", bar)
        self.patterns_button.setToolTip(
            "Fill the display with calibration patterns and adjust from the keyboard.\n"
            "Number keys switch pattern, Tab picks a control, arrows adjust, Esc exits."
        )
        self.patterns_button.clicked.connect(self._open_pattern_view)
        runtime_row.addWidget(self.patterns_button)
        runtime_row.addStretch(1)
        self.refresh_profile_button = PushButton("Reapply", bar)
        self.refresh_profile_button.setToolTip("Force a full reinstall of the current settings. Use this if Windows has dropped the HDR association, typically after a mode change or resume from sleep.")
        self.refresh_profile_button.clicked.connect(lambda: self._apply_mode_profile("Reapply", force=True))
        runtime_row.addWidget(self.refresh_profile_button)
        self.apply_profile_button = PrimaryPushButton("Apply Edits", bar)
        self.apply_profile_button.setToolTip("Install and associate the profile described by the sliders as they are right now.")
        self.apply_profile_button.clicked.connect(lambda: self._apply_mode_profile("Apply Edits"))
        runtime_row.addWidget(self.apply_profile_button)
        layout.addLayout(runtime_row)
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

    def _edit_signature(self) -> str:
        """Cheap fingerprint of everything that affects the generated profile."""
        state = self.state.hdr
        return json.dumps(
            {field: getattr(state, field) for field in EDIT_FIELDS},
            sort_keys=True,
            default=str,
        )

    def _update_activity_bar(self) -> None:
        if not hasattr(self, "dirty_label"):
            return
        self.active_profile_label.setText(self._describe_active_profile())

        if self._applied_signature is None:
            # Nothing has been applied yet in this session, which is not the same
            # as having edited something. Saying "unapplied edits" on a fresh
            # launch would be alarming and wrong.
            label, dirty = "Not applied this session", False
        elif self._applied_signature != self._edit_signature():
            label, dirty = "Unapplied edits", True
        else:
            label, dirty = "No unapplied edits", False

        self.dirty_label.setText(label)
        self.dirty_label.setStyleSheet(
            "StrongBodyLabel { padding: 4px 10px; border-radius: 6px; background: "
            + ("rgba(220, 154, 45, 0.20);" if dirty else "rgba(50, 170, 110, 0.16);")
            + " }"
        )
        self.apply_profile_button.setText("Apply Edits •" if dirty else "Apply Edits")

    def _describe_active_profile(self) -> str:
        """Describe what Windows actually has, never what our state wishes it had.

        The two can legitimately disagree — a previous session, or the watchdog,
        may have left a different variant associated — so the correction status
        is read off the active filename whenever that filename is one of ours.
        """
        name = self._active_profile_name
        if not name:
            return "Active HDR profile: none set by this app · Windows is using its own profile"
        if self._is_managed_profile(name):
            if name.endswith("_On.icm"):
                correction = "gamma correction ON"
            elif name.endswith("_Off.icm"):
                correction = "gamma correction OFF"
            else:
                correction = "legacy working profile"
            return f"Active HDR profile: {name}  ·  {correction}"
        return f"Active HDR profile: {name}  ·  not generated by this app"

    def _hotkey_registration_changed(self, ok: bool, detail: str) -> None:
        self.hotkey_status_label.setText(f"Hotkeys: {'Alt+1 / Alt+2 active' if ok else 'not owned by this window'}")
        self.hotkey_status_label.setToolTip(detail)
        if not ok:
            self._set_status(f"Global gamma hotkeys unavailable — {detail}", "warning")

    def _gamma_correction_changed(self, text: str) -> None:
        if self._loading_controls:
            return
        option = text if text in CORRECTION_OPTIONS else "Off"
        white = self._effective_sdr_white_nits(option)
        detail = f" · {white:.0f} nits" if white is not None else ""
        self._set_status(
            f"SDR-in-HDR gamma correction: {option}{detail}. Alt+1 disables; Alt+2 restores.",
            "warning" if option != "Off" else "ok",
        )
        # This dropdown is an explicit correction switch, not an editor trim. Apply it
        # immediately even when Live Apply is disabled so selecting Off can never leave
        # a previously corrected profile active.
        self._select_gamma_correction(option, "Gamma correction changed")

    def _select_gamma_correction(self, option: str, reason: str) -> None:
        """Single path for the dropdown and both hotkeys.

        Only the *active association* differs between Off and On, so the working
        pair is generated once and switching is a cheap default-profile swap.
        """
        self.state.hdr.sdr_gamma_correction = option
        if option != "Off":
            self._last_enabled_gamma_correction = option
        with QSignalBlocker(self.gamma_correction_combo):
            self.gamma_correction_combo.setCurrentText(option)
        self._save_state_now()
        self.live_timer.stop()

        display = self._selected_display()
        if display is not None:
            self._publish_gamma_runtime_intent(display)
            if display.current_mode == "HDR":
                self._apply_mode_profile(reason)
                return
        self._update_activity_bar()

    def _effective_sdr_white_nits(self, option: str | None = None) -> float | None:
        option = option if option is not None else self.state.hdr.sdr_gamma_correction
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
        self._set_status(
            "Alt+1: SDR-in-HDR gamma correction disabled. The uncorrected HDR profile is now authoritative.",
            "ok",
        )
        self._select_gamma_correction("Off", "Gamma hotkey OFF")

    def _gamma_hotkey_enable(self) -> None:
        target = self._last_enabled_gamma_correction if self._last_enabled_gamma_correction != "Off" else "Auto (Recommended)"
        self._set_status(f"Alt+2: SDR-in-HDR gamma correction enabled ({target}).", "warning")
        self._select_gamma_correction(target, "Gamma hotkey ON")

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
            payload = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8-sig"))
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
            active = entry.get("active_profile")
            if isinstance(active, str) and active:
                self._active_profile_name = active
            self._save_state_now()
            self._update_activity_bar()
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
        self._update_activity_bar()
        self._save_state_soon()
        self._queue_live_apply()

    def _save_state_soon(self) -> None:
        """Persist slider edits shortly after the user stops adjusting.

        Writing on every step would rewrite the file dozens of times during a
        drag; writing only on apply or on a clean close lost the edit whenever the
        process was killed.
        """
        self.state_save_timer.start()

    def _load_mode_into_controls(self) -> None:
        state = self.state.hdr
        self._loading_controls = True
        try:
            for key, control in self.control_widgets.items():
                control.set_value(float(getattr(state, key)), emit=False)
            if hasattr(self, "gamma_correction_combo"):
                self.gamma_correction_combo.setCurrentText(state.sdr_gamma_correction)
        finally:
            self._loading_controls = False
        self._update_activity_bar()

    def _reset_all_controls(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset All Sliders",
            "Return every slider to its neutral default?\n\n"
            "The loaded base profile is kept. Your current adjustments cannot be recovered afterwards.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._loading_controls = True
        try:
            for key, control in self.control_widgets.items():
                control.set_value(control.spec.default, emit=False)
                setattr(self.state.hdr, key, control.spec.default)
        finally:
            self._loading_controls = False
        self._save_state_now()
        self._set_status("All sliders reset to neutral. Apply Edits to install the neutral profile.", "ok")
        self._update_activity_bar()
        self._queue_live_apply()

    def _revert_to_base(self) -> None:
        base = self.state.hdr.base_profile or self.state.hdr.imported_profile
        if not base or not Path(base).is_file():
            self._set_status(
                "No base profile is available to revert to. Import an HDR profile first.",
                "error",
            )
            return
        answer = QMessageBox.question(
            self,
            "Revert to Base Profile",
            f"Discard your slider edits and reload:\n\n{base}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._load_profile_from_path(Path(base))

    def _automatic_mode_switching_toggled(self, checked: bool) -> None:
        # These legacy state fields are kept synchronized for backward-compatible
        # state/profile deserialization, but the GUI exposes one unambiguous control.
        self.state.follow_windows_mode = checked
        self.state.auto_refresh_after_mode_change = checked
        if checked:
            self._set_status("Automatic Mode Switching enabled. SDR/HDR transitions will restore the existing SDR association or reapply the active HDR profile as appropriate.", "ok")
        else:
            self._set_status("Automatic Mode Switching disabled. Windows mode changes will be detected for status only; no profile will be reapplied automatically.", "warning")
        self._save_state_now()

    def _live_mode_toggled(self, checked: bool) -> None:
        self.state.live_mode = checked
        if checked:
            self._set_status("Live Apply enabled. Slider changes are installed shortly after you stop adjusting.", "warning")
            # A one-shot kick, deliberately not live_timer.start(120): QTimer.start(int)
            # reassigns the interval property, which would permanently shorten the
            # debounce for every later edit in this session.
            QTimer.singleShot(120, self._apply_live_edit)
        else:
            self.live_timer.stop()
            self._set_status("Live Apply disabled. Use Apply Edits when you are ready to install your changes.", "ok")
        if self._guide_dialog is not None:
            self._guide_dialog.refresh_status()

    def _queue_live_apply(self) -> None:
        if self.state.live_mode:
            self.live_timer.start()

    def _apply_live_edit(self) -> None:
        if not self.state.live_mode:
            return
        self._apply_mode_profile("Live update")

    def _show_help(self) -> None:
        HelpDialog(self).exec()

    def _show_guide(self) -> None:
        actions = {
            "enable_hdr": lambda: self.hdr_switch.setChecked(True),
            "display_settings": open_windows_display_settings,
            "hdr_calibration_app": open_windows_hdr_calibration_app,
            "focus_profiles": self._highlight_profile_pickers,
            "import_profile": self._import_profile,
            "enable_live": lambda: self.live_checkbox.setChecked(True),
            "watchdog": self._show_watchdog_settings,
            "export_profile": self._export_profile,
        }
        checks = {
            "hdr_active": self._check_hdr_active,
            "profile_imported": self._check_profile_imported,
            "live_enabled": self._check_live_enabled,
        }
        dialog = GuideDialog(actions, checks, self)
        self._guide_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._guide_dialog = None

    def _highlight_profile_pickers(self) -> None:
        """Refresh and focus the profile row so the guide can point at it."""
        self._populate_profile_pickers()
        self.hdr_profile_combo.setFocus(Qt.FocusReason.OtherFocusReason)
        count = max(0, self.hdr_profile_combo.count())
        self._set_status(
            f"Profile row refreshed: {count} installed HDR profile(s) to choose from. "
            "Pick one and the sliders start editing it right away.",
            "ok",
        )

    def _check_hdr_active(self) -> tuple[bool, str]:
        display = self._selected_display()
        if display is None:
            return False, "No display is selected yet. Use Refresh in the Target Display row."
        if display.current_mode == "HDR":
            return True, f"HDR is active on {display.friendly_name}."
        if not display.advanced_color_supported:
            return False, f"{display.friendly_name} does not report HDR support to Windows."
        return False, f"HDR is currently off for {display.friendly_name}."

    def _check_profile_imported(self) -> tuple[bool, str]:
        base = self.state.hdr.base_profile or self.state.hdr.imported_profile
        binding = self._selected_binding()
        sdr = (binding.sdr_profile if binding else "") or SDR_AUTO
        sdr_note = {
            SDR_AUTO: "SDR: Auto",
            SDR_UNMANAGED: "SDR: left to your calibration software",
        }.get(sdr, f"SDR: {sdr}")
        if base and Path(base).is_file():
            return True, f"HDR base: {Path(base).name}  ·  {sdr_note}"
        return False, f"No HDR profile is selected for this display yet.  ·  {sdr_note}"

    def _check_live_enabled(self) -> tuple[bool, str]:
        if self.state.live_mode:
            return True, "Live Apply is on. Slider changes install automatically."
        return False, "Live Apply is off. Turn it on, or use Apply Edits after each change."

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
        installing = name.startswith("2-")
        if installing:
            # The watchdog switches between the Off/On working pair by name. Installing
            # it before those profiles exist leaves it with nothing to restore, so make
            # sure the pair is present and recorded first.
            display = self._selected_display()
            if display is not None and display.current_mode == "HDR":
                self._apply_mode_profile("Watchdog install")
            elif display is not None:
                self._set_status(
                    "Enable HDR for this display before installing the watchdog, so the Off/On working profiles can be prepared.",
                    "warning",
                )
        path = RESOURCE_ROOT / name
        if not path.is_file():
            # Source-tree fallback.
            candidate = PACKAGE_ROOT.parents[1] / name
            path = candidate if candidate.is_file() else path
        if not path.is_file():
            QMessageBox.critical(self, "Watchdog Settings", f"Watchdog script not found:\n{path}")
            return
        # `start` detaches immediately and discards the installer's exit code and its
        # console output, so a failed install was indistinguishable from a successful
        # one: the button appeared to do nothing at all. Give the installer its own
        # visible console instead, then verify the outcome and report it.
        before = self._watchdog_script_stamp()
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", str(path)],
                cwd=str(path.parent),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Watchdog Settings", f"Could not launch watchdog setup:\n\n{exc}")
            return
        self._set_status(
            f"Running {name} in a separate window. Follow its prompts; the result is "
            "reported here when it finishes.",
            "warning",
        )
        QTimer.singleShot(9000, lambda: self._report_watchdog_outcome(installing, before))

    @staticmethod
    def _watchdog_script_stamp() -> float:
        """Modification time of the installed watchdog script, or 0 when absent."""
        try:
            return (WATCHDOG_INSTALL_ROOT / "Watchdog.ps1").stat().st_mtime
        except OSError:
            return 0.0

    def _report_watchdog_outcome(self, installing: bool, before: float) -> None:
        """Say whether the installer actually changed anything."""
        after = self._watchdog_script_stamp()
        if installing:
            if after > before:
                self._set_status(
                    "Watchdog installed. It now keeps your SDR/HDR associations stable and "
                    "owns Alt+1 / Alt+2.",
                    "ok",
                )
            else:
                self._set_status(
                    "Watchdog install did not complete — the installed script was not "
                    "updated. Check the installer window for an error message.",
                    "error",
                )
            return
        if after == 0.0:
            self._set_status("Watchdog uninstalled.", "ok")
        else:
            self._set_status(
                "Watchdog uninstall did not complete — its files are still present. "
                "Check the uninstaller window for an error message.",
                "error",
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
            self._sync_display_widgets(selected)
            self._sync_active_profile_from_windows(selected)
            self._set_status(f"Detected {len(displays)} active display(s). Selected {selected.friendly_name}.", "ok")
            self._warn_if_panel_gamut_changed(selected)

    def _display_selected(self, _index: int) -> None:
        selected = self.display_combo.currentData()
        if not isinstance(selected, DisplayInfo):
            return
        self.state.selected_display_key = selected.key
        self._current_display_snapshot = selected
        self._last_detected_mode = None
        self._remember_current_sdr_profile(selected)
        self._update_mode_badge(selected)
        self._sync_display_widgets(selected)
        self._sync_active_profile_from_windows(selected)

    def _sync_display_widgets(self, display: DisplayInfo) -> None:
        """Refresh the per-display widgets after the target or its mode changes."""
        if not hasattr(self, "hdr_switch"):
            return
        with QSignalBlocker(self.hdr_switch):
            self.hdr_switch.setChecked(display.current_mode == "HDR")
        self.hdr_switch.setEnabled(display.advanced_color_supported)
        self._populate_profile_pickers()

    def _sync_active_profile_from_windows(self, display: DisplayInfo) -> None:
        """Report the HDR default Windows actually has, not what we last wrote."""
        try:
            current = get_default_profile(display, "HDR")
        except Exception:
            current = ""
        self._active_profile_name = Path(current).name if current else ""
        self._update_activity_bar()

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
        if hasattr(self, "hdr_switch"):
            with QSignalBlocker(self.hdr_switch):
                self.hdr_switch.setChecked(selected.current_mode == "HDR")
        detected: DisplayMode = "HDR" if selected.advanced_color_enabled else "SDR"
        previous = self._last_detected_mode
        self._last_detected_mode = detected
        if previous is None or previous == detected:
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
            # Windows can drop the association across the transition, so force a
            # full reinstall rather than trusting the installed-content cache.
            QTimer.singleShot(650, lambda: self._apply_mode_profile("Automatic Mode Switching", force=True))

    # ----------------------------------------------------------------------------------
    # Per-display profile bindings

    def _selected_binding(self) -> "DisplayBinding | None":
        display = self._selected_display()
        if display is None:
            return None
        binding = self.state.binding(display.stable_key)
        binding.display_label = display.friendly_name
        return binding

    def _populate_profile_pickers(self) -> None:
        """Refill both pickers from the Windows colour directory.

        The app's own working profiles are excluded from the HDR list: selecting
        one as the base would mean editing already-edited data.
        """
        display = self._selected_display()
        if display is None:
            return
        binding = self.state.binding(display.stable_key)
        installed = [
            name for name in list_installed_profiles()
            if not self._is_generated_profile(name)
        ]
        if self._is_generated_profile(binding.hdr_profile):
            # An older build listed its own output as a selectable base and this
            # display was pinned to one. That pin outranks the Windows default
            # permanently, so recalibrating -- with Windows HDR Calibration, or
            # with Calman writing a new profile and making it the default -- left
            # the app still editing the superseded one. Settings already loaded
            # from it are kept; only the pin goes.
            binding.hdr_profile = ""
            self._save_state_soon()

        self._loading_controls = True
        try:
            with QSignalBlocker(self.sdr_profile_combo):
                self.sdr_profile_combo.clear()
                self.sdr_profile_combo.addItems([SDR_AUTO, SDR_UNMANAGED, *installed])
                current = binding.sdr_profile or SDR_AUTO
                index = self.sdr_profile_combo.findText(current)
                if index < 0:
                    # A pinned profile that has since been uninstalled.
                    self.sdr_profile_combo.addItem(current)
                    index = self.sdr_profile_combo.count() - 1
                self.sdr_profile_combo.setCurrentIndex(max(0, index))

            with QSignalBlocker(self.hdr_profile_combo):
                self.hdr_profile_combo.clear()
                entries = list(installed)
                chosen = binding.hdr_profile or Path(self.state.hdr.base_profile or "").name
                # An imported file living outside the colour directory still needs a row.
                if chosen and chosen not in entries:
                    entries.append(chosen)
                self.hdr_profile_combo.addItems(entries)
                if chosen:
                    index = self.hdr_profile_combo.findText(chosen)
                    if index >= 0:
                        self.hdr_profile_combo.setCurrentIndex(index)
        finally:
            self._loading_controls = False

    def _sdr_profile_chosen(self, text: str) -> None:
        if self._loading_controls or not text:
            return
        binding = self._selected_binding()
        if binding is None:
            return
        binding.sdr_profile = "" if text == SDR_AUTO else text
        self._save_state_now()

        if text == SDR_AUTO:
            self._set_status(
                "SDR profile: following whatever Windows already has associated.", "ok"
            )
        elif text == SDR_UNMANAGED:
            self._set_status(
                "SDR profile left unmanaged. This app will not touch the SDR association, "
                "so your calibration software keeps full ownership of it.",
                "ok",
            )
        else:
            self._set_status(
                f"SDR profile pinned to {text}. It will be restored when Windows drops back "
                "to SDR. Nothing is applied to SDR right now.",
                "ok",
            )

    def _hdr_profile_chosen(self, text: str) -> None:
        """Selecting an HDR profile loads it as the base immediately."""
        if self._loading_controls or not text:
            return
        binding = self._selected_binding()
        if binding is None:
            return
        binding.hdr_profile = text
        candidate = Path(text)
        if not candidate.is_file():
            try:
                candidate = get_color_directory() / text
            except Exception:
                self._set_status(f"Could not locate {text} in the Windows colour folder.", "error")
                return
        if not candidate.is_file():
            self._set_status(f"{text} is no longer installed.", "error")
            return
        self._load_profile_from_path(candidate)

    def _hdr_switch_toggled(self, checked: bool) -> None:
        display = self._selected_display()
        if display is None:
            self._set_status("Select a display first.", "error")
            return
        if checked == (display.current_mode == "HDR"):
            return
        try:
            set_hdr_enabled(display, checked)
        except Exception as exc:
            self._set_status(f"Could not turn HDR {'on' if checked else 'off'}: {exc}", "error")
            with QSignalBlocker(self.hdr_switch):
                self.hdr_switch.setChecked(display.current_mode == "HDR")
            return
        self._set_status(
            f"HDR turned {'on' if checked else 'off'} for {display.friendly_name}. "
            "Waiting for Windows to settle…",
            "warning",
        )
        # Let the mode poller pick the transition up and run the normal handling.
        QTimer.singleShot(700, self._refresh_displays)

    def _remember_current_sdr_profile(self, display: DisplayInfo) -> None:
        """Remember Windows' existing STANDARD profile without modifying any association."""
        try:
            profile_name = get_default_profile(display, "SDR")
        except Exception:
            profile_name = None
        self._remembered_sdr_profiles[display.key] = profile_name or None

    def _restore_remembered_sdr_profile(self, display: DisplayInfo, reason: str) -> None:
        """Reapply the SDR profile for this display; never create a neutral fallback.

        A pinned binding wins over what happened to be observed earlier, and an
        explicit "unmanaged" binding suppresses the restore entirely so that
        third-party calibration software keeps sole ownership of SDR.
        """
        binding = self.state.display_bindings.get(display.stable_key)
        pinned = binding.sdr_profile if binding else ""

        if pinned == SDR_UNMANAGED:
            self._set_status(
                f"{reason}: SDR is set to unmanaged, so the SDR association was left untouched.",
                "ok",
            )
            return

        profile_name = pinned or self._remembered_sdr_profiles.get(display.key) or ""
        if not profile_name:
            self._set_status(
                f"{reason}: no SDR profile is pinned for this display and none was observed, "
                "so nothing was applied. Pin one in the Profiles row to make this reliable.",
                "warning",
            )
            return

        # If Windows already has it, do not re-assert it — a redundant association
        # write is exactly what fights a third-party calibration loader.
        try:
            if Path(get_default_profile(display, "SDR")).name.casefold() == Path(profile_name).name.casefold():
                self._set_status(f"{reason}: Windows already has {profile_name} for SDR.", "ok")
                return
        except Exception:
            pass

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
        if self._guide_dialog is not None:
            self._guide_dialog.refresh_status()

    @staticmethod
    def _is_managed_profile(name: str) -> bool:
        return any(name.startswith(prefix) for prefix in MANAGED_PREFIXES)

    def _is_generated_profile(self, name: str) -> bool:
        """True for any profile this app produced, whatever it ended up called.

        The filename prefixes only recognise what *this* build writes. Releases
        before the stable working-profile names installed their output as
        ``<base>_HDR.icm``, so for anything already on disk the content check is
        the one that counts.

        Such a profile in the colour directory is a live working profile, not a
        calibration source: it is this app's own output for the display and is
        rewritten on every Apply. Listing one as a base is how a display ends up
        pinned to it, and a pin outranks the Windows default, so a freshly
        calibrated profile can then never be adopted. Import is deliberately not
        filtered -- loading a saved copy restores its exact settings.
        """
        if not name:
            return False
        if self._is_managed_profile(Path(name).name):
            return True
        path = Path(name)
        if not path.is_file():
            try:
                path = get_color_directory() / path.name
            except Exception:
                return False
        try:
            stamp = path.stat()
        except OSError:
            return False
        cached = self._generated_profile_cache.get(path.name)
        if cached is not None and cached[0] == stamp.st_mtime_ns and cached[1] == stamp.st_size:
            return cached[2]
        generated = is_app_generated(path)
        self._generated_profile_cache[path.name] = (stamp.st_mtime_ns, stamp.st_size, generated)
        return generated

    # Sliders worth reaching without leaving a fullscreen pattern. Deliberately short:
    # every one costs a line of the overlay, and the overlay shares the screen with the
    # patch. Colour trims are omitted because they are judged against a reference the
    # patterns cannot supply.
    PATTERN_VIEW_CONTROLS = ("gamma", "brightness_trim", "contrast")

    def _pattern_view_bindings(self) -> list["ControlBinding"]:
        bindings: list[ControlBinding] = []
        for key in self.PATTERN_VIEW_CONTROLS:
            control = self.control_widgets.get(key)
            if control is None:
                continue
            bindings.append(ControlBinding(
                key=key,
                label=control.spec.title,
                read=control.value,
                # emit=True so Live Apply sees the change; adjusting a pattern that does
                # not update the display would be worse than having no controls at all.
                nudge=lambda delta, c=control: c.set_value(c.value() + delta, emit=True),
                step=float(control.spec.step),
                suffix=control.spec.suffix,
            ))
        return bindings

    # Each threshold pattern answers exactly one of the three luminance figures a
    # generated profile carries: minimum and peak go into the MHC2 header, full frame into
    # the lumi tag. Measuring something the profile does not then record would be pointless.
    MEASUREMENT_FIELDS = {
        "black-level": "minimum_luminance_nits",
        "peak-white": "peak_luminance_nits",
        "full-frame-white": "full_frame_luminance_nits",
    }

    def _record_measurement(self, pattern_key: str, nits: float) -> None:
        """Store a reading taken in the pattern view, so Apply writes it into the profile.

        These are exactly the numbers Windows HDR Calibration produces, and until now the
        app could only inherit them from whatever profile it was handed. Bounds match
        import_profile's, so a measured value and an imported one are constrained alike.
        """
        field = self.MEASUREMENT_FIELDS.get(pattern_key)
        if field is None:
            return
        value = float(nits)
        if field == "minimum_luminance_nits":
            value = max(0.0, min(100.0, value))
        else:
            value = max(80.0, min(10000.0, value))
        setattr(self.state.hdr, field, value)
        # Full frame cannot exceed peak; a panel that did that would be reporting fiction.
        if self.state.hdr.full_frame_luminance_nits > self.state.hdr.peak_luminance_nits:
            self.state.hdr.full_frame_luminance_nits = self.state.hdr.peak_luminance_nits
        self._save_state_soon()
        self._update_activity_bar()

        warning = self._implausible_measurement(pattern_key)
        if warning:
            self._set_status(warning, "warning")
            return
        self._set_status(
            f"Recorded {value:g} nits as {field.replace('_', ' ')}. Apply Edits writes it "
            "into the profile.",
            "ok",
        )

    # A full-frame reading this close to peak means the display never actually clipped.
    FULL_FRAME_IMPLAUSIBLE_RATIO = 0.85

    def _implausible_measurement(self, pattern_key: str) -> str:
        """Say what a full-frame reading equal to peak actually means.

        These steps find the level at which the display stops separating two adjacent
        values -- a clipping point. That is a real measurement and it is what Windows HDR
        Calibration records too, but it is not the same quantity as sustained full-field
        luminance. What clips is usually the display's own tone-mapping curve, and that
        curve does not move with window size, so a panel whose full-field output is a
        fraction of its peak can still clip at the same signal level either way.

        Reporting the two as equal is therefore not a failed measurement and must not be
        called one. It does mean the number describes signal handling rather than how much
        light the panel sustains, which is worth saying before it goes into a profile.
        """
        if pattern_key != "full-frame-white":
            return ""
        peak = self.state.hdr.peak_luminance_nits
        full_frame = self.state.hdr.full_frame_luminance_nits
        if peak <= 0 or full_frame < peak * self.FULL_FRAME_IMPLAUSIBLE_RATIO:
            return ""
        return (
            f"Recorded {full_frame:g} nits full-frame, close to the {peak:g} peak. That is "
            "a genuine clipping point, but it means this display clips at the same level "
            "whatever the window size -- its tone-mapping curve, not the panel running out "
            "of light. An emissive panel sustains far less than peak across a whole screen, "
            "so treat this as signal handling rather than brightness. A meter would read "
            "lower, and an HGIG or tone-mapping-off mode in the monitor's menu would "
            "separate the two."
        )

    def _open_pattern_view(self) -> None:
        display = self._selected_display()
        if display is None:
            self._set_status("Select a display first.", "error")
            return
        capability = capability_for_device_name(display.gdi_name)
        try:
            sdr_white = get_sdr_white_level_nits(display)
        except Exception:
            sdr_white = 240.0

        # Adjusting a control that cannot change the display is pointless, and live mode
        # is forced off at startup, so without this the tone steps silently did nothing.
        previous_live = self.state.live_mode
        self.state.live_mode = True
        window = PatternWindow(
            capability, sdr_white, self._pattern_view_bindings(),
            measure=self._record_measurement,
            on_close=lambda: self._restore_live_mode(previous_live),
            apply=self._apply_from_pattern_view,
        )
        screen = self._screen_for(display)
        if screen is not None:
            window.setGeometry(screen.geometry())
        window.showFullScreen()
        # The swapchain needs the real window handle and its final size, so it is created
        # after the window is on screen rather than in the constructor.
        QApplication.processEvents()
        if not window.begin():
            window.close()
            self._set_status(
                f"Calibration patterns need an HDR surface, which this display did not "
                f"provide: {window.failure}",
                "error",
            )
            return
        window.setFocus(Qt.FocusReason.OtherFocusReason)
        self._pattern_window = window
        self._set_status(
            "Calibration patterns are on screen. Number keys switch pattern, Tab picks a "
            "control, arrows adjust, Esc exits.",
            "ok",
        )

    def _apply_from_pattern_view(self) -> bool:
        """Apply without leaving the patterns, so the readings are not lost on exit."""
        display = self._selected_display()
        if display is None:
            return False
        try:
            self._apply_mode_profile("Calibration measurements", force=True)
        except Exception:
            return False
        return True

    def _restore_live_mode(self, previous: bool) -> None:
        """Put Live Apply back as the user had it once the patterns are gone."""
        self.state.live_mode = previous
        if hasattr(self, "live_checkbox"):
            with QSignalBlocker(self.live_checkbox):
                self.live_checkbox.setChecked(previous)

    @staticmethod
    def _screen_for(display: DisplayInfo):
        """Match a Windows display to the Qt screen sitting on it."""
        for screen in QApplication.screens():
            if screen.name() == display.gdi_name:
                return screen
        return QApplication.primaryScreen()

    def _warn_if_panel_gamut_changed(self, display: DisplayInfo) -> None:
        """Say so when the HDR base profile no longer describes the panel.

        A monitor's colour-space mode is set in its own OSD, where nothing on the PC can
        watch it change. Switch a display from DCI-P3 to sRGB and every HDR profile on it
        silently describes a panel that is no longer there, with no error anywhere.

        Only the HDR base is compared, and only while HDR is on. A pinned SDR profile is
        frequently and correctly targeted at BT.709 even on a wide-gamut panel -- Calman
        does exactly that -- so comparing it here would warn about a display that is set
        up properly. DXGI likewise reports the current mode's colour volume, so its
        primaries mean nothing about HDR while the display is in SDR.
        """
        if display.current_mode != "HDR":
            return
        capability = capability_for_device_name(display.gdi_name)
        if capability is None or not capability.is_hdr:
            return
        base = self.state.hdr.base_profile
        if not base:
            return
        try:
            described = profile_primaries_xy(Path(base).read_bytes())
        except OSError:
            return
        if described is None:
            return
        panel = (capability.red_primary, capability.green_primary, capability.blue_primary)
        worst = primaries_disagree(described[:3], panel)
        if not worst:
            return
        self._set_status(
            f"{Path(base).name} describes different primaries than {display.friendly_name} "
            f"now reports (up to {worst:.3f} xy apart). If you changed the monitor's colour "
            "space in its own menu, recalibrate or pick a profile made for the new mode.",
            "warning",
        )

    def _announce_diverged_base(self, pinned: str, windows_default: str) -> None:
        """Say once that Windows' HDR default no longer matches the pinned base.

        Repeated on every poll this would bury every other message, so each new
        default is reported a single time.
        """
        name = Path(windows_default).name
        if self._announced_base_divergence == name:
            return
        self._announced_base_divergence = name
        self._set_status(
            f"Windows now uses {name} as the HDR profile for this display, but "
            f"{Path(pinned).name} stays pinned as the base being edited. Choose {name} "
            "in the HDR box to build on the newer calibration instead.",
            "warning",
        )

    def _capture_current_hdr_base(self, display: DisplayInfo, *, load_controls: bool = False) -> None:
        """Remember the real Windows HDR default as the fallback source profile.

        App-managed working profiles are never promoted to base profiles. This prevents
        repeated HDR transitions from recursively editing an already edited profile.

        The Windows default is always recorded for this display, because the watchdog and
        the park-before-reinstall step need a profile that is genuinely installed. It only
        becomes the *editing* base when the user has not chosen one: a profile picked with
        Import is the ICC tag template every generated profile is built from, and silently
        replacing it here made Apply produce the colorimetry of the Windows default while
        the path field still showed the imported filename.
        """
        try:
            profile_name = get_default_profile(display, "HDR")
        except Exception:
            return
        if not profile_name or self._is_managed_profile(Path(profile_name).name):
            return
        try:
            profile_path = get_color_directory() / Path(profile_name).name
        except Exception:
            return
        if not profile_path.is_file():
            return
        self._base_hdr_profiles[display.key] = {"profile_name": profile_name, "profile_path": str(profile_path)}

        binding = self.state.display_bindings.get(display.stable_key)
        if binding is not None and self._is_generated_profile(binding.hdr_profile):
            # Never let one of our own profiles hold the pin; see _is_generated_profile.
            binding.hdr_profile = ""
        if binding is not None and binding.hdr_profile and not load_controls:
            # The user pinned an HDR profile for this display; it outranks whatever
            # Windows currently happens to have as the default. Say so when the two
            # diverge, or recalibrating in Calman or Windows HDR Calibration looks
            # like the app quietly ignoring the new profile.
            if Path(profile_name).name != Path(binding.hdr_profile).name:
                self._announce_diverged_base(binding.hdr_profile, profile_name)
            self._save_state_now()
            return
        if not load_controls and self._base_is_user_selected:
            chosen = self.state.hdr.base_profile
            if chosen and Path(chosen).is_file():
                self._save_state_now()
                return

        self.state.hdr.base_profile = str(profile_path)
        self.state.hdr.base_profile_name = profile_name
        self.state.hdr.imported_profile = str(profile_path)
        # Adopting the Windows default as the base replaces any earlier import.
        self._base_is_user_selected = False
        if binding is not None:
            # The picker reads the binding. Leaving the previous value here let it
            # name one profile while the sliders were editing another.
            binding.hdr_profile = Path(profile_name).name
        if load_controls:
            try:
                imported = import_profile(profile_path, "HDR")
                correction = self.state.hdr.sdr_gamma_correction
                self.state.hdr = imported.state
                self.state.hdr.sdr_gamma_correction = correction
                self.state.hdr.base_profile = str(profile_path)
                self.state.hdr.base_profile_name = profile_name
                self.state.hdr.imported_profile = str(profile_path)
                self._load_mode_into_controls()
                self._populate_profile_pickers()
            except Exception:
                pass
        self._save_state_now()

    def _cleanup_legacy_managed_profiles(self, display: DisplayInfo) -> None:
        """Remove old Virtual HDR OSD timestamped/companion profiles, once per session.

        Profiles are removed by their installed filename, never by ICC description, so a
        user's original Windows HDR Calibration profile is not touched even when older
        app-generated copies inherited the same visible description.

        The candidate names are gathered from the registry and runtime state, which cover
        *every* display this app has ever managed, across every past session. Two kinds of
        entry must survive: the current display's pair, and the pair belonging to any other
        display Windows currently reports — on a multi-monitor system, uninstalling the
        latter would silently break the calibration of a display the user is not editing.

        Everything else goes, and that deliberately includes stale pairs whose filename
        token no longer matches any attached display. The token is a hash of the display
        key, which embeds the adapter LUID, and Windows reissues adapter LUIDs across
        reboots and driver restarts — so without this the colour directory accumulates an
        orphaned pair every time the LUID changes.
        """
        if display.key in self._legacy_cleaned:
            return
        self._legacy_cleaned.add(display.key)
        names: set[str] = set()
        for entry in self._persisted_live_registry.values():
            if isinstance(entry, dict):
                name = str(entry.get("profile_name", ""))
                if self._is_managed_profile(name):
                    names.add(name)
        if GAMMA_HOTKEY_STATE_PATH.is_file():
            try:
                payload = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8-sig"))
                displays = payload.get("displays", {}) if isinstance(payload, dict) else {}
                for entry in displays.values() if isinstance(displays, dict) else ():
                    if not isinstance(entry, dict):
                        continue
                    profiles = entry.get("profiles", {})
                    if isinstance(profiles, dict):
                        for name in profiles.values():
                            if isinstance(name, str) and self._is_managed_profile(name):
                                names.add(name)
                    active = entry.get("active_profile")
                    if isinstance(active, str) and self._is_managed_profile(active):
                        names.add(active)
            except Exception:
                pass
        # The registry records only whichever variant was active, so an orphaned pair
        # leaves its sibling named nowhere. Derive it: the sibling of an app-owned
        # working profile is by definition also app-owned.
        for name in list(names):
            if name.endswith("_Off.icm"):
                names.add(name[: -len("_Off.icm")] + "_On.icm")
            elif name.endswith("_On.icm"):
                names.add(name[: -len("_On.icm")] + "_Off.icm")

        # The current display is always protected, even if enumeration fails here,
        # so a transient failure can never uninstall what we are about to activate.
        protected = {path.name for path in self._working_profile_paths(display)}
        try:
            for attached in enumerate_displays():
                protected.update(path.name for path in self._working_profile_paths(attached))
        except Exception:
            pass
        # Also spare every display the user has configured, attached or not. A monitor
        # that is switched off, unplugged, or on an inactive input is not enumerated,
        # and deleting its pair would silently discard its calibration until the next
        # time it happens to be connected while the app is open.
        for stable_key in self.state.display_bindings:
            protected.update(path.name for path in self._working_profile_paths_for(stable_key))
        for name in names - protected:
            try:
                remove_profile(name, display, "HDR")
            except Exception:
                pass

    @staticmethod
    def _working_profile_paths_for(stable_key: str) -> tuple[Path, Path]:
        # Keyed on stable_key, not key: `key` embeds the adapter LUID, which Windows
        # reissues on reboot, so the filename changed every boot and orphaned the
        # previous pair in the Windows colour directory — one leaked profile per
        # reboot, in the directory this app promises holds at most two.
        #
        # Taking the key rather than a DisplayInfo means the names of a display that
        # is not attached right now are still derivable, which is what lets cleanup
        # spare a monitor that is merely switched off.
        token = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:10]
        return (
            LIVE_ROOT / f"Virtual_HDR_OSD_{token}_Off.icm",
            LIVE_ROOT / f"Virtual_HDR_OSD_{token}_On.icm",
        )

    def _working_profile_paths(self, display: DisplayInfo) -> tuple[Path, Path]:
        return self._working_profile_paths_for(display.stable_key)

    def _toggle_windows_mode(self) -> None:
        try:
            display = self._selected_display()
            if display is not None and display.current_mode == "HDR":
                self._remember_current_sdr_profile(display)
            send_hdr_toggle_shortcut()
            self._set_status("Sent Win + Alt + B. Waiting for Windows to settle…", "warning")
        except Exception as exc:
            self._set_status(f"Could not toggle SDR/HDR: {exc}", "error")

    @staticmethod
    def _safe_stem(text: str, fallback: str) -> str:
        cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in text.strip())
        return cleaned.strip("._")[:100] or fallback

    # ----------------------------------------------------------------------------------
    # Profile generation and installation

    def _build_working_payloads(self, display: DisplayInfo) -> tuple[dict[str, tuple[Path, bytes]], str]:
        """Generate both working variants from the current editor state.

        Two profiles with stable filenames are always produced — one with the
        SDR-in-HDR correction off and one with it on — so that toggling the
        correction never has to regenerate or reinstall anything.
        """
        state = self.state.hdr
        off_path, on_path = self._working_profile_paths(display)

        off_state = ModeState.from_dict(state.to_dict(), "HDR")
        off_state.sdr_gamma_correction = "Off"
        off_state.profile_name = "Virtual HDR OSD - Working Profile (Correction Off)"

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

        payloads = {
            "Off": (off_path, build_profile("HDR", off_state, build_transform(off_state, hdr=True, sdr_white_nits=None))),
            "On": (on_path, build_profile("HDR", on_state, build_transform(on_state, hdr=True, sdr_white_nits=on_white))),
        }
        return payloads, on_option

    def _installed_matches(self, path: Path, digest: str) -> bool:
        """True when Windows already has byte-identical content installed.

        Hashing the installed copy rather than trusting the in-memory cache means
        the fast path also survives an app restart, and correctly misses when
        something outside this app has replaced the file.
        """
        try:
            installed = get_color_directory() / path.name
            if not installed.is_file():
                return False
            if self._installed_digests.get(path.name) == digest:
                return True
            if content_digest(installed.read_bytes()) != digest:
                return False
        except Exception:
            return False
        self._installed_digests[path.name] = digest
        return True

    def _install_variant(self, display: DisplayInfo, path: Path, payload: bytes) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        # Stable filenames are intentionally reused, so the previous app-owned copy
        # is removed before reinstalling. Windows therefore never accumulates
        # timestamped or numbered HDR profiles.
        try:
            remove_profile(path.name, display, "HDR")
        except Exception:
            pass
        name = install_and_associate_profile(path, display, "HDR", make_default=False)
        self._installed_digests[path.name] = content_digest(payload)
        return name

    def _apply_mode_profile(self, reason: str, *, force: bool = False) -> None:
        """Regenerate the Off/On working pair and activate the selected one.

        Installation is skipped for any variant whose generated bytes match what
        Windows already has, so toggling the correction, or applying with no
        edits, costs a single default-association call. ``force`` bypasses that
        cache for the cases where the association itself may have been lost.
        """
        display = self._selected_display()
        if display is None:
            self._set_status("Select a detected display before applying a profile.", "error")
            return
        if display.current_mode != "HDR":
            self._set_status(
                f"{reason}: Windows is not in HDR mode for {display.friendly_name}, so the HDR profile was not applied. "
                "Enable HDR (Win + Alt + B) and try again.",
                "warning",
            )
            return

        # Capture the real Windows HDR profile only when it is not one of our two
        # stable working slots. All edits are always derived from this base.
        self._capture_current_hdr_base(display)
        self._cleanup_legacy_managed_profiles(display)
        signature = self._edit_signature()
        enabled = self.state.hdr.sdr_gamma_correction != "Off"

        try:
            payloads, on_option = self._build_working_payloads(display)
            pending = {
                label
                for label, (path, payload) in payloads.items()
                if force or not self._installed_matches(path, content_digest(payload))
            }

            if pending:
                # Uninstalling a profile that is currently the default leaves the
                # association in Windows' hands. Park on the known base first so the
                # intermediate state is deterministic rather than arbitrary.
                self._release_active_working_profile(
                    display, {payloads[label][0].name for label in pending}
                )

            installed: dict[str, tuple[str, Path]] = {}
            for label, (path, payload) in payloads.items():
                if label in pending:
                    name = self._install_variant(display, path, payload)
                else:
                    # Content is unchanged, so the file need not be reinstalled — but the
                    # association still must be re-asserted. Removing a profile drops it
                    # from the display's association list, and setting an unassociated
                    # profile as the default silently fails to persist: the read-back
                    # succeeds and Windows reverts moments later. Skipping this is why
                    # Reapply worked while changing the correction did not.
                    name = path.name
                    associate_profile(name, display, "HDR")
                installed[label] = (name, path)

            active_label = "On" if enabled else "Off"
            active_name, active_path = installed[active_label]
            reapply_existing_default_profile(display, "HDR", active_name)
        except Exception as exc:
            if reason == "Live update":
                self.state.live_mode = False
                self.live_timer.stop()
                with QSignalBlocker(self.live_checkbox):
                    self.live_checkbox.setChecked(False)
                self._set_status(
                    f"Live update failed and Live Apply was switched off to avoid repeating it: {exc}",
                    "error",
                )
            else:
                self._set_status(f"{reason} failed for HDR: {exc}", "error")
            return

        self._applied_signature = signature
        self._active_profile_name = active_name
        key = f"{display.key}|HDR"
        self._persisted_live_registry[key] = {
            "profile_name": active_name,
            "profile_path": str(active_path),
            "base_profile_name": self._base_hdr_profiles.get(display.key, {}).get("profile_name", self.state.hdr.base_profile_name),
            "base_profile_path": self._base_hdr_profiles.get(display.key, {}).get("profile_path", self.state.hdr.base_profile),
        }
        self._save_live_registry()
        self._write_gamma_runtime_state(display, installed, on_option, enabled, active_name, active_path)
        self._save_state_now()
        self._update_activity_bar()

        reinstalled = ", ".join(sorted(pending))
        detail = (
            f"Rebuilt the {reinstalled} variant."
            if reinstalled
            else "Nothing needed rebuilding; switched the association only."
        )
        self._set_status(
            f"{reason}: {display.friendly_name} is using the Virtual HDR OSD working profile with "
            f"gamma correction {'ON' if enabled else 'OFF'}. {detail}",
            "ok",
        )

    def _release_active_working_profile(self, display: DisplayInfo, pending_names: set[str]) -> None:
        """Point Windows back at the base profile before replacing a live working file."""
        try:
            current_name = Path(get_default_profile(display, "HDR")).name
        except Exception:
            return
        if current_name not in pending_names:
            return
        base_name = self.state.hdr.base_profile_name
        if not base_name:
            return
        try:
            reapply_existing_default_profile(display, "HDR", base_name)
        except Exception:
            # Best effort. If the base is gone, installation below still proceeds.
            pass

    def _write_gamma_runtime_state(
        self,
        display: DisplayInfo,
        installed: dict[str, tuple[str, Path]],
        on_option: str,
        enabled: bool,
        active_name: str,
        active_path: Path,
    ) -> None:
        entry = self._runtime_entry(display)
        if entry is None:
            return
        payload, displays_state, record = entry
        base = self._base_hdr_profiles.get(display.key, {})
        binding = self.state.display_bindings.get(display.stable_key)
        off_name, off_path = installed["Off"]
        on_name, on_path = installed["On"]
        record.update(
            {
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
                # The watchdog force-restores the STANDARD association every five
                # seconds. Publish the SDR choice so it can honour "third-party
                # calibration owns SDR" rather than overriding it on a timer; the
                # GUI's own restraint is worth nothing while the watchdog writes.
                "sdr_unmanaged": binding is not None and binding.sdr_profile == SDR_UNMANAGED,
                # Timezone-aware and full precision: the watchdog compares this against
                # its own timestamp to decide who acted last. A naive local time is
                # ambiguous across a DST fall-back, and truncating to whole seconds made
                # a same-second write compare as older than the watchdog's 100ns stamp.
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )
        displays_state[display.key] = record
        payload["schema"] = GAMMA_RUNTIME_SCHEMA
        self._write_json_atomic(GAMMA_HOTKEY_STATE_PATH, payload)

    def _publish_gamma_runtime_intent(self, display: DisplayInfo) -> None:
        """Publish ON/OFF intent before profile generation to prevent watchdog races."""
        entry = self._runtime_entry(display)
        if entry is None:
            return
        payload, displays_state, record = entry
        record.setdefault("display_name", display.friendly_name)
        record.setdefault("gdi_name", display.gdi_name)
        record.setdefault("profiles", {})
        record.setdefault("paths", {})
        record["selected"] = self._last_enabled_gamma_correction
        record["enabled"] = self.state.hdr.sdr_gamma_correction != "Off"
        record["updated_at"] = datetime.now().astimezone().isoformat()
        displays_state[display.key] = record
        payload["schema"] = GAMMA_RUNTIME_SCHEMA
        self._write_json_atomic(GAMMA_HOTKEY_STATE_PATH, payload)

    def _runtime_entry(
        self, display: DisplayInfo
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]] | None:
        """Load the shared watchdog state and return (payload, displays, this display).

        Runtime coordination is best effort and must never prevent profile
        application, so a malformed or unreadable file simply starts over.
        """
        payload: dict[str, object] = {}
        if GAMMA_HOTKEY_STATE_PATH.is_file():
            try:
                candidate = json.loads(GAMMA_HOTKEY_STATE_PATH.read_text(encoding="utf-8-sig"))
                if isinstance(candidate, dict):
                    payload = candidate
            except (OSError, ValueError, json.JSONDecodeError):
                payload = {}
        displays_state = payload.get("displays")
        if not isinstance(displays_state, dict):
            displays_state = {}
            payload["displays"] = displays_state
        record = displays_state.get(display.key)
        if not isinstance(record, dict):
            record = {}

        # Drop records describing this same monitor under a previous adapter LUID.
        # The watchdog looks entries up by gdi_name, so leftovers are not merely
        # clutter: they are rival records for the same display, and it used to act
        # on whichever came first.
        for stale_key in [
            key for key, value in displays_state.items()
            if key != display.key
            and isinstance(value, dict)
            and value.get("gdi_name") == display.gdi_name
        ]:
            displays_state.pop(stale_key, None)

        return payload, displays_state, record

    # ----------------------------------------------------------------------------------
    # Import / export

    def _load_profile_from_path(self, source: Path) -> None:
        try:
            imported = import_profile(source, "HDR")
        except Exception as exc:
            QMessageBox.critical(self, "Load Profile", f"Could not load the profile:\n\n{exc}")
            return
        imported.state.imported_profile = str(source)
        if not imported.state.base_profile:
            imported.state.base_profile = str(source)
            # Filename, not description; see import_profile.
            imported.state.base_profile_name = source.name
        self.state.set_mode_state("HDR", imported.state)
        self._base_is_user_selected = True
        binding = self._selected_binding()
        if binding is not None:
            try:
                in_colour_dir = source.parent.samefile(get_color_directory())
            except Exception:
                in_colour_dir = False
            binding.hdr_profile = source.name if in_colour_dir else str(source)
        self._load_mode_into_controls()
        self._populate_profile_pickers()
        self.hdr_profile_combo.setToolTip("Editing base profile:\n" + str(source))
        self._save_state_now()
        message = (
            f"Loaded {imported.description}. Exact slider state recovered."
            if imported.exact_state
            else f"Loaded {imported.description}. " + " ".join(imported.warnings)
        )
        self._set_status(message, "warning" if imported.warnings else "ok")
        self._update_activity_bar()
        if self._guide_dialog is not None:
            self._guide_dialog.refresh_status()
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
        self._load_profile_from_path(Path(filename))

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
        self.gamma_runtime_timer.stop()
        self.state_save_timer.stop()
        if self._hotkey_listener is not None:
            self._hotkey_listener.close()
        self._save_state_now()
        super().closeEvent(event)
