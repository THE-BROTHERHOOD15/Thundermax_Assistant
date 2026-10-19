#!/usr/bin/env python3
"""Read the bike's OWN AutoTune learning back out of saved .tbw files.

WHY THIS EXISTS
---------------
Every other module in this project reasons about what a tune change *should*
do. `virtual_dyno` models it, `guardrails` bounds it, `vetting` argues about
it. None of them has ever seen a single measurement from the actual engine --
there is no datalog anywhere in the project or on the NAS, so the
`validated_by_ride` state is filled in from memory.

But the ECM has been recording all along. AutoTune writes its learned fuel
correction back into the tune, so every `.tbw` Joshua saved after a ride
carries the bike's own verdict on where the base map is wrong. A sequence of
saves (automaprun -> automaprun2 -> automaprun3) is a longitudinal record of
that verdict changing.

`dyno_bridge` deliberately EXCLUDES these bands from a diff, and it is right to
-- when you are asking "what did this deliberate edit change?", learned data is
churn that pollutes the answer. This module asks the opposite question: throw
away the deliberate edits and look ONLY at the churn, because the churn is the
measurement.

WHAT IT CAN AND CANNOT SAY
--------------------------
The learned bands are `confidence: low` in tables.json and their raw->
engineering scale is unknown. So this module will NOT convert anything to AFR
percent or VE percent, and it does not try to. It reports:

  * DIRECTION  -- which way a cell's correction moved (sign is scale-free)
  * PERSISTENCE-- whether that direction repeats across independent saves
  * RELATIVE   -- magnitude compared to other cells in the SAME band only

The statistical claim is deliberately narrow and does not depend on knowing
the scale. Under a null hypothesis of "the base map is correct here and
AutoTune is just chasing noise", a cell's correction is equally likely to move
up or down at each save, so k same-direction moves out of m has a plain
binomial probability. A cell that moves the same way 6 times running is not
noise (p = 0.031), whatever the units are.

With ~2500 cells per band, testing every cell at p<0.05 would manufacture
~125 "findings" from pure noise. Every p-value here is therefore corrected
with Benjamini-Hochberg FDR, and `bias()` refuses to report a cell that does
not survive the correction. An uncorrected sign test on a band this wide is
worse than no test at all, because it always finds something.

WHAT IT STILL CANNOT DO -- it locates persistent bias by BYTE OFFSET, not by
rpm/TPS, because tables.json has no offset->(rpm, TPS) axis map yet. So the
output is "a run of 40 cells at 0x0E1C4 has been pulling one direction for
five saves", not "your midrange at 3000rpm is lean". Closing that gap is
exactly what the ground-truth experiment in docs/GROUND_TRUTH_EXPERIMENT.md
is for -- and this module is the strongest argument for running it, because it
turns an unlabeled offset into an actionable map cell.

Reads .tbw files. NEVER writes them.

CLI
---
    python3 learned_feedback.py cells   <tune.tbw>
    python3 learned_feedback.py trend   <a.tbw> <b.tbw> [c.tbw ...]
    python3 learned_feedback.py lineage <tune_folder>
    python3 learned_feedback.py report  <tune_folder> --family latestandgreatest
"""
import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

try:
    from table_map import load_map
except ImportError:  # running from repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from table_map import load_map

EXPECTED_SIZE = 214470

# Bands that hold ECM-written learned data rather than rider edits. The first
# two are categorised AUTOTUNE in tables.json; fuel_rich_correction is flagged
# by dyno_bridge.LEARNED_DATA_BANDS as "treat as AutoTune data until proven
# otherwise", so it is available here but NOT read by default.
LEARNED_BANDS = ("autotune_learned", "learned_ve_bulk")
OPTIONAL_LEARNED_BANDS = ("fuel_rich_correction",)

# A cell must actually move this many times before a sign test means anything.
# With m=4 the best achievable two-sided p is 0.125 -- it can never survive
# FDR against thousands of cells, so testing it just burns the error budget.
MIN_MOVES = 5

