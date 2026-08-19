"""Help and first-run walkthrough dialogs.

These live outside ``app.py`` so the main window keeps to display detection and
profile application. Both dialogs are passive: the walkthrough asks the main
window to perform actions through a small callback map rather than reaching
into its internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

HELP_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "What this tool is",
        "Most monitors grey out brightness, gamma and colour-temperature controls in their own OSD as soon as "
        "HDR is switched on. Virtual HDR OSD puts those adjustments back by editing the MHC2 calibration block "
        "inside the HDR ICC profile that Windows already has associated with your display.\n\n"
        "It is a subjective, by-eye correction layer. It is not a substitute for a colorimeter, a "
        "spectrophotometer, or a professional calibration workflow.",
    ),
    (
        "What it changes on your system",
        "Only the EXTENDED (HDR) colour-profile association for the display you select. Two profiles are "
        "installed with stable filenames — one with the SDR-in-HDR gamma correction off, one with it on — and "
        "the app switches which of the two is the Windows default.\n\n"
        "No SDR profile is ever created, edited or overwritten, and no neutral SDR fallback is invented. "
        "The STANDARD (SDR) association is set at exactly one moment — an HDR to SDR transition, with "
        "Automatic Mode Switching on — and only to a profile already installed on this PC: the one you "
        "pinned in row 2, or, on Auto, the one Windows previously had. Set SDR to Leave unmanaged and "
        "the association is never touched at all.",
    ),
    (
        "The editing controls",
        "Gamma / Midtone Response — power-law tone response. 2.200 is neutral; lower brightens midtones.\n\n"
        "Midtone Brightness — lifts or lowers the midrange while holding black and peak white fixed.\n\n"
        "Contrast / Tonal Separation — symmetric S-curve anchored at 0, 50% and 100%.\n\n"
        "White Balance Temperature — offset along the Planckian locus around D65. Positive is warmer.\n\n"
        "Green–Magenta Tint — trim perpendicular to that locus, so it is a true tint rather than a colour cast.\n\n"
        "Color Saturation — luminance-preserving Rec.2020 chroma trim.\n\n"
        "Red / Green / Blue Fine Balance — small linear-light channel trims, normalised so they change "
        "chromaticity without secretly acting as a brightness control.",
    ),
    (
        "SDR-in-HDR gamma correction",
        "Windows composes SDR desktop content into HDR using a piecewise-sRGB response. Many people find this "
        "raises shadows compared with the pure gamma 2.2 they expect. This optional correction remaps that "
        "response, following dylanraga's documented PQ-domain method.\n\n"
        "Auto reads the SDR reference white Windows currently reports for the display, so there is no duplicate "
        "brightness slider here. The remaining entries mirror the published fixed-white presets.\n\n"
        "IMPORTANT: the correction is display-wide. It also affects native HDR10 and RTX HDR content, where it "
        "is usually not what you want. Alt+1 turns it off, Alt+2 restores it.",
    ),
    (
        "Live Apply, Apply and Reapply",
        "Live Apply regenerates and installs the profile automatically a moment after each slider change, so "
        "you can judge adjustments as you make them. Turn it off when you want to stage several edits first.\n\n"
        "Apply Edits installs whatever the sliders currently show. Reapply Profile forces a full reinstall of "
        "the same settings — use it when Windows has dropped the association, typically after a display-mode "
        "transition, a driver reset, or resume from sleep.\n\n"
        "When nothing has actually changed, applying is a fast association switch: the app compares the "
        "generated profile against what Windows already has installed and skips the reinstall entirely.",
    ),
    (
        "The optional watchdog",
        "The watchdog is a standalone scheduled task, independent of this GUI. It keeps the SDR and HDR "
        "associations stable across Win + Alt + B transitions, and keeps Alt+1 / Alt+2 working after you close "
        "this window.\n\n"
        "Because a hotkey chord can only be owned by one process, this GUI's own Alt+1 / Alt+2 registration "
        "will fail while the watchdog is running. That is expected — the GUI follows the watchdog's shared "
        "state instead. Re-run Install after you intentionally change either default profile.",
    ),
    (
        "If something looks wrong",
        "Reset All returns every slider to neutral. Revert to Base reloads the untouched profile you imported, "
        "discarding your edits.\n\n"
        "To back out completely, open Windows Settings › System › Display › Advanced display, and pick your "
        "original profile as the default for HDR. The app-generated profiles are the two files named "
        "Virtual_HDR_OSD_<id>_Off.icm and _On.icm; you can uninstall them from the colour profile folder.\n\n"
        "If applying fails with an access-denied error, close the app and start it as administrator once.",
    ),
)


class HelpDialog(QDialog):
    """Scrollable, sectioned usage guide."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Virtual HDR OSD — Help & Usage Guide")
        self.setMinimumSize(760, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("Help & Usage Guide", self))

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 14, 0)
        content_layout.setSpacing(18)
        for heading, body in HELP_SECTIONS:
            content_layout.addWidget(SubtitleLabel(heading, content))
            paragraph = BodyLabel(body, content)
            paragraph.setWordWrap(True)
            content_layout.addWidget(paragraph)
        content_layout.addStretch(1)

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = PushButton("Close", self)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)


