#!/usr/bin/env python3
"""ThunderMax TBW table map — name the byte regions of a .tbw tune.

Loads the frozen band map (`tables.json`, next to this file) and labels the
changed regions of a tune diff with a semantic table name + confidence, so the
assistant can say "your edit touched the AFR pages (high confidence) and the
timing map (medium)" instead of "column 4 dropped by 12".

The map was derived by differential analysis of semantically-labeled tune
pairs (see LABELED_PAIRS). `derive` recomputes the per-band evidence from a
local folder of those tunes so the JSON stays reproducible rather than magic.

stdlib only; no hardcoded absolute paths -> runs identically on the Kali master
node and the Raspberry Pi (ARM). Reads .tbw files, never writes them.
"""
import argparse
import json
import struct
import sys
from pathlib import Path

MAP_PATH = Path(__file__).resolve().parent / "tables.json"

# Semantically-labeled tune pairs used to derive the band map. Filenames are
# relative to whatever tune folder you point `derive` at (the shop NAS
# THROTTLE LOGIC folder, or a local mirror). Category = the *intended* edit.
LABELED_PAIRS = [
    ("final131autotunedtune.tbw",
     "final131autotunedtuneretardedtiming-1degreeglobaly.tbw", "TIMING"),
    # REMOVED 2026-08-26: ("dynopull.tbw", "dynopullreartiming.tbw",
    # "TIMING_REAR"). The filename claims a rear-cylinder timing edit, but the
    # pair moves ZERO cells in every named table band -- all 2528 changed bytes
    # sit in learned_ve_bulk (1828), shared_churn (305), autotune_learned (224)
    # and metadata. It is an AutoTune-run difference wearing a timing label.
    # Feeding it to `derive` attributed AutoTune churn to a TIMING_REAR column
    # that was 0 for every real band, so the map has no rear-cylinder evidence
    # at all -- despite guardrails modelling front/rear separately. Verify with
    #   python3 src/axis_infer.py verify-labels <tune_dir>
    # Do not re-add it without a pair that actually moves a timing band.
    ("FINGERSCROSSED.tbw",
     "FINGERSCROSSEDRAISEDTIMING2DEGREESEACHPOINTPLUSELEVATEDFUELJUGG2ALOTINMIDDLE.tbw",
     "TIMING_FUEL"),
    ("FINGERSCROSSEDRAISEDTIMING2DEGREESEACHPOINTPLUSELEVATEDFUELJUGG2ALOTINMIDDLE.tbw",
     "FINGERSCROSSEDRAISEDTIMING2DEGREESEACHPOINTPLUSELEVATEDFUELJUGG2ALOTINMIDDLE+RICHADJUSTMENTS.tbw",
     "FUEL_RICH"),
    ("advancedtiming.tbw", "advancedtimingplusAFR CORRECTION.tbw", "AFR"),
    ("automaprun.tbw", "automaprun2.tbw", "AUTOTUNE"),
    ("automaprun2.tbw", "automaprun3.tbw", "AUTOTUNE"),
]

EXPECTED_SIZE = 214470
METADATA_BOUNDARY = 0x3C0


def load_map(path=MAP_PATH):
    with open(path) as f:
        m = json.load(f)
    for b in m["bands"]:
        b["_lo"] = int(b["range"][0], 16)
        b["_hi"] = int(b["range"][1], 16)
    return m


def band_for_offset(offset, tables):
    """Most specific band containing offset (narrowest range wins)."""
    hits = [b for b in tables["bands"] if b["_lo"] <= offset < b["_hi"]]
    if not hits:
        return None
    return min(hits, key=lambda b: b["_hi"] - b["_lo"])


def _read(path):
    data = Path(path).read_bytes()
    if len(data) != EXPECTED_SIZE:
        print(f"warning: {Path(path).name} is {len(data)} bytes "
              f"(expected {EXPECTED_SIZE})", file=sys.stderr)
    return data


def diff_regions(a, b, gap=48, floor=0):
    if len(a) != len(b):
        raise ValueError("files are different sizes; cannot compare")
    idxs = [i for i in range(len(a)) if a[i] != b[i] and i >= floor]
    if not idxs:
        return []
    out, start, last = [], idxs[0], idxs[0]
    for i in idxs[1:]:
        if i - last > gap:
            out.append((start, last + 1))
            start = i
        last = i
    out.append((start, last + 1))
    return out