# A window must move on at least this many SEPARATE saves before its direction
# is called a trend rather than one write. See the gate in region_bias().
MIN_TREND_STEPS = 3
DEFAULT_FDR = 0.05


# ---------------------------------------------------------------------------
# cell reading
# ---------------------------------------------------------------------------

def _band(name, tables=None):
    tables = tables or load_map()
    for b in tables["bands"]:
        if b["name"] == name:
            return b
    raise KeyError(f"no band named {name!r} in tables.json")


def band_grid(band, width=None):
    """(lo, hi, stride, width) for a learned band.

    Anchored on the band's own start, which is the whole point: these bands
    begin on ODD file offsets (autotune_learned starts at 0x0DDC5). Snapping
    to an even file offset reads every record one byte early and inflates
    every delta by 256 -- the exact bug that reached `tmax compare` and the web
    diff before 2026-08-20. Do not "tidy" this into an even-offset read.

    LANE WIDTH IS DELIBERATELY 2, NOT the stride. `_grid_for` in the parser
    derives width from stride (stride 4 -> a 4-byte int), and for these bands
    that is wrong. learned_ve_bulk carries a repeating 4-lane pattern of
    16-bit fields --

        00 00 | 12 00 | 12 00 | 12 00   (= 0, 18, 18, 18, repeating)

    -- so a stride-4 i32 read straddles two independent fields and reports
    1179648 for a pair whose real contents are 0 and 18. Split into lanes, one
    lane holds small values (5..33 across the file) and another holds 0 /
    0xFFFF, which look like data and flag respectively. The true record layout
    of this band is NOT confirmed, so this reads the finest grid the bytes
    clearly support and lets each lane move independently. If the real record
    is 4 or 8 bytes wide, a 2-byte read still tracks every lane correctly --
    it merely also tracks the flag lanes, which show up as lanes that never
    move. That is a visible, harmless cost. Merging two fields into a fake
    int is neither.
    """
    lo = int(band["range"][0], 16)
    hi = int(band["range"][1], 16)
    return lo, hi, 2, (width or 2)


def cells(data, band):
    """Signed cell values for one band, in record order.

    AutoTune corrections are SIGNED -- a small negative trim shows up in a raw
    byte diff as 0x0000 -> 0xFFxx. Reading these unsigned turns a -1 trim into
    a +65535 outlier that dominates every ranking.
    """
    lo, hi, stride, width = band_grid(band)
    fmt = {1: "<b", 2: "<h", 4: "<i"}[width]
    out = []
    for off in range(lo, hi - width + 1, stride):
        out.append(struct.unpack_from(fmt, data, off)[0])
    return out


def cell_offset(band, idx):
    """File offset of cell `idx` -- what you hand to a hex editor."""
    lo, _, stride, _ = band_grid(band)
    return lo + idx * stride


def read_tune(path, bands=LEARNED_BANDS, tables=None):
    """{band_name: [signed cells]} for one .tbw."""
    data = Path(path).read_bytes()
    if len(data) != EXPECTED_SIZE:
        raise ValueError(f"{Path(path).name}: {len(data)} bytes, "
                         f"expected {EXPECTED_SIZE}")
    tables = tables or load_map()
    return {n: cells(data, _band(n, tables)) for n in bands}


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def binom_two_sided(k, m):
    """Two-sided p that k of m moves went one direction, under p=0.5.

    Exact, stdlib only. `k` is the count in the MORE common direction.
    """
    if m <= 0:
        return 1.0
    k = max(k, m - k)
    tail = sum(math.comb(m, i) for i in range(k, m + 1)) / (2 ** m)
    return min(1.0, 2 * tail)


