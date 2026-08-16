# Virtual HDR OSD for Windows

**Virtual HDR OSD for Windows** is a lightweight Windows 11 HDR profile editor designed as a software counterpart to the controls that many monitors disable when HDR mode is enabled.

<img src="assets/tab1.png">

<img src="assets/tab2.png">

Most HDR monitors lock or substantially reduce access to their physical OSD controls after switching to HDR. White balance, gamma, per-channel RGB balance, saturation, brightness, contrast, and related adjustments may become unavailable or much more limited. Virtual HDR OSD provides a practical **software pseudo-calibration layer** for making small subjective corrections to an existing Windows HDR ICC/ICM profile.

The application is intended for visual fine-tuning: correcting a slight warm/cool cast, reducing a green or magenta bias, matching HDR white balance more closely to a preferred SDR appearance, or making small tonal changes that the monitor's HDR OSD does not expose.

**In addition to the app, a watchdog has been integrated that fixes the bug causing incorrect switching between SDR and HDR profiles in Windows 11. This watchdog is standalone and can be distributed without the app. Feel free to use it. Below is a detailed explanation of how it works.**

> [!NOTE]
> Virtual HDR OSD is not a replacement for a colorimeter, spectrophotometer, reference display, or professional calibration software. Adjustments made by eye are inherently subjective. For an objective calibration workflow, use appropriate measurement hardware and color-management software.

---

## Recommended workflow

The recommended starting point is a profile created with **Windows HDR Calibration** from Microsoft.

Apart from that one Microsoft download, the whole workflow runs inside the app: HDR is switched
from the top bar, and both profiles are chosen from dropdowns of what is already installed —
there is no round trip through Windows Settings and no import/restart cycle.

> [!TIP]
> The **Getting Started** button in the top bar walks you through this entire
> sequence one step at a time, and checks your progress as it goes: it tells you
> whether HDR is actually active, whether a base profile has been imported, and
> whether Live Apply is on. It opens automatically the first time you run the app.

1. Run **Windows HDR Calibration** and complete its black-level, peak-luminance, full-frame luminance, and color-saturation calibration, then save the profile it generates. Skip this if you already have an HDR profile you trust.
2. Start Virtual HDR OSD for Windows.
3. Select the correct monitor under **1 · Target Display** and switch **HDR On** next to it.
4. In **2 · Profiles for this Display**, pick that profile from the **HDR** dropdown. It loads as the editable base immediately.
5. Set the **SDR** dropdown (see below). If another program calibrates your SDR, choose *Leave unmanaged*.
6. Use **Import…** only for a file that is not installed in the Windows colour folder.
7. Enable **Live Apply** if you want every adjustment to be reflected on the display automatically.
8. Make small tonal and color corrections. **Reset All Sliders** returns everything to neutral, and **Revert to Base** reloads the imported profile untouched.
9. Flip the **HDR** switch in row 1 off and on when you want to compare white balance, overall color appearance, and perceived brightness against the SDR desktop.
10. When satisfied, use **Export Copy…** to save the result.

Windows HDR Calibration is the preferred base because it is specifically designed to calibrate HDR-capable displays under Windows 11. Virtual HDR OSD is best treated as the final subjective fine-adjustment stage rather than the primary HDR calibration stage.

---

# Installation

## Install the compiled portable .exe from Releases page

The easiest way to install it is to go to the “Releases” section of this repository and download the attached .exe file. It's portable and contains the entire app. 

## Run from source

The source edition uses a self-contained Python/uv environment. Python, uv, and the virtual environment remain inside the project directory and do not require a system-wide Python installation.

Clone the repository or download it as a ZIP, then run:

```text
1- Install & Run.bat
```

On first launch the script prepares the project-local runtime. Subsequent launches reuse it.

## Build the single portable EXE (advanced usera & developers)

Run:

```text
3- (Advanced users & developers) - Build Portable EXE.bat
```

The builder creates only the portable one-file release:

```text
release\
├─ Virtual HDR OSD for Windows.exe
└─ Virtual HDR OSD for Windows.sha256.txt
```

The resulting EXE contains the Python/Qt application runtime and does not require Python, uv, the source tree, or the development virtual environment on the destination PC.

The builder intentionally does **not** create a second standalone-folder distribution. It first runs the project's tests, then builds the GUI with Nuitka in one-file mode and disables the console window.

---

# Interface overview

The top bar is numbered. Everything that writes to your Windows colour
configuration lives at the right-hand end of row 3:

