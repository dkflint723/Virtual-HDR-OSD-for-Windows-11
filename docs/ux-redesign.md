<!--
Produced 2026-08-29 by a judge-panel analysis: five independent audits of the current
GUI, four redesigns written from four different starting philosophies, three judging
lenses (owner / newcomer / maintaining engineer), one synthesis.

Nothing here is implemented. It is a proposal, kept in the repository so the reasoning
survives the session that produced it.

Two caveats worth carrying:

* The control counts (63 interactive widgets, 45 on screen at once) come from agents
  reading app.py. The structure was spot-checked; every individual number was not.
* The aggregate ranking first reported "Instrument" last because the tally keyed on the
  design's name and two judges wrote it differently from the third. Its real total is
  117 -- second place. The winner, Ground Truth at 124, is unaffected. The table below
  is the corrected one.

A condensed version is published at
https://claude.ai/code/artifact/4eb6cc4f-ff4a-452d-9784-457adabb817a
-->

# Virtual HDR OSD — Redesign Recommendation

**Build Ground Truth's screen. Graft Instrument's mechanism work onto it. Do not start fresh.**

---

## 1. THE VERDICT: restructure — and the answer to "should I start totally fresh" is no

You asked directly, so here is the direct answer: **starting fresh would cost you 4–6 months and would probably end with a worse app than you have now.** Not because the code is beautiful — `app.py` is a 4,087-line class with one `setEnabled` call in it — but because the value in this repository is not in its structure. It is in its bug history, and a rewrite re-derives that history from prose.

The measurable case, verified against the tree:

The package is 12,703 lines. `app.py` is 4,087 of them; the other **8,616 lines are correct, hardware-verified, and almost entirely UI-agnostic** — `measure.py` (1,179), `pattern_view.py` + `patterns.py` (2,052), `windows_api.py` (912), `icc.py` (770), `hdr_display.py` (484), `measure_view.py` (485), `meter.py`, `curves.py`, `greyscale.py`, `model.py`, `edid.py`. Every one of those files carries comments recording a specific failure found on hardware: DXGI reporting the associated profile's primaries back as the panel's; `InstallColorProfileW` returning TRUE without copying anything; `channel_contributions` recovering each primary's magnitude from the white measured beside it so a panel reading its primaries at 2.2× their share of white still solves; `BlockingQueuedConnection` so a patch is read only once presented; `DirectConnection` for cancel because the worker's loop is busy; compose-for-white-balance versus replace-for-the-response. **The 2.79% median greyscale figure is the product of those specific fixes, not of the general approach.** A fresh build has a material chance of never reaching it, and no way to tell that it hasn't.

Inside `app.py` the split is cleaner than its size suggests. Roughly **490 lines are pure layout** — `_build_ui`, `_build_global_bar`, `_build_tone_tab`, `_build_color_tab`, `_build_activity_bar`, the page-stack plumbing — plus `dialogs.py`'s 354. That ~840-line view layer should be deleted and rewritten. The remaining ~3,600 lines are orchestration that this redesign changes almost none of in substance, and it reaches the widget tree through only three seams: `self.control_widgets`, `self._set_status` (104 call sites), and `self._selected_display`. That is a presentation restructure by definition.

The engineer judge scored migration credibility highest for Instrument (8) and Ground Truth (7), and lowest for the two designs that promised the most rewriting. That ranking is right, and it points at the same conclusion from the other side: **the plans that survive contact with a solo maintainer are the ones that ship correctness before layout.**

One correction to the record, because it changes the plan. Display Record's central claim — that provenance is already derivable from today's state — is **false**, and I verified it. `panel_source_key` records *which display* a figure came from, not *which tier*: `model.py:86`, "Which display the panel figures above came from, as `DisplayInfo.stable_key`." A measured peak and an EDID-declared peak are the same float in the same `peak_luminance_nits` field. `UNSET_LUMINANCE = (0.0, 1000.0, 400.0)` at `app.py:3715` distinguishes never-set from set, and `panel_response` non-empty tells you grey was measured — and that is all. **Provenance requires a new stored field**, and stamping it at every mutation site is a hard requirement of this design rather than a detail. Three Truths named this risk precisely ("the chip becomes the same class of lie as today's green watchdog line, in a more prominent place") and it is the single thing most likely to make this redesign worse than the status quo if done carelessly. Section 6 says how to prevent it.

**Estimate: 20–25 working days for the full redesign, of which the first 4–5 days are shippable on their own and are worth doing even if you build nothing else here.**

---

## 2. THE PROPOSED UX

One window, **880×640 default** (from 1380×880), resizable, one scrolling column, no tabs, no modes, no modal on launch. Seven regions.

### Region A — Display bar (fixed, ~48px)

Display picker (the existing per-display binding model, unchanged) · an **HDR chip** that is itself the toggle, reading "HDR on" / "HDR off — turn on" / "This display does not report HDR support" (the existing `hdr_switch` call, which targets the *picked* display and is genuinely better than Win+Alt+B — keep the argument, drop the ambiguous switch affordance) · an **overflow "…"** holding: Rescan displays, Windows display settings, Windows colour folder, **Open the measurement log**, Save a copy…, Open a profile…, Run as administrator (self-hiding when elevated), Advanced…, Help & About.