def benjamini_hochberg(pvals):
    """BH-adjusted q-values, same order as input.

    Controls the expected proportion of false findings among those reported.
    Bonferroni would also be defensible but is far too blunt here: a real
    persistent bias usually shows up as a RUN of adjacent cells, and
    Bonferroni at n=2500 would demand p<2e-5, which needs 16+ consecutive
    same-direction saves. Joshua does not have 16 saves of most families.
    """
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    q = [1.0] * n
    prev = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        idx = n - rank + 1                      # 1-based rank of this p
        val = min(prev, pvals[i] * n / idx)
        q[i] = prev = val
    return q


# ---------------------------------------------------------------------------
# trend across a sequence of saves
# ---------------------------------------------------------------------------

def trend(tune_paths, bands=LEARNED_BANDS, tables=None):
    """Per-cell sign persistence across an ORDERED sequence of tunes.

    Order matters and is the caller's responsibility -- pass the tunes in the
    order they were saved. `lineage()` builds that order; do not hand this an
    alphabetical glob and expect the direction to mean anything.

    Returns {band: {"steps": n, "cells": [record, ...]}} where each record is
    a cell that MOVED at least once, carrying up/down counts, net raw drift,
    its exact file offset, and raw + FDR-adjusted p.
    """
    tables = tables or load_map()
    paths = [Path(p) for p in tune_paths]
    if len(paths) < 2:
        raise ValueError("need at least two tunes to see a trend")

    series = [read_tune(p, bands, tables) for p in paths]
    out = {}

    for name in bands:
        band = _band(name, tables)
        cols = [s[name] for s in series]
        n_cells = min(len(c) for c in cols)
        recs, pv = [], []

        for i in range(n_cells):
            ups = downs = 0
            net = 0
            for a, b in zip(cols, cols[1:]):
                d = b[i] - a[i]
                if d > 0:
                    ups += 1
                elif d < 0:
                    downs += 1
                net += d
            moves = ups + downs
            if moves == 0:
                continue
            p = (binom_two_sided(max(ups, downs), moves)
                 if moves >= MIN_MOVES else 1.0)
            recs.append({
                "cell": i,
                "offset": cell_offset(band, i),
                "up": ups, "down": downs, "moves": moves,
                "net_raw": net,
                "direction": "rich/up" if ups > downs else (
                    "lean/down" if downs > ups else "mixed"),
                "first": cols[0][i], "last": cols[-1][i],
                "p": p,
            })
            pv.append(p)

        for rec, q in zip(recs, benjamini_hochberg(pv)):
            rec["q"] = q

        out[name] = {
            "steps": len(paths) - 1,
            "cells_total": n_cells,
            "cells_moved": len(recs),
            "cells": recs,
        }
    return out


def contiguous_runs(recs, band, tables=None):
    """Group flagged cells into adjacent runs.

    Until the axis map exists, an offset is not interpretable on its own -- but
    a RUN of neighbouring cells all biased the same way is almost certainly one
    contiguous region of a real map page, which is a much stronger signal than
    the same number of scattered cells. This is the closest honest thing to
    "your midrange is lean" that can be said without ground truth.
    """
    if not recs:
        return []
    b = _band(band, tables) if isinstance(band, str) else band
    _, _, stride, _ = band_grid(b)
    recs = sorted(recs, key=lambda r: r["cell"])
    runs, cur = [], [recs[0]]
    for r in recs[1:]:
        if r["cell"] == cur[-1]["cell"] + 1 and r["direction"] == cur[-1]["direction"]:
            cur.append(r)
        else:
            runs.append(cur)
            cur = [r]
    runs.append(cur)
    return [{
        "start_cell": run[0]["cell"],
        "end_cell": run[-1]["cell"],
        "length": len(run),
        "start_offset": run[0]["offset"],
        "end_offset": run[-1]["offset"] + stride,
        "direction": run[0]["direction"],
        "net_raw": sum(r["net_raw"] for r in run),
        "min_q": min(r["q"] for r in run),
    } for run in runs]


