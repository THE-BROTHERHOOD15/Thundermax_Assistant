#!/usr/bin/env python3
"""ThunderMax BASEMAPDEFSM8.PPM — base-map ID answer key.

This is TMax's M8 catalog, not NetPBM and not a .tbw decoder. It maps the
16-character ID inside a .tbw to ItemId, mint date, family, and HISTORY.
Hardware later changed in the shop (6.3 injectors, S&S 550) is NOT in the ID.

Read-only. Never writes .tbw.
"""
from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PPM = ROOT / "data" / "basemapdefs" / "BASEMAPDEFSM8.PPM"

# First two ID chars → engine-family index in this M8 PPM (measured 32 records).
FAMILY_ENGINE = {
    "HK": 19,
    "HT": 21,
    "HX": 23,  # house live base HXSSEDCAAN061617
    "SM": 25,
    "H1": 28,
    "H2": 29,
    "H3": 30,
}

HOUSE_LIVE = "HXSSEDCAAN061617"


def decode_map_id(map_id: str) -> dict:
    """Split a 16-char TMax ID. Last 6 are MMDDYY when that ID was minted."""
    s = (map_id or "").strip().upper()
    out = {
        "id": s,
        "standard": False,
        "family": None,
        "family_engine_index": None,
        "combo": None,
        "minted": None,
        "ss_m8_catalog": False,
    }
    if len(s) != 16:
        return out
    combo, suffix = s[:10], s[10:]
    out["combo"] = combo
    out["family"] = s[:2]
    out["family_engine_index"] = FAMILY_ENGINE.get(s[:2])
    out["ss_m8_catalog"] = s[2:4] == "SS" and s[:2] in FAMILY_ENGINE
    if suffix.isdigit():
        mm, dd, yy = int(suffix[:2]), int(suffix[2:4]), int(suffix[4:6])
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            out["standard"] = True
            out["minted"] = f"20{yy:02d}-{mm:02d}-{dd:02d}"
    return out


def _hist_ids(notes: str) -> tuple[list[str], list[str]]:
    m = re.search(r"<HISTORY>(.*?)</HISTORY>", notes, re.S)
    if not m:
        return [], []
    lines, ids = [], []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line)
        ids.append(line.split("-", 1)[0].strip())
    return lines, ids


def load(path: Path | None = None) -> dict:
    """Parse BASEMAPDEFSM8.PPM. Returns {count, records, by_id, by_combo}."""
    path = Path(path) if path else DEFAULT_PPM
    data = path.read_bytes()
    if len(data) < 8 or data[0] != 0x20:
        raise ValueError(f"{path.name}: not an M8 PPM catalog (expected u32 32)")
    nrec = struct.unpack_from("<I", data, 0)[0]
    hits = []
    i = 4
    for _ in range(nrec):
        j = data.find(b"\xff", i)
        if j < 0 or j + 3 >= len(data):
            break
        ln = struct.unpack_from("<H", data, j + 1)[0]
        if data[j + 3 : j + 8] != b"Notes":
            i = j + 1
            continue
        hits.append((j, ln))
        i = j + 3 + ln
    records = []
    by_id: dict[str, int] = {}
    by_combo: dict[str, list[int]] = {}
    for rec_i, (ff, ln) in enumerate(hits):
        hdr = ff - 61
        if hdr < 0:
            continue
        fields = list(struct.unpack_from("<15I", data, hdr))
        notes = data[ff + 3 : ff + 3 + ln].decode("latin1", "replace")
        hist, ids = _hist_ids(notes)
        current = ids[-1] if ids else ""
        rec = {
            "index": rec_i,
            "item_id": fields[0],
            "engine_index": fields[2],
            "date": f"{fields[14] >> 8:04d}-{fields[12] >> 8:02d}-{fields[13] >> 8:02d}",
            "current": current,
            "history": hist,
            "history_ids": ids,
            "decoded": decode_map_id(current) if current else decode_map_id(""),
        }
        records.append(rec)
        for mid in ids:
            by_id.setdefault(mid, rec_i)
        if current:
            by_id.setdefault(current, rec_i)
            combo = current[:10]
            by_combo.setdefault(combo, []).append(rec_i)
    return {
        "path": str(path),
        "count": nrec,
        "records": records,
        "by_id": by_id,
        "by_combo": by_combo,
    }


