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
from . import elevation, measure, measure_view
from .edid import read_panel_metadata
from .meter import MeterError, find_spotread, list_instruments, read_emissive
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
from .model import ApplicationState, DisplayBinding, DisplayMode, ModeState, normalize_primaries
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
    overwrite_installed_profile,
    set_hdr_enabled,
    reapply_existing_default_profile,
    remove_profile,
    send_hdr_toggle_shortcut,
    watchdog_is_running,
    WindowsColorError,
)

LOCAL_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Virtual_HDR_OSD_for_Windows"
STATE_PATH = LOCAL_ROOT / "last_gui_state.json"
LIVE_ROOT = LOCAL_ROOT / "live_profiles"
LIVE_REGISTRY_PATH = LOCAL_ROOT / "live_registry.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / "resources"
GAMMA_HOTKEY_STATE_PATH = LOCAL_ROOT / "gamma_hotkeys.json"
# Raw meter readings and the outcome of each run. A refused measurement reports a
# sentence in the status bar and then the numbers behind it are gone, which made
# every failure a round trip through the user. Written as JSON lines, appended,
# and never read back by the app.
METER_LOG_PATH = LOCAL_ROOT / "meter_log.jsonl"
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

# Editing an existing HDR profile is the normal path, but it requires already
# having one -- which is why the guide sent people to Microsoft's separate
# calibration app first. This sentinel says "there is no base profile; describe
# the panel from what it reports about itself", which the display can answer
# directly: EDID carries its luminance and DXGI its primaries.
HDR_FROM_PANEL = "Build from this display's own panel data (no base profile)"

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


