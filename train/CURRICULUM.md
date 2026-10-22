# Throttle Logic — TBW methods curriculum (2026-09-16)

Canonical training text for every agent seat. Numbers live in
`src/tables.json` (`afr_scale_finding`, `timing_scale_finding`). This file
is the *why* and *how*. Do not hex-edit `.tbw` files.

## 1. Hardware (this shop)

- 2023 FXLRST, M8 131ci, S&S 550 / CAM2, **6.3 injectors**, 2-into-1, ThunderMax TBW
- Live base map `HXSSEDCAAN061617`
- Flash-first file: `M8_131_550CAM_63inj.tbw` (file-claimed on-ECM, not a live module read)
- Never flash 17AUG / `*55inj*` / base `ZZSSQXETDN100720` onto 6.3s. v6 ≠ 6.3 injectors.

## 1b. Base-map ID catalog (PPM answer key)

`BASEMAPDEFSM8.PPM` is TMax's M8 map picker, **not** a Portable PixMap and **not** AFR cells.

- `tmax info FILE.tbw` and `tmax catalog ID` look up the 16-char ID
- Standard ID = 10-char combo + MMDDYY mint date. `HX` = this shop's family (engine index 23)
- Exact miss with same first 10 chars = a later suffix of the same combo
- `HYSSPVCAHN051320` / `ZZSSQXETDN100720` are **not** in the M8 PPM
- ID = TMax starting base. Shop 6.3 / 550 files still say `HXSSEDCAAN061617`

## 1c. Module-command FIDs (the table legend)

From `TMaxI_ModuleCommandStaticDefinitions.dat` (grids are hard-coded in TMax `Map.cpp`):

| FID | Name | What TMax shows |
|-----|------|-----------------|
| `0x20` | Front Fuel Map | Fuelflow vs TPS @ RPM, SID 0–63. **This is TMax VE.** Also the directory marker on trailers. |
| `0x26` | Ign Timing Map vs TPS | Timing vs TPS @ RPM, SID 0–63 |
| `0x29` | AFR Target Map | AFR vs TPS @ RPM, SID 0–63 |

`tmax read` names these three. 0x29 has scale 50 (high). 0x26 map scale is unconfirmed (49 raw/deg is the timing-limit array only). 0x20 fuel/VE scale is unlocked. Axis still unconfirmed.

## 2. The 8-bit read trick (Joshua)

1. Treat the `.tbw` as raw bytes. Convert to 8-bit binary.
2. Landmark **`0b00100000` = 32 = `0x20`**. Search `20 00 00 00`. That byte is also FID Front Fuel Map / directory marker.
3. On the 225718-byte header-`0x84` family that tail sits at **0x01BB5** (end of AFR / start of 0x20+0x29 trailer).
4. On 214470-byte header-`0x87` files the AFR *band* is the same; the landmark is **not** at that offset. Scan, do not hard-code one address.

## 3. AFR (high confidence)

- Signed i16, 4-byte stride, start **`0x1981`**
- **50 raw = 1.00 AFR**
- Evidence: `advancedtiming.tbw` vs `advancedtimingplusAFR CORRECTION.tbw` — 50 cells moved exactly −50 raw (14.40 → 13.40)
- `tmax read FILE.tbw` prints the curve. `--vs OTHER` diffs in AFR points.
- **Axis (RPM × TPS) is unconfirmed.** Do not name a cell as “3000 rpm / 40% TPS” without a TMax screenshot.

## 4. Timing

- Limit array `0xC3C9`: **49 raw/deg** (medium), from the global −1° pair
- Main RPM×TPS timing map scale is **not** locked. Do not quote those cells as degrees.

## 5. Adjustments (shop loop)

Read → propose → Joshua approves → **edit in TMax Tuner** → Save As sibling → `tmax ecm bag`.

```
tmax ecm show
tmax read FILE.tbw
tmax read FILE.tbw --vs last_known_good
tmax read FILE.tbw --propose afr-audit
tmax read FILE.tbw --propose decel-pop
```

`--propose` writes nothing. Guardrails vet the click-list. Never `WRITE Module Maps` unless he says flash.

House gates: AutoTune learn >200°F / off >280°F. WOT AFR 12.4–12.8. Spark ceiling 32°. Rear timing equal or retarded vs front. Decel pops >4k: +2% VE @ 0–2% TPS, 3840–4608 rpm.

## 6. What “educated” means (pass the drill)

An agent is trained when `tmax train drill` exits 0 and they can answer the quiz in `train/drills.json` without inventing axis labels or offering to hex-edit.
