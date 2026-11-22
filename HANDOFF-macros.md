# Handoff — Macro workflow redesign (Jim, 2026-11-22)

Consolidated plan from a live design session. Captures intent + concrete code findings so nothing
is lost to a context drop. Build in slices; each slice is one TestFlight increment. Swift compiles
only on the Mac (TestFlight Action), so keep slices coherent and low-risk.

## What a macro IS (Jim's intent, 2026-11-22)
- A macro is a **full starting recipe**. Selecting it **loads model + settings + add-ons + words,
  replacing** the current setup (NOT a layer-on preset). This REVERSES the earlier "model null unless
  it has add-ons" change in `_recommend_macros_system` — for a recipe, the model is part of it.
- After launching into it you **edit live, then push the tweaks back**: an **"Update this macro"**
  action so your changes become the saved macro.
- **Pinned favorites** strip inline in Make (one-tap apply) + the manager sheet for the full list.
- A **"Clear for a new prompt" control at the top that wipes everything EXCEPT the add-ons (LoRAs).**

## Tracks

### Track 1 — Macro = a recipe you launch, tune, update (core workflow)
- Apply = full recipe (model + settings + add-ons + words, replacing). Revert the "model null" line in
  `_recommend_macros_system` (`fixer_desktop.py` ~3135) so suggestions carry their model again.
  `applyPendingMacro` (`MakeLayout.swift:414`) already applies model/settings/prompt/loras; make it
  behave as a replace, not an append, for the recipe case.
- **"Update this macro"** button (save live form back onto the macro). Reuse `saveCurrentAsMacro`
  (`MakeDock.swift:449`) but update-in-place by id via `Desktop.saveMacro`.
- **Pinned favorites** strip inline in Make: one-tap apply, long-press for edit/delete/duplicate/pin.
  Needs a `pinned` flag on `Desktop.Macro` (round-trips via its `extra` dict). Manager for the rest.
- Rebuild the manager as a real Make component instead of reusing Settings' `MacroList` in a sheet
  (`Settings/Macros.swift:7`). That reuse is why the **swipe-delete confirmation is mis-placed**
  (`confirmationDialog` anchored on a Form inside a sheet). Show **what's currently applied** (active
  macro as a chip you can tap to remove; show which add-ons were dropped, not a transient alert).
- **"Clear for a new prompt (keep add-ons)"** at the top of Make: clears prompt/negative/quick-picks/
  settings but leaves `loras`. (Compare the full reset at `CreateScreen.swift:601` `clearedCount`,
  which clears everything — this one keeps loras.)

### Track 2 — Suggest redesign
- **Seed from the current Make setup** (send current prompt + model + active add-ons as context to
  `recommend_macros` / `job_recommend_macros` `fixer_desktop.py:3175`), not a blank "what are you
  going for?" box (`MacroRecommend`, `Settings/Macros.swift:336`).
- **Edit before saving** — inline editor per suggestion (reuse `MacroEditor`), not accept/reject only.
- **Refine/regenerate ONE** suggestion ("more like this"), not all-or-nothing per batch.
- **Tell the AI the real add-on cap (4)** — `_job_loras` caps a job at 4 LoRAs. The recommend prompt
  (`_recommend_macros_system`) never states this; add it and let it use as many as help, up to 4.
- **Weighting awareness:** strength IS applied on run (`strength_model`/`strength_clip`,
  `fixer_desktop.py:3520/3644/3755`), but the AI isn't told per-model weighting behavior
  (SDXL/Chroma honor `(phrase:1.3)`, Qwen strips it). State it so it doesn't waste weighting on Qwen.
- **Art of the possible** — also suggest NOT-yet-installed add-ons worth acquiring, wired to the LoRA
  browser / Civitai search (`LoraBrowser.swift`, `Desktop.loraSearch`), with a "Get it" action. Keep
  these clearly separated from installed picks so they don't get dropped by the family/install filter.

### Track 3 — Performance with many pictures (70+ slows the phone a lot)
- Root cause: every result is a **full-res `UIImage` held in memory** (`CreateResult.image`,
  `CreateScreen.swift:88`, "in memory only"). 70+ full-res images = ~0.5–1 GB RAM → iOS throttles.
- The thumbnail strip renders 44pt tiles **from the full-res image** (`MakeCanvas.swift:85`) with no
  downsampled cache, re-sampling big bitmaps each layout.
- Fix: cache a small thumbnail per result (downsample once, decode off main via
  `preparingThumbnail`), and **cap resident full-res images** — spill older ones to the Library/disk
  and reload on demand.

### Track 4 — AI edit-after-make quality (separate subsystem)
- Jim: after a make, the edit is "hyper-specific," "lacks understanding or creativity," and "doesn't
  stick to the original make pic's requirements." This is the revise/change pipeline, not macros.
- Needs its own investigation (the revise prompt, whether the original make's prompt/settings/seed are
  carried into the edit). Investigate before committing a direction.

## Recommended build order
1. **Quick-relief slice** — Track 3 perf fix + "Clear (keep add-ons)" + delete-placement fix. Small,
   safe, immediately felt.
2. **Track 1** — the recipe/update/pinned workflow (the big one).
3. **Track 2** — Suggest redesign (seed-from-current, edit, refine-one, 4-cap, acquire).
4. **Track 4** — edit-AI quality (investigate first).

## Constraints / rules in play
- Commit straight to `main`, never feature branches. One TestFlight release per batch.
- Server (`desktop/fixer_desktop.py`) deploys via `desktop/fixerctl.py deploy` (copies to
  `C:\FixerDesktop`, checks, restarts); it refuses while a job is running — don't `--force` into a
  live generation.
- `.md` changes don't trigger the TestFlight build (`paths-ignore`).