@dataclass(frozen=True, slots=True)
class GuideStep:
    title: str
    body: str
    action_text: str = ""
    action_key: str = ""
    # Returns (satisfied, detail) so each step can show live progress.
    check_key: str = ""


GUIDE_STEPS: tuple[GuideStep, ...] = (
    GuideStep(
        "1 · Pick your display and turn HDR on",
        "Nothing here applies until Windows is in HDR mode for the display you want to adjust.\n\n"
        "Choose the monitor in row 1, then use the HDR switch right next to it. That switch targets the "
        "display you selected, unlike Win + Alt + B which only ever toggles whichever display Windows "
        "considers current. The badge at the top right follows along on its own.",
        "Turn HDR On",
        "enable_hdr",
        "hdr_active",
    ),
    GuideStep(
        "2 · Decide where the colour data comes from",
        "A profile needs to know what the display can actually do. There are two ways to supply that, "
        "and either produces a complete profile."
        "\n\n"
        "Build it from the display itself. In row 2, choose the first entry in the HDR profile list. "
        "The panel reports its own primaries and peak brightness, and those are used directly. Nothing "
        "to install, and no measurements to take."
        "\n\n"
        "Or start from a profile you already have. Anything from Microsoft's free Windows HDR "
        "Calibration app, or from calibration software such as Calman or DisplayCAL, can be used as "
        "the base. That is worth doing if the profile was made with a meter, because a measurement "
        "describes your individual unit where the panel's own figures describe the model.",
        "Get Windows HDR Calibration",
        "hdr_calibration_app",
    ),
    GuideStep(
        "3 · Pin the SDR and HDR profiles for this display",
        "Row 2 lists every colour profile installed on this PC. Pick which one is this display's HDR profile "
        "and the sliders start editing it immediately — no import, no restart.\n\n"
        "The SDR box is about what happens when Windows drops back to SDR. Leave it on Auto to restore "
        "whatever Windows had, pin a specific profile to make that reliable, or choose "
        "\"Leave unmanaged\" if Calman, DisplayCAL or similar owns your SDR calibration — this app will then "
        "never touch the SDR association at all.\n\n"
        "Both choices are remembered per monitor and survive restarts, so this is a one-time setup. Use "
        "Import… only for a file that is not installed in the Windows colour folder.",
        "Show me the profile list",
        "focus_profiles",
        "profile_imported",
    ),
    GuideStep(
        "4 · Switch on Live Apply and adjust by eye",
        "With Live Apply on, each slider change is installed a moment after you stop moving it, so you can "
        "judge the result directly.\n\n"
        "Work in small steps and in this order: Gamma first, then Midtone Brightness and Contrast, then "
        "Temperature and Tint, and only then the per-channel Fine Balance trims. Flip the HDR switch off and "
        "on to compare against the SDR desktop, and use Reset All if you lose your bearings.",
        "Enable Live Apply",
        "enable_live",
        "live_enabled",
    ),
    GuideStep(
        "5 · Optional — fix washed-out SDR content",
        "If the SDR desktop looks flat or grey inside HDR, set SDR-in-HDR Gamma Correction to Auto on the "
        "Tone & Brightness tab.\n\n"
        "This correction is display-wide, so it also touches native HDR10 and game HDR, where it is usually "
        "wrong. Press Alt+1 before playing HDR content and Alt+2 to bring it back for the desktop.",
    ),
    GuideStep(
        "6 · Lock the profile in place",
        "Windows drops the HDR profile association from time to time — after a display mode change, "
        "a resume from sleep, or a graphics driver reset. Turn on Keep Profile Locked in row 3 and a "
        "small background program puts it back whenever that happens, and keeps Alt+1 / Alt+2 working "
        "once this window is closed."
        "\n\n"
        "It installs separately and needs administrator rights, so expect a console window and a "
        "prompt. The switch reports what is actually running rather than what was last clicked, so "
        "dismissing the prompt simply leaves it off."
        "\n\n"
        "Later slider edits are picked up on their own. Re-run the installer only when you change "
        "which profile Windows should fall back to.",
        "Open Watchdog Settings",
        "watchdog",
    ),
    GuideStep(
        "7 · Save a copy",
        "Export writes a standalone .icm containing your corrections plus the exact slider positions. "
        "Re-importing that file restores your settings precisely, which makes it a reliable backup before you "
        "experiment further.",
        "Export Edited HDR Profile…",
        "export_profile",
    ),
)