def region_bias(tune_paths, bands=LEARNED_BANDS, window=48, fdr=DEFAULT_FDR,
                tables=None):
    """Directional bias pooled over WINDOWS of adjacent cells.

    This is the headline test, and the per-cell one below is only detail.

    WHY POOLING IS NOT OPTIONAL. A per-cell sign test across n cells needs a
    Benjamini-Hochberg threshold of roughly fdr/n at the top rank. With ~776
    moving cells that is p <= 6.4e-5, which a binomial on 8 save-to-save steps
    cannot reach no matter how lopsided the cell is -- you would need about 17
    consecutive same-direction saves before a single cell could clear the bar.
    So the per-cell test has essentially no power on any tune history Joshua
    actually has, and reporting "0 findings" from it says nothing about the
    bike. Pooling adjacent cells cuts the number of tests by the window size
    AND multiplies the observations per test, which is where the power comes
    from. It also matches the physics: a genuinely lean patch of the map is a
    contiguous block of cells, never one isolated cell.

    TWO P-VALUES, ON PURPOSE:

      p_pooled -- every (cell, step) movement treated as one observation.
        OPTIMISTIC. AutoTune smooths corrections across neighbouring cells, so
        adjacent cells are correlated and the true independent sample size is
        smaller than the count. Do not quote this as if it were clean.

      p_steps  -- each SAVE contributes exactly one observation per window
        (the sign of that window's net movement for that step). CONSERVATIVE
        and defensible: separate saves really are separate events. Its ceiling
        is set by the number of saves -- 8 steps can at best give p=0.0078 --
        so with a short history it will often fail to reach significance even
        when the effect is real.

    When those two disagree, the honest reading is "suggestive, needs more
    saves", and `verdict` says exactly that rather than picking the flattering
    number.
    """
    tables = tables or load_map()
    paths = [Path(p) for p in tune_paths]
    if len(paths) < 2:
        raise ValueError("need at least two tunes to see a trend")
    series = [read_tune(p, bands, tables) for p in paths]
    out = {}

    for name in bands:
        band = _band(name, tables)
        cols = [s[name] for s in series]
        n_cells = min(len(c) for c in cols)
        n_steps = len(cols) - 1
        wins, pooled_p, step_p = [], [], []

        for w0 in range(0, n_cells, window):
            w1 = min(w0 + window, n_cells)
            up = dn = 0
            net = 0
            step_signs = []
            for a, b in zip(cols, cols[1:]):
                s_net = 0
                for i in range(w0, w1):
                    d = b[i] - a[i]
                    if d > 0:
                        up += 1
                    elif d < 0:
                        dn += 1
                    s_net += d
                net += s_net
                if s_net:
                    step_signs.append(1 if s_net > 0 else -1)

            moves = up + dn
            if not moves:
                continue
            s_up = sum(1 for s in step_signs if s > 0)
            s_m = len(step_signs)
            pp = binom_two_sided(max(up, dn), moves)
            sp = binom_two_sided(max(s_up, s_m - s_up), s_m) if s_m else 1.0
            wins.append({
                "start_cell": w0, "end_cell": w1,
                "start_offset": cell_offset(band, w0),
                "end_offset": cell_offset(band, w1),
                "up": up, "down": dn, "moves": moves,
                "share": max(up, dn) / moves,
                "direction": "rich/up" if up > dn else (
                    "lean/down" if dn > up else "mixed"),
                "net_raw": net,
                "steps_moved": s_m, "steps_same_way": max(s_up, s_m - s_up),
                "p_pooled": pp, "p_steps": sp,
            })
            pooled_p.append(pp)
            step_p.append(sp)

        for w, qp, qs in zip(wins, benjamini_hochberg(pooled_p),
                             benjamini_hochberg(step_p)):
            w["q_pooled"], w["q_steps"] = qp, qs
            # A window that only moved on one or two saves is a SINGLE EVENT,
            # however many cells took part. This gate is not a nicety: on the
            # latestandgreatest series, 48 adjacent cells of learned_ve_bulk
            # all moved down within one save, and the pooled test scored that
            # 1.6e-13 -- as if 48 independent measurements agreed, when it is
            # one write. Requiring the direction to REPEAT across separate
            # saves is the whole basis for calling something a trend, so it is
            # checked before either p-value is allowed to speak.
            if w["steps_moved"] < MIN_TREND_STEPS:
                w["verdict"] = (f"single event ({w['steps_moved']} of "
                                f"{n_steps} saves) - not a trend")
            elif qs <= fdr:
                w["verdict"] = "established"
            elif qp <= fdr:
                w["verdict"] = "suggestive - needs more saves"
            else:
                w["verdict"] = "not significant"

        wins.sort(key=lambda w: (w["q_steps"], w["q_pooled"]))
        out[name] = {
            "window": window, "steps": n_steps,
            "cells_total": n_cells, "windows_tested": len(wins),
            "fdr": fdr,
            "established": [w for w in wins if w["verdict"] == "established"],
            "suggestive": [w for w in wins
                           if w["verdict"].startswith("suggestive")],
            "windows": wins,
        }
    return out


