# COLLABORATION.md — Thundermax Tuning Assistant (primary path)

This is the primary coordination document for the project. Any AI or human
picking up this project starts here.

## What this project is

A local tuning assistant for Joshua's **2023 Harley-Davidson Low Rider ST,
131ci, 2-into-1 exhaust, ThunderMax TBW ECM**. It:

1. **Parses `.tbw` tune files** (`src/thundermax_parser.py`) — validates
   integrity, extracts the base-map ID, diffs two tunes region-by-region,
   generates markdown decode reports, and indexes whole folders of tunes.
2. **Answers tuning questions locally** (`src/tune_assistant.py`) — an
   Ollama-backed chat/Q&A grounded in the shop's tuning documentation from
   the NAS brain_vault, plus an `analyze` mode that explains what changed
   between two tunes in rider terms.

## Provenance

- The original effort ran through Grok and ChatGPT ("Throttle Logic")
  sessions in Aug–Sep 2025. Those sessions produced the tuning docs, tune
  cards, ride packets, and one decode report — but **no source code
  survived** to the NAS. See `HANDOFF_FROM_GROK.md` for what was recovered.
- This codebase was rebuilt from those artifacts on 2026-07-11 by Claude
  (Claude Code), reverse-engineering the TBW format from real tune files.

## Where the data lives

| What | Where |
|---|---|
| Tune files (`.tbw`, ~30) | `/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC/` (NAS not writable from tower user) |
| USB TMAX pull 2026-08-19 | `/home/joshua/tmax-exchange/tunes-from-usb-2026-08-19/` (read-only copies) |
| Latest pair (Desktop) | `/home/joshua/Desktop/M8_131_CAM2_63inj fuelmoto origional.tbw` + `auto tune run from ch home 330 345 degrees f head temp.tbw` |
| Tuning manuals & guides | `/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC/TMAX TUNING MANUAL/` |
| AI project docs (ride packet, tune card, templates) | `/mnt/nas/ADMIN/LOCAL NAS/NEWER IPAD 16SEP2025/Thundermax AI Project/` |
| Searchable markdown corpus (37+ docs) | `/mnt/nas/ADMIN/brain_vault/` (files matching `*thundermax*` / `*thunder_max*`) |
| Prior decode report (format reference) | `.../TMAX TUNING MANUAL/FINALALLAROUNDGOODTUNEVatlV2_decode_report.md` |

## TBW format — what we know so far

- Observed sizes: **214470** (classic TBW, header `0x87/0x147`), **214967**
  (2026 FuelMoto 6.3/CAM2 pair, header `0x8B/0x148`), **225718** (S&S 550 /
  6.3 flash file, header `0x84/0x143`), **225990** (other 131/550 hunt maps).
- Header: four LE u32 at 0x00 (`0x87, 0x4000, 0x1, 0x147` in every sample).
- **Base-map ID** at `0x10`: one length byte (0x10=16) + 16 ASCII chars
  (e.g. `HYSSPVCAHN051320`, `HXSSEDCAAN06161…`, `ZZSSQXETDN100720`).
  This is the only ASCII string in the file.
- `0x21`–`~0x3C0`: per-section metadata/checksums (these bytes churn
  whenever *any* table changes).
- Beyond `~0x3C0`: table data. Cells are little-endian fields on a **4- or
  8-byte stride** (a value word often paired with a flag/limit word) — *not*
  a densely packed grid, so the earlier "16-byte-stride u16 (value,flag)
  pairs" description was an oversimplification. **AutoTune correction cells
  are signed**, so small negative trims appear in a raw byte diff as
  `0x0000 → 0xFFxx` borrows.
- **Table map (NEW):** `src/tables.json` + `src/table_map.py` locate and name
  the major bands (AFR target, AFR/VE pages, fuel pages, timing map, timing
  limit array, fuel/rich correction, autotune-learned, metadata) with a
  confidence tier each, derived by differential analysis of semantically
  labeled tune pairs. See "Table map" below.
- **Timing scale:** the global −1° edit shifted every cell of the
  `0xC3C9` timing-limit array by exactly −49 raw → **~49 raw units per
  degree** (34°×49 = 1666). Medium confidence: measured on that array, and
  *assumed* (not yet proven) for the main RPM×TPS timing map.
- **Per-cell engineering-unit scaling is still unconfirmed for most tables.**
  Diff analysis locates *where* each table lives and *what category* it is,
  but exact axis order and unit scale need one TMax Tuner ground-truth
  reading per band — the top remaining open task.

## House tuning rules (from validated shop history)