# Every deferred callback below passes ``self`` as QTimer.singleShot's context
# object, so Qt cancels it if this window is destroyed first. Without one the
# callback still runs and reaches into deleted widgets: closing the window within
# 1.2 seconds of pressing Calibrate Display raised
# "Internal C++ object (ComboBox) already deleted" from inside a timer, where Qt
# prints the traceback and carries on -- so the test suite stayed green while an
# unhandled exception went through it on every run.

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

        # Which direction a watchdog install/uninstall was last asked to go, so the
        # outcome can be reported once it actually lands. Empty when nothing is pending.
        self._lock_pending = ""

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

        # The watchdog is a separate process with its own installer, and it can be
        # stopped from outside this app entirely. Polling is the only way for the
        # switch to keep telling the truth; an install also completes long after
        # the click, in a console this app does not wait on.
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(2500)
        self.watchdog_timer.timeout.connect(self._sync_lock_switch)

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
        self._sync_lock_switch()
        self.watchdog_timer.start()
        self._update_activity_bar()

        if self._first_run:
            QTimer.singleShot(400, self, self._show_guide)

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
        """Persist the editor state, and say so when it does not land.

        _write_json_atomic reports whether the rename succeeded and every caller
        threw that away. For the two runtime files the consequence is small -- the
        watchdog re-reads on a timer and the filenames are stable -- but this one is
        the user's own work, and it is called from closeEvent, so a failure here is
        every unsaved edit disappearing with no indication that anything went wrong.
        The usual cause is a security product holding the file open; the same one the
        watchdog installer already warns about.
        """
        if self._write_json_atomic(STATE_PATH, self.state.to_dict()):
            return
        # Guarded: closeEvent calls this after the status bar may already be gone,
        # and a failure to save must not become a failure to close.
        try:
            self._set_status(
                f"Could not save your settings to {STATE_PATH.name}. Something else is "
                "holding the file open — check Controlled Folder Access under Windows "
                "Security, Ransomware protection.",
                "error",
            )
        except Exception:
            pass

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
        self.calibrate_button = PrimaryPushButton("Calibrate Display", self)
        self.calibrate_button.setToolTip(
            "Describe this display from its own data and install the result, in one step.\n\n"
            "Turns HDR on if it is off, reads the panel's peak, sustained and black luminance "
            "and its primaries from its EDID, builds an HDR profile from them, and makes it "
            "the Windows default.\n\n"
            "Nothing here is guessed and nothing needs judging by eye. Use the sliders "
            "afterwards only if you want to depart from what the panel reports."
        )
        self.calibrate_button.clicked.connect(self._calibrate_display)
        heading.addWidget(self.calibrate_button)
        guide_button = PushButton("Getting Started", self)
        guide_button.setToolTip("Open the step-by-step walkthrough of the recommended calibration workflow.")
        guide_button.clicked.connect(self._show_guide)
        heading.addWidget(guide_button)
        watchdog_button = PushButton("Watchdog…", self)
        watchdog_button.setToolTip(
            "Install, remove or reinstall the standalone watchdog, with an explanation of "
            "what it does.\n\n"
            "The Lock Profile switch in row 3 covers turning it on and off. This is here for "
            "the case that switch cannot express: forcing a reinstall while it is already "
            "running, which is needed after you change which profile Windows falls back to."
        )
        watchdog_button.clicked.connect(self._show_watchdog_settings)
        heading.addWidget(watchdog_button)
        # "Run as Admin" rather than "Run as Administrator": this row already carries a
        # title, four buttons and the mode badge, and the longer label is wider than
        # "Calibrate Display" -- the most important button here -- for a thing most
        # people never need to press.
        self.elevate_button = PushButton("Run as Admin", self)
        self.elevate_button.setToolTip(
            "Restart this app with administrator rights, keeping your current edits.\n\n"
            "Most of what this app does needs no such thing: every colour setting it "
            "writes is per-user, so profiles apply and lock perfectly well as an "
            "ordinary user. It matters in two cases -- registering the watchdog as a "
            "scheduled task rather than a plain startup entry, and installing a profile "
            "on a machine whose Windows colour folder has been locked down.\n\n"
            "This button is not here once the app is already elevated."
        )
        self.elevate_button.clicked.connect(self._relaunch_elevated)
        self._refresh_elevation_button()
        heading.addWidget(self.elevate_button)
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
        self.sdr_profile_combo.textActivated.connect(self._sdr_profile_chosen)
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
        self.hdr_profile_combo.textActivated.connect(self._hdr_profile_chosen)
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
            "Switches on this row are standing behaviour that stays on until you turn it off. "
            "The row beneath holds one-off actions, with the two on its right being the ones "
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
        self.automatic_mode_checkbox.setOffText("Auto Mode Switching")
        self.automatic_mode_checkbox.setOnText("Auto Mode Switching")
        self.automatic_mode_checkbox.setToolTip("Automatically follow Windows SDR/HDR transitions. On HDR → SDR the app restores the SDR profile pinned in row 2, or, on Auto, the one Windows previously had; with SDR set to Leave unmanaged it does nothing. On SDR → HDR it reapplies the active HDR profile. An SDR profile is never created, edited, or overwritten — only the association is set.")
        automatic_enabled = self.state.follow_windows_mode and self.state.auto_refresh_after_mode_change
        self.automatic_mode_checkbox.setChecked(automatic_enabled)
        self.automatic_mode_checkbox.checkedChanged.connect(self._automatic_mode_switching_toggled)
        runtime_row.addWidget(self.automatic_mode_checkbox)
        self.lock_switch = SwitchButton(bar)
        self.lock_switch.setOffText("Lock Profile")
        self.lock_switch.setOnText("Lock Profile")
        self.lock_switch.setToolTip(
            "Install the standalone watchdog, which re-asserts this display's HDR profile "
            "whenever Windows drops it — after a mode change, a resume from sleep, or a "
            "driver reset — and keeps Alt+1 / Alt+2 working once this window is closed.\n\n"
            "It is a separate program with its own installer, so switching this on opens a "
            "console window. No administrator rights are needed: everything it registers is "
            "per-user. If Windows refuses to register its scheduled task, the installer says "
            "so and falls back to a plain startup entry — press Run as Admin and install "
            "again to get the scheduled task instead.\n\n"
            "The switch follows what is actually running, not what was last clicked."
        )
        self.lock_switch.checkedChanged.connect(self._lock_toggled)
        runtime_row.addWidget(self.lock_switch)
        runtime_row.addStretch(1)
        layout.addLayout(runtime_row)

        # Ten controls on one row stopped fitting once the meter button arrived:
        # the switches truncated to "Automatic Moc" and "Keep Profile Lo", and the
        # meter button lost its ellipsis. The split follows what the controls are
        # rather than where they happened to sit -- switches above are standing
        # behaviour, buttons below are one-off actions.
        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        revert_button = PushButton("Revert", bar)
        revert_button.setToolTip("Revert to base: discard your slider edits and reload the selected HDR profile untouched.")
        revert_button.clicked.connect(self._revert_to_base)
        action_row.addWidget(revert_button)
        reset_button = PushButton("Reset Sliders", bar)
        reset_button.setToolTip("Reset all sliders to their neutral defaults, without changing which profile is selected.")
        reset_button.clicked.connect(self._reset_all_controls)
        action_row.addWidget(reset_button)
        self.patterns_button = PushButton("Test Patterns…", bar)
        self.patterns_button.setToolTip(
            "Fill the display with calibration patterns and adjust from the keyboard.\n"
            "Number keys switch pattern, Tab picks a control, arrows adjust, Esc exits."
        )
        self.patterns_button.clicked.connect(self._open_pattern_view)
        action_row.addWidget(self.patterns_button)
        self.meter_button = PushButton("Measure…", bar)
        self.meter_button.setToolTip(
            "Measure this display with a colorimeter instead of by eye: black level, peak "
            "white, and the three primaries, each read from a centred patch on black.\n\n"
            "Needs ArgyllCMS installed separately; you will be asked for it the first time. "
            "Most meters need no driver, and other calibration software keeps working."
        )
        self.meter_button.clicked.connect(self._measure_with_meter)
        action_row.addWidget(self.meter_button)
        action_row.addStretch(1)
        self.refresh_profile_button = PushButton("Reapply", bar)
        self.refresh_profile_button.setToolTip("Force a full reinstall of the current settings. Use this if Windows has dropped the HDR association, typically after a mode change or resume from sleep.")
        self.refresh_profile_button.clicked.connect(lambda: self._apply_mode_profile("Reapply", force=True))
        action_row.addWidget(self.refresh_profile_button)
        self.apply_profile_button = PrimaryPushButton("Apply Edits", bar)
        self.apply_profile_button.setToolTip("Install and associate the profile described by the sliders as they are right now.")
        self.apply_profile_button.clicked.connect(lambda: self._apply_mode_profile("Apply Edits"))
        action_row.addWidget(self.apply_profile_button)
        layout.addLayout(action_row)
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
        if ok:
            self.hotkey_status_label.setText("Hotkeys: Alt+1 / Alt+2 active")
            self.hotkey_status_label.setToolTip(detail)
            return

        # RegisterHotKey is exclusive, so losing it to the watchdog is the normal
        # outcome of following the guide and turning on Lock Profile -- and the
        # hotkeys are not unavailable at all, the watchdog owns them and the GUI
        # follows the shared state. Reporting that as an amber "unavailable" once
        # per launch told the user a feature was broken because they had done what
        # they were told. The warning is kept for the case that really is one:
        # Windows refusing the registration for some other reason.
        if watchdog_is_running():
            self.hotkey_status_label.setText("Hotkeys: Alt+1 / Alt+2 held by the watchdog")
            self.hotkey_status_label.setToolTip(
                "The standalone watchdog owns the hotkeys while it runs, which is what "
                "keeps them working after this window closes. They are not disabled.\n\n"
                + detail
            )
            return

        self.hotkey_status_label.setText("Hotkeys: not owned by this window")
        self.hotkey_status_label.setToolTip(detail)
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
            QTimer.singleShot(120, self, self._apply_live_edit)
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

    def _guide_wiring(self) -> tuple[dict, dict]:
        """The action and check tables the guide is built from.

        Separated from _show_guide so a test can require every step to
        resolve. A step naming a key the window does not provide is a dead
        button in the walkthrough, and nothing else would notice."""
        actions = {
            "enable_hdr": lambda: self.hdr_switch.setChecked(True),
            "display_settings": open_windows_display_settings,
            "hdr_calibration_app": open_windows_hdr_calibration_app,
            "focus_profiles": self._highlight_profile_pickers,
            "import_profile": self._import_profile,
            "enable_live": lambda: self.live_checkbox.setChecked(True),
            "watchdog": self._show_watchdog_settings,
            "calibrate": self._calibrate_display,
            "export_profile": self._export_profile,
        }
        checks = {
            "hdr_active": self._check_hdr_active,
            "profile_imported": self._check_profile_imported,
            "live_enabled": self._check_live_enabled,
        }
        return actions, checks

    def _show_guide(self) -> None:
        actions, checks = self._guide_wiring()
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
        # A panel-built profile has no base file by design -- _build_from_panel
        # clears both fields, because there is nothing to inherit ICC tags from.
        # Testing only for a base therefore reported the guide's own Calibrate
        # Display step as still outstanding no matter how many times it was
        # pressed, which is the one step that step exists to confirm.
        # Not panel_primaries: _capture_panel_primaries fills that on every
        # _refresh_displays, including the one in __init__, so this step reported
        # itself already done on a genuinely first launch -- with no profile built,
        # none installed and none associated -- and the guide invited the user to
        # click past the only step that does any work. binding.hdr_profile is set to
        # HDR_FROM_PANEL by _calibrate_now and by nothing else.
        binding_built = binding is not None and binding.hdr_profile == HDR_FROM_PANEL
        if binding_built and self.state.hdr.panel_primaries:
            state = self.state.hdr
            return True, (
                f"Built from this display: {state.peak_luminance_nits:g} nits peak, "
                f"{state.full_frame_luminance_nits:g} sustained  ·  {sdr_note}"
            )
        return False, f"No HDR profile is selected for this display yet.  ·  {sdr_note}"

    def _check_live_enabled(self) -> tuple[bool, str]:
        if self.state.live_mode:
            return True, "Live Apply is on. Slider changes install automatically."
        return False, "Live Apply is off. Turn it on, or use Apply Edits after each change."

    def _sync_lock_switch(self) -> None:
        """Point the switch at whether the watchdog is actually running.

        Deliberately not a record of what was last clicked. Installing runs a
        separate elevated installer that the user can dismiss at the UAC prompt,
        and the watchdog can be stopped from outside this app, so the only
        honest source is the process itself.
        """
        if not hasattr(self, "lock_switch"):
            return
        running = watchdog_is_running()
        if running == self.lock_switch.isChecked():
            return
        with QSignalBlocker(self.lock_switch):
            self.lock_switch.setChecked(running)
        if running and self._lock_pending == "install":
            self._set_status(
                "Profile lock is on. The watchdog will re-apply this display's HDR profile "
                "whenever Windows drops it.",
                "ok",
            )
            self._lock_pending = ""
        elif not running and self._lock_pending == "uninstall":
            self._set_status("Profile lock is off. Windows may drop the association on a mode change.", "ok")
            self._lock_pending = ""

    def _lock_toggled(self, checked: bool) -> None:
        if self._loading_controls:
            return
        if checked == watchdog_is_running():
            return
        self._lock_pending = "install" if checked else "uninstall"
        self._run_watchdog_script(
            "2- OPTIONAL - Install-Watchdog.bat" if checked else "Uninstall-Watchdog.bat"
        )

    def _refresh_elevation_button(self) -> None:
        """The button is only present while it still has something left to offer."""
        self.elevate_button.setVisible(not elevation.is_elevated())

    def _relaunch_elevated(self) -> None:
        """Restart as administrator, carrying the current edits across.

        Asked first, because it closes this window. The prompt says what elevation is
        actually for rather than implying the app needs it -- on most machines nothing
        here does, and elevating out of habit costs the ability to drag a profile in
        from an ordinary Explorer window, which UIPI blocks across that boundary.
        """
        if elevation.is_elevated():
            self._refresh_elevation_button()
            self._set_status("Already running as administrator.", "ok")
            return

        answer = QMessageBox.question(
            self,
            "Restart as Administrator",
            "Restart this app with administrator rights?\n\n"
            "Your edits are saved first and restored afterwards, and the profile "
            "Windows is using right now does not change.\n\n"
            "This is only worth doing for:\n"
            + "".join(f"  • {reason}\n" for reason in elevation.BUYS)
            + "\nEverything else works without it, because every colour setting this "
            "app writes is per-user.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # Written before Windows is asked, not after. The elevated copy reads this file
        # as it starts, and closing this window is what would otherwise write it -- so
        # the other order is a race the new process usually wins, losing the edits.
        self._save_state_now()
        result = elevation.relaunch_elevated()
        if result.started:
            self.close()
            return
        self._set_status(
            result.message or "Could not restart as administrator.",
            "warning" if result.outcome is elevation.Relaunch.DECLINED else "error",
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
        # Wall clock, not the script's mtime. The mtime is 0.0 when nothing is
        # installed yet, and every stale result file on disk is "newer" than that.
        launched_at = time.time()
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
        QTimer.singleShot(
            9000, self,
            lambda: self._report_watchdog_outcome(installing, before, launched_at),
        )

    @staticmethod
    def _watchdog_script_stamp() -> float:
        """Modification time of the installed watchdog script, or 0 when absent."""
        try:
            return (WATCHDOG_INSTALL_ROOT / "Watchdog.ps1").stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _watchdog_result(newer_than: float) -> dict | None:
        """What the installer or uninstaller recorded, if it did so on this run.

        The mtime check is what makes a stale file from a previous run unusable as
        this run's answer.
        """
        path = WATCHDOG_INSTALL_ROOT / "install_result.json"
        try:
            if path.stat().st_mtime < newer_than:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _report_watchdog_outcome(
        self, installing: bool, before: float, launched_at: float | None = None
    ) -> None:
        """Say whether the installer actually changed anything.

        The mtime of Watchdog.ps1 answers a much narrower question than it looks like
        it does: the .bat writes that file before the integrity check, before -Install,
        before the display capture and before Task Scheduler registration. So it proves
        extraction, and everything after it -- including an outright throw, which is the
        realistic first-time failure when the Off/On pair is incomplete -- was still
        reported as a green "Watchdog installed." Prefer what the script itself
        recorded, and fall back to the timestamp only when there is no record.
        """
        after = self._watchdog_script_stamp()
        result = self._watchdog_result(launched_at if launched_at is not None else before)
        if installing:
            if result is not None:
                warnings = [str(text) for text in (result.get("warnings") or [])]
                if not result.get("ok"):
                    self._set_status(
                        "Watchdog install did not complete. "
                        + (warnings[0] if warnings else "Check the installer window."),
                        "error",
                    )
                elif warnings:
                    # It works, but not the way it was meant to. Amber, not green.
                    self._set_status(f"Watchdog installed, with a caveat: {warnings[0]}", "warning")
                else:
                    self._set_status(
                        f"Watchdog installed via {result.get('startup') or 'the usual startup entry'}. "
                        "It now keeps your SDR/HDR associations stable and owns Alt+1 / Alt+2.",
                        "ok",
                    )
                return
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

        if result is not None and not result.get("ok"):
            warnings = [str(text) for text in (result.get("warnings") or [])]
            self._set_status(
                "Watchdog uninstall did not complete. "
                + (warnings[0] if warnings else "Check the uninstaller window."),
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
            if self._prefill_luminance_from_panel(selected):
                state = self.state.hdr
                self._set_status(
                    f"{selected.friendly_name} declares {state.peak_luminance_nits:g} nits peak "
                    f"and {state.full_frame_luminance_nits:g} full-frame; using those until "
                    "measured. They are the model's specification, not this panel measured.",
                    "ok",
                )
            self._capture_panel_primaries(selected)
            self._warn_if_panel_gamut_changed(selected)

    def _display_selected(self, _index: int) -> None:
        selected = self.display_combo.currentData()
        if not isinstance(selected, DisplayInfo):
            return
        self.state.selected_display_key = selected.key
        self._current_display_snapshot = selected
        self._last_detected_mode = None
        # Re-read the panel, because the editor holds one HDR ModeState for every
        # display. Nothing here used to touch the colorimetry at all, so picking a
        # second display wrote the first one's gamut and luminance into its profile.
        # Both calls are no-ops when the figures already describe this display.
        self._adopt_panel_for(selected)
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
                QTimer.singleShot(650, self, lambda d=selected: self._restore_remembered_sdr_profile(d, "Automatic Mode Switching"))
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
            QTimer.singleShot(650, self, lambda: self._apply_mode_profile("Automatic Mode Switching", force=True))

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
                entries = [HDR_FROM_PANEL, *installed]
                chosen = binding.hdr_profile or Path(self.state.hdr.base_profile or "").name
                if not chosen:
                    # Nothing pinned and nothing loaded yet: show whatever Windows
                    # actually has, which is what Apply would adopt.
                    chosen = Path(
                        self._base_hdr_profiles.get(display.key, {}).get("profile_name", "")
                    ).name
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
        if text == HDR_FROM_PANEL:
            self._build_from_panel(binding)
            return
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
        QTimer.singleShot(700, self, self._refresh_displays)

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
                minimum=float(control.spec.minimum),
                maximum=float(control.spec.maximum),
                write=lambda value, c=control: c.set_value(value, emit=True),
            ))
        return bindings

    # Each threshold pattern answers exactly one of the three luminance figures a
    # generated profile carries: minimum and peak go into the MHC2 header, full frame into
    # the lumi tag. Measuring something the profile does not then record would be pointless.
    # full-frame-white is absent on purpose: it finds a clipping point, not sustained
    # luminance, and writing one into a field meaning the other is how a profile ends up
    # claiming a display holds 1010 nits full-screen when the panel declares 265. That
    # figure comes from the EDID, or from a meter. See patterns.MEASUREMENT_SEQUENCE.
    MEASUREMENT_FIELDS = {
        "black-level": "minimum_luminance_nits",
        "peak-white": "peak_luminance_nits",
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
        measured = float(nits)
        if field == "minimum_luminance_nits":
            value = max(0.0, min(100.0, measured))
        else:
            value = max(80.0, min(10000.0, measured))
        setattr(self.state.hdr, field, value)
        # The bounds keep the ICC fields sane, but a reading that hits one is no longer
        # the reading that was taken. Silently storing a different number than the user
        # measured is the one thing a measurement step must never do.
        if abs(value - measured) > 1e-6:
            self._save_state_soon()
            self._update_activity_bar()
            self._set_status(
                f"Measured {measured:g} nits, which is outside the range a profile can "
                f"carry for {field.replace('_', ' ')}. Stored as {value:g}. Check the "
                "reading before applying.",
                "warning",
            )
            return
        # Full frame cannot exceed peak; a panel that did that would be reporting fiction.
        if self.state.hdr.full_frame_luminance_nits > self.state.hdr.peak_luminance_nits:
            self.state.hdr.full_frame_luminance_nits = self.state.hdr.peak_luminance_nits
        self._save_state_soon()
        self._update_activity_bar()

        display = self._selected_display()
        if pattern_key == "black-level" and display is not None:
            note = self._black_level_note(value, display)
            if note:
                self._set_status(note, "warning")
                return

        self._set_status(
            f"Recorded {value:g} nits as {field.replace('_', ' ')}. Apply Edits writes it "
            "into the profile.",
            "ok",
        )

    # Above this, a by-eye black reading is far more likely to describe the room than the
    # panel. An emissive display's black is effectively zero, and even a good LCD sits
    # around 0.05; anything higher is usually stray light or eyes that have not adapted.
    SUSPICIOUS_BLACK_NITS = 0.05

    def _black_level_note(self, measured: float, display: DisplayInfo) -> str:
        """Say when a black reading looks like the room rather than the display.

        The pattern asks whether a shape is visible, which cannot separate "the panel
        cannot show it" from "I cannot see it" -- and only the first belongs in a profile.
        Recorded as minimum luminance it tells Windows the display cannot go darker, and
        Windows tone-maps against that.
        """
        if measured <= self.SUSPICIOUS_BLACK_NITS:
            return ""
        claimed = ""
        if not self._active_profile_overrides_metadata(display):
            capability = capability_for_device_name(display.gdi_name)
            if capability is not None and capability.min_nits < measured / 4.0:
                claimed = f" The display itself reports {capability.min_nits:g}."
        return (
            f"Recorded {measured:g} nits as the black level.{claimed} This test finds the "
            "darkest level you can see, which in a lit room or with unadapted eyes is "
            "brighter than the darkest the panel can show -- and it is the panel's figure "
            "a profile is meant to carry. On an emissive display black is effectively "
            "zero. Re-measure in the dark, or set it to zero, if that is the case here."
        )

    def _close_fullscreen_surfaces(self) -> None:
        """Release any pattern or measurement window still holding a swapchain.

        Both are fullscreen WA_PaintOnScreen windows owning a D3D swapchain on
        the display. Replacing the attribute without closing the old one leaks
        that swapchain, and the leak is cumulative -- open the patterns a few
        times over a session and the display eventually stops handing them out.
        """
        for attribute in ("_pattern_window", "_measure_window"):
            window = getattr(self, attribute, None)
            if window is None:
                continue
            setattr(self, attribute, None)
            try:
                window.close()
            except Exception:
                # A window already destroyed by Qt is not worth failing over.
                pass

    def _fullscreen_surface_busy(self) -> str:
        """Why a fullscreen surface cannot be opened right now, or an empty string.

        Two swapchains on one display is not a supported arrangement, and the
        second one silently failing to present looks exactly like a freeze.
        """
        if getattr(self, "_measure_window", None) is not None:
            return "A measurement is still running. Let it finish, or press Esc, first."
        if getattr(self, "_pattern_window", None) is not None:
            return "The calibration patterns are still open. Close them with Esc first."
        return ""

    def _calibrate_display(self) -> None:
        """Describe the display from its own data and install it, in one action.

        The three decisions this replaces -- find the right entry among every
        profile installed on the machine, apply, and know that applying is a
        separate step from building -- are decisions nobody should have to make.
        Everything it needs is already declared by the panel.
        """
        display = self._selected_display()
        if display is None:
            self._set_status("No display detected to calibrate.", "error")
            return

        if display.current_mode != "HDR":
            # An HDR profile cannot be built or judged while Windows is in SDR,
            # and the alternative is telling the user to go and do it themselves.
            try:
                set_hdr_enabled(display, True)
            except Exception as exc:
                self._set_status(
                    f"Could not turn HDR on for {display.friendly_name}: {exc}", "error"
                )
                return
            self._set_status(
                f"Turning HDR on for {display.friendly_name}…", "warning"
            )
            QTimer.singleShot(1200, self, lambda: self._calibrate_after_hdr(display))
            return

        self._calibrate_now(display)

    def _calibrate_after_hdr(self, display: DisplayInfo) -> None:
        """Continue once Windows has actually switched the display over."""
        self._refresh_displays()
        current = self._selected_display()
        if current is None or current.current_mode != "HDR":
            self._set_status(
                "HDR did not come on, so there is nothing to calibrate yet. Turn it on "
                "with the switch in row 1 and press Calibrate Display again.",
                "warning",
            )
            return
        self._calibrate_now(current)

    def _calibrate_now(self, display: DisplayInfo) -> None:
        """Read the panel, build from it, install it."""
        binding = self._selected_binding()
        if binding is not None:
            binding.hdr_profile = HDR_FROM_PANEL
        self._build_from_panel(binding)

        state = self.state.hdr
        if not state.panel_primaries:
            self._set_status(
                f"Could not read {display.friendly_name}'s own colour data, so there is "
                "nothing better than a generic profile to build. Pick an existing HDR "
                "profile as the base instead.",
                "error",
            )
            return

        if not self._apply_mode_profile("Calibrate Display", force=True):
            # It has already said why. Saying "calibrated" over the top of that
            # is how this reported success for an install that never happened.
            return

        self._populate_profile_pickers()
        got_primaries, got_luminance = getattr(self, "_panel_data_read", (False, False))
        locked = watchdog_is_running()
        tail = (
            "It is locked in place."
            if locked
            else "Turn on Lock Profile to stop Windows dropping it."
        )

        if got_luminance:
            figures = (
                f"{state.peak_luminance_nits:g} nits peak, "
                f"{state.full_frame_luminance_nits:g} sustained, "
                f"{state.minimum_luminance_nits:g} black"
            )
        else:
            # The panel carries no usable HDR luminance, so these are this app's
            # own defaults. Calling them the panel's own data would be a lie about
            # the one thing this button exists to get right.
            figures = (
                f"no luminance declared, so {state.peak_luminance_nits:g}/"
                f"{state.full_frame_luminance_nits:g} nits are defaults rather than measured"
            )

        source = "its own panel data" if got_primaries else "a generic gamut"
        level = "ok" if (got_primaries and got_luminance) else "warning"
        self._set_status(
            f"{display.friendly_name} calibrated from {source}: {figures}. {tail}",
            level,
        )

    def _open_pattern_view(self) -> None:
        display = self._selected_display()
        if display is None:
            self._set_status("Select a display first.", "error")
            return
        busy = self._fullscreen_surface_busy()
        if busy:
            self._set_status(busy, "warning")
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
            panel=read_panel_metadata(display.device_path),
            measure=self._record_measurement,
            on_close=lambda: self._pattern_view_closed(previous_live),
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

    def _spotread(self) -> Path | None:
        """Where spotread is, or None. Honours a configured directory first."""
        return find_spotread(self.state.argyll_path or None)

    def _choose_argyll_path(self) -> bool:
        """Ask for the Argyll bin directory. True once spotread is found there.

        Argyll ships on Windows as a zip with no installer, so there is no
        registry key or standard location to consult and PATH is usually unset.
        Asking is more reliable than guessing.
        """
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select the ArgyllCMS bin folder (containing spotread.exe)",
            self.state.argyll_path or str(Path.home()),
        )
        if not chosen:
            return False
        if find_spotread(chosen) is None:
            QMessageBox.warning(
                self,
                "ArgyllCMS",
                f"No spotread was found in:\n{chosen}\n\n"
                "Choose the bin folder inside the unzipped ArgyllCMS distribution.",
            )
            return False
        self.state.argyll_path = chosen
        self._save_state_now()
        return True

    def _measure_with_meter(self) -> None:
        """Measure this display with a colorimeter and adopt the readings."""
        display = self._selected_display()
        if display is None:
            self._set_status("Select a display first.", "error")
            return
        if display.current_mode != "HDR":
            self._set_status(
                f"Turn HDR on for {display.friendly_name} before measuring. The patches are "
                "shown in absolute luminance, which only means anything in HDR.",
                "warning",
            )
            return
        busy = self._fullscreen_surface_busy()
        if busy:
            self._set_status(busy, "warning")
            return

        spotread = self._spotread()
        if spotread is None:
            answer = QMessageBox.question(
                self,
                "ArgyllCMS not found",
                "Measuring needs ArgyllCMS, a separate free download from argyllcms.com. "
                "Get the executable distribution, unzip it somewhere without spaces in the "
                "path, and point this at the bin folder inside it.\n\nLocate it now?",
            )
            if answer != QMessageBox.StandardButton.Yes or not self._choose_argyll_path():
                return
            spotread = self._spotread()
            if spotread is None:
                return

        try:
            instruments = list_instruments(spotread)
        except MeterError as exc:
            self._set_status(f"Could not ask Argyll what it can see: {exc}", "error")
            return
        if not instruments:
            self._set_status(
                "Argyll found no colorimeter. Check that it is plugged in, and that no other "
                "calibration software is holding it open.",
                "error",
            )
            return
        instrument = instruments[0]

        try:
            sdr_white = get_sdr_white_level_nits(display)
        except Exception:
            sdr_white = 240.0
        panel = read_panel_metadata(display.device_path)
        capability = capability_for_device_name(display.gdi_name)
        # Ask for what the panel claims it can do, not for what was measured last
        # time. Driving the peak patch at the stored figure makes the measurement
        # self-fulfilling: the first run asked for the EDID's 1015 nits and found
        # 450, and every run after asked for 450 and found 450, confirming only
        # that the display can produce what it was told to.
        peak = self.state.hdr.peak_luminance_nits
        if panel is not None and panel.credible:
            peak = max(peak, panel.peak_nits)

        # Said before the surface opens, because it cannot be said afterwards. The
        # screen goes black with one patch on it and nothing else -- deliberately, since
        # any text on the frame is light the meter would integrate along with the patch --
        # and _set_status writes to a status bar that this window then covers. So the
        # step count, the duration and the way out have to be given while there is still
        # somewhere to put them. A minute of a black screen with no way out stated is
        # indistinguishable from a hang.
        steps = len(measure.plan(peak))
        proceed = QMessageBox.question(
            self,
            "Measure with the colorimeter",
            f"About to measure {display.friendly_name} with {instrument.label}.\n\n"
            f"The screen goes black and shows {steps} patches in turn, taking about a "
            "minute. Nothing is written to the profile until it finishes.\n\n"
            "Place the meter flat against the centre of the screen and keep it still.\n\n"
            "Press Esc at any point to stop. Nothing is changed if you do.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if proceed != QMessageBox.StandardButton.Ok:
            self._set_status("Measurement cancelled. Nothing was changed.", "ok")
            return

        window = measure_view.MeasureWindow(capability, sdr_white, panel)
        screen = self._screen_for(display)
        if screen is not None:
            window.setGeometry(screen.geometry())
        window.showFullScreen()
        # The swapchain needs the real window handle and its final size, so it is
        # created once the window is actually on screen.
        QApplication.processEvents()
        if not window.begin():
            window.close()
            self._set_status(
                f"Measuring needs an HDR surface, which this display did not provide: "
                f"{window.failure}",
                "error",
            )
            return

        # Without this the window never takes focus, so Escape goes to whatever had it
        # before -- the covered main window -- and MeasureWindow.keyPressEvent is never
        # reached. Measured: focusWidget was not the window and the closed signal never
        # fired, which left the entire abort path unreachable for a meter run while the
        # status line promised "Esc cancels". The pattern surface has always done this;
        # this path was simply missing it.
        window.activateWindow()
        window.setFocus(Qt.FocusReason.OtherFocusReason)

        self._measure_window = window
        self._log_meter({
            "event": "start",
            "display": display.friendly_name,
            "instrument": instrument.label,
            "requested_peak_nits": peak,
        })
        self._set_status(
            f"Measuring {display.friendly_name} with {instrument.label}. Place the meter "
            "over the centre of the screen. Esc cancels.",
            "warning",
        )

        thread, worker = measure_view.start(
            window,
            lambda: read_emissive(spotread, port=instrument.port),
            peak,
            self._measure_progress,
            self._measure_finished,
            on_reading=self._measure_reading,
        )
        # Held on self so neither is collected mid-run: a QThread that goes out of
        # scope takes its worker with it and reports nothing useful about why.
        self._measure_thread = thread
        self._measure_worker = worker

    def _log_meter(self, record: dict) -> None:
        """Append one line to the meter log, and never fail the run over it."""
        try:
            METER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            record["at"] = datetime.now().astimezone().isoformat()
            with METER_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:
            # Diagnostics must never be the reason a measurement fails.
            pass

    def _measure_reading(self, key: str, reading) -> None:
        """Record a patch as it is read, before anything decides what it means."""
        self._log_meter({
            "event": "reading",
            "patch": key,
            "X": reading.X, "Y": reading.Y, "Z": reading.Z,
            "x": reading.x, "y": reading.y,
        })

    def _measure_progress(self, label: str, index: int, total: int) -> None:
        self._set_status(
            f"Measuring step {index + 1} of {total}: {label}. Keep the meter still.",
            "warning",
        )

    def _measure_finished(self, result, message: str) -> None:
        """Adopt a completed measurement, or explain why there is not one."""
        window = getattr(self, "_measure_window", None)
        if window is not None:
            window.close()
        self._measure_window = None
        self._measure_thread = None
        self._measure_worker = None

        if result is None:
            self._log_meter({
                "event": "cancelled" if not message else "refused",
                "message": message,
            })
            if message:
                self._set_status(f"Measurement stopped: {message}", "error")
            else:
                self._set_status("Measurement cancelled. Nothing was changed.", "ok")
            return

        self._log_meter({
            "event": "accepted",
            "peak_nits": result.peak_nits,
            "black_nits": result.black_nits,
            "white_xy": list(result.white_xy),
            "white_cct": result.white_cct,
            "white_delta_uv": result.white_delta_uv,
            "verified": result.verified,
            "channel_gains": list(result.channel_gains),
            "channel_trims": list(result.channel_trims),
            "trims_already_applied": [
                self.state.hdr.red_channel,
                self.state.hdr.green_channel,
                self.state.hdr.blue_channel,
            ],
        })

        state = self.state.hdr
        state.peak_luminance_nits = max(80.0, min(10000.0, result.peak_nits))
        state.minimum_luminance_nits = max(0.0, min(100.0, result.black_nits))
        # Full frame cannot exceed peak; a panel that did that would be reporting
        # fiction. _record_measurement reconciles them and this did not, so a measured
        # peak below the stored sustained left the two contradicting each other. The
        # profile itself was safe -- _build_working_payloads round-trips through
        # ModeState.from_dict, which clamps -- but the status line went on quoting a
        # sustained figure above the peak until the app was restarted.
        if state.full_frame_luminance_nits > state.peak_luminance_nits:
            state.full_frame_luminance_nits = state.peak_luminance_nits

        # panel_primaries is deliberately untouched. The patches are presented in
        # scRGB, which is defined on BT.709, so a measured "red" is BT.709 red as
        # the display renders it -- not the display's own primary. Writing that to
        # the profile's colorant tags replaced correct DXGI figures with a
        # narrower, wrong gamut. The same readings are exactly right for the
        # correction below, which acts on the signal this app sends rather than
        # describing the panel. See measure.py's module docstring.
        limit = 100.0 * measure.MAX_CHANNEL_TRIM
        # The measurement describes the display as it is *currently corrected*, so
        # its gains are relative to the trims already in force rather than
        # absolute. Replacing them would make a second pass undo the first: a
        # display corrected to neutral measures neutral, solves (1, 1, 1), and
        # stores that -- discarding the correction and returning the display to
        # where it started. Composing also makes repeated passes converge.
        applied = (
            1.0 + state.red_channel / 100.0,
            1.0 + state.green_channel / 100.0,
            1.0 + state.blue_channel / 100.0,
        )
        was_corrected = any(abs(gain - 1.0) > 1e-4 for gain in applied)
        red, green, blue = (
            round((gain - 1.0) * 100.0, 3)
            for gain in measure.compose_gains(applied, result.channel_gains)
        )
        state.red_channel = max(-limit, min(limit, red))
        state.green_channel = max(-limit, min(limit, green))
        state.blue_channel = max(-limit, min(limit, blue))

        self._save_state_now()
        self._load_mode_into_controls()

        contrast = result.contrast
        contrast_text = (
            "contrast too high to measure" if contrast == float("inf")
            else f"{contrast:,.0f}:1 contrast"
        )

        # The error this run *found* is the verdict on whatever was applied before
        # it, which is what makes every pass after the first a verification.
        error = result.white_delta_uv
        cct = result.white_cct
        if result.verified:
            white_note = (
                f"White is neutral: {cct:,.0f}K, {error:.4f} from D65 in u'v', inside the "
                f"{measure.VERIFIED_DELTA_UV} a calibrated display is held to"
            )
            if was_corrected:
                white_note = "Verified. " + white_note + ", so the correction is working"
        else:
            direction = "warm" if result.white_error[0] > 0 else "cool"
            white_note = (
                f"White measured {cct:,.0f}K, {direction} of D65 by {error:.4f} in u'v'; "
                f"trims now R {red:+.1f}%, G {green:+.1f}%, B {blue:+.1f}%"
            )
            if was_corrected:
                white_note = (
                    f"Still {error:.4f} off after the last correction. " + white_note
                )
        level = "ok"
        if result.trims_exceed_range:
            white_note += (
                f". The correction needed exceeds the {limit:.0f}% a profile can carry and "
                "has been clamped, so white will still be off -- check the monitor's own "
                "colour temperature setting first"
            )
            level = "warning"
        self._set_status(
            f"Measured {result.peak_nits:.1f} nits peak on a "
            f"{result.window_fraction:.0%} window and {result.black_nits:.4f} black "
            f"({contrast_text}). {white_note}. Press Apply Edits to store it.",
            level,
        )

    def _pattern_view_closed(self, previous_live: bool) -> None:
        """Forget the window as it closes, so the next open is not refused."""
        self._pattern_window = None
        self._restore_live_mode(previous_live)

    def _apply_from_pattern_view(self) -> bool:
        """Apply without leaving the patterns, so the readings are not lost on exit.

        Returns what the apply actually did. _apply_mode_profile reports failure by
        returning False -- Windows not in HDR, or its own except branch -- and this
        used to return True regardless, guarding only against an exception. The
        pattern surface then latched "Written into the profile." in green and refused
        every later Enter, while the real message sat in a status bar covered by a
        fullscreen window.
        """
        display = self._selected_display()
        if display is None:
            return False
        try:
            return self._apply_mode_profile("Calibration measurements", force=True)
        except Exception:
            return False

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
        if binding is not None and binding.hdr_profile == HDR_FROM_PANEL:
            # A panel-built profile is not derived from any installed file, so
            # there is no base to adopt and nothing to load controls from. This
            # guard has to hold even when load_controls is set, which the one
            # below does not: on the SDR->HDR path it ran, replaced the pin with
            # whatever Windows happened to have, and overwrote the whole HDR
            # state with what import_profile could estimate from a third-party
            # profile -- generic BT.2020 primaries and 1000/400 nits. A game that
            # flips the display to SDR and back therefore destroyed the
            # calibration, and nothing on screen showed it, because the luminance
            # figures and panel_primaries have no widgets.
            self._save_state_now()
            return
        if binding is not None and binding.hdr_profile and not load_controls:
            # The user pinned an HDR profile for this display; it outranks whatever
            # Windows currently happens to have as the default. Say so when the two
            # diverge, or recalibrating in Calman or Windows HDR Calibration looks
            # like the app quietly ignoring the new profile.
            if (
                binding.hdr_profile != HDR_FROM_PANEL
                and Path(profile_name).name != Path(binding.hdr_profile).name
            ):
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

    def _installed_matches(self, path: Path, digest: str, *, trust_cache: bool = True) -> bool:
        """True when Windows already has byte-identical content installed.

        Hashing the installed copy rather than trusting the in-memory cache means
        the fast path also survives an app restart, and correctly misses when
        something outside this app has replaced the file.

        ``trust_cache=False`` is for verifying an install that has just happened. The
        cache is only ever written from a hash of the file on disk, but the caller of
        that check is precisely the code that would be about to record a new entry, so
        consulting it there would be asking the claim to vouch for itself.
        """
        try:
            installed = get_color_directory() / path.name
            if not installed.is_file():
                return False
            if trust_cache and self._installed_digests.get(path.name) == digest:
                return True
            if content_digest(installed.read_bytes()) != digest:
                return False
        except Exception:
            return False
        self._installed_digests[path.name] = digest
        return True

    def _install_variant(self, display: DisplayInfo, path: Path, payload: bytes) -> str:
        """Install one variant and prove Windows really took it.

        Nothing here used to be checked. ``InstallColorProfileW`` returns TRUE without
        copying anything when the destination already exists, so the whole sequence --
        remove, install, cache the digest, report success -- ran to completion while
        the file in the colour folder stayed exactly as it was. The removal that was
        supposed to prevent that is the step that fails: a profile written by an
        elevated run is owned by ``BUILTIN\\Administrators`` and this account is
        refused DELETE, and its failure was swallowed by ``except Exception: pass``.

        The result was an app that reported "Rebuilt the Off, On variant." after every
        edit while the display went on using an older profile, with the association
        correct and the bytes stale, and nothing anywhere that could notice.
        """
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

        digest = content_digest(payload)
        if not self._installed_matches(path, digest, trust_cache=False):
            # The install was a no-op. The bytes can still be written straight into the
            # existing file, because the ACL that denies DELETE still allows
            # FILE_WRITE_DATA -- so this recovers without asking for elevation.
            if not overwrite_installed_profile(name, payload):
                raise WindowsColorError(
                    f"Windows kept the previous {name} instead of the profile just "
                    "built, and it could not be replaced by this account. It was "
                    "installed by an earlier elevated run, so press Run as Admin at "
                    "the top of the window and apply again."
                )
            self._installed_digests[path.name] = digest

        return name

    def _apply_mode_profile(self, reason: str, *, force: bool = False) -> bool:
        """Regenerate the Off/On working pair and activate the selected one.

        Installation is skipped for any variant whose generated bytes match what
        Windows already has, so toggling the correction, or applying with no
        edits, costs a single default-association call. ``force`` bypasses that
        cache for the cases where the association itself may have been lost.

        Returns whether the profile actually reached Windows. Every failure here
        already reports itself and then returns, so a caller that assumed an
        exception on failure got silence instead: Calibrate Display wrapped this
        in a try/except that could never fire, then overwrote the error with a
        green success line while nothing had been installed.
        """
        display = self._selected_display()
        if display is None:
            self._set_status("Select a detected display before applying a profile.", "error")
            return False
        if display.current_mode != "HDR":
            self._set_status(
                f"{reason}: Windows is not in HDR mode for {display.friendly_name}, so the HDR profile was not applied. "
                "Enable HDR (Win + Alt + B) and try again.",
                "warning",
            )
            return False

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
            return False

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
        return True

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
                # The pinned name, not just the fact that a choice was made. Publishing
                # only the boolean above left the watchdog reasserting its install-time
                # capture every five seconds, so a profile pinned here was reverted
                # within five seconds while the GUI had already said it was restored.
                # Empty means "no opinion": follow whatever was captured.
                "sdr_profile": (
                    binding.sdr_profile
                    if binding is not None
                    and binding.sdr_profile not in ("", SDR_UNMANAGED)
                    else ""
                ),
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

    # What ModeState.neutral("HDR") carries. Matching all three exactly means nothing has
    # ever set them: no measurement, no import from a profile that declared any. It is a
    # heuristic, but a wrong guess only replaces a generic default with the panel's own
    # figures, which is the better default either way.
    UNSET_LUMINANCE = (0.0, 1000.0, 400.0)

    def _prefill_luminance_from_panel(self, display: DisplayInfo) -> bool:
        """Start from what the display declares rather than from generic defaults.

        A profile generated before anyone opens the patterns used to claim 1000 nits peak
        and 400 full-frame for every display in the world. The panel states its own
        figures in its EDID, and 1015/265 describes this one; 1000/400 describes nothing.

        Only fills values nobody has set. A measurement or an imported profile's own data
        always wins, because both are about this display and the EDID is about the model.
        """
        state = self.state.hdr
        current = (
            state.minimum_luminance_nits,
            state.peak_luminance_nits,
            state.full_frame_luminance_nits,
        )
        # "Nobody has set these" is the usual reason to fill them, but figures
        # belonging to a different panel are worse than unset: they are wrong and they
        # look deliberate. There is one HDR ModeState for every display, so switching
        # target used to carry display A's peak, sustained and black straight into
        # display B's profile.
        foreign = bool(state.panel_source_key) and state.panel_source_key != display.stable_key
        if current != self.UNSET_LUMINANCE and not foreign:
            return False
        panel = read_panel_metadata(display.device_path)
        if panel is None or not panel.credible:
            return False
        state.minimum_luminance_nits = max(0.0, min(100.0, panel.min_nits))
        state.peak_luminance_nits = max(80.0, min(10000.0, panel.peak_nits))
        state.full_frame_luminance_nits = max(
            80.0, min(state.peak_luminance_nits, panel.max_frame_average_nits or panel.peak_nits)
        )
        state.panel_source_key = display.stable_key
        self._save_state_soon()
        return True

    def _build_from_panel(self, binding: DisplayBinding) -> None:
        """Describe the display from what it reports, with no base profile.

        With no base, build_profile falls back to a self-contained set of tags,
        so the panel's own primaries and luminance are the only thing standing
        between that and a profile describing a display that does not exist.

        Slider corrections are deliberately left alone: they are relative trims,
        and silently zeroing them when someone changes where the colorimetry
        comes from would discard work with no warning.

        Luminance is replaced rather than preserved, because choosing this option
        is a request for the panel's own figures and keeping earlier ones would
        describe the panel with numbers from elsewhere. The status line names what
        was adopted, so a measurement worth keeping can be taken again.
        """
        display = self._selected_display()
        if display is None:
            self._set_status("Select a detected display first.", "error")
            return

        self.state.hdr.base_profile = ""
        self.state.hdr.base_profile_name = ""
        self.state.hdr.imported_profile = ""
        self._base_is_user_selected = True

        if display.current_mode != "HDR":
            self._set_status(
                f"Turn HDR on for {display.friendly_name} first. While it is in SDR the driver "
                "reports BT.709 for this panel, and building now would describe a wide-gamut "
                "display as sRGB.",
                "warning",
            )
            self._save_state_now()
            return

        # Cleared first, because this is a single value shared by every display and
        # nothing else ever resets it. Left in place, a panel whose EDID cannot be
        # read inherits the previous monitor's gamut, the "could not read" branch
        # below becomes unreachable once any display has been read, and the second
        # monitor's profile is written with the first one's primaries.
        self.state.hdr.panel_primaries = ()
        got_primaries = self._capture_panel_primaries(display)
        got_luminance = self._adopt_panel_luminance_from_edid(display)
        self._panel_data_read = (got_primaries, got_luminance)
        state = self.state.hdr
        if not state.panel_primaries:
            self._set_status(
                f"Could not read the primaries {display.friendly_name} reports, so the profile "
                "will claim the generic BT.2020 gamut. Pick an existing HDR profile as the base "
                "instead if colour accuracy matters.",
                "warning",
            )
        else:
            detail = (
                f"{state.peak_luminance_nits:g} nits peak, {state.full_frame_luminance_nits:g} "
                f"full-frame, {state.minimum_luminance_nits:g} black"
                if got_luminance
                else f"no usable EDID luminance, so {state.peak_luminance_nits:g} nits peak is unchanged"
            )
            self._set_status(
                f"Building from {display.friendly_name}: its own primaries and {detail}. "
                "These are what the panel reports, not this unit measured. Press Apply Edits to install.",
                "ok",
            )
        _ = got_primaries
        self._save_state_now()
        self._load_mode_into_controls()

    def _adopt_panel_for(self, display: DisplayInfo) -> None:
        """Make the panel figures in state describe this display.

        Cheap and idempotent when they already do: both calls below compare against
        what is stored and return without writing. The luminance half only replaces
        values when their recorded source is a different display, so a measurement
        taken on this one is never overwritten by its own EDID.

        Luminance first, and the order is load-bearing: both calls stamp
        ``panel_source_key`` with this display, and the luminance check reads it to
        decide whether the stored figures belong to somebody else. Capturing the
        primaries first overwrote that provenance, so the luminance check then saw its
        own new stamp, concluded the figures were native and kept the previous
        display's peak.
        """
        self._prefill_luminance_from_panel(display)
        self._capture_panel_primaries(display)

    def _capture_panel_primaries(self, display: DisplayInfo) -> bool:
        """Record the panel's own gamut, for profiles generated without a base.

        The EDID is the source, not DXGI. ``DXGI_OUTPUT_DESC1`` reports whatever
        ICC profile is currently associated rather than the panel: on the display
        this was developed against it answered (0.6746, 0.3144) for red under one
        profile and (0.6486, 0.3312) under the next, each matching that profile's
        colorant tags to four decimal places, while the EDID said (0.6836, 0.3047)
        throughout. Believing DXGI closes a loop -- a profile written from its
        answer becomes its next answer -- and this app watched its own BT.709
        output come back as the panel's gamut and stick.

        DXGI remains the fallback for a display whose EDID cannot be read, where a
        possibly-echoed answer still beats the generic BT.2020 table. It is only
        taken in HDR mode, because with HDR off the same panel reports BT.709.
        """
        panel = read_panel_metadata(display.device_path)
        measured = normalize_primaries(panel.primaries) if panel is not None else ()
        if not measured:
            measured = self._dxgi_primaries(display)
        if not measured or measured == self.state.hdr.panel_primaries:
            # Still this display's gamut even when the value did not change, and the
            # provenance is what decides whether a later display switch re-reads.
            if measured:
                self.state.hdr.panel_source_key = display.stable_key
            return False
        self.state.hdr.panel_primaries = measured
        self.state.hdr.panel_source_key = display.stable_key
        self._save_state_soon()
        return True

    def _dxgi_primaries(self, display: DisplayInfo) -> tuple[float, ...]:
        """What the driver reports, which may be the applied profile talking back."""
        if display.current_mode != "HDR":
            return ()
        try:
            capability = capability_for_device_name(display.gdi_name)
        except Exception:
            return ()
        if capability is None or not capability.is_hdr:
            return ()
        return normalize_primaries(
            tuple(capability.red_primary)
            + tuple(capability.green_primary)
            + tuple(capability.blue_primary)
            + tuple(capability.white_point)
        )

    def _adopt_panel_luminance_from_edid(self, display: DisplayInfo) -> bool:
        """Replace the luminance figures with what the panel declares.

        _prefill_luminance_from_panel deliberately fills only values nobody has
        set, so a measurement is never overwritten by a specification. That is the
        right rule everywhere except here: choosing to build from the display's own
        panel data is a request for exactly these numbers, and leaving whatever was
        there before would describe the panel with figures from somewhere else.

        The values this replaces are usually DXGI's, which reports this panel's
        sustained full-frame luminance as equal to its peak -- impossible on an
        emissive display, and the reason EDID is read at all.
        """
        panel = read_panel_metadata(display.device_path)
        if panel is None or not panel.credible:
            return False
        state = self.state.hdr
        state.minimum_luminance_nits = max(0.0, min(100.0, panel.min_nits))
        state.peak_luminance_nits = max(80.0, min(10000.0, panel.peak_nits))
        state.full_frame_luminance_nits = max(
            80.0,
            min(state.peak_luminance_nits, panel.max_frame_average_nits or panel.peak_nits),
        )
        self._save_state_soon()
        return True

    def _active_profile_overrides_metadata(self, display: DisplayInfo) -> bool:
        """Whether what DXGI reports could be this app's own output coming back.

        It is not, on the one display this has been tested against. Associating a profile
        with no MHC2 tag at all left the reported luminance unchanged for six seconds, so
        DXGI describes the panel rather than the profile in force.

        The suspicion arose because the reported figures changed from 0/1080/1080 to
        0.1956/1010.404 between two days and happened to match the profile applied on each.
        The likelier explanation is that a monitor setting changed what the panel itself
        advertises -- its metadata follows its picture mode -- and this app then measured
        against it and wrote the same numbers.

        Kept as a single place to answer the question, because it is the right question and
        an echo would be invisible in the resulting profile. It now reports False unless
        the association cannot be read at all, which is a genuine reason not to trust it.
        """
        try:
            name = get_default_profile(display, "HDR")
        except Exception:
            return True   # cannot tell what is applied, so cannot vouch for the reading
        return not name

    def _adopt_panel_luminance(self, imported) -> bool:
        """Fill in luminance the profile does not carry, from what the panel reports.

        A profile with no MHC2 tag says nothing about black point, peak or full-frame
        luminance, so importing one used to replace measured figures with the generic
        defaults of a neutral state -- switching to a Calman ICC on this display turned a
        measured 1080/1080 into 1000/400, and wrote that into the next profile.

        The display itself knows better, and by now the app reads it. Only used when the
        profile is silent and the panel's own numbers are credible; a profile that carries
        MHC2 is left entirely alone, and so is a panel reporting nonsense.
        """
        if "MHC2" in imported.tags:
            return False
        display = self._selected_display()
        if display is None:
            return False
        if self._active_profile_overrides_metadata(display):
            return False
        panel = read_panel_metadata(display.device_path)
        capability = capability_for_device_name(display.gdi_name)
        if panel is not None and panel.credible:
            # The panel's own declaration, which unlike DXGI distinguishes maximum
            # frame-average from peak. See edid.read_panel_metadata.
            minimum, peak = panel.min_nits, panel.peak_nits
            full_frame = panel.max_frame_average_nits or peak
        elif capability is not None and capability.luminance_is_credible:
            minimum, peak = capability.min_nits, capability.max_nits
            full_frame = capability.max_full_frame_nits
        else:
            return False
        imported.state.minimum_luminance_nits = max(0.0, min(100.0, minimum))
        imported.state.peak_luminance_nits = max(80.0, min(10000.0, peak))
        imported.state.full_frame_luminance_nits = max(
            80.0, min(imported.state.peak_luminance_nits, full_frame)
        )
        return True

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
        adopted = self._adopt_panel_luminance(imported)
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
        if adopted:
            state = self.state.hdr
            message += (
                f" It carries no luminance data, so the display's own figures were used: "
                f"{state.peak_luminance_nits:g} nits peak, "
                f"{state.full_frame_luminance_nits:g} full-frame."
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
        # A fullscreen surface outlives this window otherwise, holding a swapchain
        # and leaving a black screen with nothing left to close it.
        self._close_fullscreen_surfaces()
        self.live_timer.stop()
        self.mode_timer.stop()
        self.watchdog_timer.stop()
        self.gamma_runtime_timer.stop()
        self.state_save_timer.stop()
        if self._hotkey_listener is not None:
            self._hotkey_listener.close()
        self._save_state_now()
        super().closeEvent(event)
