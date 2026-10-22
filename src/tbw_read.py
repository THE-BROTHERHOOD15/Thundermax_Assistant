#!/usr/bin/env python3
"""Read a ThunderMax .tbw into engineering units. Never writes the file.

Joshua's 8-bit landmark 0b00100000 (32 decimal, 0x20) plus the labeled
AFR-correction pair (exactly −50 raw on 50 cells) is what makes AFR
readable. Timing-limit degrees still come from the −1° pair (−49 raw).

0x20 / 0x26 / 0x29 are TMax module-command FIDs from
TMaxI_ModuleCommandStaticDefinitions.dat (layouts hard-coded in Map.cpp):
  0x20 Front Fuel Map  — fuelflow vs TPS @ RPM, SID 0-63 (TMax VE)
  0x26 Ign Timing Map  — timing vs TPS @ RPM, SID 0-63
  0x29 AFR Target Map  — AFR vs TPS @ RPM, SID 0-63
A leading 0x20 on a trailer is also the directory marker (same byte as the
fuel FID). Axis is NOT locked. Edits in TMax only, never hex-edit.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardrails  # noqa: E402
import table_map  # noqa: E402
import thundermax_parser as tmx  # noqa: E402

LANDMARK = 0b00100000  # 32, 0x20  — also FID Front Fuel Map / directory marker
AFR_SCALE = 50  # raw per AFR point. High confidence: AFR-correction pair.
TIMING_LIMIT_SCALE = 49  # raw per degree. Medium: timing-limit array only.
AFR_START = 0x1981  # one 8-byte record before tables.json 0x1989
AFR_HINT_END = 0x1C80
AFR_LO, AFR_HI = 400, 900  # raw window that /50 looks like AFR 8.0–18.0
TIMING_LIMIT_START = 0xC3C9
TIMING_LIMIT_END = 0xC7C3

# Official names: TMaxI_ModuleCommandStaticDefinitions.dat comments.
# 0x20/0x21/0x26/0x29 cell grids are "Currently hard coded in Map.cpp".
COMMANDS = {
    0x20: {
        "name": "Front Fuel Map",
        "aka": "VE",
        "dat": "Fuelflow vs TPS @ RPM, SID 0-63",
        "unit": "raw",
        "scale": None,
        "scale_confidence": "unlocked",
    },
    0x26: {
        "name": "Ign Timing Map vs TPS",
        "aka": "timing",
        "dat": "Timing vs TPS @ RPM, SID 0-63",
        "unit": "raw",
        "scale": None,
        "scale_confidence": "unlocked-on-map",
        "scale_note": "49 raw/deg is the timing-limit array only (medium), not this map",
    },
    0x29: {
        "name": "AFR Target Map",
        "aka": "afr_target",
        "dat": "Air/Fuel Ratio vs TPS @ RPM, SID 0-63",
        "unit": "afr",
        "scale": AFR_SCALE,
        "scale_confidence": "high",
    },
}


def i16(data: bytes, off: int) -> int:
    return struct.unpack_from("<h", data, off)[0]


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def command_directories(data: bytes) -> list[dict]:
    """Runs of 16-byte trailers [fid, 0, fid, fid] for FID 0x20–0x42.

    A leading 0x20 with more FIDs after it is the directory marker, not
    'this page is fuel'. Payload = next FID in that case.
    """
    hits: list[tuple[int, int]] = []
    i = 0
    n = len(data)
    while i + 16 <= n:
        a, z, c, d = struct.unpack_from("<4I", data, i)
        if z == 0 and a == c == d and 0x20 <= a <= 0x42:
            hits.append((i, a))
            i += 16
            continue
        i += 1
    if not hits:
        return []
    groups: list[list[tuple[int, int]]] = [[hits[0]]]
    for h in hits[1:]:
        if h[0] == groups[-1][-1][0] + 16:
            groups[-1].append(h)
        else:
            groups.append([h])
    out = []
    for g in groups:
        fids = [x[1] for x in g]
        marker = fids[0] == 0x20 and len(fids) > 1
        payload = fids[1] if marker else fids[0]
        out.append({
            "offset": g[0][0],
            "end": g[-1][0] + 16,
            "fids": fids,
            "payload": payload,
            "marker_20": marker,
        })
    return out


def _i16_stride4(data: bytes, start: int, end: int) -> list[tuple[int, int]]:
    cells = []
    off = start
    end = min(end, len(data) - 1)
    while off + 1 < end:
        cells.append((off, i16(data, off)))
        off += 4
    return cells


def _band_offsets(name: str) -> tuple[int, int] | None:
    try:
        tables = table_map.load_map()
    except Exception:
        return None
    for b in tables.get("bands") or []:
        if b.get("name") == name:
            return int(b["range"][0], 16), int(b["range"][1], 16)
    return None


def command_pages(data: bytes) -> dict:
    """FID 0x20 / 0x26 / 0x29 pages. Never writes.

    0x29 uses the proven AFR scan (start 0x1981, stop at landmark 0x20).
    0x26 uses data immediately before 0x26 trailers (TOC is a trailer).
    0x20 has no sole-payload trailer (0x20 is also the directory marker);
    location is the labeled-pair afr_ve_pages band. Scale unlocked.
    """
    dirs = command_directories(data)
    pages: dict[int, dict] = {}

    # --- 0x29 AFR Target (measured) ---
    afr_cells = afr_engineering(afr_raw_cells(data))
    afr_trailers = [d for d in dirs if d["payload"] == 0x29]
    pages[0x29] = {
        **COMMANDS[0x29],
        "fid": 0x29,
        "source": "afr_scan",
        "start": f"0x{afr_start_for(data):05X}",
        "trailers": [f"0x{d['offset']:05X}" for d in afr_trailers],
        "summary": summarize_afr(afr_cells),
        "n": summarize_afr(afr_cells).get("n", 0),
        "axis": "unconfirmed",
    }

    # --- 0x26 timing: windows before payload-0x26 trailers ---
    timing_windows = []
    prev_end = 0x3C0
    for d in dirs:
        gap_lo, gap_hi = prev_end, d["offset"]
        if d["payload"] == 0x26 and gap_hi > gap_lo:
            raw = _i16_stride4(data, gap_lo, gap_hi)
            nonzero = [(o, v) for o, v in raw if v != 0]
            timing_windows.append({
                "start": f"0x{gap_lo:05X}",
                "end": f"0x{gap_hi:05X}",
                "trailer": f"0x{d['offset']:05X}",
                "n": len(nonzero),
                "raw_min": min((v for _, v in nonzero), default=None),
                "raw_max": max((v for _, v in nonzero), default=None),
            })
        prev_end = d["end"]
    band_26 = _band_offsets("timing_map_main")
    pages[0x26] = {
        **COMMANDS[0x26],
        "fid": 0x26,
        "source": "trailer-windows" if timing_windows else "timing_map_main-band",
        "trailers": [f"0x{d['offset']:05X}" for d in dirs if d["payload"] == 0x26],
        "windows": timing_windows,
        "band": (
            {"name": "timing_map_main",
             "range": [f"0x{band_26[0]:05X}", f"0x{band_26[1]:05X}"]}
            if band_26 else None
        ),
        "n": sum(w["n"] for w in timing_windows),
        "axis": "unconfirmed",
    }

    # --- 0x20 Front Fuel / VE: labeled-pair band (no sole 0x20 trailer) ---
    band_20 = _band_offsets("afr_ve_pages")
    fuel_cells = []
    if band_20 and band_20[1] <= len(data):
        lo, hi = band_20
        raw = _i16_stride4(data, lo, hi)
        fuel_cells = [(o, v) for o, v in raw if v != 0]
    fuel_trailers = [d for d in dirs if 0x20 in d["fids"]]
    pages[0x20] = {
        **COMMANDS[0x20],
        "fid": 0x20,
        "source": "afr_ve_pages-band",
        "note": (
            "0x20 is also the directory marker on trailers; fuel cells are the "
            "labeled-pair afr_ve_pages band, not a 0x20-payload trailer"
        ),
        "trailers": [f"0x{d['offset']:05X}" for d in fuel_trailers],
        "band": (
            {"name": "afr_ve_pages",
             "range": [f"0x{band_20[0]:05X}", f"0x{band_20[1]:05X}"]}
            if band_20 else None
        ),
        "n": len(fuel_cells),
        "raw_min": min((v for _, v in fuel_cells), default=None),
        "raw_max": max((v for _, v in fuel_cells), default=None),
        "axis": "unconfirmed",
        "scale_confidence": "unlocked",
    }
    return pages


def landmarks(data: bytes) -> list[int]:
    """Offsets of 20 00 00 00 (u32 == 32)."""
    out = []
    for i in range(0, len(data) - 3):
        if data[i] == LANDMARK and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 0:
            out.append(i)
    return out


def afr_start_for(data: bytes) -> int:
    """0x84 family is phase-1 at 0x1981. 0x8B FuelMoto family is phase-2 at 0x19C6."""
    candidates = (AFR_START, 0x1975, 0x19C2, 0x19C6, 0x1982)
    best, best_n = AFR_START, -1
    for start in candidates:
        if start + 1 >= len(data):
            continue
        n = 0
        off = start
        end = min(AFR_HINT_END, len(data) - 1)
        while off + 1 < end:
            if off + 3 < len(data) and u32(data, off) == LANDMARK:
                break
            v = i16(data, off)
            if 600 <= abs(v) <= 730:
                n += 1
            off += 4
        if n > best_n:
            best, best_n = start, n
    return best


def afr_raw_cells(data: bytes) -> list[tuple[int, int]]:
    """(offset, signed i16) from family AFR start until landmark-32 or AFR_HINT_END."""
    cells = []
    off = afr_start_for(data)
    end = min(AFR_HINT_END, len(data) - 1)
    while off + 1 < end:
        if off + 3 < len(data) and u32(data, off) == LANDMARK:
            break
        cells.append((off, i16(data, off)))
        off += 4
    return cells


def afr_engineering(cells: list[tuple[int, int]]) -> list[dict]:
    out = []
    for off, raw in cells:
        if AFR_LO <= abs(raw) <= AFR_HI:
            out.append({
                "offset": off,
                "raw": raw,
                "afr": round(raw / AFR_SCALE, 4),
                "kind": "afr",
            })
        elif raw == 0:
            out.append({"offset": off, "raw": raw, "afr": None, "kind": "zero"})
        else:
            out.append({"offset": off, "raw": raw, "afr": None, "kind": "other"})
    return out


def timing_limit_deg(data: bytes) -> list[dict]:
    rows = []
    off = TIMING_LIMIT_START
    while off + 1 < min(TIMING_LIMIT_END, len(data)):
        raw = struct.unpack_from("<H", data, off)[0]
        rows.append({
            "offset": off,
            "raw": raw,
            "deg": round(raw / TIMING_LIMIT_SCALE, 4) if raw else 0.0,
        })
        off += 8
    return rows


def summarize_afr(eng: list[dict]) -> dict:
    vals = [c["afr"] for c in eng if c["kind"] == "afr"]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "mean": round(sum(vals) / len(vals), 2),
        "n_wot_window": sum(1 for v in vals if guardrails.AFR_WOT[0] <= v <= guardrails.AFR_WOT[1]),
        "n_below_wot_hard": sum(1 for v in vals if v < guardrails.AFR_WOT_HARD[0]),
        "n_above_wot_hard": sum(1 for v in vals if v > guardrails.AFR_WOT_HARD[1]),
        "n_cruise_window": sum(1 for v in vals if guardrails.AFR_CRUISE[0] <= v <= guardrails.AFR_CRUISE[1]),
        "scale_raw_per_afr": AFR_SCALE,
        "scale_confidence": "high",
        "axis": "unconfirmed — do not name RPM/TPS from this list",
    }


def read_tune(path: str) -> dict:
    t = tmx.TbwFile(path)
    marks = landmarks(t.data)
    eng = afr_engineering(afr_raw_cells(t.data))
    cmds = {f"0x{fid:02x}": p for fid, p in command_pages(t.data).items()}
    return {
        "file": t.path.name,
        "path": str(t.path),
        "size": len(t.data),
        "base_map_id": t.base_map_id,
        "header": [f"0x{w:X}" for w in t.header],
        "valid": t.valid,
        "landmark": {"bit": "0b00100000", "decimal": LANDMARK, "hex": "0x20",
                     "also": "FID 0x20 Front Fuel Map / directory marker",
                     "u32_count": len(marks),
                     "offsets": [f"0x{i:05X}" for i in marks[:24]]},
        "commands": cmds,
        "afr": {
            "fid": 0x29,
            "name": COMMANDS[0x29]["name"],
            "start": f"0x{afr_start_for(t.data):05X}",
            "cells": eng,
            "summary": summarize_afr(eng),
        },
        "timing_limit": {
            "start": f"0x{TIMING_LIMIT_START:05X}",
            "scale_raw_per_deg": TIMING_LIMIT_SCALE,
            "scale_confidence": "medium",
            "n": len(timing_limit_deg(t.data)),
            "sample_deg": [r["deg"] for r in timing_limit_deg(t.data)[:8]],
        },
        "never_write_tbw": True,
    }


def public_payload(decoded: dict, full: bool = False) -> dict:
    """JSON-safe payload for the Command Center / API. Never includes write paths."""
    s = decoded.get("afr", {}).get("summary") or {}
    values = [c["afr"] for c in decoded.get("afr", {}).get("cells", []) if c.get("kind") == "afr"]
    out = {
        "ok": bool(decoded.get("valid")),
        "file": decoded.get("file"),
        "base_map_id": decoded.get("base_map_id"),
        "size": decoded.get("size"),
        "header": decoded.get("header"),
        "landmark": {
            "bit": decoded.get("landmark", {}).get("bit"),
            "decimal": decoded.get("landmark", {}).get("decimal"),
            "u32_count": decoded.get("landmark", {}).get("u32_count"),
        },
        "afr": {
            "scale_raw_per_afr": AFR_SCALE,
            "scale_confidence": "high",
            "axis": "unconfirmed",
            "summary": s,
            "values": values,
        },
        "timing_limit": {
            "scale_raw_per_deg": TIMING_LIMIT_SCALE,
            "scale_confidence": "medium",
            "sample_deg": decoded.get("timing_limit", {}).get("sample_deg"),
        },
        "commands": {
            fid: {
                "name": p.get("name"),
                "aka": p.get("aka"),
                "dat": p.get("dat"),
                "n": p.get("n") or (p.get("summary") or {}).get("n"),
                "scale": p.get("scale"),
                "scale_confidence": p.get("scale_confidence"),
                "source": p.get("source"),
                "trailers": p.get("trailers"),
                "axis": p.get("axis"),
            }
            for fid, p in (decoded.get("commands") or {}).items()
        },
        "never_write_tbw": True,
        "apply_in": "TMax Tuner Save As sibling",
    }
    if full:
        out["afr"]["cells"] = decoded.get("afr", {}).get("cells")
        out["landmark"]["offsets"] = decoded.get("landmark", {}).get("offsets")
    return out


def context_block(decoded: dict) -> str:
    """Short block the copilot/CLI must see so they can quote AFR numbers."""
    pub = public_payload(decoded)
    s = pub["afr"]["summary"]
    vals = pub["afr"]["values"]
    head = " ".join(f"{v:.2f}" for v in vals[:40])
    more = f" … +{len(vals)-40} more" if len(vals) > 40 else ""
    return (
        f"DECODED AFR from {pub['file']} (FID 0x29 AFR Target Map; "
        f"8-bit landmark 0b00100000 = FID 0x20; "
        f"scale {AFR_SCALE} raw/AFR, high; axis UNCONFIRMED — do not name RPM/TPS).\n"
        f"n={s.get('n')} min={s.get('min')} max={s.get('max')} mean={s.get('mean')} "
        f"WOT-window={s.get('n_wot_window')} cruise-window={s.get('n_cruise_window')}\n"
        f"values: {head}{more}\n"
        "Never hex-edit. Apply changes in TMax Tuner after Joshua approves."
    )


def claimed_file_context(question: str = "") -> str:
    """Decode the claimed on-ECM file for copilot/CLI. Empty if not tune-related."""
    ql = (question or "").lower()
    keys = ("afr", "tune", "tbw", "map", "read", "lean", "rich", "fuel",
            "timing", "wot", "idle", "cruise", "decode")
    if question and not any(k in ql for k in keys):
        return ""
    st_path = Path(__file__).resolve().parent.parent / "data" / "ecm_state.json"
    try:
        st = json.loads(st_path.read_text())
        path = (st.get("on_ecm") or {}).get("path")
        if not path or not Path(path).is_file():
            return ""
        return context_block(read_tune(path))
    except Exception:
        return ""


def afr_diff(old: dict, new: dict) -> list[dict]:
    a = [c for c in old["afr"]["cells"] if c["kind"] == "afr"]
    b = [c for c in new["afr"]["cells"] if c["kind"] == "afr"]
    n = min(len(a), len(b))
    out = []
    for i in range(n):
        if a[i]["afr"] != b[i]["afr"]:
            out.append({
                "offset": a[i]["offset"],
                "old": a[i]["afr"],
                "new": b[i]["afr"],
                "delta": round(b[i]["afr"] - a[i]["afr"], 4),
            })
    return out


def propose_from_read(decoded: dict, kind: str = "afr-audit") -> dict:
    """TMax-language proposal. Never writes a .tbw."""
    changes = []
    notes = []
    s = decoded["afr"]["summary"]
    if kind in ("afr-audit", "all") and s.get("n"):
        notes.append(
            f"AFR curve (scale {AFR_SCALE} raw/AFR, high confidence, axis unconfirmed): "
            f"n={s['n']} min={s['min']} max={s['max']} mean={s['mean']}"
        )
        if s["n_below_wot_hard"]:
            notes.append(
                f"{s['n_below_wot_hard']} cells < {guardrails.AFR_WOT_HARD[0]} AFR "
                "(richer than WOT hard floor) — confirm in TMax they are not a WOT column "
                "before leaning them."
            )
        if s["min"] < guardrails.AFR_WOT[0]:
            changes.append({
                "table": "afr_target",
                "cylinder": "both",
                "rpm_band": [768, 4608],
                "tps_band": [75, 100],
                "direction": "increase",
                "magnitude": round(guardrails.AFR_WOT[0] - s["min"], 2),
                "unit": "afr",
                "target_value": guardrails.AFR_WOT[0],
                "current_value": s["min"],
                "claim": (
                    f"lowest decoded AFR {s['min']} is below house WOT {guardrails.AFR_WOT}; "
                    "IF that cell is WOT, raise toward 12.4 in TMax AFR Targets. Axis not locked."
                ),
            })
    if kind in ("decel-pop", "all"):
        changes.append({
            "table": "ve_front",
            "cylinder": "both",
            "rpm_band": list(guardrails.DECEL_POP_HIGH["rpm"]),
            "tps_band": list(guardrails.DECEL_POP_HIGH["tps"]),
            "direction": "increase",
            "magnitude": guardrails.DECEL_POP_HIGH["ve_pct"],
            "unit": "ve_pct",
            "target_value": None,
            "current_value": None,
            "claim": "house decel-pop >4k: +2% VE @ 0–2% TPS, 3840–4608 rpm. Apply in TMax.",
        })
        changes.append({
            "table": "spark_advance_front",
            "cylinder": "both",
            "rpm_band": list(guardrails.DECEL_POP_BROAD["rpm"]),
            "tps_band": list(guardrails.DECEL_POP_BROAD["tps"]),
            "direction": "decrease",
            "magnitude": abs(guardrails.DECEL_POP_BROAD["spark_deg"]),
            "unit": "deg",
            "target_value": None,
            "current_value": None,
            "claim": "house broad pops: −1° spark @ 0–2% TPS, 2048–2816 rpm. Apply in TMax.",
        })
    vet = guardrails.check_proposal(changes)
    return {
        "title": f"{kind} from {decoded['file']}",
        "kind": kind,
        "notes": notes,
        "changes": changes,
        "vet": vet,
        "never_write_tbw": True,
        "apply_in": "TMax Tuner Save As sibling — thundermax-tune-loop",
    }


def _print_read(decoded: dict, out) -> None:
    w = out.write
    a = decoded["afr"]["summary"]
    w(f"file:     {decoded['file']}\n")
    w(f"base map: {decoded['base_map_id']}\n")
    w(f"size:     {decoded['size']}\n")
    w(f"header:   {' '.join(decoded['header'])}\n")
    w(f"landmark: {decoded['landmark']['bit']} = {decoded['landmark']['decimal']} "
      f"({decoded['landmark']['u32_count']} × u32-32)  "
      f"also FID 0x20 Front Fuel Map / directory marker\n")
    cmds = decoded.get("commands") or {}
    w("commands: TMaxI_ModuleCommandStaticDefinitions.dat  "
      "(0x20/0x26/0x29 grids hard-coded in Map.cpp)\n")
    p20 = cmds.get("0x20") or {}
    w(f"  0x20  {p20.get('name')} ({p20.get('aka')})  {p20.get('dat')}\n")
    w(f"        n={p20.get('n')} raw {p20.get('raw_min')}..{p20.get('raw_max')}  "
      f"scale UNLOCKED  src={p20.get('source')}\n")
    if p20.get("band"):
        w(f"        band {p20['band']['name']} {p20['band']['range'][0]}-{p20['band']['range'][1]}\n")
    p26 = cmds.get("0x26") or {}
    w(f"  0x26  {p26.get('name')}  {p26.get('dat')}\n")
    w(f"        n={p26.get('n')}  scale unconfirmed on this map  "
      f"(limit array {TIMING_LIMIT_SCALE} raw/deg, medium)  "
      f"trailers {', '.join(p26.get('trailers') or []) or 'none'}\n")
    p29 = cmds.get("0x29") or {}
    s29 = p29.get("summary") or a
    w(f"  0x29  {p29.get('name')}  {p29.get('dat')}\n")
    w(f"afr:      FID 0x29  scale {AFR_SCALE} raw/AFR (high)  axis UNCONFIRMED\n")
    if s29.get("n"):
        w(f"          n={s29['n']} min={s29['min']} max={s29['max']} mean={s29['mean']}\n")
        w(f"          in WOT {guardrails.AFR_WOT} window: {s29.get('n_wot_window')}   "
          f"cruise {guardrails.AFR_CRUISE}: {s29.get('n_cruise_window')}\n")
    w(f"timing-limit scale {TIMING_LIMIT_SCALE} raw/deg (medium)  "
      f"sample {decoded['timing_limit']['sample_deg']}\n")
    w("never writes .tbw — edits in TMax only\n")
    vals = [c["afr"] for c in decoded["afr"]["cells"] if c["kind"] == "afr"]
    if vals:
        w("\nAFR cells (candidate):\n")
        for i in range(0, len(vals), 8):
            row = " ".join(f"{v:5.2f}" for v in vals[i:i + 8])
            w(f"  {row}\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Read a .tbw: FID 0x20 fuel/VE, 0x26 timing, 0x29 AFR. Never writes."
    )
    p.add_argument("file")
    p.add_argument("--vs", dest="vs", help="second .tbw; print AFR engineering diff")
    p.add_argument("--propose", nargs="?", const="afr-audit",
                   choices=("afr-audit", "decel-pop", "all"),
                   help="TMax-language proposal from the decode (no file write)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    decoded = read_tune(args.file)
    payload = {"read": decoded}
    if args.vs:
        other = read_tune(args.vs)
        payload["vs"] = other["file"]
        payload["afr_diff"] = afr_diff(decoded, other)
    if args.propose:
        payload["proposal"] = propose_from_read(decoded, args.propose)

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    _print_read(decoded, sys.stdout)
    if args.vs:
        diff = payload["afr_diff"]
        print(f"\nAFR diff vs {payload['vs']}: {len(diff)} cells changed")
        for row in diff[:24]:
            print(f"  0x{row['offset']:05X}  {row['old']:.2f} → {row['new']:.2f}  ({row['delta']:+.2f})")
        if len(diff) > 24:
            print(f"  … {len(diff) - 24} more")
    if args.propose:
        prop = payload["proposal"]
        print(f"\nPROPOSAL {prop['kind']}  vet blocks={prop['vet']['blocks']} "
              f"warns={prop['vet']['warns']} passed={prop['vet']['passed']}")
        for n in prop["notes"]:
            print(f"  note: {n}")
        for i, ch in enumerate(prop["changes"]):
            print(f"  [{i}] {ch['table']} {ch['direction']} {ch['magnitude']} {ch['unit']}  "
                  f"rpm {ch['rpm_band']} tps {ch['tps_band']}")
            print(f"      {ch['claim']}")
        for f in prop["vet"]["findings"]:
            print(f"  {f['severity'].upper()} {f['rule']}: {f['message']}")
        print("Apply in TMax Tuner. Save As a sibling. Do not hex-edit.")
    return 0 if decoded["valid"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except tmx.TbwError as e:
        print(f"tmax read: {e}", file=sys.stderr)
        sys.exit(2)