def classify_diff(a_path, b_path, tables=None):
    """Return labeled changed regions between two tunes.

    Each item: dict(offset, length, changed_bytes, band, category, confidence).
    """
    tables = tables or load_map()
    a, b = _read(a_path), _read(b_path)
    rows = []
    for s, e in diff_regions(a, b):
        changed = sum(1 for i in range(s, e) if a[i] != b[i])
        band = band_for_offset(s, tables)
        rows.append({
            "offset": s, "end": e, "length": e - s, "changed_bytes": changed,
            "band": band["name"] if band else None,
            "category": band["category"] if band else "UNMAPPED",
            "confidence": band["confidence"] if band else "-",
            "desc": band["desc"] if band else "not in any known band",
        })
    return rows


def summarize(rows):
    """Roll labeled regions up to a per-category, confidence-weighted view."""
    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"regions": 0, "bytes": 0,
                                              "confidences": set()})
        c["regions"] += 1
        c["bytes"] += r["changed_bytes"]
        c["confidences"].add(r["confidence"])
    return by_cat


# ---- derivation (reproducibility) ----------------------------------------

def derive(tune_dir):
    """Recompute per-band category evidence from a local tune folder.

    Prints, for each band in tables.json, how many changed cells each labeled
    pair contributes inside that band -> lets you audit that the frozen map
    still matches the data. Returns nonzero if any listed tune is missing.
    """
    tune_dir = Path(tune_dir)
    tables = load_map()
    missing = [f for pair in LABELED_PAIRS for f in pair[:2]
               if not (tune_dir / f).exists()]
    if missing:
        print("missing tunes (cannot derive):", file=sys.stderr)
        for f in sorted(set(missing)):
            print(f"  {f}", file=sys.stderr)
        return 1

    # per-category changed-offset sets
    cat_sets = {}
    for A, B, cat in LABELED_PAIRS:
        a = (tune_dir / A).read_bytes()
        b = (tune_dir / B).read_bytes()
        s = {i for i in range(len(a)) if a[i] != b[i] and i >= METADATA_BOUNDARY}
        cat_sets.setdefault(cat, set()).update(s)

    cats = list(cat_sets)
    print(f"band evidence (changed cells per category) from {tune_dir}\n")
    hdr = f"{'band':22} {'conf':6} " + " ".join(f"{c[:8]:>8}" for c in cats)
    print(hdr)
    print("-" * len(hdr))
    for band in tables["bands"]:
        lo, hi = band["_lo"], band["_hi"]
        cells = {c: sum(1 for i in s if lo <= i < hi) for c, s in cat_sets.items()}
        print(f"{band['name']:22} {band['confidence']:6} "
              + " ".join(f"{cells[c]:>8}" for c in cats))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="ThunderMax TBW table map")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bands", help="print the known table bands")

    pc = sub.add_parser("classify", help="label the changed regions of a diff")
    pc.add_argument("file_a")
    pc.add_argument("file_b")

    pd = sub.add_parser("derive", help="recompute band evidence from a tune folder")
    pd.add_argument("tune_dir")

    args = p.parse_args(argv)
    tables = load_map()

    if args.cmd == "bands":
        print(f"# ThunderMax table map  (file size {tables['file_size']}, "
              f"metadata < {tables['metadata_boundary']})")
        sf = tables["timing_scale_finding"]
        print(f"# timing scale: ~{sf['raw_per_degree']} raw/deg "
              f"({sf['confidence']} confidence)\n")
        for b in tables["bands"]:
            print(f"{b['range'][0]}-{b['range'][1]}  [{b['confidence']:6}] "
                  f"{b['category']:9} {b['name']}")
            print(f"    {b['desc']}")
        return 0

    if args.cmd == "classify":
        rows = classify_diff(args.file_a, args.file_b, tables)
        if not rows:
            print("no differences.")
            return 0
        print(f"{'offset':>9} {'len':>5} {'chg':>5}  {'conf':6} {'category':9} band")
        for r in rows:
            print(f"0x{r['offset']:05X} {r['length']:>5} {r['changed_bytes']:>5}  "
                  f"{r['confidence']:6} {r['category']:9} {r['band'] or '-'}")
        print("\nsummary by category:")
        for cat, v in sorted(summarize(rows).items(),
                             key=lambda kv: -kv[1]["bytes"]):
            conf = ",".join(sorted(c for c in v["confidences"] if c != "-"))
            print(f"  {cat:9} {v['bytes']:>6} changed bytes in {v['regions']} "
                  f"regions  [{conf or 'unmapped'}]")
        return 0

    if args.cmd == "derive":
        return derive(args.tune_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