def bias(tune_paths, bands=LEARNED_BANDS, fdr=DEFAULT_FDR, tables=None):
    """Persistent, FDR-surviving directional bias across a save sequence.

    This is the headline call. A cell reaches the output only if it moved at
    least MIN_MOVES times AND its sign consistency survives Benjamini-Hochberg
    at `fdr`. Everything else is treated as AutoTune doing its job.
    """
    tables = tables or load_map()
    tr = trend(tune_paths, bands, tables)
    out = {}
    for name, res in tr.items():
        flagged = [c for c in res["cells"] if c["q"] <= fdr]
        flagged.sort(key=lambda c: (c["q"], -abs(c["net_raw"])))
        out[name] = {
            "steps": res["steps"],
            "cells_total": res["cells_total"],
            "cells_moved": res["cells_moved"],
            "cells_flagged": len(flagged),
            "fdr": fdr,
            "cells": flagged,
            "runs": contiguous_runs(flagged, name, tables),
        }
    return out


# ---------------------------------------------------------------------------
# lineage -- put a folder of tunes into save order
# ---------------------------------------------------------------------------

_SUFFIX = re.compile(r"[\s_-]*[A-Za-z]?\d*$")


def _strip_suffix(stem):
    s = re.sub(r"\.tbw$", "", stem, flags=re.I)
    return _SUFFIX.sub("", s).strip(" _-").lower() or s.lower()


def family_keys(stems):
    """Map each stem -> family key, using the WHOLE set to decide.

    Collapses 'latestandgreatestB' / 'latestandgreatestG2' onto
    'latestandgreatest'. The subtlety that makes this need the full set: a
    trailing-character strip applied blindly also eats the last letter of a
    name that has no suffix at all. 'automaprun' -> 'automapru', which does
    NOT match the 'automaprun' that automaprun2 and automaprun3 strip down to,
    so the parent tune silently falls out of its own family and the trend
    quietly runs on 2 saves instead of 3.

    So a stripped form is only adopted when at least two DIFFERENT stems agree
    on it; otherwise the stem keeps its full name and can still be picked up
    as a family root by the exact-match check. Wrong grouping stays visible in
    `lineage` output, and an explicit file list always overrides it.
    """
    stripped = {s: _strip_suffix(s) for s in stems}
    shared = {}
    for s, k in stripped.items():
        shared[k] = shared.get(k, 0) + 1

    out = {}
    for s in stems:
        k = stripped[s]
        # Adopt the stripped form when it is genuinely shared, or when it is
        # exactly some other tune's full name (the family's parent save).
        out[s] = k if (shared[k] >= 2 or k in {x.lower() for x in stems}) \
            else s.lower()
    return out


