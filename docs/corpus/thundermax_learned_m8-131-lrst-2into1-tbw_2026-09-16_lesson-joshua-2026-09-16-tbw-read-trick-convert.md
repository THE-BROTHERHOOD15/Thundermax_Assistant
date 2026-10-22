---
type: learned
setup: m8-131-lrst-2into1-tbw
title: lesson: Joshua 2026-09-16 tbw read trick. Convert bytes
source: manual
date: 2026-09-16
---

lesson: Joshua 2026-09-16 tbw read trick. Convert bytes to 8-bit binary. Landmark 0b00100000 = 32 decimal = 0x20. On M8_131_550CAM_63inj.tbw that value is LE 20 00 00 00 at 0x01BB5, tail of AFR-target band 0x01989-0x01BCA. 32 is a structure landmark, NOT the AFR scale (u16/32 = 19.2, not AFR). Candidate AFR for remaining u16 615-709 is divide-by-50 -> 12.30-14.18 mean 13.81, not TMax-locked. Timing-limit remains 49 raw/deg from the -1 degree pair. Skill reading-tbw-8bit. Never hex-edit .tbw.
