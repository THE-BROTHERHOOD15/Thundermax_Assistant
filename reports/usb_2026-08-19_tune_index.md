# TBW Index — USB TMAX pull 2026-08-19

Source stick: `/dev/sdb1` label `TMAX` (exfat). Joshua also saved the two
newest maps to Desktop; those match the USB copies byte-for-byte.

Archive (read-only copies): `/home/joshua/tmax-exchange/tunes-from-usb-2026-08-19/`

| file | base map ID | size | header | role |
|---|---|---|---|---|
| `M8_131_CAM2_63inj fuelmoto origional.tbw` | `HXSSEDCAAN061617` | 214967 | 0x8B / 0x148 | FuelMoto original (Desktop + USB root) |
| `auto tune run from ch home 330 345 degrees f head temp.tbw` | `HXSSEDCAAN061617` | 214967 | 0x8B / 0x148 | CH-home AutoTune after FuelMoto orig |
| `TMAX_TUNES/M8_131_550CAM_63inj.tbw` | `HXSSEDCAAN061617` | 225718 | 0x84 / 0x143 | READ_ME flash-first (6.3 / S&S 550). Identical to Desktop `131-SS-550-fuel-maps/M8_131_CAM2_63inj.tbw` |
| `TMAX_TUNES/17AUG2025ATLRUNNINGGREATv6TODAY.tbw` | `ZZSSQXETDN100720` | 214470 | 0x87 / 0x147 | Last SE8-517 "running great". v6 ≠ 6.3 injectors. |

SHA256 (archive):

```
2702c01574c6e08bea6c3d289032598df24c54b31b6b623af1acb787b793dad9  M8_131_CAM2_63inj fuelmoto origional.tbw
66d6b1a1fe761f0531711b38d9576fbe540cd30fda3934e9207225a8a920db7a  auto tune run from ch home 330 345 degrees f head temp.tbw
2893e3f4c759ded5f1e52a19ae7fe38bae0ed71c00c6adca73b329aa34228138  TMAX_TUNES/M8_131_550CAM_63inj.tbw
8e4f02e8fd159d995554f3e9c253a7ee807e7f3f0d095146804a412030e53a4b  TMAX_TUNES/17AUG2025ATLRUNNINGGREATv6TODAY.tbw
183692f103d0f227b521991a6e962e1209550b4b00d7bbc2137596f76552814b  TMAX_TUNES/READ_ME_FIRST.txt
```

FuelMoto orig → CH-home autotune: AutoTune / learned-VE trims only
(see `compare_fuelmoto_orig_vs_chhome_autotune.md`). Claimed CHT 330–345°F
is above the shop 280°F AutoTune disable gate.
