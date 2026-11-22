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

## PROGRESS (2026-11-22)
- **Track 2 server composer — DONE & LIVE** (deployed to the running server, not yet git-committed). Rewrote
  `_recommend_macros_system`, `_lora_catalog_for_ai` (now shows `rec ~N` strength + a "don't invent triggers"
  note), `_macro_vocab` (states the dynamic `lora_cap`), and added a strength-budget guard in `_sanitize_macro`
  (trim to cap, scale summed strength to ~1.6). Encodes the research heuristics: one add-on per concept slot,
  2-4 sweet spot, strength ranges by role, trigger words verbatim, per-model weighting (sdxl yes, qwen/chroma
  no), macro = full recipe so it always sets a model. **Testable now via Suggest.**
- **Build discipline:** `fixerctl deploy` pushes server (.py) changes live with NO Mac build. git-committing any
  non-.md triggers the one TestFlight build, so ALL commits (server + client) are held until the full batch is
  ready → ONE combined compile (Jim's call).
- Research agent + edit-AI diagnosis agent: both complete; findings folded into Tracks 2 and 4 below.

## Track 4 ROOT CAUSE (diagnosis agent, confirmed)
The "edit" change mode is the DEFAULT for Qwen models (what Jim is steered to). Its `revise()` edit branch
(`CreateScreen.swift:284-288`) sends ONLY the bare instruction with an EMPTY loras list — discarding the
original prompt, LoRAs, negative, settings and seed. "Redraw" mode (juggernaut/chroma default,
`CreateScreen.swift:289-304`) carries context, which is why the problem is model-dependent. FIX (client-led,
goes in the batch): in the edit branch, pass the original prompt as preserved context (`values["base_prompt"]`)
and reuse `base.loras`; server composes the Qwen-Edit prompt as "<original>. Change: <instruction>." in
`_create_pictures` (`fixer_desktop.py:~5697`/qwen21 `~5685`). Consider defaulting `enhanceEdits` on + letting the
I2I enhancer restate (not just minimally edit) the subject. Seed re-randomizes every edit (`fixer_desktop.py:5620`).

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
- **Tell the AI the real, DYNAMIC add-on cap** — `_job_loras` caps a job at `int(_tune("lora_cap", 4))`
  add-ons (`fixer_desktop.py:6452`), **currently 10** (set in `C:/FixerDesktop/local.json`; 4 is only the
  default). The recommend prompt (`_recommend_macros_system`) never states this. Read the live tune value
  and put it in the prompt, and tell the AI to use as many complementary add-ons as genuinely help UP TO
  that current cap — NOT a hard 4. Must track the tune, not a literal.
- **Weighting awareness:** strength IS applied on run (`strength_model`/`strength_clip`,
  `fixer_desktop.py:3520/3644/3755`), but the AI isn't told per-model weighting behavior
  (SDXL/Chroma honor `(phrase:1.3)`, Qwen strips it). State it so it doesn't waste weighting on Qwen.
- **Art of the possible** — also suggest NOT-yet-installed add-ons worth acquiring, wired to the LoRA
  browser / Civitai search (`LoraBrowser.swift`, `Desktop.loraSearch`), with a "Get it" action. Keep
  these clearly separated from installed picks so they don't get dropped by the family/install filter.
- **Per-add-on strength ranges & usage notes (make the AI a PRO at this).** Civitai import already parses
  a suggested strength (`_suggested_strength`, `fixer_desktop.py:6808`) and keeps `description`, but the
  library entry (`library.json`) persists only a single `strength` — no range, no notes — and
  `_lora_catalog_for_ai` (`fixer_desktop.py:3021`) shows the AI only `file [family] name triggers`, not
  even that strength. Work: (1) persist recommended strength + range + a short usage note per add-on at
  import; (2) surface them in `_lora_catalog_for_ai`; (3) tell the recommend AI to respect each add-on's
  recommended range AND adjust strengths when stacking several (they interact). Goal: it composes like a
  practitioner, not a guesser.
- Data availability (Jim, 2026-11-22): the recommended-strength / usage data IS coming in and is already
  **rendered in the LoRA browser UI for Civitai-source add-ons** (HuggingFace-source ones may lack it). So
  it's reachable both server-side (import) and client-side (browser) — persist and reuse it; don't inspect
  the live library by hand to confirm, just wire the ingest we already have.
- **HuggingFace metadata gap (Jim, 2026-11-22).** Civitai-source add-ons carry recommended strength/usage;
  HF-source ones give much less. There's heavy crossover (same LoRA on both). Likely solution: hash the
  installed `.safetensors` (SHA256) and query Civitai `GET /model-versions/by-hash/{hash}` to enrich the
  HF entry with recommended strength + trigger words + base model. Research agent to confirm this is the
  cleanest path (vs. scraping HF READMEs). Enrich once at import/scan, cache in `library.json`.
- **RESEARCH FIRST (Jim, 2026-11-22): does a tool already exist** for AI-assisted LoRA/preset composition
  (auto-selecting + weighting multiple LoRAs from an installed library + metadata, prompt-weighting best
  practice, stacking-interaction heuristics)? Adopt vs. build decision before implementing the "pro"
  composer. A research agent is running on this; fold its findings in here before coding Track 2's AI.

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

---

## STATUS 2026-11-22 — batch built, server deployed

All Swift below is in ONE batch, committed together for a single TestFlight build.
Server (`desktop/fixer_desktop.py`) is **deployed live** (fixerctl deploy, server idle, no `--force`).

**Track 1 — macro workflow: DONE.** A macro loads as a full recipe (model+words+add-ons+settings
*replace* the form); `appliedMacroID` tracked; "Update this macro" button (`MakeDock.updateCurrentMacro`);
pinned-favorites strip + pin/unpin swipe + `Macro.pinned` now persisted through `job_macros_set` (server
was silently dropping it — fixed); delete dialog → `.alert`; "New prompt (keep add-ons)" in the tidy menu.

**Track 2 — Suggest "pro": DONE.** Server composer rewritten with pro heuristics (one add-on per concept
slot; strength ranges + ~1.0–1.5 budget; trigger words verbatim; SDXL-only weighting; always set a model;
family enforced; dynamic `lora_cap`, not a hard 4; strength-budget trim in `_sanitize_macro`). Client
Suggest UI: detail rows (add-ons/model/settings); **edit-then-save** (opens `MacroEditor` from a
suggestion); **"More like this"** (regenerate one in place); and **"Add-ons worth getting"** — a new
server `wishlist` of uninstalled add-on *types* worth acquiring → taps open `LoraBrowser` with the search
pre-filled (`initialQuery`). `recommendMacros` now returns `(macros, wishlist)`; added `Desktop.Wish`.

**Track 3 — perf: thumbnail slice DONE; resident-memory cap DEFERRED (needs device profiling).**
`CreateResult.thumb` — a 256px cached thumbnail (bounded `NSCache`, ~24 MB) — now feeds the make roll
(`MakeCanvas:85`) and the video-still picker (`MakeDrawer:665`), so dozens of 44–64px tiles no longer
decode/resample full-res bitmaps on every layout. **Still open:** capping the 70+ resident full-res
`CreateResult.image` bitmaps. Not done blind because the Library copy is **async + encrypted**
(`LibraryStore.image` is async, per-item DEK), so evict-and-reload on the main thread (compose preview and
revise read `.image` synchronously) would jank or needs a redesign. Decide the eviction/reload strategy
with on-device memory numbers before coding — pick wrong and it hurts revise/share quality or latency.

**Track 4 — edit-after-make: DONE (client+server).** Edit branch of `revise()` sends
`values["base_prompt"] = original` and reuses `base.loras`; server `_create_pictures` composes
`prompt = "{base_prompt}. Change: {instr}."` for `image_mode == "edit"`, so an edit keeps the original
make's intent and add-ons instead of only the bare instruction.

**Quick-picks drop — fixed for good.** `activeGroups` always seeds the full everyday group set and *adds*
applied chips (was replacing them with an 8-of-19 subset); the 18+ block is gated by `makeExplicit`.
