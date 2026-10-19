#!/usr/bin/env python3
"""Recover table GEOMETRY from labelled tune pairs, without TMax Tuner.

WHY
---
`tables.json` locates each table's byte range, but `BAND_AXES` is empty and a
self-check deliberately FAILS if anyone fills it with a guess. While it is
empty, `rpm_band`/`tps_band` are null for every change, `BAND_TO_DYNO_TABLES`
is empty, the virtual dyno can simulate nothing from a real diff, and
`learned_feedback` can only say "offset 0x159A7" instead of "your midrange at
3000 rpm". Every downstream capability is gated on this one gap.

The documented way to close it is a TMax Tuner session: change one cell, save,
diff. That is still the gold standard and this module does NOT replace it.

But a 1-D array that is really a 2-D table leaks its own geometry when someone
edits a REGION of it. If an edit covers columns c0..c1 of every row, the
changed indices form a run of length L per row, separated by a gap back to the
next row, and

    row_length = gap + L - 1

That arithmetic needs no knowledge of scale, units, or axis order. It is
measured from tunes Joshua already has. What it yields is *geometry* —
candidate row lengths and lane structure — which narrows the TMax experiment
from "discover the layout" to "confirm one number".

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not write `BAND_AXES` and it does not decide which axis is RPM and
which is TPS. Row length alone cannot tell you that, and asserting it would be
exactly the fabricated axis map the existing self-check exists to prevent.
Output is evidence with agreement counts, for a human to confirm.
"""
import argparse
import json
import struct
import sys
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from table_map import load_map  # noqa: E402

EXPECTED_SIZE = 214470

# Pairs whose FILENAME states the intended edit. Kept here rather than reusing
# table_map.LABELED_PAIRS because that list carries a pair proven not to match
# its own label -- see verify_labels().
GEOMETRY_PAIRS = [
    ("region-fuel",
     "FINGERSCROSSED.tbw",
     "FINGERSCROSSEDRAISEDTIMING2DEGREESEACHPOINTPLUSELEVATEDFUELJUGG2ALOTINMIDDLE.tbw"),
    ("region-rich",
     "FINGERSCROSSEDRAISEDTIMING2DEGREESEACHPOINTPLUSELEVATEDFUELJUGG2ALOTINMIDDLE.tbw",
     "FINGERSCROSSEDRAISEDTIMING2DEGREESEACHPOINTPLUSELEVATEDFUELJUGG2ALOTINMIDDLE+RICHADJUSTMENTS.tbw"),
    ("afr-correction", "advancedtiming.tbw", "advancedtimingplusAFR CORRECTION.tbw"),
    ("global-timing", "final131autotunedtune.tbw",
     "final131autotunedtuneretardedtiming-1degreeglobaly.tbw"),
]

TABLE_BANDS = ("afr_target", "afr_ve_pages", "fuel_flow_pages",
               "fuel_rich_correction", "timing_map_main", "timing_limit_array")

# A run boundary only means something if both runs are long enough to be a real
# row segment. Two adjacent single-cell changes produce a "row length" of 2,
# which is noise, not geometry.
MIN_RUN = 8


def band_cells(data, band):
    lo = int(band["range"][0], 16)
    hi = int(band["range"][1], 16)
    stride = int(band.get("stride") or 2)
    width = 4 if stride == 8 else min(stride, 4)
    fmt = {1: "<b", 2: "<h", 4: "<i"}[width]
    return [struct.unpack_from(fmt, data, off)[0]
            for off in range(lo, hi - width + 1, stride)]


def changed_cells(a, b, band):
    ca, cb = band_cells(a, band), band_cells(b, band)
    return [i for i, (x, y) in enumerate(zip(ca, cb)) if x != y], len(ca)


def runs(idxs):
    """[(start, length)] for contiguous runs of changed indices."""
    if not idxs:
        return []
    out, s, prev = [], idxs[0], idxs[0]
    for i in idxs[1:]:
        if i != prev + 1:
            out.append((s, prev - s + 1))
            s = i
        prev = i
    out.append((s, prev - s + 1))
    return out


