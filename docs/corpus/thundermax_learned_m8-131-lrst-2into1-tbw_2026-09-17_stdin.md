---
type: learned
setup: m8-131-lrst-2into1-tbw
title: stdin
source: /dev/stdin
date: 2026-09-17
---

USB WARSTATION cool-night data run 2026-09-17 processed.

File: cool night and bike stayedunder 250 degrees but surged above3000rpm-4500 rpm.tbw
sha256 063284d5fab2366f0a708f9d6d501b24ffed393f9e673a9decebbdeb0709c3b1
sha1 70e2edf95b9e18e462c5086918d524e43a9cc656
base HYSSPVCAHN051320 size 214967 header 0x8B/0x148
Stick mtime 10:49 EDT. Original left on WARSTATION. Bagged local+NAS+Desktop. Mini Desktop copy 11:54 EDT same bytes.

Complaint: cool night, CHT stayed under 250F (AutoTune IN GATE 200-280), surge 3000-4500 rpm. Rider later: way better than prior, cooler, pulled harder, still surges 3k-4.5k.

AFR FID 0x29 UNCHANGED vs current tune.tbw and vs corrected-tune.tbw (0 cells). n=109 min 10.70 max 12.58 mean 12.37. 84 cells in 12.4-12.8. 0 cells in 13.8-14.6 cruise window of this slice. Axis unconfirmed — do not lean the 10.70 cells.

Diff vs last bagged current tune: 23405 bytes / 82 regions. Category (compare): UNMAPPED 8824, SHARED 5957, AUTOTUNE 3598, FUEL 2308, AFR_VE 1466, TIMING 1238. High-confidence: AFR_VE pages + fuel_rich_correction (mostly negative raw). Learned VE bulk mixed +1..+5 and small negatives. This IS the data run — AutoTune/learned VE already inside the pulled file.

Closer to last-night corrected-tune.tbw than to current tune.tbw (classify unmapped 1886 vs 3486). Likely rode corrected-tune then pulled.

Do NOT apply canned afr-audit (would raise 10.70 toward 12.4 without axis). Do NOT apply canned decel-pop (complaint is surge not pop). Do NOT flash. Next adjustment: +2% VE front+rear at 3072-4608 rpm, 8-30% TPS for remaining surge. Confirm cells in TMax before Save As sibling.
