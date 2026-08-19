---
type: learned
setup: m8-131-lrst-2into1-tbw
title: USB TMAX 2026-08-19 pull inventory and diff
source: /tmp/tmax-learn-usb-pull.md
date: 2026-08-19
---

USB Type-C thumb drive labeled TMAX pulled on the tower 2026-08-19. Joshua also saved the two newest maps to the Desktop; those files are byte-identical to the USB copies.

Archive (read-only, never write .tbw): /home/joshua/tmax-exchange/tunes-from-usb-2026-08-19/

Inventory:

- M8_131_CAM2_63inj fuelmoto origional.tbw — base-map HXSSEDCAAN061617, 214967 bytes, header 0x8B/0x148. FuelMoto original for the current 131 / CAM2 / 6.3 setup. Desktop + USB root.

- auto tune run from ch home 330 345 degrees f head temp.tbw — same base-map and size. AutoTune pass after the FuelMoto original. Filename claims cylinder-head temp 330–345F.

- TMAX_TUNES/M8_131_550CAM_63inj.tbw — HXSSEDCAAN061617, 225718 bytes, header 0x84/0x143. READ_ME flash-first file (6.3 / S&S 550). Byte-identical to Desktop/131-SS-550-fuel-maps/M8_131_CAM2_63inj.tbw (already on the tower from the Aug 8 map hunt).

- TMAX_TUNES/17AUG2025ATLRUNNINGGREATv6TODAY.tbw — ZZSSQXETDN100720, classic 214470. Last SE8-517 "running great" map. Historical only for the current 6.3 bike.

Classified diff, FuelMoto original → CH-home autotune (same 214967-byte family, so compare is valid):

- Not a timing rewrite and not an AFR-target rewrite.
- Table-data change is AutoTune / learned-VE trims (AUTOTUNE 305 bytes, UNMAPPED 93 bytes in the compare rollup; classify also flags autotune_learned + learned_ve_bulk at low confidence, plus checksum metadata).
- Learned-VE bulk cells mostly small negative deltas (about -1 to -5 raw). A few autotune_learned cells stepped +1 to +4 raw. One unmapped region at 0x0FA12 stepped +22 raw (4 u16s) — unmapped, do not call it AFR or timing.

CRITICAL: house AutoTune gates are enable >200F, disable >280F. A ride logged at 330–345F CHT is heat-soak territory. Those trims are suspect until a cooler validation ride (CHT in the 200–280F window) confirms them. Do not treat this file as a finished VE lock.

Current "my setup" base-map to prefer: HXSSEDCAAN061617 (6.3 / 550 / CAM2 / FuelMoto). HYSSPVCAHN051320 and ZZSSQXETDN100720 remain historical shop IDs.

NAS path /mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC/ was not writable from this seat; USB copies live in tmax-exchange until NAS ingest is possible.