- AutoTune learning gates: **enable >200°F, disable >280°F**; cold-start and
  heat-soak trims are garbage — block them.
- **Current hardware (2026-08-19 USB TMAX + Desktop save):** 6.3 injectors,
  S&S 550 / CAM2, FuelMoto lineage, base-map `HXSSEDCAAN061617`.
- **Injector mismatch is a brick-the-fueling risk.** Never flash a 5.5 /
  `*55inj*` map onto 6.3 injectors. `17AUG2025ATLRUNNINGGREATv6TODAY.tbw`
  is SE8-517 history — **v6 = version 6, not 6.3 injectors**. Read injector
  size in TMax before WRITE; if it says 5.5, do not flash it onto 6.3s.
- Flash-first file: `M8_131_550CAM_63inj.tbw`. Confirm injector size 6.3,
  idle 1024, decel cut OFF, AutoTune OFF. After WRITE: initialize if asked,
  hands off throttle; one start, no throttle; both pipes hot in 30 seconds
  or shut off.
- CH-home autotune file claims **330–345°F CHT** — above the 280°F disable
  gate. Treat those learned trims as suspect heat-soak, not trusted VE.
- Decel pop protocol: closed-throttle 4000→2000 rpm decels in 3rd/4th.
  Pops above 4k → **+2% VE @ 0–2% TPS, 3840–4608 rpm**. Broad-range pops →
  **−1° spark @ 0–2% TPS, 2048–2816 rpm**.
- Always log TPS, RPM, AFR target vs actual, CHT, trims.
- Every tune change gets a validation ride + tune report
  (template in the Thundermax AI Project folder).

## Table map

`src/tables.json` is the frozen band map; `src/table_map.py` reads it and:

- `python3 table_map.py bands` — list every named band + confidence.
- `python3 table_map.py classify A.tbw B.tbw` — label the changed regions of a
  diff and roll them up by category (TIMING / AFR / FUEL / AUTOTUNE / …).
- `python3 table_map.py derive <tune_folder>` — recompute per-band evidence
  from the labeled tune pairs so the JSON stays auditable, not magic.

`thundermax_parser.py compare` now also prints a **table** column and a
per-category byte summary, using this map when it's importable (degrades
gracefully if `tables.json` is absent). Confidence tiers: **high** = band
isolated cleanly to one intent across independent pairs; **medium** =
one-sided evidence; **low** = churns on every edit (autotune/checksum). Treat
medium/low bands as *located but not cell-accurate*.

## Current state (2026-08-19) — TMax Command Center GOAL

**Product UI:** TMax Command Center (`src/webui_server.py`) on **:8181**
(LAN). Phases 1–7 landed on main. Never writes `.tbw`. Guardrails are the
only hard-block. USB 2026-08-19 shop facts (`shop_override`) apply in CLI
and co-pilot. Tune library reads `~/tmax-exchange/tunes-from-usb-2026-08-19`
+ Desktop (NAS optional). `:8090` is AI Operator — do not steal.

- [x] Phase 1–3: dashboard, streaming chat, tune library + visual diff
- [x] Phase 4: journal + learned KB
- [x] Phase 5: proposals + deterministic vetting (`guardrails.py`)
- [x] Phase 6: virtual dyno gauges (house-limit model, not live ECU)
- [x] Phase 7: systemd `tmax-api` serves Command Center on :8181

## Prior USB pull (2026-08-19)

- [x] **USB TMAX pull** — remounted `/dev/sdb1` (label TMAX) as joshua at
      `/run/media/joshua/TMAX`; copied 4 `.tbw` + `READ_ME_FIRST.txt` to
      `~/tmax-exchange/tunes-from-usb-2026-08-19/` (sha256 verified). Joshua
      also saved the two newest maps to Desktop (byte-identical to the USB
      copies). NAS THROTTLE LOGIC is not writable from this seat.
- [x] New working pair (both `HXSSEDCAAN061617`, 214967 bytes, header
      `0x8B/0x148`): FuelMoto original vs CH-home autotune. Classified
      diff is AutoTune/learned-VE trims only (no timing/AFR-target rewrite).
      Filename CHT 330–345°F is above the 280°F AutoTune disable gate.
- [x] `M8_131_550CAM_63inj.tbw` (225718, same bytes as Desktop
      `131-SS-550-fuel-maps/M8_131_CAM2_63inj.tbw`) is the READ_ME flash-first
      6.3 / S&S 550 file.
