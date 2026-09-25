# Recovery notes

These notes were written on 24 September 2026. `flint-pc` had been reformatted on about 12 September, and C: was lost with no backup: the user profile, `%LOCALAPPDATA%`, the earlier Claude Code transcripts and the installed watchdog. D: survived.

They record what was established so that the next session does not start from zero. Everything here comes from the code, from commands and from files; nothing comes from memory, and a commit message on its own does not count as evidence. Each mechanical check was first run against a case known to exist. Nothing in this work called a Windows API that changes colour or display configuration.

## Where the work was dropped

**Proven**
- `D:\git_projects\VHDR-fork` is the only source copy. Every fixed drive and 2,573 zips were searched.
- The only other artefact is a single Nuitka build dated 14 August, `D:\Proton-Drive\My files\Software\Virtual.HDR.OSD.for.Windows.exe`. It predates the fork. Which build it is remains unverified, because comparing it with upstream's release asset needs a download.
- Nothing was in flight:
  - no uncommitted changes, stashes, unpushed commits or unreachable commits;
  - local, `origin` and GitHub all agree;
  - PR #1 merged `reliability-and-usability-pass` into `main` as `8504bd2`, tagged `1.1.0`, and CI passed.
- Upstream (`Mixomo`) has no commits the fork lacks. `0c871e4` recorded its last two commits without taking their content.
- The plan is `docs/adr/0001-split.md`. Its open item is Phase 1's Independent-Flip question (a PresentMon capture plus a tint test, to be recorded in `docs/measurements/`). That folder does not exist.
- `0c871e4` promised three upstream watchdog ideas; none landed:
  - Alt+3 manual restore;
  - independent per-hotkey registration;
  - a stale-key sweep of `$lastModes`.
- Upstream issue #2, opened 18 September, reports unreliable automatic association recovery.

**Lost with C:**
- `last_gui_state.json`
- `gamma_hotkeys.json`
- `original_profiles.json`
- `meter_log.jsonl`, and with it every measurement taken before 12 September
- the installed watchdog: its folder, its scheduled task and its Run key.

## The two pre-reformat planning pages