class GuideDialog(QDialog):
    """Step-by-step walkthrough of the recommended calibration workflow.

    ``actions`` maps a step's ``action_key`` to a callable on the main window;
    ``checks`` maps a ``check_key`` to a predicate describing whether the step
    already looks done, so returning users can see where they are.
    """

    def __init__(
        self,
        actions: dict[str, Callable[[], None]],
        checks: dict[str, Callable[[], tuple[bool, str]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Getting Started")
        self.setMinimumSize(720, 480)
        self._actions = actions
        self._checks = checks
        self._index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 20)
        layout.setSpacing(14)

        self._progress = CaptionLabel("", self)
        layout.addWidget(self._progress)
        self._title = TitleLabel("", self)
        self._title.setWordWrap(True)
        layout.addWidget(self._title)
        self._body = BodyLabel("", self)
        self._body.setWordWrap(True)
        self._body.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._body, 1)

        self._status = StrongBodyLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._action_button = PushButton("", self)
        self._action_button.clicked.connect(self._run_action)
        action_row = QHBoxLayout()
        action_row.addWidget(self._action_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        navigation = QHBoxLayout()
        self._back = PushButton("Back", self)
        self._back.clicked.connect(lambda: self._go(self._index - 1))
        self._next = PrimaryPushButton("Next", self)
        self._next.clicked.connect(self._advance)
        close = PushButton("Close", self)
        close.clicked.connect(self.accept)
        navigation.addWidget(close)
        navigation.addStretch(1)
        navigation.addWidget(self._back)
        navigation.addWidget(self._next)
        layout.addLayout(navigation)

        self._go(0)

    def _go(self, index: int) -> None:
        self._index = max(0, min(len(GUIDE_STEPS) - 1, index))
        step = GUIDE_STEPS[self._index]
        self._progress.setText(f"Step {self._index + 1} of {len(GUIDE_STEPS)}")
        self._title.setText(step.title)
        self._body.setText(step.body)
        self._back.setEnabled(self._index > 0)
        self._next.setText("Finish" if self._index == len(GUIDE_STEPS) - 1 else "Next")

        has_action = bool(step.action_key) and step.action_key in self._actions
        self._action_button.setVisible(has_action)
        if has_action:
            self._action_button.setText(step.action_text)
        self.refresh_status()

    def refresh_status(self) -> None:
        """Re-run the current step's check. Called again after each action."""
        step = GUIDE_STEPS[self._index]
        check = self._checks.get(step.check_key) if step.check_key else None
        if check is None:
            self._status.setText("")
            self._status.setStyleSheet("")
            return
        try:
            satisfied, detail = check()
        except Exception as exc:  # a failed probe must not break the walkthrough
            satisfied, detail = False, f"Could not check this step: {exc}"
        self._status.setText(("✓  " if satisfied else "•  ") + detail)
        self._status.setStyleSheet(
            "StrongBodyLabel { padding: 6px 10px; border-radius: 6px; background: "
            + ("rgba(50, 170, 110, 0.16);" if satisfied else "rgba(220, 154, 45, 0.16);")
            + " }"
        )

    def _run_action(self) -> None:
        action = self._actions.get(GUIDE_STEPS[self._index].action_key)
        if action is None:
            return
        action()
        self.refresh_status()

    def _advance(self) -> None:
        if self._index == len(GUIDE_STEPS) - 1:
            self.accept()
            return
        self._go(self._index + 1)
