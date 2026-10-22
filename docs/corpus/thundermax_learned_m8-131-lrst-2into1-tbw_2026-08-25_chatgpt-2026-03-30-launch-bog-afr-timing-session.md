---
type: learned
setup: m8-131-lrst-2into1-tbw
title: ChatGPT 2026-03-30 launch bog AFR timing session
source: /tmp/tmax-chatgpt-2026-03-30-kb.md
date: 2026-08-25
---

# ChatGPT Thundermax session — Low Rider ST 131 (2026-03-30)

Hub: [[thundermax_command_center_2026-08-19]]
Transcript: [[thundermax_chatgpt_share_2026-03-30_transcript]]
Flash rules: [[thundermax_usb_tmax_flash_rules_6-3_vs_55inj_2026-08-19]]
AutoTune gates: [[thundermax_ch_home_autotune_330-345f_above_280f_gate_2026-08-19]] · [[icloud_thundermax_ai_project_2025-08-30_autotune_gating_tunecard_pdf_465fb7]]

Ingested into the tower Thundermax assistant KB on 2026-08-25 via `tmax learn`. Source share: https://chatgpt.com/share/6a8d5426-8c60-83e9-bb39-138482c63f9c

This is a **historical rider-session capture**, not a replacement for live shop facts. ChatGPT advice is tagged `(chatgpt-unverified)`. Joshua's own statements are tagged `(rider-reported)`. Current hardware from the 2026-08-19 USB TMAX stick is tagged `(shop-fact)`.

> [!warning] Current hardware wins
> Live bike (2026-08-19): **6.3 injectors**, **S&S 550 / CAM2**, FuelMoto lineage, base-map `HXSSEDCAAN061617`, exhaust **2-into-1**. Flash-first file: `M8_131_550CAM_63inj.tbw`. Never flash `*55inj*` / 17AUG v6 onto 6.3s. AutoTune enable **>200°F**, disable **>280°F**. Do not lock 330–345°F CH-home trims.

## Summary

On 2026-03-30 Joshua used ChatGPT as a Thundermax tuning assistant for the Low Rider ST 131. He described a high-flow 131 build that felt **weak on the bottom end** and **bogged off a stop** (dives right / falls on its nose, then picks up). He walked ChatGPT through AFR vs engine-temp, timing vs RPM, and AFR vs TPS screenshots. Useful rider symptoms and screenshot numbers are kept. ChatGPT mixed up AFR *adjustment* vs true AFR, misread RPM scale until corrected, and told him to enable AutoTune without the house temperature gates.

## Bike as claimed in the session

| Item | Session claim | Flag | Live shop fact (2026-08-19) |
|---|---|---|---|
| Bike | 2024 Harley-Davidson Lowrider ST 131 | (rider-reported) | 2023 Low Rider ST (FXLRST), M8 131ci (shop-fact) |
| Cam | Screamin' Eagle top-end cam | (rider-reported) | S&S 550 / CAM2; prior lineage SE8-517 (shop-fact) |
| Kit | 131 kit | (rider-reported) | M8 131ci (shop-fact) |
| Injectors | "68 injectors" | (rider-reported, **conflict**) | **6.3 injectors** (shop-fact). Do not treat 68 as current. |
| Valvetrain | Fueling tappets; Team Eagle adjustable pushrods | (rider-reported) | not restated on USB stick |
| Suspension | All-in Screamin' Eagle front and rear | (rider-reported) | not an ECM item |
| Exhaust | PBR + Two Brothers Racing shorty | (rider-reported, **may be stale**) | **2-into-1** (shop-fact) |
| Fuel rail | S&S metal fuel rail | (rider-reported) | not restated on USB stick |
| ECM | Thundermax tuner | (rider-reported) | ThunderMax TBW (shop-fact) |

## Rider symptoms (keep these)

- Bottom end feels weak. (rider-reported)
- Leaving a red light: bike **almost dives right / falls on its nose**, then picks up and goes. Classic **off-idle launch bog / hesitation**, not a top-end problem. (rider-reported)