- **1 · Target Display** — selects the display, and turns its HDR on or off without leaving the app.
- **2 · Profiles for this Display** — pins which installed profile is this display's SDR profile and which HDR profile the sliders edit. Remembered per monitor, across restarts.
- **3 · Edits & Apply** — Live Apply, automatic mode switching, and edit management on the left; the two buttons on the right are the ones that write to Windows.

Below that:

- **Tone & Brightness** — fine tonal controls, plus the **SDR-in-HDR Gamma Correction** dropdown (optional Windows piecewise-sRGB → pure gamma 2.2 correction, with automatic SDR-white readback and global hotkeys).
- **Color & White Balance** — white-point, chroma, and RGB fine adjustments.
- **Getting Started** — the step-by-step walkthrough, with live progress checks.
- **Watchdog Settings…** — installs/removes the independent association watchdog and persistent gamma hotkeys.
- **Help** — the full usage guide, control reference, and recovery notes.

Two bars run along the bottom:

- **Activity bar** — the HDR profile Windows currently has associated, whether this window owns the Alt+1 / Alt+2 hotkeys, and whether your sliders differ from what is installed.
- **Status** — reports profile operations, mode changes, and errors.

Almost every interactive control has a tooltip. Hover over a button, switch, slider, numeric field, or status element for a concise explanation.

## Pinning the SDR and HDR profiles

Row 2 lists every ICC/ICM profile installed on the PC, so both choices are made
in the app rather than through Windows' colour management dialogs.

**HDR** selects the profile the sliders edit. Choosing one loads it as the base
immediately — no import, no restart, no reapplying your settings afterwards. The
app's own working profiles are deliberately excluded from this list, so edits can
never compound on already-edited data.

**SDR** decides what happens when Windows drops back to SDR:

| Setting | Behaviour |
|---|---|
| *Auto* (default) | Restores whatever profile Windows had associated when the app last observed it. |
| *Leave unmanaged* | The app never touches the SDR association at all. |
| A specific profile | That profile is restored on every HDR → SDR transition. |

Both choices are stored per monitor and survive restarts. They are keyed on the
monitor's device path rather than its adapter LUID, because Windows reissues
adapter LUIDs on reboot — anything keyed on those would be lost every restart.

> [!IMPORTANT]
> **If you calibrate SDR with Calman, DisplayCAL, i1Profiler or similar**, choose
> *Leave unmanaged*. Those tools install an SDR profile *and* run a loader that
> re-asserts its VCGT calibration curve; a second program re-associating profiles
> behind their back is how calibration silently breaks. With *Leave unmanaged*
> this app confines itself entirely to the HDR (EXTENDED) association.
>
> Even on *Auto* or a pinned profile, the app checks what Windows already has and
> skips the write when it matches, so it never re-asserts an association
> needlessly. Note that restoring an ICC association does **not** reload a VCGT —
> that is the job of Windows' calibration loader or your calibration software's
> own service.

## Knowing what is actually applied

The activity bar answers the two questions that are otherwise invisible:

- **Active HDR profile** is read back from Windows, not from what the app last
  wrote. If a previous session or the watchdog left a different variant
  associated, that is what you will see. The correction status shown next to it
  is derived from the active filename, so it cannot contradict reality.
- **Unapplied edits** appears only once you have changed a slider since the last
  apply. A freshly opened window shows *Not applied this session* instead, which
  is a different thing. The **Apply Edits** button gains a `•` marker whenever
  there is something to apply.

---

# Target Display

## Target Display

Selects the physical Windows display that Virtual HDR OSD will monitor and edit.

HDR state and profile associations are tracked per display. On multi-monitor systems, always confirm that the intended HDR monitor is selected before importing or applying a profile.

## Windows mode indicator

The status badge reports the mode currently detected for the selected display:

```text
Windows mode: HDR
```

or:

```text
Windows mode: SDR
```

The application is an HDR editor. SDR is exposed primarily so that the user can compare modes and so that profile associations can be handled safely during an SDR/HDR transition.

## Refresh

Rescans the active Windows display topology.

Use this after:

- connecting or disconnecting a monitor;
- changing display topology;
- changing GPU/display routing;
- waking a display that was unavailable;
- Windows fails to show the expected monitor.

## HDR switch

Turns Windows HDR on or off for the display selected in the same row.

This targets that specific display, unlike `Win + Alt + B`, which only ever
toggles whichever display Windows currently considers the active one — on a
multi-monitor system the shortcut frequently switches the wrong panel. Flipping
this switch is also the quickest way to compare the HDR result against SDR.

