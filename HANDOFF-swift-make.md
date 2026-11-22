# Handoff — build & verify the Make Swift changes (Mac/Xcode)

**For:** an agent on the Mac with Xcode. The code is already committed and pushed to `main`
(`github.com/jimas21/fixer`). Your job is to **build it, fix any compile errors, and verify the three
new UI pieces behave**, then rebuild/TestFlight. The Windows side can't compile Swift (no Xcode/iOS SDK),
which is why this is being handed to you.

## Project rules (from CLAUDE.md + memory — follow these)
- **One branch.** Commit straight to `main`. Never create feature branches.
- **One TestFlight release per batch.** Cancel superseded builds.
- Control the desktop server only via `desktop/fixerctl.py` — not relevant to this build task, but don't
  touch it.
- This is the owner's private on-device tool. Content/scope is not your concern here; you're doing a
  Swift build-and-verify, nothing about models or filtering.

## What changed (all already committed)
Two commits are the subject of this handoff:
- `d5f891d` — collapsible quick-pick sections + per-model prompt grammar
- `a25689b` — graded Body slider panel

Files involved (all under `Fixer/Create/`):

| File | State | What it does |
|---|---|---|
| `MakeSections.swift` | **NEW** | Collapsible named sections for quick picks (`MakeSectionsCard`), the section map, `quickRowTiled`, and the `Pictograph` / `PictographTile` plumbing. |
| `MakeBodyPanel.swift` | **NEW** | The graded **Body** slider panel — 5 axes (Build/Fitness/Bust/Curves/Height), each writing one mutually-exclusive term into the same prompt words the chips edit. |
| `MakeCompose.swift` | modified | `composeQuickCard` now returns `quickSectionsCard`; adds the Body panel as a `drawerSection("Body")` in the picture branch. |
| `MakeRecipes.swift` | modified | Adds `PhraseList.replacing(_:remove:with:)` used by the sliders. |

`desktop/fixer_desktop.py` also changed in `d5f891d` (server-side prompt weighting) but that's **already
deployed and live** — not part of your build.

## How the pieces fit (so you can read errors fast)
- Everything is an `extension CreateScreen`. The new views call existing members — if the compiler says
  one is missing, it's a rename, not a design change. Expected to exist already:
  `activeGroups` (`[QuickGroup]`), `quickBinding` (Binding<String> to the prompt words), `quickRow(_:)`,
  `tapQuick(_:)`, `quickTick` (@State Int, bumped for haptics), `makeExplicit` (@AppStorage Bool),
  `recipeStore.catalog`, `recipe`, `drawerSection(_:content:)`.
- Theme tokens used: `Fx.tile`, `Fx.hairline`, `Fx.secondaryText`, `Fx.accent`, `Fx.accentSoftText`,
  `Fx.onTintStrong`, `Fx.tileRadius`, `Fx.explicitText/Fill/Stroke`; plus `Motion.pop` and
  `.fxAnimation(_:value:)`. These were verified against the repo on the Windows side, but confirm on build.
- `PhraseList` (in `MakeRecipes.swift`) is the comma-separated phrase editor: `contains`, `toggled`, and
  the new `replacing`. The sliders read position back from the prompt each render via `contains`, and
  write via `replacing` (drops the axis's other terms, appends the chosen one).

## Likely-error / watch list (check these first if it won't compile or misbehaves)
1. **`quickTick` must be a mutable `@State var` on `CreateScreen`.** `MakeBodyPanel.swift` does
   `self.quickTick += 1`. If it's `let`/computed, that won't compile — point the write at whatever the
   haptic counter actually is.
2. **`quickBinding` must be a `Binding<String>`** (the words the sheet is currently showing). The slider
   get/set both go through it. If its name/signature differs, adjust `bodyAxisBinding` and
   `quickSelectedCount`.
3. **Section expand-state persistence (runtime, not compile).** `MakeSectionsCard` holds its own
   `@State private var expanded: Set<String>`. It sits at a stable offset in `composeStack`, so SwiftUI
   *should* keep that state across the sheet's re-renders. **If a section snaps shut after you edit the
   prompt or toggle a chip,** lift `expanded` up to `CreateScreen` and pass it in as a `@Binding`. Low
   risk, but it's the one thing I couldn't verify without running it.
4. **Pictographs render as plain chips by design.** `Pictograph.table` is intentionally empty (`[:]`),
   so `quickRowTiled` falls through to `quickRow` for every group and you get chips, not dashed-square
   tile placeholders. That's correct for this build — the icon art is a separate, deferred track. Don't
   "fix" the empty table.

## Verify after it builds (on device or simulator)
1. **Make screen opens** — no crash. (There's a known `swift_getTypeByMangledName` stack-overflow trap if
   the control stack stops being type-erased; `composeStack` erases each section to `AnyView` to avoid it.
   If Make crashes instantly on open, that's the regression to look at — keep each section its own
   `AnyView`.)
2. **Quick picks are now collapsible sections** (People / Body / Hair / Face / Wardrobe / Scene / Action /
   Explicit, plus a trailing "More" only if a catalogue group is unmapped). Collapsed by default; header
   shows a count badge of what's selected; tap expands.
3. **Body section** (sliders) appears **only for person recipes** (those whose quick picks include a
   body/body_size/height/fitness group) and sits right under the quick-pick card. Moving a slider
   writes/replaces one term in the prompt words; a chip you tapped for the same axis moves the slider, and
   vice-versa (they share the prompt text, so they stay in sync).
4. **Explicit section** only shows its groups while the Explicit switch is on (existing behavior,
   unchanged).
5. **Prompt sanity:** generate once with a weighted term — SDXL (Juggernaut) and Chroma honor
   `(phrase:1.3)`; Qwen ignores it (server strips weighting for qwen builders). This is already live
   server-side; just confirm nothing in the client got double-escaped.

## When done
- Rebuild and push one TestFlight build (per-batch rule).
- Commit any fixes to `main`.
- If you touched a shared symbol, say which and why in the commit.
