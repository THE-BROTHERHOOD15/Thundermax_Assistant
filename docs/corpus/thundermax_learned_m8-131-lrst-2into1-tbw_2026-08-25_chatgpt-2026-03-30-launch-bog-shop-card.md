---
type: learned
setup: m8-131-lrst-2into1-tbw
title: ChatGPT 2026-03-30 launch bog shop card
source: /tmp/tmax-chatgpt-facts.md
date: 2026-08-25
---

ChatGPT share 2026-03-30 (https://chatgpt.com/share/6a8d5426-8c60-83e9-bb39-138482c63f9c) ingested 2026-08-25.

RIDER SYMPTOMS (keep): Weak bottom end. From a red-light stop the Low Rider ST 131 almost dives right / falls on its nose, then picks up — launch bog / hesitation on throttle crack, not a top-end problem.

SESSION SCREENSHOTS (Joshua-corrected): Timing peak 32 degrees at 5632 RPM (ChatGPT first misread 51200 RPM; Joshua said 5120 then 5632). AFR vs TPS around 3840 RPM started near 15.0 at low TPS and fell toward 12.5-12.8 at WOT. AFR vs engine-temp graph was a FLAT 1.5 ADJUSTMENT with AutoTune off (cursor -0.000). That 1.5 is an OFFSET, not AFR 1.5:1.

SESSION HARDWARE CLAIMS (do not override shop facts): Screamin Eagle top-end cam, 131 kit, "68 injectors", fueling tappets, Team Eagle pushrods, SE suspension, PBR + Two Brothers Racing shorty, S&S metal fuel rail, Thundermax. CURRENT SHOP HARDWARE 2026-08-19: 6.3 injectors, S&S 550/CAM2, 2-into-1, base-map HXSSEDCAAN061617, flash-first M8_131_550CAM_63inj.tbw.

WHAT TO TUNE FIRST: idle to 2500 RPM, 0-20% TPS (throttle crack). Target about 13.0-13.5 AFR off-idle, 12.5-13.0 high load. Idle 1024-1100. Spark +/-2 deg per step max. VE +/-5% max. AutoTune only with enable >200F disable >280F. Never write .tbw. Never flash 55inj / 17AUG v6 onto 6.3s.
