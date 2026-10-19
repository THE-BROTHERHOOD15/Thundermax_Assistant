# HANDOFF_FROM_GROK.md — recovered context from the Grok/ChatGPT era

> **Honesty note:** the original handoff file was never saved to disk — two
> exhaustive sweeps of the NAS and this machine (2026-07-11) found no copy.
> This document reconstructs what the Grok and ChatGPT ("Throttle Logic")
> sessions produced, from the artifacts they left on the NAS. Where
> something is inferred rather than recovered, it says so.

## Recovered artifacts, in chronological order

| Date | Artifact | What it tells us |
|---|---|---|
| 2025-08-12 | `GROKTUNING12AUG2025V1FIRSTEVERTUNE.pdf` (TMAX TUNING MANUAL/) | Grok produced the first-ever tune strategy for the 131 build |
| 2025-08-18/19 | 131ci flash sheets, autotune zone-lock guides, master log/reference | The knowledge base was built out: zone locking, timing logic, before/after comparisons |
| 2025-08-26 | `FINALALLAROUNDGOODTUNEVatlV2_decode_report.md` | ChatGPT ("Throttle Logic") verified a `.tbw` binary, extracted base-map ID `ZZSSQXETDN100720` from offset 0x0010, and generated the decode-report template this repo's parser now reproduces |
| 2025-08-30 | Ride packet, AutoTune gating tune card, tune report template (Thundermax AI Project/) | The validation-ride protocol and AutoTune temperature gates (200–280°F) were finalized |
| Sep 2025 | `25SEPTEMBER2025COOLERTUNE…` tunes, AI ADJUSTMENTS / AI CHANGES folders | AI-guided tune iterations were being flashed and logged |

## What the original sessions established

1. **The `.tbw` is a proprietary binary** — integrity can be verified and
   the base-map ID read from `0x0010`, but full numeric tables were captured
   via TMax Tuner screenshots, not binary decode.
2. **A capture checklist** (10 steps through TMax Tuner) so grid values land
   in a consistent report format.
3. **House tuning rules** — AutoTune temp gating, decel-pop VE/spark
   corrections by TPS/RPM cell, "cold-start and heat-soak trims are
   garbage." These are encoded in `COLLABORATION.md` and in the assistant's
   system prompt.

## What was lost (and has now been rebuilt)

- `src/thundermax_parser.py` — referenced but never saved. **Rebuilt** in
  this repo, and it goes further than the original: it also diffs two tunes
  region-by-region and batch-indexes tune folders.
- `COLLABORATION.md` — the coordination doc. **Rebuilt** as the primary path.
- The conversational assistant itself — **rebuilt** as
  `src/tune_assistant.py` on a local Ollama model, so it no longer depends
  on Grok/ChatGPT sessions that can vanish.

## Standing advice to whoever picks this up

Work from `COLLABORATION.md`. Do not write to `.tbw` files until scaling is
confirmed. Every suggested change still gets a validation ride.