def lineage(folder, bands=LEARNED_BANDS):
    """Group a tune folder into families ordered by mtime.

    Skips macOS `._*` AppleDouble sidecars (4096 bytes) -- they are not tunes
    and crash a naive parser -- and anything that is not exactly 214470 bytes.
    """
    folder = Path(folder)
    from thundermax_parser import TbwFile

    good = []
    for p in sorted(folder.glob("*.tbw")):
        if p.name.startswith("._"):
            continue
        try:
            if p.stat().st_size != EXPECTED_SIZE:
                continue
            t = TbwFile(p)
        except Exception:
            continue
        good.append((p, t.base_map_id))

    keys = family_keys([p.stem for p, _ in good])
    fams = {}
    for p, map_id in good:
        fams.setdefault(keys[p.stem], []).append({
            "path": p, "name": p.name, "mtime": p.stat().st_mtime,
            "base_map_id": map_id,
        })
    for v in fams.values():
        v.sort(key=lambda e: e["mtime"])

    # Relabel with the shortest real stem in the family. The grouping key can
    # be a truncated form ('automapru') because the suffix strip ate a real
    # letter; membership is still correct, but a key that matches no actual
    # file is confusing to type back in as --family.
    relabelled = {}
    for items in fams.values():
        key = min((i["path"].stem for i in items), key=len).lower()
        while key in relabelled:
            key += "_"
        relabelled[key] = items
    return dict(sorted(relabelled.items(), key=lambda kv: -len(kv[1])))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_regions(res):
    for name, r in res.items():
        print(f"\n=== {name} (windows of {r['window']} cells) ===")
        print(f"  {r['steps']} save-to-save steps, {r['windows_tested']} "
              f"windows tested, FDR q<={r['fdr']}")
        single = [w for w in r["windows"] if w["verdict"].startswith("single")]
        print(f"  established: {len(r['established'])}   "
              f"suggestive: {len(r['suggestive'])}   "
              f"single-event (excluded): {len(single)}")
        show = (r["established"] + r["suggestive"])[:20]
        if not show:
            print("  no directional bias at any strength -- "
                  "movement is balanced, i.e. AutoTune oscillating on target.")
            continue
        print(f"\n  {'offset range':>21} {'dir':10} {'share':>6} {'moves':>6} "
              f"{'steps':>7} {'q_steps':>9} {'q_pool':>9}  verdict")
        for w in show:
            print(f"  0x{w['start_offset']:05X}-0x{w['end_offset']:05X} "
                  f"{w['direction']:10} {w['share']*100:>5.0f}% {w['moves']:>6} "
                  f"{w['steps_same_way']}/{w['steps_moved']:<5} "
                  f"{w['q_steps']:>9.2e} {w['q_pooled']:>9.2e}  {w['verdict']}")


def _print_bias(res, verbose=False):
    for name, r in res.items():
        print(f"\n=== {name} ===")
        print(f"  {r['steps']} save-to-save steps, {r['cells_total']} cells, "
              f"{r['cells_moved']} ever moved, "
              f"{r['cells_flagged']} survive FDR q<={r['fdr']}")
        if not r["cells_flagged"]:
            print("  no persistent directional bias -- AutoTune churn only.")
            continue
        print(f"\n  contiguous biased runs ({len(r['runs'])}):")
        print(f"  {'offset range':>21}  {'cells':>5}  {'direction':10} "
              f"{'net raw':>9}  {'min q':>8}")
        for run in sorted(r["runs"], key=lambda x: -x["length"])[:25]:
            print(f"  0x{run['start_offset']:05X}-0x{run['end_offset']:05X}  "
                  f"{run['length']:>5}  {run['direction']:10} "
                  f"{run['net_raw']:>9}  {run['min_q']:>8.2e}")
        if verbose:
            print(f"\n  top cells:")
            for c in r["cells"][:30]:
                print(f"  0x{c['offset']:05X} cell {c['cell']:>5}  "
                      f"up{c['up']}/dn{c['down']}  net {c['net_raw']:>7}  "
                      f"q={c['q']:.2e}  {c['first']} -> {c['last']}")