def row_lengths(idxs, min_run=MIN_RUN):
    """Candidate row lengths from run/gap arithmetic: R = gap + L - 1."""
    rs = runs(idxs)
    out = []
    for (s1, l1), (s2, l2) in zip(rs, rs[1:]):
        if l1 < min_run or l2 < min_run:
            continue
        gap = s2 - (s1 + l1 - 1)
        out.append(gap + l1 - 1)
    return out


def lane_period(idxs, max_period=16):
    """Spacing of an evenly-spaced set of changed cells, or None.

    A table stored as records of several fields shows this: a global edit to
    ONE field lights up every p-th cell. On the global -1 degree pair,
    timing_map_main changes every 4th cell -- so its record holds 4 lanes and
    only one carries the commanded value.

    Uses the GCD of the gaps, not "smallest p with a shared residue". Those
    differ and the shared-residue version silently under-reports: indices
    50, 54, 58 all share a residue mod 2, so it would answer 2 when the real
    record period is 4. Under-reporting the period splits one record into
    imaginary halves, which is exactly the kind of fabricated structure the
    BAND_AXES self-check exists to keep out.
    """
    if len(idxs) < 4:
        return None
    from math import gcd
    g = 0
    for a, b in zip(idxs, idxs[1:]):
        g = gcd(g, b - a)
    return g if 1 < g <= max_period else None


def analyse(folder, tables=None, pairs=GEOMETRY_PAIRS, bands=TABLE_BANDS):
    folder = Path(folder)
    tables = tables or load_map()
    by_name = {b["name"]: b for b in tables["bands"]}
    out = {"pairs": [], "row_length_votes": Counter(), "lane_periods": {}}

    for label, fa, fb in pairs:
        pa, pb = folder / fa, folder / fb
        if not (pa.exists() and pb.exists()):
            out["pairs"].append({"label": label, "error": "missing file"})
            continue
        A, B = pa.read_bytes(), pb.read_bytes()
        rec = {"label": label, "a": fa, "b": fb, "bands": {}}
        for bn in bands:
            band = by_name.get(bn)
            if not band:
                continue
            idxs, n = changed_cells(A, B, band)
            if len(idxs) < 3:
                continue
            Rs = row_lengths(idxs)
            lp = lane_period(idxs)
            rec["bands"][bn] = {
                "cells": n, "changed": len(idxs),
                "runs": len(runs(idxs)),
                "row_length_candidates": Counter(Rs).most_common(4),
                "lane_period": lp,
            }
            for R in Rs:
                out["row_length_votes"][(bn, R)] += 1
            if lp:
                out["lane_periods"].setdefault(bn, Counter())[lp] += 1
        out["pairs"].append(rec)
    return out