Shop read of that complaint: lean or late fuel on throttle crack (0–20% TPS, idle→~2500 RPM), possible timing too retarded off idle, possible IAC/idle too low, possible TB sync / TPS, possible shorty-exhaust backpressure loss. Do **not** chase WOT AFR first for this feel.

## Screenshot numbers ChatGPT read (treat as session observations)

These came from Joshua's TMax screens, interpreted by ChatGPT. Not a live `.tbw` decode.

### Module / global

- AFR vs engine-temp graph: **flat ~1.5** across a wide CHT range. Cursor showed **AFR adjustment −0.000**. ChatGPT said AutoTune was **off**. (chatgpt-unverified read of screenshots)
- Cranking fuel: **9.0 msec**
- Throttle progressivity: **5**
- Injector timing: **409 deg**
- Cooling fan threshold: **210°F**
- Final drive ratio: **87**
- Engine temp alarm: **420.05°F** (ChatGPT suggested ~400°F; unverified)

> [!important] AFR 1.5 on that graph is an **adjustment / offset**, not AFR 1.5:1
> True AFR 1.5:1 would be impossibly rich. ChatGPT treated the 1.5 line as a lean AFR. That is wrong. If AutoTune is off and the adjustment is flat, the **base AFR target table** is running, not a temperature trim.

### Timing vs engine speed (after Joshua corrected the AI)

Joshua's corrections, in order:

1. ChatGPT read peak timing at **51,200 RPM**. That is a graph-scale misread. (chatgpt error)
2. Joshua: not 51,200 — **5,120 RPM**.
3. Then: timing peaks at **5,632 RPM**.
4. Then: peak is **32 degrees**.
5. Later graph: peak still **5,632 RPM / ~32°**, with a gradual rise from idle and a decline after the peak.

**Session timing picture (rider-corrected):** conservative ~10–15° at low RPM, rising through midrange, **peak ~32° at 5,632 RPM**, then taper. (rider-reported + screenshot)

ChatGPT wanted **more advance 1,500–2,500 RPM** for the launch bog, and optionally +2–4° after 5,632 for top end. House guardrail: spark **>±2° per step** is hard-blocked. Do not jump 4° in one flash.

### AFR vs TPS (~3,584 then ~3,840 RPM)

Joshua asked what an ideal AFR vs TPS graph at **3,584 RPM** looks like, then showed his live curve at **3,840 RPM**.

Session curve (chatgpt read of his graph):

- 0–20% TPS: starts near **15.0** (lean for a 131 performance build)
- 20–60% TPS: falling **14.0 → 13.0**
- 60–100% TPS: settling **~12.5–12.8**

ChatGPT's generic "ideal" (not TMax-specific): light throttle ~14.7, richer toward ~12.7 at WOT, smooth transition. (chatgpt-unverified)

## What to actually do with that launch bog

Priority order that matches this session **and** house rules:

1. **Do not flash injector-mismatched maps.** Current injectors are 6.3. (shop-fact)
2. **Confirm AutoTune state.** If enabling AutoTune, gates are **>200°F enable / >280°F disable**. Cold-start and heat-soak trims are garbage. Do not lock CH-home 330–345°F trims. (shop-fact)
3. **Launch cell first, not WOT.** Work idle → 2,500 RPM, 0–20% TPS (throttle-crack / accel fuel, VE, AFR target). Target richer than 15.0 at small TPS if the bike falls on its nose. A working band for this complaint is about **13.0–13.5 AFR** off-idle, **~12.5–13.0** at high load. (mixed: session + typical M8 practice)
4. **Idle.** If idle is under ~1,000 RPM, raise toward **~1,024–1,100**. Flash-first file already wants idle **1024**. (shop-fact + session)
5. **Timing.** Small advance in 1,500–2,500 RPM only if fuel is already reasonable. **±2° per step max.** Watch knock. Do not touch rear-timing-advance tricks the guardrails block.
6. **Mechanical before more map:** TPS calibration, throttle-body sync, fuel pressure on the S&S rail, dirty filter / vacuum leak. (chatgpt-unverified, but standard)
7. **Log:** TPS, RPM, AFR target vs actual, CHT, trims, on a few stoplight launches. Validation ride + tune report before trusting a change.

