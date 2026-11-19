# Handoff — LoRA & Make open actions

**Repo:** `C:\Users\prosn\fixer` · branch `main` (fixer uses one branch; no feature branches).
**Written:** 2026-11-18, after the "LoRA search exact match behavior" session. For an Opus 5.5 / ultra-code run to finish the open items in one pass.

## How this app ships (read first)
- **Backend** = `desktop/fixer_desktop.py` (+ siblings). Runs on *this PC* from `C:\FixerDesktop`.
  Deploy a backend change **live** with: `python desktop/fixerctl.py deploy` (compiles each file, copies to the
  install, restarts; refuses while a job is running unless `--force`; check idle first with
  `python desktop/fixerctl.py status`). **A backend change can go live immediately** — do it.
- **Phone** = the `Fixer/` Swift app. **Cannot be compiled on this Windows box.** Pushing to `main` triggers a
  TestFlight build (`.github/workflows/testflight.yml`); `gh run list --workflow testflight.yml` shows status.
  **Lump phone changes; they only take effect after a build.** If a build fails to compile, fix it.
- Fast-flow: small slices, merge often, live-tunable knobs over rebuilds. Commit co-author:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` (use the running model's line).
- Backend test harnesses (stub the Civitai API / library; no network): in the session scratchpad —
  `test_lora_search.py`, `test_job_loras.py`. Pattern: `importlib` the module, monkeypatch `_api_json` /
  `_lora_entries`, assert on the emitted payload. Re-create similar tests for new backend logic.

## Already done & LIVE on the PC (do NOT redo)
- `job_lora_search`: **engine-scoped** (a search on Chroma returns Chroma, not SDXL/Juggernaut); broadened
  `LORA_CIVITAI_BASE` labels (Wan 2.2 I2V/T2V/TI2V; Qwen + Qwen 2.1); optional `families` list; **discovery
  facets** `sort` (popular/rated/new), `period` (trending window), `category` (a Civitai tag browse).
- `_job_loras`: honours a **per-use `triggers` override** per add-on (backward-compatible).

## Done & committed, COMPILING into TestFlight (verify the build is green: commit `0125d70`)
- `MakeDrawer.swift`: **swipe-to-delete** add-ons (+ long-press + confirm), and each add-on is a **tap-to-expand
  dropdown** (strength + "extra activation words" input + always-use). `SwipeToDelete` is hand-rolled (the
  add-ons card is a VStack, not a List).
- `LoraBrowser.swift`: **discovery browser** — multi-select engine chips, category row, Popular/Trending/Top-
  rated/Newest sort, engine badges. `Desktop.loraSearch` gained `families/sort/period/category`.
- **⚠ These were written without a Mac to compile against.** First action: confirm the `0125d70` TestFlight
  build compiled; if not, fix the Swift errors. Watch especially: `SwipeToDelete` gesture vs the strength
  Slider; the horizontal chip `ScrollView`s inside `List` rows; the custom `LoraBrowser.init`.

---

## OPEN ACTIONS

### 1. Explicit toggle: additive → one-or-the-other  (the active bug)
The "Explicit options in Make" toggle (`@AppStorage("make.explicit")`, Settings → SettingsScreen.swift:59)
currently **adds** explicit chips to the everyday ones (shows the combination) instead of switching between them.
- Recipes: `Fixer/Create/MakeLayout.swift:155` `visibleStyles` →
  `return clean + all.filter { $0.explicit }` (comment: "11-18: additive, not a mode swap").
- Quick picks: `Fixer/Create/MakeDock.swift:175` →
  `let options = g.options + (makeExplicit ? g.explicitOptions : [])`.
**Intended:** when the toggle is ON, show the explicit set **instead of** the clean set — "one or the other,"
not both. **DECISION NEEDED (ask Jim):** (A) ON ⇒ explicit-only (hide all clean), or (B) ON ⇒ explicit
*variant replaces its clean counterpart*, keeping clean recipes that have no explicit twin. (B) needs a
pairing key between clean/explicit recipes — check the catalog from `job_make_recipes` in
`desktop/fixer_desktop.py` for whether explicit recipes map to clean ones. Phone-only change once decided.

### 2. Consolidate the duplicate style systems
Two separate horizontal chip systems both inject phrasing into the prompt — this is the "duplicates / two
spots that overlap" complaint, and it overlaps with #1 (explicit chips live in both):
- **"Style tips"** — `MakeDrawer.swift` `styleTips(_:)` (~125–170), card in `MakeCompose.swift:144`. Stores
  opaque toggle IDs in `chosen`, sent as `values["toggles"]`, expanded **server-side**.
- **"Quick picks"** — `MakeDock.swift` `quickRow(_:)` (174), card `MakeCompose.swift:154` `composeQuickCard`.
  Splices **literal** comma phrases into the prompt via `PhraseList` (`MakeLayout.swift:271 tapQuick`).
**Goal:** merge into one coherent system. **DECISION NEEDED:** which mechanism wins (server-expanded toggle
IDs, or literal prompt phrases) — pick one, migrate the other's data, remove the duplicate card. Mostly phone;
may touch the backend `toggles` expansion if that mechanism is retired.

### 3. "Some inputs don't work" — de-silence (NOT censorship)
Prompts are sent verbatim; there is no word filter (backend or client). The felt "filtering" is one of three
**silent** behaviors — surface/explain each instead of dropping quietly:
- Output **ContentGate** silently discards a *finished* image (`Fixer/Imaging/ContentGate.swift`; thresholds
  `Fixer/Rules/Rules.swift:~185–205`, default OFF). If a filter slider was turned up, the pic generates then
  vanishes with a terse note — make the reason explicit.
- Explicit words are simply **absent** until `make.explicit` is on (ties to #1).
- **Edit mode** drops a recipe's prefix/suffix — `MakeLayout.swift:~115 assembledPrompt` sends only raw words
  when `photoMode == "edit"`, so a style's words silently don't apply.
**Action:** confirm with Jim which one he hit, then make that path visible. Likely phone-only.

### 4. Strength slider clarity
`MakeDrawer.swift` `loraRow` strength `Slider(value:, in: 0...2, step: 0.05)` with a raw `%.2f` readout. Jim
finds the 0–2.00 scale unclear ("0 to 100 or whatever"). Relabel (e.g. "1.0 = full strength", or show a %, or
mark 1.0 as "Normal"). Phone-only, trivial.

### 5. "Recommended," not just browse  (optional)
Discovery ships Popular + Trending (proxy). Add a curated/recommended section to `LoraBrowser.swift` if wanted
(e.g. a "Recommended for {engine}" row). Backend already supports the facets.

### 6. Extra per-LoRA knobs  (deferred — build only when a LoRA needs it)
Only strength + trigger text ship today. Separate CLIP strength and a bipolar "slider LoRA" range (−range..+range)
are not built. Backend `_job_loras` clamps strength 0–2 (`fixer_desktop.py`, in `add()`); SDXL ties
model+clip at `_recipe_workflow` (~line 3365). Touching this changes the generation path — test carefully.

### 7 & housekeeping
- **Verify build `0125d70` compiled** (see above). First, not last.
- "Gold chips" question is **answered**: gold = explicit, gated by the `make.explicit` toggle. No action
  unless Jim wants that UX changed (relates to #1).
- Duplicate `fixer_desktop.py` / `comfy_guard` processes on the PC — environmental flakiness; chase only if asked.

## Decisions to get from Jim before coding #1/#2
1. Explicit toggle (#1): **explicit-only** vs **explicit-replaces-counterpart**.
2. Style consolidation (#2): keep **server-expanded toggle IDs** or **literal prompt phrases**.
3. Which specific input silently failed (#3).
