---
type: learned
setup: m8-131-lrst-2into1-tbw
title: USB TMAX flash rules 6.3 vs 55inj
source: /tmp/tmax-learn-flash-rules.md
date: 2026-08-19
---

USB TMAX stick READ_ME_FIRST (copied 2026-08-19). FLASH THESE ON WINDOWS TMax Tuner.

1) M8_131_550CAM_63inj.tbw
   131 / S&S 550 / 6.3 injectors. THIS is the file to flash first.
   File -> Open -> WRITE Module Maps and Settings.
   Confirm injector size says 6.3. Idle 1024. Decel cut OFF. AutoTune OFF.
   Leave this map's timing / IAC / cranking / accel.

2) 17AUG2025ATLRUNNINGGREATv6TODAY.tbw
   Last map labeled RUNNING GREAT (Aug 17 2025). That was the SE8-517 cam.
   v6 = version 6, NOT 6.3 injectors.
   Open it and READ Injector Size before you write it.
   If it says 5.5, do NOT flash it onto 6.3s.

Do not flash any *55inj* file onto 6.3 injectors.

After WRITE: initialize if asked, hands off throttle.
One start, no throttle. Both pipes hot in 30 seconds or shut off.
