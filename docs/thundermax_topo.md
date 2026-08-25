---
title: ThunderMax topo map
created: 2026-08-25
---

# ThunderMax topo (repo copy)

Canonical Obsidian canvas lives in the office vault:

- Mac: `~/work/ai-profit-venture/brain/thundermax_topo.canvas`
- Tower: `/home/joshua/Projects/ai-profit-venture/brain/thundermax_topo.canvas`

This file is the GitHub-readable copy. Open the `.canvas` in Obsidian for the native map.

# ThunderMax topo

Native Obsidian map of ThunderMax intel. Open the canvas (not the graph view) to pan the terrain:

**[[thundermax_topo.canvas]]**

![[thundermax_topo.canvas]]

Green = live shop fact (flash this). Red = conflict or brick-the-bike. Orange = Mar 30 session. Cyan = tower assistant. Yellow = saddlebag brace. Purple = iCloud manuals.

Hubs: [[thundermax_command_center_2026-08-19]] · [[user_bike]] · [[chatgpt_shares]]

## Terrain

```mermaid
flowchart TB
  subgraph PEAK["LIVE PEAK — USB 2026-08-19"]
    CC["Command Center :8181"]
    HW["6.3 inj · S&S 550 · 2-into-1"]
    FF["Flash-first M8_131_550CAM_63inj.tbw"]
    BM["Base-map HXSSEDCAAN061617"]
    CC --- HW --- FF
    HW --- BM
  end

  subgraph CONFLICT["DO NOT FLATTEN"]
    BIKE["user_bike still says 5.5 g/s + SE 475"]
    T68["Mar 30 ChatGPT: 68 inj + shorty"]
    BIKE -.->|"same cal ID"| BM
    T68 -.->|"live stick wins"| HW
  end

  subgraph RULES["BRICK-THE-BIKE"]
    FR["Never flash *55inj* / 17AUG v6 onto 6.3s"]
    AT["AutoTune enable >200°F disable >280°F"]
    GR["Spark ±2° · VE ±5% · no rear advance · never write .tbw"]
    FR --> CC
    AT --> CC
    GR --> CC
  end

  subgraph SESSION["MAR 30 LAUNCH BOG"]
    CUR["Curated session note"]
    TR["Transcript"]
    FEEL["Dives / falls on its nose from a stop"]
    NUM["32° @ 5632 RPM · AFR~15.0 low TPS · 1.5 is OFFSET"]
    CUR --> TR --> FEEL --> NUM
    CUR -->|"tmax learn 2026-08-25"| CC
  end

  subgraph FAB["CHASSIS"]
    BR["Saddlebag brace — photos still on phone"]
    DK["3/8 docking hardware"]
    FEEL -->|"same bike"| BR
    BR --> DK
  end

  subgraph CORPUS["LIBRARY"]
    TC["AutoTune gating TuneCard 200–280"]
    ZL["Zone-lock guides"]
    RT["Rear timing logic"]
    TT["Front/rear timing tables"]
    AT --> TC
    GR --> RT
    NUM --> TT
  end
```

## How to read it

1. Start at **LIVE PEAK**. That is what is on the bike if you flash today.
2. Left ridge is the **injector fight** (5.5 vs 68 vs 6.3). Do not average them. 6.3 wins until Joshua says otherwise.
3. Below the peak is the **Mar 30 ride complaint** — launch bog, 32° at 5,632 RPM, lean small-TPS AFR. That session is in the tower KB.
4. Right ridge is the **assistant**: `:8181`, GitHub, never writes `.tbw`.
5. Far right/below is **fab** (brace), not ECM.
6. Bottom shelf is the **manual pile**. Use it for protocol, not as current hardware.

## Key file nodes on the canvas

- [[thundermax_command_center_2026-08-19]]
- [[user_bike]]
- [[thundermax_usb_tmax_flash_rules_6-3_vs_55inj_2026-08-19]]
- [[thundermax_ch_home_autotune_330-345f_above_280f_gate_2026-08-19]]
- [[thundermax_chatgpt_share_2026-03-30_lowrider-st-131]]
- [[thundermax_chatgpt_share_2026-03-30_transcript]]
- [[chatgpt_share_2026-08-07_saddlebag-brace-project-plan]]
- [[icloud_thundermax_ai_project_2025-08-30_autotune_gating_tunecard_pdf_465fb7]]
- [[icloud_thunder_max_tuning_thundermax_rear_timing_logic_pdf_b96c1c]]
- [[icloud_thunder_max_tuning_m8_131ci_timing_tables_front_and_rear_pdf_9e7b0b]]