Virtual HDR OSD does **not** create an SDR profile. On an HDR → SDR transition it
only restores the SDR profile you pinned in row 2, or the one Windows already had,
and does nothing at all when SDR is set to *Leave unmanaged*.

## Display Settings

Opens the main Windows display settings page.

Use it for Windows-level display configuration that is intentionally outside the scope of Virtual HDR OSD.

## Profile Folder

Opens the Windows system color-profile directory, normally:

```text
C:\Windows\System32\spool\drivers\color
```

This is where installed ICC/ICM profiles are normally stored.

---

### Stable working-profile model

Virtual HDR OSD never edits or overwrites the user's original Windows HDR Calibration profile. When HDR becomes active, the application reads the **currently selected Windows HDR profile** and treats it as the immutable base for the editing session. It then maintains at most two app-owned working profiles for that display:

```text
Virtual_HDR_OSD_<display>_Off.icm
Virtual_HDR_OSD_<display>_On.icm
```

The names are stable and reused. Slider changes, Live Apply, gamma-correction changes, and SDR/HDR transitions replace these working copies instead of creating timestamped profiles. The original HDR profile remains the source and safe fallback.

# Apply to Windows

This section controls when the edited HDR profile is generated, associated, and reapplied.

## Reapply

Forces a full reinstall of the current settings, bypassing the change detection
described below.

This is useful when:

- Windows appears to have dropped the expected HDR association;
- you have just changed SDR/HDR mode;
- the display or graphics driver was reset;
- you want to explicitly force the current settings back onto Windows.

It does not reset any sliders.

### Change detection

Applying does not blindly reinstall. Before touching Windows, the application
compares the profile it just generated against the copy already installed in the
Windows colour directory, ignoring the ICC creation timestamp so that identical
settings compare equal at any time of day.

If they match, nothing is uninstalled, rewritten, or reinstalled — only the
default association is set. The practical consequences:

- Toggling the SDR-in-HDR correction with **Alt+1** / **Alt+2** is a single
  association call, because the *Correction Off* and *Correction On* profiles are
  both already installed. Neither is regenerated.
- Pressing **Apply Edits** with nothing changed is close to free.
- Live Apply only pays the reinstall cost for edits that genuinely alter the
  profile.

**Reapply** exists precisely for the case where the installed bytes are correct
but the *association* has been lost, which change detection cannot see.

## Live Apply

When enabled, slider changes are automatically converted into an updated HDR profile and applied after a short debounce.

This creates an OSD-like workflow: move a control and observe the display.

When disabled, adjustments remain in the editor until the profile is explicitly applied.

For careful visual matching, **Live Apply** is usually the most convenient mode.

## Automatic Mode Switching

Combines mode tracking and profile recovery into one user-facing option.

When enabled:

### HDR → SDR

Virtual HDR OSD waits briefly for Windows to complete the mode transition and restores the SDR `STANDARD` profile that Windows previously had associated with that display.

If there was no SDR profile, the application does nothing.

### SDR → HDR

After Windows completes the transition back to HDR, Virtual HDR OSD reapplies the active HDR profile.

This is intended to reduce profile-association problems around repeated `Win + Alt + B` transitions.

---

# Profiles for this Display

## SDR and HDR pickers