**3 interactive elements**, replacing today's five header buttons plus five-control display row plus mode badge.

### Region B — State card (always visible, three lines, at most one button)

This is the current activity bar promoted from footer to second-most-read element, and it is load-bearing.

- **Line 1 — what Windows actually has.** "DELL U2723QE · HDR on · using this app's profile, SDR correction off." From `_describe_active_profile` (app.py:883) unchanged, including its "not generated by this app" branch.
- **Line 2 — where the numbers came from.** Exactly one of three shapes: "Measured 2 Sept: 454 nits peak, 0.0003 black, grey response from 33 points." / "Built from the panel's declared figures: 1015 peak, 600 full-screen. Never measured." / "Not set up yet."
- **Line 3 — at most one condition, amber, with its fix inline.** "Windows has dropped this profile. [Put it back]" · "The SDR-in-HDR correction is on; it flattens native HDR games. [Turn it off]" · "The grey correction was measured with different tone settings. [Measure again]" · "HDR is off, so an HDR profile cannot be applied. [Turn HDR on]" · "The last apply failed. [Details]".

**Line 3 is derived from state, never from a message.** That is Ground Truth's best structural idea and the cheapest cure for the app's worst bug class: a warning that a four-minute measurement was discarded currently lives for 420 ms before Live Apply's green success line destroys it. A derived condition cannot be overwritten, and it survives a restart — which is exactly when the app currently cannot answer its own question.

**0–1 interactive.**

### Region C — The two actions (always visible, two buttons of equal weight)

> **[Set up this display]** — "Uses what the panel reports about itself. About 5 seconds, no equipment."
> **[Calibrate with a meter…]** — "Measures what your unit actually does. About 5 minutes, needs a colorimeter."

When ArgyllCMS is not located the second subtitle becomes "Needs a colorimeter and ArgyllCMS — press to see what to download." **The button stays enabled**; a disabled button with no explanation is how the meter path became invisible.

Two co-primaries, not one. They differ in what you physically do and in what the result can honestly claim, and collapsing them produces a control that sometimes takes 5 seconds and sometimes 5 minutes — the magic button you rejected. **Moving the word "calibrate" off the spec-sheet read and onto the meter run is the single highest-leverage rename in the app.**

**2 interactive.**

### Region D — The record (empty until there is something to record; auto-expands after a run)

Before setup this is one line: "Nothing is known about this display yet." After setup or a measurement it is six rows, each **fact · value · provenance chip · row action**:

```
Peak brightness (small window)   454.2 nits              [Measured · 2 Sept · i1 Display Pro]   Measure…
Full-screen brightness           265.0 nits              [Declared by the panel]                Measure (+30s)…
Black level                      0.0002 nits             [Measured · 2 Sept]
Colour gamut                     this panel's primaries  [Declared by the panel]                ⓘ
Grey response                    33 points · 2.8% median [Measured · 2 Sept]                    View the run →
White balance                    6504 K · 0.0012 from D65[Measured · verified]
```

Rows 1–3 are **click-to-edit inline values**. This honours `app.py:754-767` — those figures stay visible and editable, because two bugs hid for exactly as long as they had no widgets — while they stop being *sliders*, which is what makes every reset path structurally incapable of destroying them.

**Chip vocabulary: exactly four values, colour-coded AND spelled out, never colour alone** (grafted from Instrument, whose wording is the best of the four):

- **Measured 2 Sept** (green) — carries date and instrument; hover gives the full result sentence verbatim.
- **From the display** (blue) — "the panel's EDID. Describes the model, not your unit."
- **Set by you** (grey) — "you typed this."
- **Assumed** (amber) — "nothing knows this yet; 1000/400 nits is a placeholder that describes no real display."

Under the rows, one full-width **accuracy line**: "Grey tracked within 3% of target from 2.3 to 280 nits, checked 2 Sept (was 28%)" or "Never checked." This is `tools/greyscale_report.py`'s arithmetic promoted into the product — 162 lines of pure maths reading a log the app already writes, and referenced today by **nothing in the repo except itself** (verified: the only hits are the file and the git index).

Footer links: **Open the measurement log** · **Check it by eye…** (the existing pattern view) · **Compare with the previous run** · **View the full report →**.

Full report is a pushed page, not a dialog: verdict headline, the 33-point ramp plotted requested-vs-delivered on a PQ axis, both peaks side by side with the limiter explained, black, contrast, white balance in plain language first and `u'v'` second, every caveat paragraph (ramp reversal, refused balance, additivity, clamped trims) **verbatim** — that prose is the best writing in the codebase and none of it is rewritten — and the **saturation-tracking table**, which is where the 30 colour patches that are 43% of the run and 105 of its 250 seconds finally earn their time. Labelled honestly: "measured, not corrected — a matrix-shaper cannot express an error that depends on both hue and saturation."

### Region E — Fine adjustments (one disclosure, collapsed, state remembered per display)

