# ThunderMax Map Decode Report — currenttune.tbw
**Date:** 2026-07-11  
**Author:** Throttle Logic (thundermax-assistant)  
**Bike:** 2023 Harley-Davidson Low Rider ST — 131ci, 2-into-1  
**File:** `currenttune.tbw`  
**Detected Base Map ID (from header):** `HYSSPVCAHN051320`

> This report captures all the pages and parameters we need from your tune. Your `.tbw` is a proprietary binary; we verified integrity and extracted the base-map ID. Numeric table values (AFR, Timing, Fuel) require in-app export/screenshot. Use the capture steps below and populate the values into this report.

---

## 1) Integrity & Metadata
- **Header check:** OK — ASCII signature at 0x0010 → `HYSSPVCAHN051320`
- **File status:** Valid ThunderMax TBW binary (214470 bytes)
- **Module family:** TBW (M8 compatible)
- **Header words:** 0x87, 0x4000, 0x1, 0x147

---

## 2) Capture Checklist (Use TMax Tuner on Windows)
Perform in this exact order so page indices align:
1. **Map Editing → Read Module Maps and Settings** (ensure values shown come from the module/file)
2. **Tuning Maps → AFR Targets vs TPS/RPM** — capture the full grid
3. **Tuning Maps → Front Fuel Flow vs TPS/RPM** — capture grid
4. **Tuning Maps → Rear Fuel Flow vs TPS/RPM** — capture grid
5. **Tuning Maps → Ignition Timing → Timing vs TPS @ RPM** — capture all RPM pages
6. **Tuning Maps → Ignition Timing → Rear Cylinder Timing Offset vs TPS** — capture full TPS axis
7. **Tuning Maps → Ignition Timing → Timing vs Engine Temp** — capture full curve
8. **Tuning Maps → AFR vs Engine Temp** — capture full curve
9. **Module Configuration → Basic Settings** — capture rev limit, speedo/VSS, decel fuel cut, compression releases, idle behavior
10. **Module Configuration → Idle RPM vs Engine Temp** — capture full curve

Tip: If copy/export is unavailable, take clear screenshots at 125–150% zoom so the entire table is readable.

---

## 3) AFR Targets vs TPS/RPM
_Grid capture goes here._

| RPM \ TPS | 0% | 2% | 5% | 10% | 15% | 20% | 25% | 30% | 40% | 50% | 60% | 75% | 100% |
|---:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 768 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 1024 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 1280 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 1536 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 1792 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2048 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2304 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2560 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2816 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 3072 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 3328 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 3584 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 3840 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 4096 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 4352 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 4608 |  |  |  |  |  |  |  |  |  |  |  |  |  |

---

## 4) Front Fuel Flow vs TPS/RPM
_Grid capture goes here._

(Same axis as AFR grid.)

---

## 5) Rear Fuel Flow vs TPS/RPM
_Grid capture goes here._

(Same axis as AFR grid.)

---

## 6) Ignition Timing — Timing vs TPS @ RPM
_Enter degrees for each RPM page. Use the same TPS headers as above._

### 1024 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 1536 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 1792 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 2048 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 2304 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 2560 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 2816 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 3072 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 3328 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 3584 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 3840 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 4096 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 4352 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

### 4608 RPM
| TPS% | 0 | 2 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 | 60 | 75 | 100 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| deg |  |  |  |  |  |  |  |  |  |  |  |  |  |

---

## 7) Rear Cylinder Timing Offset vs TPS
_Full TPS axis capture goes here._

---

## 8) Timing vs Engine Temp
_Full curve capture goes here._

---

## 9) AFR vs Engine Temp
_Full curve capture goes here._

---

## 10) Module Configuration — Basic Settings
- Rev limit: 
- Speedo/VSS: 
- Decel fuel cut: 
- Compression releases: 
- Idle behavior: 

---

## 11) Idle RPM vs Engine Temp
_Full curve capture goes here._
