# TBW Index — tuning laptop pull 2026-09-16

Source: Lenovo tuning laptop (`LENOVO.attlocal.net` / `LENOVO.local`,
192.168.1.247). Bluetooth name `LENOVO TUNING LAPTOP` (`AC:50:DE:60:65:B8`)
is already connected on the Mac mini (192.168.1.248, user `jd`) and
Bluetooth-drops new tune saves into `~/Downloads` and
`~/Desktop/THUNDERMAX TUNES`. Pulled via `rsync` over SSH from the tower,
sha256-verified byte-for-byte against the source on both ends.

Later the same day the laptop also answered ICMP from the tower on
`.247` (TCP 22/139/445/3389 open). SMB guest is still denied; SSH needs
credentials. Canon: `/opt/home-mesh/LENOVO-TUNING-LAPTOP.md`.

Archive (read-only copies): `/home/joshua/tmax-exchange/from-laptop/2026-09-16/`
Bagged originals of the two new files: `/home/joshua/tmax-exchange/bagged/`

| file | base map ID | size | header | status |
|---|---|---|---|---|
| `FLASH THIS - 131 STEADY 35 SMOOTH 08-08.tbw` | `HYSSPVCAHN051320` | 214967 | 0x8B / 0x148 | **NEW** — my setup, real hand-edit (see below) |
| `petemaconautomap.tbw` | `HTSSOJSAAN010423` | 214470 | 0x87 / 0x147 | **NEW** — foreign (`HT` family, not this bike) |
| `M8_131_CAM2_63inj.tbw` (Downloads) | `HXSSEDCAAN061617` | 225718 | 0x84 / 0x143 | duplicate of `M8_131_550CAM_63inj.tbw` (byte-identical) |
| `M8_131_550CAM_63inj.tbw` (Desktop) | `HXSSEDCAAN061617` | 225718 | 0x84 / 0x143 | duplicate — already indexed 2026-08-19 |
| `17AUG2025ATLRUNNINGGREATv6TODAY.tbw` (Desktop) | `ZZSSQXETDN100720` | 214470 | 0x87 / 0x147 | duplicate — already indexed 2026-08-19 |
| `M8_131_CAM2_63inj.tbw` (Desktop) | `HXSSEDCAAN061617` | 225718 | 0x84 / 0x143 | duplicate — byte-identical to `M8_131_550CAM_63inj.tbw` |

SHA256 (new files only):

```
c375e25eea8b28789537f7699ef971b5c6ae02ad252ef234f56a195aaf1631b3  FLASH THIS - 131 STEADY 35 SMOOTH 08-08.tbw
b3a18036564026f314b71db26bf448dae82256b29ab58a6c01f17df6af6b8657  petemaconautomap.tbw
```

## FLASH THIS - 131 STEADY 35 SMOOTH 08-08.tbw

Base map `HYSSPVCAHN051320` — same lineage as the NAS `currenttune.tbw`
pointer, but a newer/different-size generation (214967B / `0x8B` header vs
`currenttune.tbw`'s 214470B / `0x87`), so it can't be direct-diffed against
it. It *can* be diffed against the same-size FuelMoto-original baseline —
`table_map.py classify` shows **1200 bytes changed in FUEL rich-correction**
(11 regions, high confidence), **448 bytes in the timing-limit array**
(medium confidence), plus AutoTune learned-VE drift. For comparison the
already-known CH-home AutoTune variant only differs from that same baseline
by 23 bytes, all low-confidence AutoTune noise. **This is a real hand-edited
revision — timing and fuel were deliberately touched, not just AutoTune
learning.**

AFR target curve (FID 0x29, n=109): 10.70-12.58, mean 12.37, 84/109 cells in
the WOT window (12.4-12.8), 0 in cruise (13.8-14.6).

Filename implies flash intent. This assistant never writes `.tbw`. Before
any WRITE in TMax: confirm injector size reads 6.3, idle 1024, decel cut
OFF, AutoTune OFF — same checklist as the existing flash-first file.

## petemaconautomap.tbw

Base map `HTSSOJSAAN010423` — `HT` engine-family, not in
`bike_profile.json`'s known IDs (`HX`/`HY`/`ZZ`). `tune_assistant.py sync`
correctly skipped it as a different setup. AFR curve is richer at the top
(11.06-12.94, mean 12.73) and WOT-window-light (12/109 vs 84/109 for FLASH
THIS) — a different fuel philosophy consistent with a different bike.
Filename ("pete macon auto map") reads as another customer's file that
ended up on the shop laptop. Kept in the archive for reference; **not**
folded into this bike's working set.

## Actions taken

- `tune_index.json` refreshed (`webui_core.refresh_tune_index()`) — picks up
  `~/tmax-exchange/from-laptop/` automatically, already a default scan root.
- `tune_assistant.py sync` folded the 4 matching-setup files (incl. FLASH
  THIS) into `docs/corpus/` as `thundermax_learned_*` KB docs.
- `./tmax kb ingest` ran the two new files through the full three-layer KB
  (SQLite metadata + FTS5 + chunking) under `project-key laptop-2026-09-16`,
  tagged `laptop-pull,bluetooth-drop`. 3 of 5 files in the folder flagged as
  duplicate (already-known content), 2 ingested fresh.
- A hand-written analysis note (AFR profile, diff findings, foreign-file
  flag) was `learn`-written into the KB so `tmax kb retrieve` and
  `tune_assistant.py ask` surface this reasoning, not just raw headers.

## Follow-up: full archive ingested (same session, per Joshua's request)

Joshua asked for *all* tunes and their associated notes to be in the data
consumption, not just today's laptop haul. Ran the full three-layer KB
ingest against the whole historical archive:

- `/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC` (recursive: top-level tunes,
  `AFTERBUILD TUNES/`, `AI ADJUSTMENTS/`, `AI CHANGES/`, `OL TUNES/`,
  `TMAX TUNING MANUAL/`) — 227 files seen, 227 ok, 37 quarantined
  (unsupported formats — `.zip`, `.slk`, duplicate `.docx` — never treated
  as known content per KB rules), 49 already-known duplicates. Ran with no
  forced vehicle/engine/hardware key so the ~19 other-customer tunes in
  `OL TUNES/` (George's Road Glide, Sheldon's Twin Cam 124 — base map IDs
  `H1SSTJLAAN120822` / `ZEYDKJJCEE100711`) aren't mislabeled as this bike;
  each file's own base-map ID is still read from its actual bytes, so
  retrieval/reranking still separates them correctly.
- `/home/joshua/Desktop/131-SS-550-fuel-maps` — 8 files, 1 new, 7 already
  covered by the NAS ingest.
- `nas-docs` (53 files: ride packet, tune card, tune report template, house
  wiring/mode-switch docs) was already ingested in an earlier session —
  confirmed still present, not re-run.

KB totals after this session: **548 files, 146 `.tbw` tunes parsed** (12
more quarantined as unreadable/other-format, not fabricated), 559 chunks,
8 projects. Verified retrieval surfaces real content from the newly
ingested archive, e.g. the shop's `AFR_Targets__Recommended_.csv` RPM×TPS
grid — a genuine house AFR-target reference table, useful evidence toward
closing the still-open "table axis order" question in this doc.
