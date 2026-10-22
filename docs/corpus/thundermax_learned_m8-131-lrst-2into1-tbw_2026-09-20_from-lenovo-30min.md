---
type: learned
setup: m8-131-lrst-2into1-tbw
title: FROM-LENOVO-30MIN
source: /home/joshua/tmax-exchange/from-laptop/2026-09-20/ride_20260920-062643/FROM-LENOVO-30MIN.txt
date: 2026-09-20
---

FROM LENOVO — 2026-09-20 morning TMax data collection
Bike: M8 131 Low Rider ST  SRN 709108502  map family HYSSPVCAHN051320
Request: inspect + audit this store-run log. Propose ONE enrich-only correction only if the log supports it.
Do NOT lean fuel. Do NOT advance timing. Do NOT global polish. Do NOT flash from the tower.

WHAT THIS IS
TMax-AutoSupport Collect TMax Support Data + Monitor Log from this morning on the Lenovo.
Recorded window: 2026-09-20 06:26:43 → 06:45:56  (1153 s, 19.2 min). Tower note called it ~30 min.
Joshua: AutoTune OFF for this store run; Monitor left on for the log.

LAST WRITE BEFORE THIS RIDE (ground truth)
2026-09-19 16:01:00  WriteMap  newest idle autotune cool night …
SHA256 F0A6E03E9DB51389A58078A04088497A77C8DFC333E777789FB591AD2B051F01

SHA256 (do not mix these up)
MLOG 06:26:43 (1,840,326 B)     37EAD3B609E08CB005E28A3EE9E5E8B88894BC3BB5688BF5EF8A96095A015989
  same bytes as MLog_SRN709108502.txt and TMaxSupportMonitorLog.txt
FLASH THIS / instant-throttle   5E1EA2F7B0722E670C3DACD2BDC93BFA6ACDC861F4E9AED64639123A5EA10583
WriteMap idle autotune 16:01    F0A6E03E9DB51389A58078A04088497A77C8DFC333E777789FB591AD2B051F01
WriteMap newest cool night 15:44 E58BFC9A59BD338CBA75179639E8ED15679BD356D1115A5432F1791F8575AF75
WriteMap surge-smooth 08:06     8BF7646C2D21ECE878E7F2DFEA658AD86C9480A48562E93835D0B77B7363BFB4
SupportData map 06:26 (module read) 3D5AB2988D96415C2F4CAB04963534649D479B3FEC308FD4048CE7C2D49B5C1B
ModuleInfo: AutoTune OFF, AFR override DISABLED, Super Charger YES / MAP 2.5 bar.

QUICK DECODE (same 0x26-frame method as the 09-17 45-min pack)
RPM  n=582  min 796 / p50 1232 / p90 2507 / max 3951  mean 1473.7
     hist idle 257, 1-2k 237, 2-3k 57, 3-4k 31, 4k+ 0
     3000–4500: 31 samples (5.3%)
CHT  n=556  min 160 / p50 207 / p90 255 / max 430   >=250: 19.6%
AFR proxy ÷10  n=454  p50 16.6 / p90 17.6  (proxy, not chassis AFR — do not lean from this)
DTC at 06:26: historic U1300 / U1097 / U1064 + HF O2/fuel-sender flags — no new current pile.

FILES IN THIS PACK
- MLOG-SRN709108502-FRM6a250722 Sun 20-09-26 062643AM.txt   (the run)
- MLog_SRN709108502.txt  (identical to 062643AM)
- WriteMap idle autotune 09192026-040100PM.tbw  (last write — ground truth)
- WriteMap newest cool night 09192026-034426PM.tbw
- WriteMap cool-night-20260918-surge-smooth 09192026-080645AM.tbw
- ReadMap newest cool night 09192026-035056PM.tbw
- SRN-709108502_September 20 2026.tbw  (SupportData module read)
- FLASH THIS - 131 Low Rider ST.tbw + READ ME  (instant-throttle sibling, SHA256 5E1EA2F7…)
- cool-night-20260919-instant-throttle.tbw  (same bytes as FLASH THIS)
- ModuleInfo.txt, DTC, ServiceData, Warranty .xls/.ssd
- JoshuaHenry-30019 support form txts
- LIVE-DATA-SUMMARY.md / .json

ALREADY ON TOWER (do not treat as this run)
Evening 35-min: ride_20260917-100655  22:06:55–22:42:10
Morning 5-min: ride_20260917-033424  03:34:24–03:39:08

Hard rules: last WriteMap is ground truth; enrich-only; 0 lean cells; 0 timing advance vs last trusted; AutoTune OFF after any later flash.
Lenovo pack folder: Desktop\tmax_rides\ride_20260920-062643
