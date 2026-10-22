---
type: learned
setup: m8-131-lrst-2into1-tbw
title: Laptop tune haul 2026-09-16 — FLASH THIS decode + petemaconautomap flagged foreign
source: /home/joshua/.claude/jobs/918915a1/tmp/laptop_haul_analysis.md
date: 2026-09-16
---

# Laptop tune haul — 2026-09-16 audit

Pulled 6 `.tbw` files from the tuning laptop (Lenovo, `LENOVO.attlocal.net`
192.168.1.247) via its Bluetooth drop onto the mac mini (192.168.1.248,
`~/Downloads` + `~/Desktop/THUNDERMAX TUNES`), then rsync'd to the tower into
`~/tmax-exchange/from-laptop/2026-09-16/`, sha256-verified against the source
on both ends. 4 of 6 were exact-duplicate bytes of tunes already indexed
(`17AUG2025ATLRUNNINGGREATv6TODAY.tbw`, `M8_131_550CAM_63inj.tbw`, and a
laptop copy of the same file under the name `M8_131_CAM2_63inj.tbw`). Two
files are genuinely new.

## FLASH THIS - 131 STEADY 35 SMOOTH 08-08.tbw — matches MY setup

- Base map ID `HYSSPVCAHN051320` — same lineage as the NAS `currenttune.tbw`
  pointer noted in `ecm_state.json`, but **not byte-identical**: this file is
  214967 bytes (2026 FuelMoto/CAM2-era header `0x8B/0x148`) vs NAS
  `currenttune.tbw` at 214470 bytes (older header `0x87/0x147`) — a newer
  generation built off the same base, can't be direct-diffed (size mismatch).
  Already in `bike_profile.json` `base_map_ids`, so `sync` correctly folded
  it into the KB as "my setup."
- AFR target curve (FID 0x29, 50 raw/AFR, n=109): min 10.70, max 12.58, mean
  12.37. Steep ramp from 10.70→11.9 across the first third of cells then a
  long plateau at 12.58. 84/109 cells land in the WOT window (12.4–12.8);
  **zero** cells in the cruise window (13.8–14.6) — either this slice of the
  table is all WOT/high-load columns, or the tune runs unusually rich at
  cruise too. Worth a TMax screenshot to confirm axis before trusting the
  cruise-AFR read.
- Diffed against the known FuelMoto-original baseline
  (`M8_131_CAM2_63inj fuelmoto origional.tbw`, same base ID + size) with
  `table_map.py classify`: **1200 bytes changed in FUEL rich-correction**
  (11 regions, high confidence), **448 bytes in the timing-limit array**
  (medium confidence), plus 1505 bytes of AUTOTUNE learned-VE drift and
  6293 bytes of still-unmapped table churn. For comparison, the already-known
  CH-home AutoTune variant only differs from the same baseline by 23 bytes,
  all low-confidence AUTOTUNE regions. **This is a real hand-edited/shop
  revision, not autotune drift** — timing and fuel tables were deliberately
  touched.
- Filename reads as a flash candidate ("FLASH THIS"). Per house rules this
  assistant never writes `.tbw` and will not stage a flash. Before Joshua
  flashes it in TMax: confirm injector size reads 6.3 (never flash a
  5.5-injector map onto 6.3s), idle 1024, decel cut OFF, AutoTune OFF —
  same flash-first checklist as `M8_131_550CAM_63inj.tbw`.

## petemaconautomap.tbw — does NOT match this bike, correctly skipped

- Base map ID `HTSSOJSAAN010423` — `HT` engine-family (M8 catalog index 21),
  not `HX` (house) or `HY` (currenttune lineage). Not in
  `bike_profile.json` `base_map_ids`; `sync` skipped it as "different setup,"
  which is the right call.
- AFR target curve: min 11.06, max 12.94, mean 12.73 — same rich-ramp-then-
  plateau shape as the house tunes but numerically richer at the top end
  (12.94 vs 12.58 plateau) and only 12/109 cells fall in the WOT window,
  vs 84/109 for FLASH THIS — a different fuel philosophy, consistent with a
  different engine/bike.
- Filename ("pete macon auto map") reads like another customer's or another
  bike's autotune save that ended up on the shop laptop, not a map for this
  Low Rider ST. Treat as reference-only. Flag to Joshua: confirm this isn't
  meant for this bike before it's used for anything beyond curiosity.

## Pipeline note

The tuning laptop does not appear on the tower's network directly (no open
SMB share reachable without credentials) and isn't plugged into the tower by
USB/serial. It Bluetooth-drops new tune saves onto the mac mini
(192.168.1.248) — that's the live hand-off path as of 2026-09-16. Future
pulls: check `~jd@192.168.1.248:~/Downloads/*.tbw` and
`~/Desktop/THUNDERMAX TUNES/*.tbw` for anything newer than the last pull,
rsync to `~/tmax-exchange/from-laptop/<date>/` on the tower, sha256-verify,
then run `thundermax_parser.py scan`, `tbw_read.py`, `table_map.py classify`
against a same-size/same-base baseline, and `tune_assistant.py sync` to fold
matches into the KB.