def verify_labels(folder, tables=None):
    """Check every table_map.LABELED_PAIRS entry actually shows its own edit.

    A pair whose filename claims a TIMING edit but which moves no cell in any
    timing band is not evidence of anything, and silently feeding it into
    `derive` attributes AutoTune churn to a tuning category.
    """
    import table_map
    folder = Path(folder)
    tables = tables or load_map()
    by_name = {b["name"]: b for b in tables["bands"]}
    cat_bands = {}
    for b in tables["bands"]:
        cat_bands.setdefault(b["category"], []).append(b["name"])

    rows = []
    for fa, fb, cat in table_map.LABELED_PAIRS:
        pa, pb = folder / fa, folder / fb
        if not (pa.exists() and pb.exists()):
            rows.append({"pair": (fa, fb), "category": cat,
                         "verdict": "missing file"})
            continue
        A, B = pa.read_bytes(), pb.read_bytes()
        # which named TABLE bands (not autotune/metadata/churn) actually moved
        moved = {}
        for bn in TABLE_BANDS:
            band = by_name.get(bn)
            if not band:
                continue
            idxs, _ = changed_cells(A, B, band)
            if idxs:
                moved[bn] = len(idxs)
        # AUTOTUNE pairs are a different question and must not be judged
        # against the rider-edited tables: an AutoTune run is SUPPOSED to move
        # learned bands, and TABLE_BANDS deliberately excludes those. Scoring
        # them here would report a "failure" for correct behaviour.
        if cat == "AUTOTUNE":
            at = {}
            for bn in ("autotune_learned", "learned_ve_bulk"):
                band = by_name.get(bn)
                if not band:
                    continue
                idxs, _ = changed_cells(A, B, band)
                if idxs:
                    at[bn] = len(idxs)
            rows.append({
                "pair": (fa, fb), "category": cat,
                "table_bands_moved": moved, "learned_bands_moved": at,
                "verdict": "ok" if at else "no learned-band movement",
                # A pure AutoTune run that also moves rider tables means those
                # bands are CONTAMINATED -- they cannot be attributed cleanly
                # to a deliberate edit.
                "note": (f"also moves rider tables {moved} — those bands are "
                         f"not edit-exclusive" if moved else ""),
            })
            continue

        expect = cat.split("_")[0]
        supports = any(by_name[bn]["category"].startswith(expect)
                       or expect in by_name[bn]["category"]
                       for bn in moved) if moved else False
        rows.append({
            "pair": (fa, fb), "category": cat,
            "table_bands_moved": moved,
            "verdict": ("ok" if moved and supports else
                        "NO TABLE-BAND EVIDENCE" if not moved else
                        "moves tables, but none matching its label"),
            "note": "",
        })
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Infer table geometry from labelled tune pairs")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("geometry", help="row length + lane period per band")
    g.add_argument("folder")
    g.add_argument("--json", action="store_true")
    v = sub.add_parser("verify-labels",
                       help="check LABELED_PAIRS actually show their own edit")
    v.add_argument("folder")
    a = p.parse_args(argv)

    if a.cmd == "geometry":
        res = analyse(a.folder)
        if a.json:
            print(json.dumps({
                "pairs": res["pairs"],
                "row_length_votes": [
                    {"band": k[0], "row_length": k[1], "boundaries": v}
                    for k, v in res["row_length_votes"].most_common()],
                "lane_periods": {k: dict(v) for k, v in res["lane_periods"].items()},
            }, indent=2))
            return 0
        for rec in res["pairs"]:
            if rec.get("error"):
                print(f"\n== {rec['label']}: {rec['error']}")
                continue
            print(f"\n== {rec['label']}")
            for bn, d in rec["bands"].items():
                print(f"   {bn:22} cells={d['cells']:>5} chg={d['changed']:>5} "
                      f"runs={d['runs']:>4} rowlen={d['row_length_candidates']} "
                      f"lane={d['lane_period']}")
        print("\n--- row length, votes across ALL pairs (higher = better) ---")
        agg = Counter()
        for (bn, R), c in res["row_length_votes"].items():
            agg[(bn, R)] += c
        for (bn, R), c in agg.most_common(12):
            print(f"   {bn:22} row_length={R:<6} agreeing boundaries={c}")
        print("\n--- lane period (record holds N fields, edit touched one) ---")
        for bn, c in res["lane_periods"].items():
            print(f"   {bn:22} {dict(c)}")
        print("\nGeometry only. This does NOT assign RPM vs TPS and does NOT "
              "write BAND_AXES.\nConfirm one cell in TMax Tuner to lock "
              "orientation and scale.")
        return 0

    if a.cmd == "verify-labels":
        rows = verify_labels(a.folder)
        bad = 0
        for r in rows:
            mark = "ok " if r["verdict"] == "ok" else "!! "
            if r["verdict"] != "ok":
                bad += 1
            print(f"{mark}{r['category']:<12} {r['pair'][0][:38]:38} -> "
                  f"{r['pair'][1][:38]:38}")
            print(f"     verdict: {r['verdict']}")
            if r.get("learned_bands_moved") is not None:
                print(f"     learned bands moved: "
                      f"{r['learned_bands_moved'] or 'NONE'}")
            elif r.get("table_bands_moved") is not None:
                print(f"     table bands moved: "
                      f"{r['table_bands_moved'] or 'NONE'}")
            if r.get("note"):
                print(f"     NOTE: {r['note']}")
        print(f"\n{bad} of {len(rows)} labelled pairs do not support their "
              f"own label.")
        return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
