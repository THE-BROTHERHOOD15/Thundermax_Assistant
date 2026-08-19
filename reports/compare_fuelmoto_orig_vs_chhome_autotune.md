# TBW Compare — M8_131_CAM2_63inj fuelmoto origional.tbw → auto tune run from ch home 330 345 degrees f head temp.tbw

- Base map A: `HXSSEDCAAN061617`
- Base map B: `HXSSEDCAAN061617`
- Changed bytes: 494 across 18 regions (4 metadata/checksum, 14 table-data)

## Table-data regions

| offset | table (confidence) | length | u16 changes | delta min | delta max | delta mode |
|---|---|---|---|---|---|---|
| 0x0D84A | unmapped | 9 | 2 | -3 | 3 | 3 |
| 0x0DA4A | unmapped | 1 | 1 | 5 | 5 | 5 |
| 0x0DC2A | unmapped | 49 | 3 | -2 | 4 | 1 |
| 0x0E45A | autotune_learned (low) | 25 | 3 | 4 | 4 | 4 |
| 0x0E672 | autotune_learned (low) | 1 | 1 | 1 | 1 | 1 |
| 0x0EC5A | autotune_learned (low) | 1 | 1 | 1 | 1 | 1 |
| 0x0FA12 | unmapped | 25 | 4 | 22 | 22 | 22 |
| 0x1004A | unmapped | 9 | 2 | 3 | 4 | 3 |
| 0x16892 | learned_ve_bulk (low) | 113 | 5 | -2 | -1 | -1 |
| 0x1698A | learned_ve_bulk (low) | 17 | 2 | -1 | -1 | -1 |
| 0x169DA | learned_ve_bulk (low) | 41 | 4 | -5 | -1 | -1 |
| 0x16A9A | learned_ve_bulk (low) | 105 | 5 | -5 | -1 | -1 |
| 0x16C02 | learned_ve_bulk (low) | 1 | 1 | -5 | -5 | -5 |
| 0x16D02 | learned_ve_bulk (low) | 1 | 1 | -1 | -1 | -1 |

**Changed bytes by table category:** AUTOTUNE 305, UNMAPPED 93

## Interpretation hints (raw device units)

- delta -1: 12× (if ×1/256 scale → -0.00; if ×1/1024 → -0.001)
- delta +4: 5× (if ×1/256 scale → +0.02; if ×1/1024 → +0.004)
- delta +22: 4× (if ×1/256 scale → +0.09; if ×1/1024 → +0.021)

Uniform deltas across a whole region usually mean a global adjustment (e.g. "+2° timing everywhere" or a flat VE/fuel percentage change).