_CACHE = None


def catalog(path: Path | None = None) -> dict:
    global _CACHE
    if _CACHE is None or (path and str(path) != _CACHE.get("path")):
        _CACHE = load(path)
    return _CACHE


def lookup(map_id: str, cat: dict | None = None) -> dict:
    """Exact ID, else same 10-char combo, else family-only / none."""
    cat = cat or catalog()
    decoded = decode_map_id(map_id)
    s = decoded["id"]
    rec = None
    match = "none"
    if s in cat["by_id"]:
        rec = cat["records"][cat["by_id"][s]]
        match = "exact"
    elif decoded["combo"] and decoded["combo"] in cat["by_combo"]:
        rec = cat["records"][cat["by_combo"][decoded["combo"]][-1]]
        match = "prefix"
    return {
        "match": match,
        "decoded": decoded,
        "record": rec,
        "in_m8_catalog_family": decoded["family"] in FAMILY_ENGINE,
    }


def format_lookup(result: dict) -> list[str]:
    """Lines for `tmax info`. ID names the TMax base, not later shop hardware."""
    d = result["decoded"]
    rec = result["record"]
    lines = []
    if d["family"]:
        eng = d["family_engine_index"]
        fam = f"{d['family']} (M8 catalog engine-family index {eng})" if eng else d["family"]
        lines.append(f"id family:   {fam}")
    if d["combo"]:
        lines.append(f"id combo:    {d['combo']}")
    if d["minted"]:
        lines.append(f"id minted:   {d['minted']} (MMDDYY suffix — when this ID was issued)")
    if result["match"] == "exact" and rec:
        lines.append(f"catalog:     exact  ItemId {rec['item_id']}  PPM date {rec['date']}")
        if rec["history"]:
            lines.append("lineage:     " + " → ".join(rec["history"]))
    elif result["match"] == "prefix" and rec:
        lines.append(
            f"catalog:     prefix {d['combo']} → {rec['current']}  "
            f"ItemId {rec['item_id']} (this file's suffix is not in the PPM)"
        )
        if rec["history"]:
            lines.append("lineage:     " + " → ".join(rec["history"]))
    elif d["family"] in FAMILY_ENGINE:
        lines.append("catalog:     family known, exact ID not in BASEMAPDEFSM8.PPM")
    else:
        lines.append(
            "catalog:     not in M8 PPM (TwinCam/other libraries use BASEMAPDEFS5.PPM / SLK)"
        )
    lines.append("catalog note: the 16-char ID is the TMax base this file started from")
    if d["id"] == HOUSE_LIVE or d["family"] == "HX":
        lines.append(
            "catalog note: shop 6.3 / S&S 550 changes are not encoded in this ID"
        )
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Look up a TMax 16-char ID in BASEMAPDEFSM8.PPM")
    p.add_argument("map_id", nargs="?", help="16-char base-map ID (or a .tbw path)")
    p.add_argument("--list", action="store_true", help="print all 32 catalog currents")
    p.add_argument("--ppm", type=Path, default=None)
    args = p.parse_args(argv)
    cat = catalog(args.ppm)
    if args.list or not args.map_id:
        print(f"{cat['count']} M8 catalog records from {cat['path']}")
        for r in cat["records"]:
            cur = r["current"] or "(empty HISTORY)"
            print(f"  {r['item_id']:4d}  {cur:16}  {r['date']}  eng={r['engine_index']}")
        return 0
    mid = args.map_id
    path = Path(mid)
    if path.suffix.lower() == ".tbw" and path.is_file():
        import thundermax_parser as tmx
        mid = tmx.TbwFile(path).base_map_id
        print(f"from {path.name}")
    result = lookup(mid, cat)
    print(f"id:         {result['decoded']['id']}")
    for line in format_lookup(result):
        print(line)
    return 0 if result["match"] != "none" or result["in_m8_catalog_family"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