- [x] `17AUG2025ATLRUNNINGGREATv6TODAY.tbw` kept as SE8-517 history
      (`ZZSSQXETDN100720`, classic 214470).
- [x] Assistant taught: house-rule injector/flash protocol + heat-soak
      warning + `tmax sync` of the USB archive + Desktop pair. `tmax learn`
      / `tmax sync` wired into the unified CLI. Parser accepts known sizes
      `{214470, 214967, 225718, 225990}`. Brick-the-bike facts
      (no 17AUG onto 6.3s; no locking 330–345°F AutoTune trims) are
      `shop_override()` answers so the fast model cannot invent a flash
      procedure.

## Current state (2026-07-12)

- [x] **`tmax` unified CLI** — one entry point for parser, table map, and
      assistant; auto-starts Ollama when needed; `tmax verify` self-check
- [x] Parser: integrity, base-map ID, header decode — tested on real tunes
- [x] Parser: region diff (`compare`) with delta stats **+ named-table labels**
- [x] Parser: decode-report generation matching the shop template
- [x] Parser: batch `scan` index of a tune folder
- [x] Assistant: Ollama-backed `ask` / `chat` / `analyze`
- [x] **`analyze` is grounded**: it feeds the LLM the *classified* diff
      (categories + confidence), not raw hex, and instructs it to describe
      only what actually changed — verified live against the global −1°
      timing pair (correctly identified; no invented features)
- [x] **Test suite**: 13 unit tests on synthetic tunes (`tests/`), no
      NAS/Ollama required; wired into `scripts/verify.sh`
- [x] Ollama on `127.0.0.1:11434`; assistant auto-routes per question:
      `qwen2.5-coder:14b` (fast, on the RTX 5060 Ti GPU) for lookups,
      `hermes3:70b` (deep-thinker, slow ~0.7 tok/s) for tuning strategy and
      `analyze`. Overrides: `--fast` / `--deep` / `--model`. (2026-07-13)
- [x] **Map table offsets to named bands** — AFR target, AFR/VE pages, fuel
      pages, timing map, timing-limit array, fuel/rich correction, autotune,
      metadata (`tables.json`, confidence-tiered)
- [~] Confirm raw→engineering scaling: timing ≈49 raw/deg (medium); other
      tables still need one TMax ground-truth cell each (see below)
- [ ] Split real map pages out of the `0x3C0–0x5132` shared-churn block with
      surgically-isolated single-table edits
- [ ] Optional: GPU accel (RTX 5060 Ti present but NVIDIA driver not installed)

### The one high-value experiment to unlock cell-accuracy

For each band flagged `needs_ground_truth` in `tables.json`: in TMax Tuner,
change exactly **one** map (e.g. AFR target only), save as a new `.tbw`, then
`table_map.py classify old.tbw new.tbw`. A single-table edit isolates that
band cleanly from checksum churn; reading one known cell value in TMax and
matching it to the raw bytes locks both the axis order and the unit scale.

## Ground rules

- **Never write to `.tbw` files.** Read-only until the format is fully
  confirmed. A corrupted flash can strand the bike.
- Anything the assistant suggests still goes through the validation-ride
  protocol before it's trusted.
- NAS paths are the source of truth for data; this repo holds code only.

## Update — 2026-08-25 ChatGPT session ingest

Ingested Joshua's ChatGPT share (2026-03-30) into `docs/corpus/` via `tmax learn` and Command Center journal.

- Share: https://chatgpt.com/share/6a8d5426-8c60-83e9-bb39-138482c63f9c
- Vault: `brain/thundermax_chatgpt_share_2026-03-30_lowrider-st-131.md` + transcript
- KB: `thundermax_learned_*_chatgpt-2026-03-30-launch-bog-*`
- Session complaint: launch bog from a stop (dives / falls on its nose) + weak bottom end
- Corrected screenshot timing: **32° at 5,632 RPM** (ChatGPT first said 51,200)
- AFR vs TPS ~3840 RPM started ~15.0 at low TPS
- **AFR-vs-temp 1.5 is an adjustment/offset, not AFR 1.5:1**
- Session claimed 68 injectors / Two Brothers shorty — **live hardware remains 6.3 / S&S 550 / 2-into-1**

## Update — 2026-08-25 ThunderMax topo map

Native Obsidian canvas of ThunderMax intel (live hardware vs injector conflict vs Mar 30 launch bog vs brick-the-bike rules vs corpus).

- Vault: `brain/thundermax_topo.canvas` + `brain/thundermax_topo.md`
- This repo: `docs/thundermax_topo.md` + `docs/thundermax_topo.canvas`