Both were written at `b450ff8` and both are on claude.ai:
- The review, ["Where Virtual HDR OSD stands"](https://claude.ai/artifact/KHEgLpVEB4iJn4aNjGirGV), dated 10 September.
- The architecture decision, ["Split it. Don't restart it."](https://claude.ai/artifact/Nbpogp1ujK7HFjJYD7TYvt), dated 11 September. `docs/adr/0001-split.md` is its committed summary.

Checked against `8504bd2` on 24 September:

| | Fixed or done | Partial | Still open | Correct, keep | Needs hardware |
| --- | --- | --- | --- | --- | --- |
| Review findings (70) | 5 | 13 | 40 | 10 | 2 |
| Review roadmap rows (27) | 0 | 8 | 19 | | |

The ADR's Phase 0 row now records the two tasks from the decision page that were never done:
- an uninstaller that restores the original profile;
- Gamma held below diffuse white with the correction off.

## Environment

- The project-local uv (`.uv\uv.exe`, 0.11.32) and Python (`.python`, CPython 3.12.13) survived on D: and work.
- `.venv` is intact. `uv sync --locked --check --offline` finds all 172 locked packages. The editable project's metadata still says version `0`; nothing reads it, because `__version__` comes from `src/.../__init__.py`.
- Installed on C: after the reformat and not used by the project: Git 2.55, Python 3.11.9 and winget uv 0.12.18.
- ArgyllCMS 3.5.0 is at `D:\Argyll\Argyll_V3.5.0`. It is not on PATH and not in `meter._SEARCH_DIRS`.
- `pyproject.toml` configures no linter or type checker, so the baseline is the test suite alone.

## Running the tests on a real machine

The build runs `python -m unittest discover -s tests -v` with `PYTHONPATH=src`.

On 24 September every sink the suite could reach that changes the machine was traced by reading the code. Examples are profile install and association, the HDR toggle, DDC writes, UAC, hotkeys, process launches, Direct3D, the watchdog harnesses and the network. The trace was then confirmed at run time with a scratch runner that turns each real sink into a tripwire. Across 1,167 tests, with every guard checked as armed first, no tripwire fired.

One gap was found and closed in `f36631f`. Six measurement tests started a real `PlacementWatcher` thread that would run `spotread` against a plugged-in meter. They escaped only because test cleanup won a race. `WindowTestCase` now fakes `read_emissive` and DDC/CI for every window test, and `FixtureHardwareTests` checks that.

The stdlib-only subset that CI runs takes about 40 s. The full suite takes about an hour on this machine, most of it in `test_gui`.

## Architecture map

| Area | Files | Tests | Notably untested |
| --- | --- | --- | --- |
| ctypes Windows layer | `windows_api.py`, `edid.py`, `ddc.py`, `elevation.py`, `hotkeys.py` | `test_core` (struct sizes, overwrite), `test_edid`, `test_ddc`, `test_elevation` | The real mscms and DisplayConfig calls (faked in `test_gui`); `hotkeys` |
| Struct layouts | 15 DisplayConfig sizes asserted at import; DXGI, D3D11 and PHYSICAL_MONITOR pinned by tests since `a8dfec1` | `test_core`, `test_hdr_display`, `test_ddc` | Only against Microsoft Learn's definitions: no SDK header is installed |
| D3D11 scRGB renderer | `hdr_display.py`, `pattern_view.py`, `measure_view.py` | `test_hdr_display`, `test_pattern_view`, `test_measure_view` (surface faked) | `HdrSurface.resize` and `close`; `show_placement_target` |
| ICC / MHC2 | `icc.py` | `test_core`; the MHC2 bytes against Microsoft's table since `6452077` | Third-party `.icm` files; the `vcgt` import path |
| Colour and tone maths | `curves.py`, `gamma_correction.py`, `greyscale.py`, `delta_itp.py`, `patterns.py` | `test_core`, `test_greyscale`, `test_delta_itp`, `test_patterns` | Golden vectors for PQ, sRGB, Bradford and ICtCp |
| Measurement | `meter.py`, `measure.py`, `measure_view.py`, `tools/*_report.py` | `test_meter`, `test_measure`, `test_measure_view`, parts of `test_gui` | `_terminate`'s kill path; closing the main window mid-run; the tools |
| Qt UI and threading | `app.py` (about 5,100 lines), `dialogs.py`, `controls.py`, `__main__.py` | `test_gui` (65 classes), `test_measure_view` | Many handlers are reached only by calling them directly |
| Settings and state | `model.py`; five JSON files written by `app.py` | `test_gui`, `test_core` | Schema keys are written but never read |
| Watchdogs | `2- OPTIONAL - Install-Watchdog.bat`, byte-identical to the copies in `resources\` and `watchdogs standalone\`; `Uninstall-Watchdog.bat` | `test_watchdog` (6 payload functions run in real PowerShell); `test_packaging` (mostly text checks) | The main loop, the hotkey thread, task registration, the uninstaller |
| Build, CI, packaging | `Install.ps1`, the `.bat` launchers, `tools/portable_entry.py`, `.github/workflows/tests.yml` | `test_packaging` | The Nuitka build; CI never runs the three Qt test modules |

The standalone watchdog is **not** a different implementation. All three copies of the installer are the same bytes and carry the gamma hotkeys. The README said otherwise until `420132c`.

## Baseline and final run

Both runs used the command the build uses, `python -m unittest discover -s tests -v` with `PYTHONPATH=src` and `QT_QPA_PLATFORM=offscreen`, with the project's `.venv`.

| | Commit | Tests | Result | Skipped | Duration |
| --- | --- | --- | --- | --- | --- |
| Baseline | `8504bd2` | 1,173 | OK, 2 expected failures | 0 | 1,651 s |
| Final | `3405b87` | 1,191 | OK, 2 expected failures | 0 | 1,770 s |

- **Test count:** the difference of 18 is exactly the tests this session added. Nothing that passed before fails now.
- **Expected failures:** the two are `test_import_boundary`'s marked crossings, which ADR Phase 2 removes.
- **Durations:** both are approximate. Other test runs shared the machine during the baseline.
- **Tripwire runs:** the whole suite was also run under a scratch tripwire runner, 1,167 tests at the baseline and all 1,191 at the end (including the six measurement tests that used to reach the meter). No real sink was reached either time.
- **The runner's three errors:** each run showed three errors in `test_patterns.DeclaredMetadataTests`, caused by the runner rather than the code. Those tests import `tests.test_hdr_display`, which needs the repository root on `sys.path`. `unittest discover` from the root provides that; a script started elsewhere does not. This is low severity and not fixed.
- **No other checks:** `pyproject.toml` configures no linter or type checker.

## What changed in this session

Branch `recovery-cleanup`, cut from `main` (`8504bd2`). Nothing has been pushed.

**Phase 3: behaviour-preserving cleanup**

| Commit | Change |
| --- | --- |
| `1566475` | The ADR's Phase 0 row records the two tasks that were not done |
| `4af8bee` | Removed 9 unreachable items: `measure._channel_matrix`, `MeasureWindow.placed`, three orphaned guide actions and their Store-page helper, and three DDC constants |
| `f0bbfb1` | 16 stale comments corrected (comments only: the AST is unchanged) |
| `81120e9` | Silent broad `except` handlers routed to a debug trace. `VIRTUAL_HDR_OSD_TRACE=1` writes `trace.log`. Silent broad handlers went from 36 to 7 |
| `46b2477` | `live_registry.json` stores only the field that is read |
| `d6e1164` | A test renamed for what it actually checks |
| `3405b87` | Three test docstrings corrected about the watchdog's timing |

**Phase 4: tests and fixes, each proven by a test that fails without it**

| Commit | Change |
| --- | --- |
| `a8dfec1` | Pinned the DXGI, D3D11 and PHYSICAL_MONITOR struct layouts |
| `280c2d2` | Fixed an invalid escape in `test_packaging` (a SyntaxWarning since Python 3.12) |
| `f36631f` | The window-test fixture fakes the meter and DDC/CI |
| `0a0a687` | An infinite number in the state file no longer stops the app starting |
| `ba2faa7` | A placement thread still inside a read is held rather than dropped. Dropping it was a silent process abort, reproduced with exit code 127 |
| `6bd43b3` | A settings or originals file that could not be opened is never saved over. The originals case could defeat Restore Windows Profile |
| `df64a7f` | The pattern summary's key hint names key 0 |
| `ec9d187` | Nothing is applied while a meter run is up |
| `420132c` | Six README statements the code contradicts were corrected |
| `6452077` | The MHC2 tag is checked against Microsoft's published layout |

## Open: needs the owner's decision

| Finding | Evidence | Why it is not fixed here |
| --- | --- | --- |
| README 484-490 says Restore holds against any installed watchdog and that Alt+1/Alt+2 are refused while restored. Neither holds when the watchdog owns the hotkeys | `Install-Watchdog.bat` never reads `restored`; `Invoke-GammaHotkey` writes at :1380 unguarded | The fix could go in the watchdog (which changes its build id and the standalone zip) or in the README |
| `Launcher.vbs` is written as ASCII, so a non-ASCII profile path should stop the watchdog ever running | `.bat:1570` `-Encoding ASCII` | A watchdog change; verify on hardware first |
| Mutex probes treat access-denied as "not running"; the uninstaller cannot see or stop an elevated watchdog; the elevated `Start-Process` fallback | `.bat:154-156`, `windows_api.watchdog_is_running`, `Uninstall-Watchdog.bat:30-34`, `.bat:1737-1741` | Watchdog changes; needs an elevation test |
| A torn or locked read of `gamma_hotkeys.json` drops the other displays' records | `app.py` `_runtime_entry` | Multi-monitor only; an existing test fixes "start over" as the intended behaviour |
| Refusing to save for the rest of a session after a locked settings file (`6bd43b3`) | | Retrying the read later is the alternative |
| Closing the main window mid-run does not join the measurement thread; a placement thread still draining at exit | `app.py` `closeEvent` | Needs a decision on how long closing may block |
| Esc does not interrupt a `spotread` already in flight; the placement loop can take about 50 minutes in the worst case | `meter.read_emissive`, `measure_view.PlacementWatcher` | A change to the measurement design |
| Controls with no accessible names; the qfluentwidgets ComboBox cannot be opened from the keyboard; 21 error messages give no next step; the 1080x720 minimum window size | UI audit | UX work already on the review's roadmap (row 19) |
| `pq_eotf(NaN)` returns 10,000 nits; `pq_inverse_eotf(inf)` returns NaN; input above 10,000 nits gives a PQ code above 1.0 | Probed directly | No caller found that can pass these values |
| Hand-edited state: `"gamma": 1.0` reads as the legacy trim (3.0); `null` becomes the string `"None"`; `"false"` becomes True | `model.py` | Only reachable by editing the file by hand |
| The build does not use `uv.lock`, and `ordered-set` and `zstandard` are unpinned | Build `.bat`:49-53 | Packaging policy |
| `ddc_tune.py` is still present; the ADR removes it in Phase 2 | Only imported by `tests/test_ddc.py` | ADR Phase 2 |
| Duplicated maths: the D65 constants, `_matvec3` (three copies), `clamp` (two), 3x3 inverses with different contracts | Architecture map | ADR Phase 2 moves the colour core into a single package; `curves` and `greyscale` import each other, which blocks merging `clamp` |

## Verification gaps: manual procedures

These need hardware this session could not use. Run them before relying on the changes above.

1. **The placement-thread fix (`ba2faa7`).**
   1. With the i1 DisplayPro attached and `spotread` found, press Measure… and confirm.
   2. While the green target is up, hold the meter away from the screen, then press **Esc**.
   3. Repeat, pressing **Enter** instead.
   4. The app must stay open both times, and the status line must say the measurement was cancelled.
   5. Separately, time `spotread -e -x -O` on a black screen. If one reading takes longer than 5 s, the crash this fixes was reachable.
2. **Nothing is applied during a run (`ec9d187`).**
   1. With two monitors, start Measure… on the HDR display.
   2. Put the main window on the other monitor and press **Apply Edits** mid-run.
   3. The status line must say the apply was refused, the run must complete, and nothing in Settings > Display > Color profile may change during the run.
3. **Locked files (`6bd43b3`).**
   1. Close the app.
   2. In PowerShell, run `$f = [IO.File]::Open("$env:LOCALAPPDATA\Virtual_HDR_OSD_for_Windows\last_gui_state.json", 'Open', 'Read', 'None')`.
   3. Start the app. The status line must say the file could not be opened and will not be saved over.
   4. Close the app, run `$f.Close()`, and confirm the file's contents are unchanged.
   5. Repeat with `original_profiles.json` after a Restore Windows Profile, and confirm Restore is still in effect after a restart.
4. **The trace switch (`81120e9`).** From a Command Prompt, run `set VIRTUAL_HDR_OSD_TRACE=1` and then `"1- Install & Run.bat"`. `%LOCALAPPDATA%\Virtual_HDR_OSD_for_Windows\trace.log` must start with the version line.
5. **A full measurement run**, because `measure_view.py` and the placement path changed. Run a full sweep, then Apply, and compare the status line and `meter_log.jsonl` with a run from 1.1.0.
6. **The Independent-Flip question (ADR Phase 1).**
   1. Apply a visibly tinted profile, for example Red −20.
   2. Start a VRR HDR game fullscreen.
   3. Capture with PresentMon: `PresentMon --process_name <game>.exe --output_file flip.csv`.
   4. Confirm the tint is visible while `PresentMode` reads `Hardware: Independent Flip`.
   5. Record the result in `docs/measurements/`.
7. **The watchdog (no changes this session).**
   1. Install it from the app, then check with `schtasks /query /tn "Virtual HDR OSD - Color Profile Mode Watchdog"`.
   2. Test Alt+1 and Alt+2.
   3. After Restore Windows Profile, press Alt+1 and Alt+2. Check whether the association changes (README 487-490), and read `%LOCALAPPDATA%\ColorProfileModeWatchdog\Watchdog.log`.
   4. On an account whose name has a non-ASCII character, install it and check that `Watchdog.log` shows it running (T1).
   5. With an elevated watchdog running, check whether the app's lock switch sees it and whether the uninstaller stops it.
8. **Sleep, wake and hot-plug.**
   1. Unplug and replug the monitor.
   2. Press Ctrl+Win+Shift+B.
   3. Sleep and wake the PC.
   4. After each, the selected display must be unchanged and the association correct within about 2 s.
9. **Multi-monitor.** With two HDR displays, apply different corrections to each and restart. Neither may lose its binding or its record in `gamma_hotkeys.json`.

## Resuming

The plan remains the ADR. Phase 1 is open on the Independent-Flip measurement, and Phase 2 is the `vhdr-color` extraction. The decisions above are waiting.

The scratch files from this session were outside the repository: the tripwire runner, the audit outputs and the raw test logs. They are not needed to resume. Every fact they supported is recorded here or in the commit messages.