Header at rest: **"Fine adjustments — all neutral"**. Header when dirty: **"Fine adjustments — 3 changed · gamma 2.150, saturation +6%, blue −1.2%  [Reset]"**. Nothing is hidden; it is folded, and the fold names its own contents.

Open: two headed groups in one scroll. **Tone** — Gamma, Midtone Brightness, Midtone Contrast, and the **SDR-in-HDR correction** lifted out of the tone-slider stack into its own labelled setting with visible text saying it is display-wide, that it applies immediately regardless of the apply checkbox, and that it is switched off automatically during a measurement. **Colour** — White balance (cool ↔ warm, kelvin suffix dropped and the backwards sign fixed), Green–Magenta Tint, Saturation, and a nested sub-reveal "Per-channel balance (usually solved by measuring)" holding R/G/B.

Each slider row is one line: label, slider, numeric field. **The permanent "Range / Step / Default" caption is deleted** (12 lines restating what the slider and field already show, and the source of the "Default: panel" vs "Reset to 1000 nits" contradiction). **The per-slider Reset button is deleted** (12 unguarded 62×28 controls that each silently destroy a value); reset is double-click on the handle plus one group-scoped Reset. Each slider gains, only while expanded, one line of what it actually does — the existing tooltip prose promoted to visible copy.

Foot of the region: **"Start over…"** (one dialog, two explicitly named outcomes) and **"Open a profile as the starting point…"**.

**1 interactive at rest; 21 when open.**

### Region F — Keep this profile in place (one disclosure, collapsed, live state in the header)

Header carries the state: **"off — Windows will drop it after sleep or a mode change"** / **"on — surviving sleep, mode changes and sign-out"** / **"installing…"**. Opening reveals one switch, the explanation currently buried in the Lock Profile tooltip (the best copy in the app, promoted to visible text, including the console-window warning), and — **only while it is already running** — a Reinstall link, which is the one thing the deleted Watchdog Settings dialog could express that the switch could not.

**1 interactive at rest.**

### Region G — Delivery bar and status (fixed, two lines)

