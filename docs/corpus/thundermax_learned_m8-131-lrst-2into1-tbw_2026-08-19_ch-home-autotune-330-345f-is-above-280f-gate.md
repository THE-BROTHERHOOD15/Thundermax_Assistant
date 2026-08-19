---
type: learned
setup: m8-131-lrst-2into1-tbw
title: CH-home autotune 330-345F is above 280F gate
source: /tmp/tmax-learn-autotune-heat.md
date: 2026-08-19
---

CH-home AutoTune file (2026-08-19 Desktop + USB):
"auto tune run from ch home 330 345 degrees f head temp.tbw"

Baseline: "M8_131_CAM2_63inj fuelmoto origional.tbw"

Both HXSSEDCAAN061617 / 214967 bytes.

What changed: AutoTune learned trims and learned-VE bulk only. No evidence of a deliberate timing-map or AFR-target edit.

Rider meaning: this is a data-collection pass, not a new spark/fuel strategy. The bike should feel close to the FuelMoto original, with small VE corrections from closed-loop.

What to validate next ride (house protocol):
- Confirm CHT stays in the AutoTune window (learn above 200F, disable above 280F). 330–345F is too hot — lock nothing from that session.
- Log TPS, RPM, AFR target vs actual, CHT, trims.
- Watch for lean surge or pop after the small negative learned-VE deltas.
- If heat-soak ping appears, that is a timing/heat problem, not a reason to trust 330F trims.

Do not flash the 17AUG SE8-517 map onto this 6.3 injector bike.