Two dropdowns listing every colour profile installed on the PC. See
[Pinning the SDR and HDR profiles](#pinning-the-sdr-and-hdr-profiles) above for
what each setting does. Hover the HDR picker to see the full path of the profile
currently loaded as the editable base.

## Import…

Loads an `.icm` or `.icc` HDR profile.

The file picker opens directly in the Windows color-profile directory for convenience.

The recommended source is a profile generated by **Windows HDR Calibration**.

When a profile generated by Virtual HDR OSD is imported again, the application can recover its embedded editor state and restore the corresponding slider values.

Importing a profile does not mean that every arbitrary third-party ICC parameter can be losslessly translated into the application's slider model. The controls represent Virtual HDR OSD's correction layer.

## Export Copy…

Writes the current edited HDR profile to an `.icm` or `.icc` file. It does not
install anything or change any association.

Use this once the desired appearance has been reached. Because the exported file
embeds your exact slider positions, re-importing it restores them precisely,
which makes an export a reliable backup before experimenting further.

## Revert to Base

Discards your slider edits and reloads the base profile you imported, exactly as
it is on disk. Asks for confirmation first, and cannot be undone.

Use it when a session of adjustments has drifted somewhere you do not want, and
you would rather restart from the Windows HDR Calibration result than try to
reverse each control.

## Reset All Sliders

Returns every control to its neutral default, keeping the loaded base profile.
Asks for confirmation first, and cannot be undone.

The difference from **Revert to Base**: this neutralises your correction layer
while leaving the base profile selected, whereas Revert re-reads the file and
restores whatever slider state that file implies.

Neither button changes anything in Windows on its own — apply afterwards if you
want the result installed.

## Apply Edits

Generates the current HDR profile, installs/associates it for the selected display, and applies it immediately.

Use this when **Live Apply** is disabled or whenever an explicit final application is desired.

> [!NOTE]
> **Apply Edits** applies the sliders exactly as they currently stand. It does not
> re-read the profile selected in the HDR picker, so clicking it can never
> silently discard adjustments you have just made. To deliberately go back to the
> file on disk, use **Revert to Base**.

If Windows is not in HDR mode for the selected display, applying is refused with
an explanatory message rather than silently doing nothing.

---

# SDR-in-HDR Gamma Correction

Virtual HDR OSD optionally integrates the method documented by Dylan Raga in:

```text
https://github.com/dylanraga/win11hdr-srgb-to-gamma2.2-icm
```

Windows 11 normally represents ordinary SDR content inside the HDR desktop using the **piecewise sRGB transfer function**. A great deal of PC content was authored or visually tuned on displays behaving closer to a pure gamma 2.2 response, which can make SDR-in-HDR shadows appear more raised or washed out than expected.

The optional correction follows the upstream transform direction:

```text
PQ input
   ↓
ST 2084 EOTF → absolute luminance
   ↓
normalize against Windows SDR reference white
   ↓
piecewise-sRGB signal value
   ↓
interpret through pure gamma 2.2
   ↓
absolute luminance
   ↓
ST 2084 inverse EOTF
```

Values above diffuse SDR white are left untouched by the correction layer. The application's normal **Gamma / Midtone Response** slider remains a separate traditional tone trim and is not reversed or repurposed by this feature.

## Correction dropdown

The dropdown contains:

- **Off** — no SDR-in-HDR curve correction.
- **Auto (Recommended)** — reads the selected HDR display's current Windows SDR reference white internally using DisplayConfig and generates the correction from that value.
- **100 nits / Brightness 5** — upstream published mapping for Windows SDR Content Brightness 5.
- **200 nits / Brightness 30** — upstream published mapping for Windows SDR Content Brightness 30.
- **300 nits / Brightness 55** — upstream published mapping for Windows SDR Content Brightness 55.
- **400 nits / Brightness 80** — upstream published mapping for Windows SDR Content Brightness 80.
- **Unspecified** — compatibility entry matching the upstream download list; when an explicit Windows white level is unavailable, Virtual HDR OSD uses the upstream generator's 200-nit default basis.
- **SDR** — compatibility entry matching the upstream download list, using the traditional 80-nit SDR reference basis.

`Auto` is the recommended choice because it avoids manually duplicating the Windows setting and can use the actual current reference-white value while the GUI is running.

## Important limitation: native HDR content

This is a **display-wide MHC2 correction**. Windows does not provide a clean public mechanism for an ICC/MHC2 display calibration to affect only SDR windows while automatically bypassing native HDR10, YouTube HDR, RTX HDR, or another HDR swap chain on the same desktop.

Consequently, the upstream method can also darken shadows in genuine HDR content. Use the hotkeys when moving between desktop/SDR-in-HDR content and native HDR content:

```text
Alt + 1    Disable SDR-in-HDR gamma correction
Alt + 2    Re-enable the selected correction
```

While Virtual HDR OSD is open, the GUI registers these hotkeys when they are free. If the standalone watchdog is already running, it owns the same global hotkeys and the GUI synchronizes its dropdown/state through the shared runtime state instead of competing for duplicate registrations. The watchdog keeps the hotkeys available after the GUI closes.

Because a hotkey chord can only belong to one process at a time, registration
legitimately fails in that situation. The activity bar therefore reports which
side owns them:

```text
Hotkeys: Alt+1 / Alt+2 active          this window handles them
Hotkeys: not owned by this window      the watchdog, or another app, handles them
```

Hover the indicator for the specific reason. If registration failed for a reason
other than the watchdog — another application claimed `Alt+1` first, for example —
the status bar says so explicitly rather than leaving the keys silently dead.

**Off is authoritative:** choosing `Off` (or pressing `Alt + 1`) immediately applies an uncorrected companion profile. The internal mode watchdog and the standalone watchdog are forbidden from restoring a previously corrected profile while the shared correction state is Off. `Alt + 2` is the only hotkey that re-enables the selected correction.

While the GUI is open, **Auto** reads the current Windows SDR white level and regenerates the correction from that value. The app maintains only two fixed per-display working profiles: **Correction Off** and **Correction On**. Their filenames are reused rather than timestamped, so repeated Live Apply operations or SDR/HDR transitions do not accumulate new profiles in Windows. After the GUI closes, the standalone watchdog switches between those last prepared Off/On working profiles with Alt+1 and Alt+2.

---

# Tone & Brightness

The tone controls are intentionally independent from the Windows **SDR content brightness** setting. Virtual HDR OSD does not expose or modify that Windows slider.

The controls operate on the generated HDR profile.

## Gamma / Midtone Response

**Default:** `2.200`  
**Range:** `1.600 – 3.000`  
**Step:** `0.005`

Adjusts the traditional power-law midtone response.

- `2.200` is the neutral reference used by the editor.
- Lower values brighten the midtone response.
- Higher values darken the midtone response.

Because the step is only `0.005`, subtle changes such as `2.200 → 2.205` are possible.

## Midtone Brightness

**Default:** `0.00%`  
**Range:** `-30.00% – +30.00%`  
**Step:** `0.05%`

Raises or lowers perceived midtone brightness while keeping the black and peak-white endpoints anchored.

Use this when the overall middle of the HDR image appears too dark or too bright but you do not want a simple global offset that moves the endpoints.

This is different from Gamma:

- **Gamma** reshapes the tonal response.
- **Midtone Brightness** biases the middle of the response while retaining the endpoints.

## Contrast / Tonal Separation

**Default:** `0.00%`  
**Range:** `-30.00% – +30.00%`  
**Step:** `0.05%`

Controls tonal separation around the midrange.

Positive values increase perceived separation between nearby tonal levels. Negative values reduce it.

Black and peak-white endpoints remain anchored.

Use small values first. Large contrast changes can make an otherwise well-calibrated HDR profile look unnatural.

---

# Color & White Balance

The color engine is designed for fine correction rather than crude RGB tinting.

Temperature, Tint, RGB balance, and Saturation are composed as colorimetric transformations rather than simple screen overlays.

---

## White Balance Temperature

**Default:** `0 K`  
**Range:** `-3000 K – +3000 K`  
**Step:** `5 K`

Applies a fine white-point offset around the profile's neutral reference.

- Positive values make the image warmer.
- Negative values make the image cooler.

The implementation uses a white-point adaptation rather than simply adding red or blue. This produces a smoother and more useful transition for visual white-balance matching.

A typical workflow is to adjust Temperature first, then use Tint for the remaining green/magenta error.

---

## Green–Magenta Tint

**Default:** `0.00`  
**Range:** `-25.00 – +25.00`  
**Step:** `0.05`

Corrects the white point along the green/magenta direction.

Tint is designed to complement Temperature rather than duplicate it. Its correction direction is derived independently from the primary warm/cool temperature axis.

Use it when the image is approximately the correct warmth but still appears slightly green or magenta.

---

## Color Saturation

**Default:** `0.0%`  
**Range:** `-50.0% – +50.0%`  
**Step:** `0.1%`

Adjusts global chroma intensity.

- Negative values reduce saturation.
- Positive values increase saturation.
- `0%` leaves saturation neutral.

The saturation transform is designed to preserve luminance more effectively than naïvely multiplying RGB channels.

For calibration-oriented use, very small changes are recommended.

---

# RGB Channel Fine Balance

The individual RGB controls are intended as the final stage after Temperature and Tint.

They are composed in linear Rec.2020 and normalized to reduce unintended luminance shifts. This makes them more suitable for precise residual corrections than simple independent channel multipliers.

## Red Fine Balance

**Default:** `0.00%`  
**Range:** `-25.00% – +25.00%`  
**Step:** `0.05%`

Fine adjustment of the red channel.

Useful for residual red/cyan imbalance after the broader white-balance controls have already been adjusted.

## Green Fine Balance

**Default:** `0.00%`  
**Range:** `-25.00% – +25.00%`  
**Step:** `0.05%`

Fine adjustment of the green channel.

Useful for residual green/magenta imbalance.

## Blue Fine Balance

**Default:** `0.00%`  
**Range:** `-25.00% – +25.00%`  
**Step:** `0.05%`

Fine adjustment of the blue channel.

Useful for residual blue/yellow imbalance.

### Recommended order for white-balance matching

For the smoothest result:

```text
1. White Balance Temperature
2. Green–Magenta Tint
3. Red / Green / Blue Fine Balance
4. Color Saturation
```

Avoid immediately compensating a large Temperature error with large opposing RGB corrections. The RGB controls are most useful as fine trims.

---

# Precision controls

Every calibration slider provides three ways to adjust its value.

## Dragging

Drag the slider for fast coarse positioning.

Despite the generous total ranges, the underlying step resolution remains fine.

## Exact Value

The numeric field beside each slider accepts a precise value directly.

Examples:

```text
Gamma:       2.215
Red Balance: 0.35
Tint:       -0.20
```

Press Enter or leave the field to commit the value.

## Mouse-wheel fine calibration

Hover the mouse pointer over either:

- the slider; or
- its **Exact Value** field.

Each mouse-wheel notch changes the value by exactly **one declared Step**.

Examples:

```text
Gamma
2.200 → 2.205 → 2.210
```

```text
Red Fine Balance
0.00% → 0.05% → 0.10%
```

This is the recommended method for very fine visual matching.

## Reset

Returns only that individual control to its neutral/default value.

It does not reset the other controls or replace the imported base profile. To
neutralise every control at once, use **Reset All Sliders** in the top bar.

---

# Suggested visual calibration strategy

A useful subjective matching sequence is:

1. Start from a Windows HDR Calibration profile.
2. Enable **Live Apply**.
3. Display neutral gray or familiar real-world content.
4. Flip the **HDR** switch off and on to establish the SDR appearance you want to approach.
5. Adjust **White Balance Temperature** until the broad warm/cool difference is minimized.
6. Adjust **Green–Magenta Tint**.
7. Use the individual RGB controls only for small remaining errors.
8. Adjust **Gamma / Midtone Response** if the HDR midtones do not perceptually match the desired response.
9. Use **Midtone Brightness** for residual luminance differences.
10. Use **Contrast / Tonal Separation** only if necessary.
11. Adjust **Color Saturation** last.
12. Repeat SDR/HDR comparisons because human vision adapts quickly to white balance.
13. Export the resulting profile.

For critical work, visual matching should not be treated as measured calibration.

---

# Status messages

The status area reports operations such as:

- display detection;
- profile import;
- profile generation;
- profile application;
- Live Apply updates;
- SDR/HDR transitions;
- automatic profile restoration;
- Windows API failures.

If a profile fails to apply, read the status message before repeatedly pressing Apply. It may indicate an invalid profile, unavailable display, Windows association failure, or permissions problem.

---

# Standalone SDR/HDR Color Profile Watchdog

## What it is

The watchdog is a separate utility for users affected by a Windows 11 color-profile association problem when switching between SDR and HDR.

A typical symptom is:

1. SDR has the intended ICC/ICM profile.
2. HDR has a different intended HDR profile.
3. The user presses:

```text
Win + Alt + B
```

4. Windows changes mode, but the expected profile association is no longer the one being used or restored.
5. Repeated SDR ↔ HDR switching can therefore produce inconsistent color until the profile is manually selected/reapplied.

The watchdog exists specifically to make those mode transitions more deterministic.

## Watchdog Settings in the GUI

The main window exposes a dedicated **Watchdog Settings…** button. It opens a small explanatory dialog with **Install Watchdog** and **Uninstall Watchdog** actions. The same underlying BAT files remain independently shareable; the GUI is only a convenient front end for them.

## The watchdog is independent of Virtual HDR OSD

**The standalone watchdog does not require Virtual HDR OSD for Windows.**

It is intentionally useful as a separate Windows utility.

It does not require:

- Virtual HDR OSD;
- the application's source code;
- Python;
- uv;
- the application's `.venv`;
- the HDR editor GUI.

This means the watchdog can be shared separately with another Windows 11 user who has no interest in editing HDR profiles but does experience incorrect SDR/HDR profile restoration.

The standalone installer contains the required watchdog logic itself and installs it under the current user's local application-data directory.

> [!NOTE]
> The standalone Watchdog does not include gamma curve transformation; it only provides a minimal fix for the Windows 11 SDR-HDR profile association bug. To use gamma curve transformation, install Watchdog from the app.
>
> The watchdog is installed to `%LOCALAPPDATA%\ColorProfileModeWatchdog`. Its preferred autostart method is the Windows Task Scheduler COM API using the current account's SID and `InteractiveToken`, which avoids storing credentials and works consistently with local, Microsoft, Entra ID, and domain-backed interactive accounts. The task is named `Virtual HDR OSD - Color Profile Mode Watchdog` and starts hidden 10 seconds after sign-in. If Task Scheduler registration is unavailable on a particular system, the installer automatically falls back to a per-user `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry instead of failing the installation. The GUI-integrated and standalone installers use the same task/fallback name and installation directory; use only the installer matching the workflow you want to configure.

---

## What the standalone watchdog remembers

At installation time, it captures the existing Windows color-profile associations for each active display:

- **STANDARD** — the normal SDR display-profile association.
- **EXTENDED** — the Advanced Color/HDR display-profile association.

If a display has no profile for one of those associations, that missing association is left alone.

---

## What happens when the display mode changes

The watchdog runs silently in the background.

In addition to SDR/HDR association recovery, it watches **Alt+1** and **Alt+2**. Virtual HDR OSD prepares exactly two stable working profiles per display: Alt+1 selects the current edited profile with SDR-in-HDR gamma correction disabled, while Alt+2 selects the same edited state with the chosen correction enabled. The watchdog never creates timestamped profiles or a bank of per-luminance companions. Auto is recalculated from the exact Windows SDR white level whenever the GUI regenerates the working pair.

When Windows changes display mode, it gives the operating system a short period to complete the transition and then reasserts the previously captured profile associations.

Conceptually:

```text
Known SDR profile ──► STANDARD association
Known HDR profile ──► EXTENDED association
                         │
                         ▼
                  Win + Alt + B
                         │
                         ▼
                 Windows mode change
                         │
                         ▼
                Watchdog reassertion
```

The goal is not to alter color. The goal is to preserve the user's already chosen SDR and HDR profiles across the Windows mode transition.

---

# Installing the standalone watchdog

Run:

```text
2- OPTIONAL - Install-Watchdog.bat
```

The installer captures the **currently configured** SDR and HDR associations.

Therefore, before installation:

1. Configure the desired SDR profile in Windows.
2. Configure the desired HDR profile.
3. Confirm that both are the profiles you actually want.
4. Run the watchdog installer.

If you intentionally change either default profile later, rerun the installer so it captures the new configuration.

---

# Invisible/background operation

The standalone watchdog is designed to operate discreetly.

It does not need an open CMD window.

Its preferred startup method is a per-user Task Scheduler task registered through the Windows Task Scheduler COM API using the current account SID. The task launches Windows Script Host in background mode after a 10-second sign-in delay. If Task Scheduler registration fails, the installer transparently uses a per-user `HKCU\...\Run` fallback. There is no normal foreground application window to keep open.

The installed utility is stored under:

```text
%LOCALAPPDATA%\ColorProfileModeWatchdog\
```

Its persistent startup registration is normally the per-user Windows Task Scheduler task `Virtual HDR OSD - Color Profile Mode Watchdog`; on systems where Task Scheduler registration is rejected, the installer falls back to a current-user Run-key entry with the same watchdog installation.

No Windows service is required.

---

# Uninstalling the watchdog

Run:

```text
Uninstall-Watchdog.bat
```

The uninstaller:

- stops the watchdog instance belonging to the utility;
- removes its per-user startup registration;
- removes the watchdog's local files;
- does **not** delete ICC/ICM profiles;
- does **not** intentionally change the user's selected color profiles.

---

# Watchdog safety model

The watchdog follows a deliberately conservative policy.

It does **not**:

- create an SDR neutral profile;
- create an HDR neutral profile;
- calibrate the display;
- edit ICC/ICM profile contents;
- delete installed color profiles;
- continuously change color parameters;
- replace profiles with generic fallbacks.

Its job is association recovery only.

This makes it suitable as a standalone workaround for users whose existing SDR and HDR profiles are correct but whose Windows 11 mode switching does not consistently preserve those associations.

---

# Multi-monitor considerations

Both Virtual HDR OSD and the standalone watchdog identify displays through the Windows display configuration APIs.

For best results:

- install/capture the watchdog while the displays you normally use are connected;
- rerun the watchdog installer after materially changing the monitor/GPU topology;
- rerun it after intentionally replacing the SDR or HDR default profile;
- verify profile associations after changing GPU drivers or reorganizing display connections.

The watchdog should not be treated as a profile-discovery system. It protects the configuration captured at installation time.

---

# Troubleshooting

## A profile does not appear to change the image

Confirm:

1. the correct **Target Display** is selected;
2. Windows is actually in HDR mode;
3. the imported file is the intended HDR profile;
4. **Live Apply** is enabled, or press **Apply Edits**;
5. the application status area does not report an API error.

## SDR looks wrong after Win + Alt + B

If the GUI is open, enable **Automatic Mode Switching**.

For protection when the GUI is closed, install the standalone watchdog.

Before installing it, make sure the desired SDR and HDR profiles are already selected because those are the associations it captures.

## I changed my SDR/HDR profile after installing the watchdog

Run the watchdog installer again.

It will capture the new intended associations.

## The watchdog should no longer start with Windows

Run:

```text
Uninstall-Watchdog.bat
```

## I want to inspect the installed color profiles manually

Use **Profile Folder** in the application or open:

```text
C:\Windows\System32\spool\drivers\color
```

---

# Technical notes

Virtual HDR OSD works with Windows ICC/ICM HDR profile infrastructure and MHC2-based corrections.

The application separates the user-facing corrections into two broad groups:

### Tone transformation

```text
Gamma
   ↓
Midtone Brightness
   ↓
Contrast
   ↓
HDR tone LUT
```

### Color transformation

```text
White Balance Temperature
   ↓
Green–Magenta Tint
   ↓
RGB Fine Balance
   ↓
Color Saturation
   ↓
Colorimetric correction matrix
```

The resulting transforms are embedded into the generated HDR profile and applied through Windows color-profile association APIs.

The standalone watchdog uses Windows display/profile APIs to query and restore the display's default ICC profile associations. Modern Windows exposes separate profile subtypes for standard display color mode and extended/Advanced Color display mode.

---

# Scope and limitations

Virtual HDR OSD is intentionally narrow in scope.

It is:

- an HDR profile fine-adjustment tool;
- a virtual replacement for some OSD adjustments unavailable in HDR;
- a subjective SDR/HDR visual matching aid;
- a convenient live ICC/ICM editor;
- a Windows 11 profile-association helper.

It is not:

- a hardware calibration instrument;
- a replacement for Windows HDR Calibration;
- a colorimeter/spectrophotometer workflow;
- a display characterization laboratory;
- a guarantee of reference-grade color accuracy;
- a substitute for proper mastering/reference equipment.

The most reliable workflow remains:

```text
Measurement / Windows HDR Calibration
                ↓
        valid HDR base profile
                ↓
       Virtual HDR OSD fine trim
                ↓
         subjective final result
```

---

# Recommended base calibration

Microsoft's **Windows HDR Calibration** application is available through the Microsoft Store and is designed for Windows 11 HDR displays.

It calibrates important HDR display characteristics before Virtual HDR OSD's subjective correction stage.

---

# SDR-in-HDR gamma-correction reference

The optional SDR-in-HDR correction is an independent Python implementation of the transfer-function method documented by **dylanraga / win11hdr-srgb-to-gamma2.2-icm**. It follows the current NVIDIA-path generator logic: interpret the MHC2 LUT input as PQ/ST 2084, convert to absolute luminance, derive the piecewise-sRGB signal relative to SDR white, reinterpret that signal through a pure gamma-2.2 power law, then convert the result back to PQ. Values above diffuse SDR white are left unchanged.

Reference project:

```text
https://github.com/dylanraga/win11hdr-srgb-to-gamma2.2-icm
```

No upstream ICC/ICM binaries, ArgyllCMS executables, or AutoHotkey scripts are bundled by Virtual HDR OSD.

---

# License and third-party components

See the project's third-party notices and dependency metadata for the licenses of bundled or installed dependencies.

Virtual HDR OSD uses PySide6 and PySide6-Fluent-Widgets for its graphical interface. Review the applicable dependency licenses before redistribution.


### Standalone hotkey persistence

When installed after Virtual HDR OSD has prepared the stable `Correction Off` and `Correction On` working profiles, the standalone watchdog copies both exact installed profile names into its own `%LOCALAPPDATA%\ColorProfileModeWatchdog\State.json`. **Alt+1 / Alt+2 therefore do not depend on the GUI remaining open or on its runtime JSON remaining available.** A dedicated Win32 background thread owns `RegisterHotKey` and a blocking `GetMessage` loop, while the PowerShell watchdog continues handling display-mode/profile recovery separately.


### Watchdog stable-pair capture

The standalone watchdog does not trust legacy gamma companion filenames. At installation it examines the HDR profile Windows is actually using. If the active profile is a current Virtual HDR OSD stable slot such as `Virtual_HDR_OSD_<display-token>_On.icm`, the watchdog derives the matching `_Off.icm` sibling and verifies both files in the Windows color-profile directory before storing them in its own state. Stale `VirtualHDR_OSD_Gamma_*` references are ignored.

If only one side of the stable pair exists, installation stops with an explicit message instead of installing a watchdog that cannot service Alt+1 / Alt+2.