Left: **"Apply as I adjust"** checkbox (today's Live Apply, default **on**, remembered per display) · **[Apply]**, which appears only when there is something to apply and reads "Applied" disabled otherwise. Right: the status line, transient only, with a **chevron opening the last 50 messages with timestamps and severity** — grafted from Instrument, and the cheapest possible answer to 104 competing call sites. Prefixes become **Done / Working… / Heads up / Problem**, and a setting you deliberately chose never paints the bar amber.

**Nothing important is ever said only in the status line.** Failures route by *lifetime*, not severity: persistent conditions → Region B line 3 (derived, survives restarts); outcomes of a run → Region D (persists until the next run); transient progress and confirmation → Region G.

I am **keeping the apply-as-I-adjust checkbox** rather than making it implicit. Ground Truth's always-on version was the one thing both the owner-lens and engineer-lens judges flagged: every committed slider move writes two ICC profiles, re-asserts an association and re-hashes installed bytes, on a colour directory the watchdog is contending for — which is precisely the failure `_install_variant` already documents. Instrument's alternative (implicit while the section is expanded) removes a mode by inventing one. One honest checkbox, defaulted on, keeps the escape hatch and costs nothing.

### Control surface, counted

| | Today | Proposed |
|---|---|---|
| Interactive widgets, total | **63** (27 non-slider + 12 sliders × 3) | **32** |
| On screen at rest, before setup | 45 | **10** |
| On screen at rest, after a measurement | 45 | **17** |
| On screen with both reveals open | 45 | **38**, and only by explicit request |
| Permanent readouts / captions | 5 + 12 | 3 + 0 |
| Dialogs | 3 | **0** permanent (2 task sheets + 1 pushed report page) |
| Modal confirmations | 7 | **3**, each naming exactly what it destroys |

Non-slider controls: **27 → 3 visible, 9 relocated, 11 merged into 4, 3 automated, 5 deleted.** Sliders: **12 → 9** behind one disclosure; the three luminance figures become provenance-stamped Record values, which is the point.

---

## 3. THE FOUR FLOWS

### Flow 1 — First run, no colorimeter

1. Window opens at 880×640. **No modal.** Region B reads "DELL U2723QE · HDR is off · Windows is using its own profile" / "Not set up yet" / amber "HDR is off, so an HDR profile cannot be applied. **[Turn HDR on]**". Regions D, E, F are collapsed or empty. **10 interactive elements on screen.**
2. Press **[Set up this display]**. It turns HDR on itself (the existing `app.py:2103-2116` path), waits for Windows to actually switch, reads EDID and DXGI primaries, builds, installs, verifies the installed bytes.
3. **Success:** Region D populates — peak, full-screen, black and gamut chipped **"From the display"**; grey response and white balance read "Not measured — the curve targets generic PQ". Region B line 2 becomes "Built from the panel's declared figures, 2 Sept." Line 3 clears.
4. Immediately under the Record, one line: *"These are the manufacturer's figures for this model, not this unit measured. A meter usually finds the real peak 30–60% lower."* with **[Calibrate with a meter…]** beside it. **That is where the missing seventh guide step goes** — at the moment it is relevant, rather than in a walkthrough that never mentioned a colorimeter in seven steps while telling the user the EDID read "is the whole calibration."
5. **EDID declares no luminance:** peak and full-screen chip **"Assumed"**, not "From the display", and the message says so in the app's existing honest words. The current green tick over a failed install is *structurally impossible* here, because the chips render from what actually landed and nothing is stamped before the attempt.
6. **Failure:** no chip changes, no green anywhere, Region B line 3 carries the real reason and the matching action — **[Run as administrator]** for a locked colour folder, **[Try again]** otherwise.
7. Region F's collapsed header carries the only remaining nudge: "off — Windows will drop it after sleep or a mode change."

**Two clicks to a correct installed profile, three if HDR was off. No dialog shown. Nothing was armed before the app knew anything about the display.**

### Flow 2 — First run, with a colorimeter

1. Steps 1–3 above run first, always. The declared setup costs two seconds and guarantees a sane profile is installed even if the meter run is abandoned.
2. Press **[Calibrate with a meter…]**. Preconditions run through the **single shared `_meter_preconditions`** — the ~60-line inline duplicate inside `_measure_with_meter` is deleted (the identical eight-line comment currently sits at both 2324 and 2537, so the two entry points can already give two answers about the same hardware).
3. **Meter setup, once.** ArgyllCMS missing → a panel, not a QMessageBox, explaining it is a separate free download with the exact instruction the app already writes. **Cancelling the folder dialog now says so** — today it returns silently, making the button look dead. Instruments are listed; one attached is auto-selected, **two or more raises a picker** (today it is always `instruments[0]`). The **display-type `-y` selector** is exposed here with a sane default and one line of explanation — `meter.py:196-236` supports it and **no call site anywhere in `src/` passes it** (verified). On a QD-OLED, reading through the wrong calibration matrix is exactly the class of error `meter.py`'s own docstring warns about: "a plausible XYZ that is quietly 5–30% out."
4. **Pre-flight card**, replacing the modal. Patch count, minutes, where the meter goes, what Esc does — plus the two ordering rules that today exist only in the README and your notes, **enforced rather than documented**:
   - *"The SDR-in-HDR correction is on. At Auto it delivers 0.084 nits where PQ asks for 0.5 — your two darkest steps would be read in the meter's own noise floor, which is exactly where the 33-point ramp is most valuable. It will be switched off for the run and restored afterwards."* The app then does it.
   - *"Anything already measured is kept until this run replaces it. Do not reset first."*
   - **If unapplied edits exist, the card refuses and offers [Apply them and measure] (default) / [Measure what is installed] / [Cancel].** The meter reads the display through whatever Windows actually has; measuring against staged edits solves a curve for a pipeline that never existed. The app already computes `_applied_signature` vs `_edit_signature` and displays it in the activity bar — it simply never consults it here.
   - Two checkboxes: **"Also measure full-screen brightness (+30s)"**, default on, folding in today's buried Measure Sustained… button; **"Also measure colour saturation tracking (+2 min, diagnostic only)"**, default **off**.
5. **The app freezes the shaping for the duration:** the 450 ms `gamma_runtime_timer` is stopped, the global Alt+1/Alt+2 listener is unregistered, `_select_gamma_correction` is refused. Either can currently swap the associated profile under the meter mid-run, silently invalidating every one of the 33 pairs with no symptom — and the watchdog that drives the timer is what the app recommends installing. **At the end of the run the shaping fingerprint is recomputed and compared against the one taken at the start; a mismatch refuses to store the response rather than storing a confidently wrong one** (grafted from Instrument — a stronger guard than suspend-and-hope).
6. Fullscreen surface. **The green placement target is kept verbatim** — drawn at the *smallest* window fraction in the plan, with the instrument detecting placement itself so nobody reaches for a keyboard while holding a probe against glass. This is the best interaction in the app and nothing here touches it.
7. **New: a text layer on the measurement surface, drawn only when no patch is lit**, so it never contaminates a reading. Placement: "Put the meter flat on the green square. It starts on its own. Esc stops." **On placement timeout the message appears here**, where the user is looking, instead of on a status bar this fullscreen window is covering — the code acknowledges that constraint two hundred lines earlier and then sends six more messages there anyway. Between patches: "Step 12 of 40 · about 3 minutes left."
8. **New: black and white are steps 1 and 2, and their `validate()` checks fire the moment those readings arrive.** Today `validate` is called from exactly one place — `derive` at `measure.py:803`, from the last line of `run` at 1179 — so a black reading at 3% of white, knowable at five seconds, costs four minutes and then a second four-minute retry. `run` already has an `on_reading` hook fired per patch; the change is small.
9. Run completes. Correction and hotkeys restored. **Region D expands with the result**; four rows flip to green **"Measured, today"**; the accuracy line reads "Grey tracked within 3% of target from 2.3 to 280 nits (was 28%)". The profile is applied automatically because apply-as-I-adjust is on, and the panel says **"Applied. Your display is using this now."** — not today's "Press Apply Edits to store it", which names the wrong risk: the numbers are already on disk (`_save_state_now` at 2922); what is pending is the display still running the old profile.
10. The run is written to `runs/<timestamp>.json`, and the Grey response row gains "View the run →" so the next run can be compared against it — the two-runs-either-side-of-an-Apply comparison that `greyscale_report.py`'s own docstring calls "the test that matters."

### Flow 3 — "My games look washed out, fix it"

The diagnosis starts **before the user presses anything**, because Region B already states what is in force and where it came from. The app can separate the three real causes from state it already holds.

- **Cause A (commonest): the SDR-in-HDR correction is on.** Region B line 3 already reads: *"The SDR-in-HDR correction is on. It is for the desktop and SDR apps inside HDR, and it flattens native HDR games. **[Turn it off]**"* — one click, the same path Alt+1 takes. Today this cause is a five-entry combo box filed as a fourth tone slider, whose display-wide nature appears only in a tooltip.
- **Cause B: never measured, and the profile declares a peak far above what the panel holds.** The MHC2 header carries min and peak and the lumi tag carries full-screen; a game tone-maps to those, so declaring 1015 on a panel that holds 454 compresses every highlight and lifts the midrange — which is what "washed out" looks like. The Record's declared-result line already says this, with the meter button beside it. **Grafted from Instrument: offer the 30-second option too.** `measure.plan(peak, full=False)` is a 7-patch peak-and-black run that **exists in the code and has no caller anywhere** (verified — every call site passes `plan(peak)` with the default `full=True`). Wire it up as **[Measure the real peak — about 30 seconds]** beside **[Full measurement — about 4 minutes]**. This is the single best new affordance proposed in any of the four designs.
- **Cause C: measured, but the tone settings have moved since.** `_shaping_moved_since_measuring` becomes a persistent Region B condition instead of a status message Live Apply erases 420 ms later, and **it is consulted on every path that changes shaping** — including `_select_gamma_correction` and `_build_from_panel`, both of which bypass it today. That second omission is why pressing the app's own headline button after a meter run silently leaves a curve solved against a peak that no longer exists.
- **When the app cannot tell:** a **"Something looks wrong…"** link opens a symptom panel with five plain-language answers, each branching on what the Record actually holds, and each ending in **"This didn't help"** which walks to the next likeliest cause instead of dead-ending (grafted from Display Record — the best-built of the three symptom surfaces). **The router checks for a stored ramp reversal first and short-circuits to that message**, which is the best piece of copy in the codebase and correctly sends the user to the monitor's own menu ("try its HDR preset, DisplayHDR True Black rather than a Gaming or Cinema mode") rather than pretending a profile can fix it.
- **Only when everything measurable is measured** does it offer the by-eye layer: "What's left is preference. [Open fine adjustments] [Check it by eye…]". That ordering is the thesis made operational, and it is the opposite of where today's walkthrough puts it.

Every router action posts a message naming exactly what it changed and carrying **[Undo]**, backed by a state snapshot taken before the change.

### Flow 4 — Something went wrong

- **Windows dropped the profile.** The existing poll notices; Region B line 3 goes amber on its own — "Windows is using DELL U2723QE.icm — not your profile" — with an inline **[Put it back]**. One click. No second permanent button, and nobody has to infer the difference between Apply Edits and Reapply. **If it happens twice in a session**, that line gains **[Stop this happening]** → Region F. The watchdog's justification arrives at the moment it is true, rather than as a switch called "Lock Profile" sitting third in a row of three switches whose relationships nothing states.
- **The profile guard failed to install.** Three changes, in order of importance:
  1. **Fix it at the installer** (grafted from Instrument — the only root-cause fix proposed). The `if ($Install)` block at `.bat:1347` deletes any prior `install_result.json` at line 1349 and writes the new one only at 1567. It has **no `trap` and no `finally`** — I checked; the `finally` at 1718 belongs to the watchdog run loop, not the install block. So all three `throw`s (Windows build < 20348 at 1367, "No active displays were found" at 1372, and the incomplete Off/On pair at 1386 — by the code's own account the realistic first-time failure) exit leaving **no record at all**. Add a `trap` inside that block that writes `ok=$false` with the exception message. This is a ~10-line change to the `.bat` and it removes the failure mode entirely, at the source.
  2. **Delete the mtime fallback** at `app.py:1533-1538`. The `.bat` rewrites `Watchdog.ps1` at line 30, long before `-Install` runs at line 53, so `after > before` is always true and a throw still reports green. Its own docstring names this exact case as the one it fixed.
  3. **Delete the 9-second `QTimer.singleShot`.** Poll `install_result.json` and `watchdog_is_running()` once a second for up to 90 seconds. Report only from the result file or from the process genuinely being alive. **A process that has exited with no result file is a FAILURE, stated as one.** During the wait the header reads "installing…", never "on" — and `_sync_lock_switch`'s existing honesty (it follows the process, not the click) is what makes deleting the fallback safe rather than merely quieter.
- **An apply or install failed.** Region B line 3 persists until dismissed or resolved. `_install_variant`'s existing message — naming both the watchdog-race cause and the elevated-file cause — becomes its body, with **[Run as administrator and apply]** as the action. It is already the best failure copy in the app; it just needs somewhere a subsequent green line cannot overwrite.
- **A measurement was refused.** `METER_LOG_PATH` gets a home: a footer link in Region D, an item in the overflow, and the last clause of every meter failure message. The comment at `app.py:93-96` says the log exists so a refusal is not "a round trip through the user" — it is referenced in **zero user-visible strings**, so the round trip is currently mandatory. One link removes it. **`PlacementWatcher` also stops swallowing its exceptions**: 45 identical suppressed `MeterError`s from an unplugged meter currently produce a message blaming the user's aim. Those go to the log, and the timeout message names both possibilities.
- **A typed value was rejected.** `controls.py`'s `value()` stops parsing the field text (lines 156-163, verified) and returns committed state instead. Today an out-of-range entry never fires `editingFinished`, so it sits on screen unreported **and** is handed to the pattern view's read binding and the wheel handler — screen, slider and state can all disagree after one digit too many. Out-of-range clamps visibly and says so, reusing the sentence the app already writes on the other input path.
- **Start over.** One dialog, two explicitly named outcomes: **"Undo my adjustments, keep what was measured"** and **"Discard the measurements too and start again"** — the second naming the peak, black, full-screen and 33-point curve it destroys and how long re-measuring takes. This fixes both halves of today's problem: Revert is permanently dead on the recommended path (`_build_from_panel` clears the three fields `_revert_to_base` reads), and Reset silently swaps a measured 454-nit peak for a figure the codebase itself calls a display that does not exist.

---

## 4. WHAT GETS CUT, AND WHY EACH CUT IS SAFE

**Deleted outright (5 controls + 24 chrome widgets):**

- **The Getting Started modal, all seven steps.** It is the source of three separate findings: a green tick for a calibration that never reached Windows (`app.py:2136` stamps state before the install at 2150, and the guide's check reads exactly those fields); four buttons named differently from the ones on screen ("Reset All", "Export Edited HDR Profile…"); and no mention of the colorimeter anywhere while step 2 asserts the EDID read "is the whole calibration." **Safe because its job — telling the user what is not yet done — moves to Region B line 3, which cannot be covered by itself and reports from real state rather than from a field stamped before the attempt.** Its content moves into Help and into visible row copy at the point of use.
- **The 12 per-slider Reset buttons.** One reads "Reset Peak Luminance to 1000 nits" on a row whose caption says "Default: panel". Two labels on one control disagreeing is worse than either being wrong. **Safe because reset survives as double-click-to-reset plus one group-scoped Reset, and because the three figures those buttons most endangered have left `control_widgets` entirely.**
- **The 12 "Range / Step / Default" caption lines.** They restate the slider extents and the numeric field. **Safe: zero information lost.**
- **The two editor tabs.** Two headed groups in one scroll. **Safe: the grouping survives, the navigation layer does not.**
- **The Watchdog… header button and the Watchdog Settings dialog.** The switch and the dialog's two buttons run the identical two `.bat` strings, and the header button's own tooltip has to explain the overlap. Its one unique capability — reinstall while running — becomes a link inside Region F visible only while it is running. **Safe: nothing is lost, one narrow case is relocated to where it applies.**
- **The three luminance sliders.** Promoted to Record rows with inline editing. **Safe: the must-keep at `app.py:754-767` is strengthened, not weakened — they become more visible, permanently, at eye level instead of below a card on a scrolling tab, while losing the scroll-wheel hazard.**

**Merged (11 controls → 4):**

- **Apply Edits + Reapply → one Apply.** `force` only widens the pending set; both paths end at the same association call and the non-pending branch re-asserts anyway. The difference was never perceivable. `trust_cache` becomes always False, which is what `_installed_matches`' docstring already claims happens — and that also removes the green "Nothing needed rebuilding" lie over an externally-replaced file. The genuine Windows-dropped-it case becomes the contextual **[Put it back]**.
- **Calibrate Display + the HDR combo's `HDR_FROM_PANEL` sentinel → one "Set up this display".** Both already call `_build_from_panel`. The sentinel leaves the combo, which becomes a list of filenames — nouns, as it should be. **This also closes a high-severity hole: today a combo box, the least guarded control type there is, silently destroys `panel_response`, the measured peak and the measured black, while Reset Sliders (which destroys strictly less) puts up four paragraphs.**
- **Auto Mode Switching + Lock Profile → one "Keep this profile in place".** Both fire on a mode change; with both on, two processes race to re-assert the same association after every Win+Alt+B. The roles are stated once: the app handles transitions while it runs, the guard handles everything after it closes and owns the hotkeys. **The genuine capability loss — following transitions in-app without a background program — survives as a checkbox inside Region F.**
- **Revert + Reset Sliders → "Start over…" plus a real snapshot-backed Undo.** Revert is dead on the recommended path today; Undo works on the path the app actually recommends.
- **Refresh + Display Settings + Profile Folder + Run as Admin + Help → the overflow menu**, joined by the new **Open the measurement log**. Refresh also fires automatically on window activation, which is when it matters.

**Automated (3):** instrument selection (automatic with one meter, a picker with two or more); when to force a rebuild (always re-assert, rebuild when the installed bytes differ); the SDR-in-HDR correction being Off for a measurement (the app does it and restores it, instead of leaving a non-obvious rule in a README that runs opposite to how every other calibration suite behaves).

**Reduced (1):** the SDR-in-HDR correction goes from a five-entry combo to **On/Off** (On = Auto, which reads Windows' actual SDR white and is strictly better than the user guessing). The fixed-white presets move to Advanced. **This is the one cut with a real audience cost** — someone matching a specific SDR brightness beside Calman loses a main-screen affordance — so Region E's correction setting carries one line: "Matching a specific Windows SDR brightness? See Advanced."

**Cut from the default view but not from the app (1):** Test Patterns… → **"Check it by eye…"**, in Region D's footer where it is a verification tool after a run rather than a third calibration method competing with the other two. The 1,110-line pattern view is untouched, including its per-pattern key hints; only its entry-point tooltip is rewritten, since it currently lists four of the nine keys the view binds.

---

## 5. THE STAGED PLAN

Ordered so **each stage ships on its own**, correctness first, layout last. If the project stalls halfway you are still strictly better off than today — which is the property that matters most for a solo GPL fork.

### Where the 966-test suite protects you, and where it does not

**It protects:** `test_core.py` (1,149), `test_measure.py` (1,059), `test_measure_view.py` (867), `test_greyscale.py` (413), `test_patterns.py` (758), `test_meter.py` (323), `test_edid.py` (348), `test_hdr_display.py` (155), `test_packaging.py` (664). That is ~5,700 lines covering exactly the 8,600 lines you must not break. **Stage 1's mechanism changes land almost entirely inside that protected zone.**

**It does not protect:** `test_gui.py` is 4,331 lines and 314 tests, of which **85 reference `status_label`** and 16 reference `control_widgets` (verified). Those are the tests Stage 2 and Stage 4 break. Retarget them at the controller, where they are better tests anyway.

**It actively lies to you in two known ways** — your own memory records both. Layout shift makes Qt assertions pass while the feature is broken, and the fontless offscreen platform lets a widget-exists assertion stand in for a working feature. **So: no stage is "done" on a green suite.** Budget 3–4 days of hardware verification across the whole plan — a real monitor, a real meter, a sleep/resume cycle, a mode change, a sign-out — and check which of the two watchdog install directories the running process actually points at before believing any watchdog fix is live.

### Stage 0 — The first commit (half a day)

**`controls.py`: make `value()` read state, not field text.** Twelve lines. It is self-contained, it is covered by existing tests, it fixes a bug where the screen, the slider and the state can disagree and the pattern view then operates on a number the profile will never be built from — and it depends on nothing. Ship it, verify the suite, and you have proved the loop works.

### Stage 1 — Correctness only, no UI change (4–5 days) — **do this even if you build nothing else**

1. Run `validate`'s black and white checks the moment those readings arrive (`measure.run`'s `on_reading` hook already exists), not once after all 70 patches.
2. Suspend the Alt+1/Alt+2 listener and the 450 ms `gamma_runtime_timer` for the duration of a run; **recompute the shaping fingerprint at the end and refuse to store the response on a mismatch.**
3. Force the SDR-in-HDR correction Off for the run and restore it — **and record in the state file that a run is in flight**, so a crash mid-run is recovered on next launch rather than leaving the correction off with no explanation.
4. Refuse a measurement when `_applied_signature != _edit_signature()`, offering apply-first.
5. **Add a `trap` to the installer's `-Install` block** so its three throws write `ok=$false` with the message. Then delete the mtime fallback and the 9-second timer; poll the result file and the process for up to 90 seconds.
6. Call `_shaping_moved_since_measuring` from **every** shaping change — `_select_gamma_correction` and `_build_from_panel` included.
7. Make `_write_gamma_runtime_state` and `_publish_gamma_runtime_intent` check `_write_json_atomic`'s return value, as `_save_state_now` already does.
8. `trust_cache=False` on the apply path.
9. Delete the inline duplicate of `_meter_preconditions`.
10. Plumb `-y` and add the instrument picker.

**Every one of these is high-severity, none depends on the redesign, and all of them land in the protected zone.** This is the two-week fallback Instrument proposed, and it is the right thing to do first regardless of what you decide about the screen.

### Stage 2 — Provenance and the status router (4–5 days)

**Add `luminance_provenance` to `ModeState`** — a small dict keyed by figure, values `measured | declared | hand | assumed`, plus `measured_at` and `measured_with`. Stamp it at **all four** mutation sites: EDID adoption, measurement completion, inline edit, reset. **Migration for existing `last_gui_state.json`:** if the triple equals `UNSET_LUMINANCE` → `assumed`; else if `panel_response` is non-empty and `panel_source_key` matches this display → `measured (recorded before this app tracked dates)`; else → `set by you`. That middle branch is an inference, and the chip says so.

Then **`_set_status(text, level)` becomes `notify(subject, text, level, lifetime, actions=())`** across 104 call sites, with each classified transient / condition / result. This is mechanical but wide, and it is the prerequisite for a third of the remaining fixes.

### Stage 3 — Extract the presenter seam (3–4 days)

Move orchestration out of the `QWidget` subclass into `ProfileService` / `MeasurementController` / `PanelReader` / `Notifier`, emitting typed signals. `MainWindow` delegates, so the suite stays green. Retarget the 85 `status_label` tests here.

### Stage 4 — The new view (7–9 days)

Regions A–G, plus the pre-flight sheet, the report page and the symptom panel. ~900–1,100 lines of new widget code replacing ~490 plus `dialogs.py`. `app.py` emerges split into `window.py`, `record.py`, `adjustments.py`, `presenter.py`, `flows.py`.

### Stage 5 — The copy pass (2 days, highest value per hour in the list)

One term per concept, one name per action, four status prefixes. Apply the language audit's wordlist: **Display / Profile / Apply / Adjustments / Measured vs Declared / Grey response / Full-screen brightness / Profile guard.** Delete the ~30 tooltips that restate their label, the `Card` tooltips that duplicate the subtitle rendered two lines below, and the five-way duplication of one slider's tooltip across its siblings. Fix the temperature slider's backwards kelvin sign.

### Stage 6 — Hardware verification (3–4 days, cannot be compressed)

**Total: 20–25 working days.**

---

## 6. THE RISKS

**The provenance chip becomes the new green watchdog line.** This is the biggest risk in the design and Three Truths named it exactly: a chip is only as honest as the code that stamps it, and there are four paths that mutate luminance. **Mitigation, and it must be enforced mechanically rather than by discipline:** make the three luminance fields private on `ModeState` with a single setter that requires a provenance argument, and add a test that asserts no path can write them without one. If you cannot make that structural, do not ship the chips — an unstamped figure showing a green "Measured" chip is worse than today's undifferentiated slider.

**Row 4 can never say "Measured", and a careful user will notice.** Measured primaries are deliberately not written to the colorant tags — correctly, since patches are presented in scRGB on BT.709 and a measured "red" is not the panel's primary. So the Colour gamut row reads "Declared by the panel" even after a full run. **This needs one well-written sentence in its ⓘ, not a workaround.** Display Record found this and handed it over unsolved; it is a real crack in a design whose entire visual language is badges, and it is yours to write.

**The measurement-surface text layer trades against the accuracy the whole product rests on.** `measure_view.py`'s founding argument is that the screen must stay black. The blanking rule — off for the black patch and for any grey step under 2 nits — is a judgement, not a proof. **Verify it on hardware by measuring the black patch with and without the overlay before shipping it. If it fails, it loses.** Fallback: a panel on a second display where one exists, and on single-display setups nothing during the run beyond a longer pre-flight. This is the one element I would cut without argument if the measurement disagrees.

**Forcing the correction Off around every run changes the Windows association twice per measurement.** If the process dies mid-run the user is left with the correction off and no explanation. Stage 1 item 3 records the in-flight state so next launch recovers it — **that recovery check is not optional, it is the price of removing the rule.**

**The watchdog contract is three files and two hotkeys, and this plan touches all of them.**
- `install_result.json` — the new `trap` must write the *same schema* the GUI already parses (`ok`, `startup`, `fallback`, `warnings`, `at`). A trap that writes a different shape turns a fixed failure report into an unparsed one.
- `gamma_hotkeys.json` (`GAMMA_RUNTIME_SCHEMA = "virtual-hdr-osd-gamma-hotkeys-v2"`) — the watchdog reads it at `.bat:82` and falls back to install-time state when the read fails, which **asserts the opposite correction variant**: the user's choice appears to revert seconds after they make it, with nothing logged. Checking the atomic-write return value (Stage 1 item 7) is what closes that.
- **Alt+1 / Alt+2** — suspending them for a run means a crash mid-run leaves them unregistered. Restore in a `finally`, and re-register on next launch regardless of how the last run ended. Also: the hotkeys currently claim success in SDR when `_select_gamma_correction` silently did nothing. Fix that in the same pass.
- **Two install directories exist.** Before believing any watchdog fix is live, check what the running process points at.

**Always-on apply would have been a real regression, which is why I kept the checkbox.** Two ICC writes plus an association re-assert plus a re-hash per committed slider move, on a colour directory the guard is contending for, is precisely the failure `_install_variant` documents. Defaulted on, one checkbox, escape hatch preserved.

**Everything behind a reveal makes expert work slower.** You are a power user; you reach Contrast in two clicks today and three tomorrow. The collapsed header names every changed adjustment and its value, so nothing is hidden — only folded. If it grates, the one-line change is to start Region E expanded whenever any adjustment is non-neutral. That is a reversible preference, not a design commitment.

**Twenty-five days is a long time during which the false-success bugs are not fixed.** That is the entire argument for the staging order above. **Stage 1 stands alone, ships in a week, and removes most of the ways this app currently tells you something that is not true.** If you build nothing else, build that.