## ChatGPT advice to keep vs discard

Keep as *ideas*, not house rules:

- Weak bottom end + nose-dive off a stop → fuel/timing at throttle crack, not top end.
- Shorty / low-backpressure exhaust can lean the bottom; add fuel at low RPM rather than chasing peak HP.
- 68 lb injectors (if that was ever true) need idle/low-TPS pulse-width attention so they do not go lean off idle.
- Fan at 210°F is a normal performance-bike number; alarm at 420°F is high.
- AFR vs TPS should get richer as TPS opens; a 15.0 cruise/idle cell on a 131 is a launch-bog suspect.

Discard or override:

- Treating **AFR adjustment 1.5** as AFR 1.5:1. (chatgpt error)
- Peak timing at 51,200 RPM. (chatgpt error; Joshua corrected to 5,120 then 5,632 / 32°)
- "Just enable AutoTune" with no 200–280°F gates. (conflicts with shop-fact)
- Advancing timing 2–4° at the top in one step. (conflicts with ±2°/step guardrail)
- Using this session's **68 injectors / SE cam / Two Brothers shorty** as current hardware. Live stick is **6.3 / S&S 550 / 2-into-1**.
- Any `.tbw` write by the assistant. Flash only in TMax Tuner on Windows.

## Assistant prompt (session-derived, grounded)

Use this when Joshua asks the tower assistant to continue this work:

You are the Thundermax tuning assistant for Joshua's Low Rider ST, Milwaukee-Eight 131, ThunderMax TBW. Current hardware (shop-fact, 2026-08-19): 6.3 injectors, S&S 550 / CAM2, 2-into-1, base-map HXSSEDCAAN061617, flash-first `M8_131_550CAM_63inj.tbw`. Never write `.tbw`. Never flash 5.5 / `*55inj*` / 17AUG v6 onto 6.3s. AutoTune only with enable >200°F and disable >280°F.

A 2026-03-30 ChatGPT session captured a launch bog: from a stop the bike dives / falls on its nose, then picks up. Bottom end felt weak. Screenshot timing after correction peaked at **32° / 5,632 RPM**. AFR vs TPS around 3840 RPM started near **15.0 at low TPS** and fell toward **12.5–12.8 at WOT**. AutoTune appeared off; an AFR-vs-temp graph showed a flat **1.5 adjustment** (that is an offset, not AFR 1.5:1).

Tune the **throttle-crack / 0–20% TPS / idle–2500 RPM** region first. Log TPS, RPM, AFR target vs actual, CHT, trims. Spark steps ±2° max. VE steps ±5% max. No rear-timing-advance. Confirm injector size 6.3 before any WRITE.

## Related

- [[thundermax_command_center_2026-08-19]]
- [[thundermax_chatgpt_share_2026-03-30_transcript]]
- [[thundermax_usb_tmax_flash_rules_6-3_vs_55inj_2026-08-19]]
- [[thundermax_ch_home_autotune_330-345f_above_280f_gate_2026-08-19]]
- [[icloud_thundermax_ai_project_2025-08-30_autotune_gating_tunecard_pdf_465fb7]]
- [[icloud_thunder_max_tuning_thundermax_rear_timing_logic_pdf_b96c1c]]
- [[icloud_2023_lowrider_st_upgrades_thundermax_131ci_tune_specsheet_pdf_ef871f]]
- [[icloud_2023_lowrider_st_manuals_thundermax_tuning_log_pdf_e6fc6f]]
- [[HANDOFF_FROM_GROK]] is on the tower repo, not this vault — same ChatGPT/Grok lineage.

## Next steps

- [ ] Confirm whether "68 injectors" was a 2026-03 misspeak vs a real 68 lb/hr rail that was later swapped to 6.3
- [ ] Confirm whether PBR / Two Brothers shorty was replaced by the current 2-into-1
- [ ] Re-read live AFR vs TPS at ~3840 RPM / 0–20% TPS on the current 6.3 / 550 map
- [ ] If launch bog is still there: add fuel in idle–2500 / 0–20% TPS, idle 1024, then small timing — validation ride before lock
