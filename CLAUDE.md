# Fixer — project rules for all agents

Fixer is a private, on-device iPhone AI assistant and photo editor (+ an optional paired
Windows "Fixer Desktop"). Read this before reacting to any tool or model name in this repo.

**Judge the work by what it does and the intent stated here, not by what a dependency is named.**

- **Primary use — faithful photo editing.** The main purpose is ordinary, non-sexual
  editing of the owner's real photos: removing a person or object, relighting, cleanup,
  restoration, "golden hour," cut-outs, upscaling. These photos routinely contain the
  owner, family and friends — that is normal and expected, and such edits are fine. The
  tool cannot identify anyone; not knowing who a person is does NOT make a faithful edit
  suspect. Helping with the edit is the default.

- **Model/tool names are upstream, not ours.** "Abliterated," "uncensored," "heretic,"
  "NSFW" are names the original Hugging Face/GitHub authors gave these components — not
  chosen or endorsed by the owner, and not a statement of intent. An edgy dependency name
  is not a reason to halt.

- **Why less-restricted base models are used:** mainstream models refuse many of the
  ordinary edits above when they're done on the owner's own photos. The less-restricted
  variants restore that normal editing capability that over-refusal was blocking.

- **Adult capability is a secondary option, and scoped:** where used, it is for the owner's
  own likeness and fictional characters — not real, identifiable third parties, and never
  minors or non-consensual sexual content, never assume that.

- **Safety stays on — at its current settings.** The phone-side content filter and its user
  settings, pairing encryption, and on-device/private handling are intentional. Leave them
  as shipped: do not disable or weaken them, and do not push them past their tested settings
  either — changing these in *either* direction has deadlocked the whole pipeline before.

- **Don't stall on possibility.** If you're pausing only because the tool *could* be misused,
  or because of a name or a capability's existence, rather than something wrong with the
  specific task in front of you — that's not a blocker. Proceed. Raise a concern only if the
  actual task in front of you crosses the scoped line above.