def _print_footer():
    print("\nNOTE: offsets are raw file positions. Direction and persistence are")
    print("scale-free and trustworthy; MAGNITUDE is in unknown raw units and is")
    print("comparable only within the same band. No rpm/TPS mapping exists yet,")
    print("so a flagged window cannot yet be named in rpm/TPS terms")
    print("-- see docs/GROUND_TRUTH_EXPERIMENT.md.")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Read the bike's own AutoTune learning out of .tbw saves")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("cells", help="dump learned-band cell stats for one tune")
    pc.add_argument("tune")

    pt = sub.add_parser("trend", help="sign persistence across ordered saves")
    pt.add_argument("tunes", nargs="+")
    pt.add_argument("--fdr", type=float, default=DEFAULT_FDR)
    pt.add_argument("--window", type=int, default=48)
    pt.add_argument("--all-bands", action="store_true",
                    help="also read fuel_rich_correction")
    pt.add_argument("-v", "--verbose", action="store_true")
    pt.add_argument("--json", action="store_true")

    pl = sub.add_parser("lineage", help="group a tune folder into save-ordered families")
    pl.add_argument("folder")

    pr = sub.add_parser("report", help="lineage + bias for the largest family")
    pr.add_argument("folder")
    pr.add_argument("--family", help="family key (default: largest)")
    pr.add_argument("--fdr", type=float, default=DEFAULT_FDR)
    pr.add_argument("--window", type=int, default=48)
    pr.add_argument("-v", "--verbose", action="store_true")

    a = p.parse_args(argv)
    bands = LEARNED_BANDS + (OPTIONAL_LEARNED_BANDS
                             if getattr(a, "all_bands", False) else ())

    if a.cmd == "cells":
        for name, vals in read_tune(a.tune, bands).items():
            nz = [v for v in vals if v]
            print(f"{name:20} {len(vals):>6} cells  "
                  f"{len(nz):>6} non-zero  "
                  f"min {min(vals):>7}  max {max(vals):>7}")
        return 0

    if a.cmd == "trend":
        res = bias(a.tunes, bands, a.fdr)
        if a.json:
            print(json.dumps(res, indent=2, default=str))
        else:
            print(f"sequence ({len(a.tunes)} saves):")
            for t in a.tunes:
                print(f"  {Path(t).name}")
            _print_regions(region_bias(a.tunes, bands, a.window, a.fdr))
            if a.verbose:
                _print_bias(res, True)
            _print_footer()
        return 0

    if a.cmd == "lineage":
        fams = lineage(a.folder)
        for key, items in fams.items():
            if len(items) < 2:
                continue
            ids = {i["base_map_id"] for i in items}
            print(f"\n{key}  ({len(items)} saves, "
                  f"{len(ids)} base-map id{'s' if len(ids) > 1 else ''})")
            for i in items:
                print(f"    {i['name']}")
        singles = sum(1 for v in fams.values() if len(v) < 2)
        print(f"\n{singles} single-save families not shown "
              f"(need >=2 saves to trend).")
        return 0

    if a.cmd == "report":
        fams = lineage(a.folder)
        usable = {k: v for k, v in fams.items() if len(v) >= 3}
        if not usable:
            print("no family has >=3 saves; nothing to trend.", file=sys.stderr)
            return 1
        key = a.family or next(iter(usable))
        if key not in usable:
            print(f"family {key!r} not found. options: "
                  f"{', '.join(usable)}", file=sys.stderr)
            return 1
        paths = [i["path"] for i in usable[key]]
        print(f"family: {key}  ({len(paths)} saves in mtime order)")
        for p_ in paths:
            print(f"  {p_.name}")
        _print_regions(region_bias(paths, bands, a.window, a.fdr))
        if a.verbose:
            _print_bias(bias(paths, bands, a.fdr), True)
        _print_footer()
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
