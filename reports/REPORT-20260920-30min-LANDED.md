---
type: shop
updated: 2026-09-20
---
# 2026-09-20 — store-run collection landed (Lenovo → tower assistant)

**Flash: NO. WRITE: NO. Hex-edit: NO.** `on_ecm` stays **file-claimed**.

Packet: `~/tmax-exchange/from-laptop/2026-09-20/ride_20260920-062643/`
Also: `lenovo-drop/from-lenovo/ride_20260920-062643/` and `lenovo-drop/handed-to-assistant/ride_20260920-062643/`
Zip SHA256 `7343269b8f43dbfc0ab172c842dd056ca3168902a4210c1ea54b4773fa373644` (1,368,340 B)

## Files (sha256)

| what | sha256 | bytes |
|---|---|---|
| MLOG 06:26:43 | `37ead3b609e08cb005e28a3ee9e5e8b88894bc3bb5688bf5ef8a96095a015989` | 1840326 |
| WriteMap idle autotune 16:01 09-19 | `f0a6e03e9db51389a58078a04088497a77c8dfc333e777789fb591ad2b051f01` | 214967 |
| Support-data map 06:26 | `3d5ab2988d96415c2f4cab04963534649d479b3fec308fd4048ce7c2d49b5c1b` | 214967 |
| FLASH THIS / instant-throttle | `5e1ea2f7b0722e670c3dacd2bdc93bfa6acdc861f4e9aed64639123a5ea10583` | 214967 |
| WriteMap newest cool night 15:44 | `e58bfc9a59bd338cba75179639e8ed15679bd356d1115a5432f1791f8575af75` | 214967 |
| WriteMap surge-smooth 08:06 09-19 | `8bf7646c2d21ece878e7f2dfea658ad86c9480a48562e93835d0b77b7363bfb4` | 214967 |

Base on these maps: **`HYSSPVCAHN051320`**. AutoTune **OFF** in ModuleInfo.

Support-data map is **not** byte-identical to last WriteMap. Treat WriteMap idle autotune as the last write-session file; support-data as a collection snapshot. Neither is a live ECM confirm.

## Ride log (measured)

Window **06:26:43 → 06:45:56** = **1153 s (19.2 min)**. Tower note called it ~30 min.

RPM (0x26 frames, n=582): min 796 / p50 1232 / p90 2507 / max 3951, mean 1473.7
hist idle 257, 1–2k 237, 2–3k 57, 3–4k 31, 4k+ 0
**3000–4500: 31 samples (5.3%).**

CHT n=556: p50 **207**, p90 255, max 430. ≥250: 19.6%.

AFR proxy ÷10 n=454: p50 **16.6** / p90 17.6 (decoder-edge; **not** chassis AFR; **do not lean** from this).

DTC historic: U1300 / U1097 / U1064 + HF O2/fuel-sender flags. No new current pile.

## Proposal (stop — no TMax cell edits from this landing)

This is a data drop. Audit on the assistant. One enrich-only change only if the log supports it, after Joshua says go.

Do not flash. Do not bump `on_ecm` off file-claimed from this SupportData map alone.
