#!/usr/bin/env python3
"""Bridge between real `.tbw` tune binaries and the deterministic virtual dyno.

Joshua's question was: *"we will also need a way to test the dyno and tune to
make sure that the dyno is thinking correctly and reading the tune correctly
when it processes it."*

`virtual_dyno.simulate_pull()` has never read a `.tbw` in its life — it takes an
ABSTRACT list of `{table, rpm_band, tps_band, direction, magnitude, unit}`
deltas. This module supplies the two missing halves:

  read_tune(path)             -- what the dyno can (and cannot) see in ONE tune
  changes_from_diff(a, b)     -- a real A->B tune diff turned into dyno changes
  self_test()                 -- runtime proof the dyno's physics is self-consistent

THE HONESTY CONSTRAINT (read this before changing anything here)
---------------------------------------------------------------
Per COLLABORATION.md, per-cell engineering-unit scaling of the TBW format is
**UNCONFIRMED for almost every table**. What is actually known:

  * WHERE each table band lives (tables.json, confidence-tiered).
  * ONE scale factor: ~49 raw units per degree, MEASURED on the 0xC3C9
    timing-limit array by a global -1 deg edit. Assumed — never proven — for
    the main RPM x TPS timing map.
  * NOTHING about the axis/cell ORDER of any band. tables.json flags every
    single band `needs_ground_truth`.

Therefore this module obeys three rules, and every function's output is shaped
by them:

  1. **No invented absolute readings.** "VE at 4000 rpm / 50% TPS = 0.92" would
     be a fabrication. read_tune() reports RAW cell statistics per band, and an
     engineering-unit estimate ONLY for a band whose scale was measured.
  2. **No fabricated bands.** tables.json contains no offset -> (rpm, tps)
     mapping for ANY band, so `rpm_band` / `tps_band` come out `null`. A wrong
     band silently mis-scopes a safety check, which is worse than no band.
     (`virtual_dyno._band()` reads a null band as "applies everywhere" — the
     conservative reading, which is what we want.)
  3. **Everything carries provenance.** confidence tier, scale basis, and an
     explicit `unknown[]` / `warnings[]` naming what could not be resolved and
     why.

WHY DELTAS ARE TRUSTED FURTHER THAN ABSOLUTES
---------------------------------------------
A uniform raw DELTA across a band survives three kinds of ignorance that an
absolute reading does not: unknown axis order, unknown cell offset within the
record, and unknown zero-point/bias. "-49 raw everywhere in the timing band" is
"-1 degree everywhere" regardless of which cell is which. "1666 raw at this
offset" is only 34.0 deg if the offset, the stride alignment AND the zero point
are all right. So:

  * changes_from_diff() converts TIMING deltas to degrees (measured basis =
    medium confidence, assumed basis = low confidence).
  * read_tune() emits an absolute engineering estimate ONLY for the one band
    where the scale was actually measured, and says so.

NEVER WRITES A `.tbw`. Reads only. (Project hard rule #2.)

Stdlib only. No network, no NAS requirement, no Ollama, no LLM.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import guardrails as G           # noqa: E402
import table_map                 # noqa: E402
import thundermax_parser as tmx  # noqa: E402
import virtual_dyno as vd        # noqa: E402


# ---------------------------------------------------------------------------
# Scale knowledge — the ONLY place raw->engineering conversions are declared
# ---------------------------------------------------------------------------

# Everything not listed here has NO known scale and MUST come out as raw units.
# `basis`:
#   "measured" -- a real edit of known engineering size moved these cells by a
#                 known raw amount (the 2026 global -1 deg capture).
#   "assumed"  -- the scale of a sibling band is assumed to carry over. Not
#                 proven. Degrades magnitude_confidence to "low".
KNOWN_SCALES = {
    "timing_limit_array": {
        "raw_per_unit": 49.0,
        "unit": "deg",
        "unit_label": "degrees (timing-linked)",
        "basis": "measured",
        "confidence": "medium",
        "source": ("tables.json timing_scale_finding, re-verified by this "
                   "module against the real pair final131autotunedtune.tbw -> "
                   "final131autotunedtuneretardedtiming-1degreeglobaly.tbw: "
                   "all 128 records at 0x0C3C9 (stride 8) moved by exactly "
                   "-49 raw for a labelled global -1 deg edit "
                   "(49 -> 0 and 488 -> 439)."),
        "caveat": ("(a) This is a DELTA scale. An ABSOLUTE reading further "
                   "assumes raw 0 == 0 deg, which rests on a single "
                   "observation (a 49 -> 0 cell under the -1 deg edit) and has "
                   "never been confirmed in TMax Tuner. (b) Observed cells are "
                   "not exact multiples of 49 (488 raw = 9.96 'deg'), so the "
                   "true scale may be nearer 48.8 raw/deg or the zero point is "
                   "non-zero — absolute degrees carry ~1% scale error plus an "
                   "unknown offset. (c) Which TMax Tuner parameter this array "
                   "backs (max-advance clamp? knock-retard clamp?) is still "
                   "unidentified, so the degrees are a magnitude, not a named "
                   "setting."),
        "zero_point_basis": ("inferred from one cell (49 -> 0 under a -1 deg "
                             "edit); NOT confirmed"),
    },
    # timing_map_main is DELIBERATELY ABSENT. tables.json assumes it shares the
    # 49 raw/deg scale, but the evidence contradicts that assumption:
    #
    #   On the labelled global -1 deg pair, timing_limit_array moved by exactly
    #   -49 in every one of its 128 records, while the 0x01FC9 band moved in
    #   only 15 cells (16-byte spacing), all UPWARD, by a smooth descending
    #   ramp of +15, +14, +13 ... +2 raw. A uniform -1 deg cannot produce a
    #   non-uniform positive ramp in the same units.
    #
    # Applying 49 raw/deg there would put a fabricated degree figure in front
    # of Joshua. So the band stays raw-only until the COLLABORATION.md
    # one-cell TMax Tuner ground-truth experiment resolves it.
}

# Bands whose scale is trustworthy enough to state an ABSOLUTE engineering
# value for. Strictly a subset of KNOWN_SCALES: an absolute reading additionally
# needs the cell offset and zero-point to be right, which only the measured
# band has demonstrated.
ABSOLUTE_SCALE_BANDS = {"timing_limit_array"}

# Bands where a scale WAS assumed in tables.json, this module tested that
# assumption against real tunes, and the data did not support it. Recorded so
# the assumption is not quietly re-adopted later.
CONTRADICTED_SCALES = {
    "timing_map_main": (
        "tables.json assumes this band shares the timing_limit_array's 49 "
        "raw/deg. Tested on the labelled global -1 deg pair "
        "(final131autotunedtune.tbw -> ...retardedtiming-1degreeglobaly.tbw): "
        "timing_limit_array moved -49 in all 128 of its records, while this "
        "band moved in only 15 cells (16-byte spacing), all UPWARD, as a "
        "smooth descending ramp +15, +14, +13 ... +2 raw. A uniform -1 deg "
        "cannot produce a non-uniform positive ramp in the same units, so the "
        "49 raw/deg assumption is NOT carried over here."),
}

# Prefix for a change the dyno must NOT model. Deliberately not a
# guardrails.TABLES name: virtual_dyno._unit_of() infers "afr"/"ve_pct"/"deg"
# from a bare table name, so labelling a raw-unit change "afr_target" would let
# the dyno read a raw delta of 3000 as "+3000 AFR points". With the prefix,
# _unit_of() returns None and the dyno correctly IGNORES what it cannot
# quantify — while guardrails.check_change() blocks it as an unknown table,
# which is also correct: an unquantified change cannot be safety-checked.
# The name after the prefix is the TBW band, not a TMax Tuner page.
NON_DYNO_TABLE_PREFIX = "tbw:"

# Band -> the guardrails/TMax-Tuner tables a change in it should DRIVE on the
# virtual dyno. A band qualifies only when BOTH are true:
#   (a) its raw->engineering scale is in KNOWN_SCALES, and
#   (b) it is a COMMANDED map, not a limit/clamp or a learned store.
#
# EMPTY TODAY, and that emptiness is the honest headline finding of this
# module: the one band with a measured scale (timing_limit_array) is a
# clamp/limit array, not commanded advance — modelling a limit as if it were a
# spark command would simulate something that does not happen. And the one
# commanded map we have located (timing_map_main) has no trustworthy scale.
#
# So: the dyno can tell you the DIRECTION of everything in a real A->B diff,
# and the SIZE of nothing. Closing the COLLABORATION.md one-cell ground-truth
# experiment for timing_map_main is what turns this dict on. When it does, add:
#     "timing_map_main": ("spark_advance_front", "spark_advance_rear")
# (both cylinders: the band does not separate front from rear, and modelling
# both is conservative — the rear jug carries the KNOCK_REAR_BIAS penalty.)
BAND_TO_DYNO_TABLES = {}

# Categories that churn on every ride/save and are NOT deliberate rider edits.
CHURN_CATEGORIES = {
    "AUTOTUNE": ("AutoTune learned/correction data. It moves on every ride and "
                 "every save regardless of what the tuner touched, so a delta "
                 "here is evidence of RIDING, not of a deliberate table edit. "
                 "Feeding it to the dyno would attribute the bike's own "
                 "closed-loop learning to a change Joshua made."),
    "METADATA": ("Per-section metadata / checksums below 0x3C0. These change "
                 "whenever ANY table changes; they are integrity bytes, not "
                 "tunable content. tables.json: \"Never edit; a bad checksum "
                 "can brick a flash.\""),
}

# Bands whose own tables.json note tells us to treat them as learned data.
LEARNED_DATA_BANDS = {
    "fuel_rich_correction": ("tables.json needs_ground_truth for this band says "
                             "\"treat as AutoTune data until proven otherwise\" "
                             "— it is likely the AutoTune fuel-learn / VE trim "
                             "store, not a rider-edited page."),
}

# Categories that are located but explicitly uninterpretable.
UNRESOLVED_CATEGORIES = {
    "SHARED": ("tables.json shared_churn_unresolved: \"Anything else labeled "
               "SHARED here is unresolved -- do not interpret its values.\""),
    "UNMAPPED": "Offset falls outside every band in tables.json.",
}

# Offset -> (rpm_band, tps_band). EMPTY, AND IT MUST STAY EMPTY until the
# one-cell TMax Tuner ground-truth experiment in COLLABORATION.md lands.
# tables.json flags every band `needs_ground_truth` on exactly this point:
# axis ORDER is unknown, so no offset can be honestly resolved to an rpm/tps
# cell. Populating this with a guess would silently mis-scope every safety
# check that reads rpm_band.
BAND_AXES = {}

CONFIDENCE_ORDER = ["unknown", "low", "medium", "high"]


def _weakest(*tiers):
    """Lowest confidence tier among the arguments ('-'/None treated as unknown)."""
    seen = [t if t in CONFIDENCE_ORDER else "unknown" for t in tiers if t]
    if not seen:
        return "unknown"
    return min(seen, key=CONFIDENCE_ORDER.index)


# ---------------------------------------------------------------------------
# Raw cell reading
# ---------------------------------------------------------------------------

def _cell_width(stride):
    """Bytes read per record. Stride 1 -> u8; anything wider -> the LE u16 that
    tables.json calls the record's value word."""
    return 1 if int(stride) <= 1 else 2


def _read_cells(data, lo, hi, stride):
    """Raw cell values across [lo, hi) at `stride`, as a list of ints.

    tables.json record_note: cells are little-endian fields on a 4- or 8-byte
    stride, "a u16 value, often followed by a flag/limit word". So we take the
    u16 at the START of each record. Which word inside the record is the value
    is itself unconfirmed — that is why nothing here is presented as a decoded
    table cell, only as a raw-cell distribution.
    """
    stride = max(1, int(stride))
    width = _cell_width(stride)
    hi = min(hi, len(data))
    out = []
    if width == 1:
        for off in range(lo, hi, stride):
            out.append(data[off])
    else:
        for off in range(lo, hi - 1, stride):
            out.append(struct.unpack_from("<H", data, off)[0])
    return out


def _stats(values):
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None,
                "mode": None, "mode_count": 0, "distinct": 0}
    c = Counter(values)
    mode, mode_n = c.most_common(1)[0]
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 3),
        "mode": mode,
        "mode_count": mode_n,
        "distinct": len(c),
    }


def _wrap_i16(delta):
    """Re-read a u16 delta as a signed-16-bit delta when it obviously wrapped.

    tables.json: "AutoTune correction cells are SIGNED, so small negative trims
    appear in a raw byte diff as 0x0000 -> 0xFFxx borrows." thundermax_parser's
    region_deltas() subtracts unsigned words, so -1 shows up as +65535.
    Returns (delta, wrapped_bool). Exactly +/-32768 is genuinely ambiguous and
    is left alone.
    """
    d = int(delta)
    if d > 32768:
        return d - 65536, True
    if d < -32768:
        return d + 65536, True
    return d, False


# ---------------------------------------------------------------------------
# 1. read_tune — what the dyno can see in one tune
# ---------------------------------------------------------------------------

def _profile():
    try:
        return json.loads((_SRC / "bike_profile.json").read_text())
    except (OSError, ValueError):
        return {}


def _engineering_estimate(band_name, stats, absolute_only=True):
    """Engineering-unit estimate for a band, or None.

    `absolute_only=True` (read_tune) restricts to ABSOLUTE_SCALE_BANDS: the one
    band whose scale was actually measured. Everything else returns None and
    lands in `unknown[]`.
    """
    scale = KNOWN_SCALES.get(band_name)
    if not scale:
        return None
    if absolute_only and band_name not in ABSOLUTE_SCALE_BANDS:
        return None
    if not stats or stats["count"] == 0:
        return None
    k = scale["raw_per_unit"]
    return {
        "unit": scale["unit"],
        "unit_label": scale["unit_label"],
        "is_estimate": True,
        "scale_raw_per_unit": k,
        "scale_basis": scale["basis"],
        "zero_point_basis": scale.get("zero_point_basis", "unknown"),
        "confidence": scale["confidence"],
        "source": scale["source"],
        "caveat": scale["caveat"],
        "min": round(stats["min"] / k, 2),
        "max": round(stats["max"] / k, 2),
        "mean": round(stats["mean"] / k, 2),
        "mode": round(stats["mode"] / k, 2),
        "note": ("ESTIMATE from a raw/{:.0f} conversion at {} confidence — not "
                 "a reading from TMax Tuner.".format(k, scale["confidence"])),
    }


def _unknown_reason(band_name, category, confidence):
    """Why this band's absolute scaling cannot be resolved."""
    if band_name in CONTRADICTED_SCALES:
        return CONTRADICTED_SCALES[band_name]
    scale = KNOWN_SCALES.get(band_name)
    if scale and band_name not in ABSOLUTE_SCALE_BANDS:
        return ("A DELTA scale exists for this band ({:.0f} raw/{} , basis "
                "'{}') but no ABSOLUTE anchor: no TMax Tuner cell has ever "
                "been matched to a byte here, and the axis/cell order is "
                "unconfirmed, so a raw value cannot be turned into a reading."
                .format(scale["raw_per_unit"], scale["unit"], scale["basis"]))
    if category in CHURN_CATEGORIES:
        return ("No scale measured, and this band is churn data rather than a "
                "rider-edited table. " + CHURN_CATEGORIES[category].split(".")[0] + ".")
    if category in UNRESOLVED_CATEGORIES:
        return UNRESOLVED_CATEGORIES[category]
    return ("No raw->engineering scale factor has ever been measured for this "
            "band. tables.json marks it needs_ground_truth; only a one-cell "
            "TMax Tuner reading can close it.")


def read_tune(path, tables=None):
    """Report exactly what the dyno can and cannot tell you about ONE tune.

    Returns a dict (see module docstring for the honesty rules):

    {
      "file": {path, name, size_bytes, size_expected, size_ok},
      "base_map_id": str, "header": [4 ints], "valid": bool,
      "id_ok": bool, "is_my_setup": bool, "profile_base_map_ids": [...],
      "bands": [ {name, category, confidence, desc, range_hex, offset, end,
                  length_bytes, stride, cell_width_bytes, nested_in,
                  raw_cells: {count,min,max,mean,mode,mode_count,distinct},
                  scale_known, absolute_engineering_available,
                  engineering: {...} | null, needs_ground_truth} ],
      "unknown": [ {band, category, confidence, reason, needs_ground_truth} ],
      "coverage": {mapped_bytes, file_bytes, mapped_pct, unmapped_bytes},
      "honesty": {...}, "warnings": [...],
    }

    NEVER writes the file. Raises tmx.TbwError / OSError on an unreadable file.
    """
    tables = tables or table_map.load_map()
    tbw = tmx.TbwFile(path)
    data = tbw.data
    warnings = []

    if not tbw.size_ok:
        warnings.append(
            f"file is {len(data)} bytes, expected {tmx.EXPECTED_SIZE}. Band "
            "offsets come from a 214470-byte layout and may not line up; treat "
            "every band statistic below as suspect.")
    if not tbw.id_ok:
        warnings.append("base-map ID at 0x10 did not parse as clean uppercase "
                        "ASCII — this may not be a ThunderMax TBW file.")

    prof = _profile()
    prof_ids = list(prof.get("base_map_ids") or [])
    is_mine = tbw.base_map_id in prof_ids
    if prof_ids and not is_mine:
        warnings.append(
            f"base map '{tbw.base_map_id}' is not one of this bike's known base "
            f"maps ({', '.join(prof_ids)}). Band offsets were derived from the "
            "bike's own tune family; a foreign base map may lay out differently.")

    bands, unknown = [], []
    mapped_bytes = 0
    for b in tables["bands"]:
        lo, hi = b["_lo"], b["_hi"]
        stride = b.get("stride", 1)
        cells = _read_cells(data, lo, hi, stride)
        stats = _stats(cells)
        eng = _engineering_estimate(b["name"], stats, absolute_only=True)
        # A band nested inside another (afr_target lives inside the shared
        # churn block) — the narrower band wins for classification.
        nested = next((o["name"] for o in tables["bands"]
                       if o is not b and o["_lo"] <= lo and hi <= o["_hi"]), None)
        entry = {
            "name": b["name"],
            "category": b["category"],
            "confidence": b["confidence"],
            "desc": b["desc"],
            "range_hex": [b["range"][0], b["range"][1]],
            "offset": lo,
            "end": hi,
            "length_bytes": hi - lo,
            "stride": stride,
            "cell_width_bytes": _cell_width(stride),
            "nested_in": nested,
            "raw_cells": stats,
            "raw_cells_note": (
                "RAW DEVICE UNITS at the {}-byte record stride, reading the "
                "leading little-endian {} of each record. Which word inside a "
                "record is the value is itself unconfirmed."
                .format(stride, "byte" if _cell_width(stride) == 1 else "u16")),
            "scale_known": b["name"] in KNOWN_SCALES,
            "absolute_engineering_available": eng is not None,
            "engineering": eng,
            "needs_ground_truth": b.get("needs_ground_truth"),
        }
        bands.append(entry)
        mapped_bytes += hi - lo
        if eng is None:
            unknown.append({
                "band": b["name"],
                "category": b["category"],
                "confidence": b["confidence"],
                "reason": _unknown_reason(b["name"], b["category"], b["confidence"]),
                "needs_ground_truth": b.get("needs_ground_truth"),
            })

    # Bands overlap (afr_target nests inside shared_churn), so union the extents
    # rather than summing lengths.
    covered = _union_bytes([(b["_lo"], b["_hi"]) for b in tables["bands"]])
    n_eng = sum(1 for b in bands if b["engineering"])

    return {
        "file": {
            "path": str(Path(path)),
            "name": Path(path).name,
            "size_bytes": len(data),
            "size_expected": tmx.EXPECTED_SIZE,
            "size_ok": tbw.size_ok,
        },
        "base_map_id": tbw.base_map_id,
        "header": [int(w) for w in tbw.header],
        "header_hex": [f"0x{w:X}" for w in tbw.header],
        "id_ok": tbw.id_ok,
        "valid": tbw.valid,
        "is_my_setup": is_mine,
        "profile_base_map_ids": prof_ids,
        "bands": bands,
        "unknown": unknown,
        "coverage": {
            "file_bytes": len(data),
            "mapped_bytes": covered,
            "mapped_pct": round(100.0 * covered / max(1, len(data)), 1),
            "unmapped_bytes": max(0, len(data) - covered),
        },
        "honesty": {
            "bands_total": len(bands),
            "bands_with_absolute_engineering": n_eng,
            "bands_raw_only": len(bands) - n_eng,
            "statement": (
                "This is a RAW-CELL census, not a decoded tune. {} of {} bands "
                "can be stated in engineering units; the other {} are reported "
                "in raw device units only, because no TMax Tuner ground-truth "
                "cell has been matched to them and their axis/cell order is "
                "unconfirmed. See unknown[] for the per-band reason."
                .format(n_eng, len(bands), len(bands) - n_eng)),
            "axis_mapping": (
                "NO band has a confirmed offset -> (rpm, TPS) mapping. Nothing "
                "here can say what a value is at a given rpm/throttle."),
            "timing_scale": (
                "~49 raw units per degree, MEASURED on the 0x0C3C9 timing-limit "
                "array only (medium confidence). Assumed but UNPROVEN for the "
                "main timing map."),
            "never_writes_tbw": True,
        },
        "warnings": warnings,
    }


def _union_bytes(ranges):
    """Total bytes covered by a set of possibly-overlapping [lo, hi) ranges."""
    total = 0
    cur_lo = cur_hi = None
    for lo, hi in sorted(ranges):
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        total += cur_hi - cur_lo
    return total


# ---------------------------------------------------------------------------
# 2. changes_from_diff — the actual bridge
# ---------------------------------------------------------------------------

def _band_aligned_deltas(a, b, band, start, end):
    """Cell deltas across [start, end) read on the BAND's own record grid.

    WHY NOT JUST thundermax_parser.region_deltas():
      region_deltas() aligns to `start - start % 2` — an even offset relative
      to the file, not to the band's record boundary. Every located band starts
      at an ODD offset (0x01989, 0x01FC9, 0x0C3C9 ...), so on a real tune that
      alignment reads each record shifted by one byte and every delta comes out
      multiplied by 256. Verified on the real global -1 deg pair: the true
      delta in timing_limit_array is -49 in all 128 records, but region_deltas
      reports -12544 (= -49 * 256). A degree figure built on that would have
      been 256x wrong.

    So when we know the band (and therefore its record stride and its record
    origin `band["_lo"]`), we read `band_lo + k*stride` instead, and only fall
    back to region_deltas() for regions no band explains.

    Returns (deltas, alignment_label).
    """
    stride = max(1, int(band.get("stride", 1)))
    width = _cell_width(stride)
    lo = band["_lo"]
    fmt = "<H" if width == 2 else None
    # First record at or before `start`, on the band's own grid.
    first = lo + ((max(start, lo) - lo) // stride) * stride
    out = []
    off = first
    limit = min(end, band["_hi"], len(a.data) - width + 1)
    while off < limit:
        if fmt:
            x = struct.unpack_from(fmt, a.data, off)[0]
            y = struct.unpack_from(fmt, b.data, off)[0]
        else:
            x, y = a.data[off], b.data[off]
        if x != y:
            out.append(y - x)
        off += stride
    return out, (f"band record grid: {band['name']} origin "
                 f"0x{lo:05X}, stride {stride}, {width}-byte LE value")


def _region_delta_stats(a, b, start, end, band=None):
    """Signed-corrected cell-delta statistics across one changed region.

    `band` is a tables.json band dict (with `_lo`/`_hi`) when the region falls
    inside a named band; the deltas are then read on that band's record grid.
    Without it we fall back to thundermax_parser.region_deltas().
    """
    if band is not None:
        raw, alignment = _band_aligned_deltas(a, b, band, start, end)
    else:
        raw = tmx.region_deltas(a, b, start, end)
        alignment = ("thundermax_parser.region_deltas (even file offsets) — no "
                     "band, so the record stride and origin are unknown and "
                     "these deltas may be misaligned")
    wrapped = 0
    fixed = []
    for d in raw:
        v, w = _wrap_i16(d)
        fixed.append(v)
        wrapped += 1 if w else 0
    if not fixed:
        return None
    c = Counter(fixed)
    mode, mode_n = c.most_common(1)[0]
    dominant = [v for v in fixed if (v < 0) == (mode < 0)]
    return {
        "changed_cells": len(fixed),
        "mode": mode,
        "mode_count": mode_n,
        "mode_share": round(mode_n / len(fixed), 3),
        "min": min(fixed),
        "max": max(fixed),
        "mean": round(sum(fixed) / len(fixed), 3),
        "abs_max_in_dominant_direction": max(dominant, key=abs) if dominant else mode,
        "uniform": len(c) == 1,
        "signed_wrap_applied": wrapped,
        "distinct": len(c),
        "alignment": alignment,
    }


def _bands_for_offset(offset):
    """(rpm_band, tps_band, note). Always (None, None, why-not) today."""
    hit = BAND_AXES.get(offset)
    if hit:
        return hit["rpm_band"], hit["tps_band"], "mapped from BAND_AXES"
    return None, None, (
        "rpm_band/tps_band are null: tables.json contains NO offset -> "
        "(rpm, TPS) mapping for any band — every band is flagged "
        "needs_ground_truth on exactly this point. A guessed band would "
        "silently mis-scope the dyno's safety checks, so none is emitted. "
        "virtual_dyno reads a null band as 'applies at every operating "
        "point', which is the conservative reading.")


def _make_change(row, stats, tables):
    """Build the change dict(s) for one classified, non-excluded region.

    Returns a list — a timing-map edit becomes one change per cylinder, because
    the band does not separate front from rear.
    """
    band_name = row["band"]
    scale = KNOWN_SCALES.get(band_name)
    rpm_band, tps_band, band_note = _bands_for_offset(row["offset"])

    raw_mode = stats["mode"]
    direction = "decrease" if raw_mode < 0 else "increase"
    caveats = [band_note]

    # Representative raw delta. A uniform region has exactly one answer. A
    # SHAPED edit does not, so we take the WORST cell in the dominant
    # direction: it over-states the average, which is the safe way to be wrong
    # for a knock or duty check. The full distribution rides along in
    # raw_delta so nothing is hidden.
    if stats["uniform"]:
        raw_repr, mag_basis = raw_mode, "uniform delta (every changed cell equal)"
    else:
        raw_repr = stats["abs_max_in_dominant_direction"]
        mag_basis = ("worst cell in the dominant direction (region is a SHAPED "
                     "edit; mode is {:+d}, mean {:+.1f})"
                     .format(raw_mode, stats["mean"]))

    # `engineering` is the READABLE figure (present whenever a scale exists).
    # `unit`/`magnitude`/`magnitude_confidence` are the DYNO-FACING triple and
    # are engineering units ONLY for a change the dyno may actually model. A
    # band with a known scale that is not a commanded map (the timing CLAMP
    # array) still gets its degrees reported in `engineering`, but goes to the
    # dyno as raw — otherwise virtual_dyno._unit_of() honours the explicit
    # "deg" and applies a clamp change as commanded advance. That bug moved
    # peak torque in an early draft; the invariant below is what stops it.
    dyno_tables = BAND_TO_DYNO_TABLES.get(band_name) if scale else None
    if scale:
        k = scale["raw_per_unit"]
        eng_magnitude = round(abs(raw_repr) / k, 3)
        caveats.append(scale["caveat"])
        scale_out = {
            "raw_per_unit": k, "unit": scale["unit"], "basis": scale["basis"],
            "confidence": scale["confidence"], "source": scale["source"],
        }
        engineering = {
            "magnitude": eng_magnitude,
            "signed_magnitude": (-eng_magnitude if direction == "decrease"
                                 else eng_magnitude),
            "unit": scale["unit"],
            "unit_label": scale["unit_label"],
            "confidence": scale["confidence"],
            "basis": scale["basis"],
            "is_estimate": True,
            "source": scale["source"],
            "caveat": scale["caveat"],
        }
    else:
        eng_magnitude = None
        scale_out = None
        engineering = None
        caveats.append(
            "No raw->engineering scale is known for this band, so the "
            "magnitude is in RAW DEVICE UNITS. The dyno can use the DIRECTION "
            "of this change but not its size, and will skip it when "
            "simulating. tables.json: " + (
                (tables and next((b.get("needs_ground_truth", "")
                                  for b in tables["bands"]
                                  if b["name"] == band_name), "")) or ""))
        if band_name in CONTRADICTED_SCALES:
            caveats.append("SCALE ASSUMPTION TESTED AND REJECTED: "
                           + CONTRADICTED_SCALES[band_name])

    if not stats["uniform"]:
        caveats.append(
            "Region is NOT uniform: {} distinct deltas across {} changed cells "
            "(min {:+d}, max {:+d}, mode {:+d} at {:.0%} of cells). This was a "
            "SHAPED edit, not a flat offset — no single number represents it. "
            "The magnitude above is the worst cell in the dominant direction, "
            "which over-states the typical change on purpose."
            .format(stats["distinct"], stats["changed_cells"], stats["min"],
                    stats["max"], stats["mode"], stats["mode_share"]))
    if stats["signed_wrap_applied"]:
        caveats.append(
            "{} cell delta(s) were re-read as signed 16-bit (0x0000 -> 0xFFxx "
            "borrows); tables.json notes correction cells are SIGNED."
            .format(stats["signed_wrap_applied"]))

    # THE INVARIANT: an engineering unit reaches the dyno only for a band the
    # dyno is allowed to model. Everything else goes across as raw.
    if dyno_tables:
        unit, magnitude, mag_conf = (scale["unit"], eng_magnitude,
                                     scale["confidence"])
    else:
        unit, magnitude, mag_conf = "raw", abs(raw_repr), "unknown"

    common = {
        "rpm_band": rpm_band,
        "tps_band": tps_band,
        "direction": direction,
        "magnitude": magnitude,
        "unit": unit,
        "magnitude_confidence": mag_conf,
        "magnitude_basis": mag_basis,
        "engineering": engineering,
        "raw_representative_delta": raw_repr,
        "band": band_name,
        "band_confidence": row["confidence"],
        "category": row["category"],
        "offset": row["offset"],
        "offset_hex": f"0x{row['offset']:05X}",
        "end": row["end"],
        "length_bytes": row["length"],
        "changed_bytes": row["changed_bytes"],
        "raw_delta": stats,
        "scale": scale_out,
        "band_desc": row["desc"],
        "caveats": caveats,
    }

    if dyno_tables:
        out = []
        for t in dyno_tables:
            ch = dict(common)
            ch["table"] = t
            ch["cylinder"] = "rear" if t.endswith("_rear") else "front"
            ch["dyno_usable"] = True
            ch["cylinder_resolution"] = (
                "INFERRED, not read: the band does not separate front from "
                "rear, so the change is modelled on BOTH cylinders. That is "
                "the conservative reading — the rear jug carries the extra "
                "knock bias in the model.")
            if len(dyno_tables) > 1:
                ch["split_of"] = band_name
            out.append(ch)
        return out

    ch = dict(common)
    ch["table"] = NON_DYNO_TABLE_PREFIX + band_name
    ch["cylinder"] = "both"
    ch["dyno_usable"] = False
    ch["tmax_table_hint"] = _tmax_hint(band_name)
    if scale:
        ch["not_usable_reason"] = (
            "This band's scale IS known — the change reads {:+g} {} (see the "
            "`engineering` field) — but the band is not a COMMANDED map. It is "
            "a clamp/limit or derived array, so there is no guardrails table "
            "for the dyno to drive, and it crosses to the dyno in RAW units so "
            "the dyno cannot mistake a limit for a spark command."
            .format(engineering["signed_magnitude"], engineering["unit"]))
    else:
        ch["not_usable_reason"] = (
            "Magnitude is in RAW device units with no known scale, so "
            "virtual_dyno will IGNORE this change (its _unit_of() returns None "
            "for a '{}' table name). It is reported anyway so the DIRECTION "
            "is visible and so the dyno's answer is known to be incomplete."
            .format(NON_DYNO_TABLE_PREFIX))
    return [ch]


def _tmax_hint(band_name):
    """The TMax Tuner page a band most likely corresponds to — a HINT for the
    UI only. Never used to drive the dyno."""
    hints = {
        "afr_target": "afr_target (AFR Targets vs TPS/RPM) — unconfirmed",
        "afr_ve_pages": "ve_front / ve_rear (fuel-VE pages) — unconfirmed, "
                        "front/rear split unknown",
        "fuel_flow_pages": "fuel_flow_front / fuel_flow_rear — unconfirmed, "
                           "front/rear split unknown",
        "fuel_rich_correction": "AutoTune fuel-learn / VE trim store — "
                                "unconfirmed",
        "timing_limit_array": "an ignition limit/clamp parameter — "
                              "unidentified",
        "timing_map_main": "spark_advance_front / spark_advance_rear",
    }
    return hints.get(band_name)


def changes_from_diff(path_a, path_b, include_autotune=False,
                      include_metadata=False, tables=None):
    """Turn a REAL A->B `.tbw` diff into changes the virtual dyno can consume.

    This answers "what would flashing tune B instead of tune A do on the
    virtual dyno?" — the question that makes the dyno useful at all.

    Args:
      path_a, path_b   : the two tunes (read-only, never written)
      include_autotune : also emit AUTOTUNE-category and learned-data bands.
                         Default False — see CHURN_CATEGORIES.
      include_metadata : also emit METADATA/checksum regions. Default False.

    Returns:
    {
      "changes":   [ change, ... ]     # feed straight to simulate_pull()
      "confidence": "unknown|low|medium|high"
      "unmapped":  [ {offset, ...}, ]  # regions no band explains
      "excluded":  [ {band, category, reason, ...}, ]
      "warnings":  [str, ...]
      "diff_summary": {...}
      "directional_only": bool         # True => believe the SIGN, not the SIZE
      "honesty": {...}
    }

    Each change carries the guardrails vocabulary (`table`, `rpm_band`,
    `tps_band`, `direction`, `magnitude`, `unit`) plus provenance:
    `magnitude_confidence`, `band`, `band_confidence`, `category`, `offset`,
    `raw_delta`, `scale`, `caveats`, `dyno_usable`.
    """
    tables = tables or table_map.load_map()
    a = tmx.TbwFile(path_a)
    b = tmx.TbwFile(path_b)
    warnings = []

    if len(a.data) != len(b.data):
        raise tmx.TbwError("files are different sizes; cannot compare")
    for t in (a, b):
        if not t.size_ok:
            warnings.append(
                f"{t.path.name} is {len(t.data)} bytes, expected "
                f"{tmx.EXPECTED_SIZE} — band offsets may not line up.")
    prof_ids = list((_profile().get("base_map_ids")) or [])
    if prof_ids and a.base_map_id not in prof_ids and b.base_map_id not in prof_ids:
        warnings.append(
            f"Neither base map ('{a.base_map_id}' / '{b.base_map_id}') is one "
            f"of this bike's known base maps ({', '.join(prof_ids)}). The band "
            "map was derived from the bike's own tune family, so the labels "
            "below may not describe this tune family's layout.")
    if a.base_map_id != b.base_map_id:
        warnings.append(
            f"DIFFERENT BASE MAPS: A='{a.base_map_id}' B='{b.base_map_id}'. "
            "These are not two revisions of one tune; table layouts may not "
            "align at all and every band label below may be meaningless. Do "
            "not trust this diff as a change list.")

    rows = table_map.classify_diff(path_a, path_b, tables)

    changes, unmapped, excluded = [], [], []
    cat_bytes, cat_regions = {}, {}
    total_changed = 0

    for row in rows:
        cat = row["category"]
        cat_bytes[cat] = cat_bytes.get(cat, 0) + row["changed_bytes"]
        cat_regions[cat] = cat_regions.get(cat, 0) + 1
        total_changed += row["changed_bytes"]

        band_def = table_map.band_for_offset(row["offset"], tables)
        stats = _region_delta_stats(a, b, row["offset"], row["end"], band_def)
        base = {
            "offset": row["offset"],
            "offset_hex": f"0x{row['offset']:05X}",
            "length_bytes": row["length"],
            "changed_bytes": row["changed_bytes"],
            "band": row["band"],
            "category": cat,
            "confidence": row["confidence"],
            "desc": row["desc"],
            "raw_delta": stats,
        }

        if cat in UNRESOLVED_CATEGORIES or row["band"] is None:
            unmapped.append({**base, "reason": UNRESOLVED_CATEGORIES.get(
                cat, UNRESOLVED_CATEGORIES["UNMAPPED"])})
            continue

        # AUTOTUNE / METADATA churn on every ride and every save. They are not
        # deliberate edits, so they are kept OUT of the dyno changes by default.
        churn_reason = None
        if cat == "METADATA" and not include_metadata:
            churn_reason = CHURN_CATEGORIES["METADATA"]
        elif cat == "AUTOTUNE" and not include_autotune:
            churn_reason = CHURN_CATEGORIES["AUTOTUNE"]
        elif row["band"] in LEARNED_DATA_BANDS and not include_autotune:
            churn_reason = LEARNED_DATA_BANDS[row["band"]]

        if churn_reason:
            excluded.append({**base, "reason": churn_reason,
                             "include_with": ("include_metadata=True"
                                              if cat == "METADATA"
                                              else "include_autotune=True")})
            continue

        if stats is None:
            # Bytes differ but no LE u16 word did (odd-offset single-byte
            # change that region_deltas' even alignment stepped over).
            unmapped.append({**base, "reason": (
                "Region differs but no aligned LE u16 word changed — the edit "
                "does not sit on the 2-byte alignment region_deltas() reads, "
                "so no magnitude can be derived.")})
            continue

        changes.extend(_make_change(row, stats, tables))

    usable = [c for c in changes if c.get("dyno_usable")]
    directional_only = not usable and bool(changes)

    if changes and not usable:
        warnings.append(
            "NO change in this diff has a known engineering scale. The dyno "
            "will simulate a BASELINE pull — the change list is directional "
            "only. Believe the sign, never the size.")
    elif usable and len(usable) < len(changes):
        warnings.append(
            "Only {} of {} changes have a known scale; the other {} are raw "
            "units and the dyno will skip them, so the simulated pull "
            "UNDER-reports what flashing B actually does."
            .format(len(usable), len(changes), len(changes) - len(usable)))
    if any(c["scale"] and c["scale"]["basis"] == "assumed" for c in usable):
        warnings.append(
            "At least one magnitude uses an ASSUMED scale rather than a "
            "measured one. If the assumption is wrong every engineering figure "
            "derived from that band is wrong by the same ratio.")
    if any(c["category"] == "TIMING" and not c["dyno_usable"] for c in changes):
        warnings.append(
            "A TIMING band changed but could not be quantified for the dyno. "
            "To unblock this, run the COLLABORATION.md ground-truth "
            "experiment: in TMax Tuner change EXACTLY ONE map (the ignition "
            "timing map only), save as a new .tbw, then diff it here. A "
            "single-table edit isolates the band from checksum churn, and "
            "reading one known cell in TMax locks both the axis order and the "
            "unit scale.")
    if not rows:
        warnings.append("The two files are byte-identical.")

    # Overall confidence is the WEAKEST link. Scope is 'low' for everything
    # today because no band has a confirmed rpm/TPS mapping.
    if not usable:
        confidence = "unknown"
    else:
        mag = _weakest(*[c["magnitude_confidence"] for c in usable])
        band = _weakest(*[c["band_confidence"] for c in usable])
        confidence = _weakest(mag, band, "low")
    if a.base_map_id != b.base_map_id:
        confidence = "unknown"

    diff_summary = {
        "file_a": Path(path_a).name,
        "file_b": Path(path_b).name,
        "base_map_a": a.base_map_id,
        "base_map_b": b.base_map_id,
        "base_maps_match": a.base_map_id == b.base_map_id,
        "identical": not rows,
        "regions_total": len(rows),
        "changed_bytes_total": total_changed,
        "by_category": {c: {"changed_bytes": cat_bytes[c],
                            "regions": cat_regions[c]}
                        for c in sorted(cat_bytes, key=lambda k: -cat_bytes[k])},
        "regions_as_changes": len(changes),
        "regions_excluded": len(excluded),
        "regions_unmapped": len(unmapped),
        "dyno_usable_changes": len(usable),
    }

    return {
        "changes": changes,
        "confidence": confidence,
        "confidence_reason": (
            "Capped at 'low' while no band has a confirmed offset -> (rpm, TPS) "
            "mapping — the dyno cannot scope a change to an operating region, "
            "only to the whole map. Magnitude confidence is the weakest scale "
            "basis among usable changes."
            if usable else
            "No change in this diff has a known engineering scale, so no "
            "magnitude can be believed at all."),
        "unmapped": unmapped,
        "excluded": excluded,
        "warnings": warnings,
        "diff_summary": diff_summary,
        "directional_only": directional_only,
        "honesty": {
            "axis_mapping_available": bool(BAND_AXES),
            "rpm_tps_bands_are_null": True,
            "null_band_means": ("virtual_dyno._band() reads a null rpm/tps band "
                                "as (-inf, +inf) — the change applies at EVERY "
                                "operating point. Conservative, not precise."),
            "raw_changes_are_ignored_by_the_dyno": True,
            "autotune_metadata_excluded_by_default": not (include_autotune
                                                          and include_metadata),
            "never_writes_tbw": True,
        },
    }


# ---------------------------------------------------------------------------
# 3. self_test — proof the dyno is thinking correctly
# ---------------------------------------------------------------------------

def _check(name, passed, expected, got, detail, severity="critical"):
    return {"name": name, "passed": bool(passed), "expected": expected,
            "got": got, "detail": detail, "severity": severity}


def _close(a, b, tol):
    return abs(float(a) - float(b)) <= tol


def _spark_change(mag, direction, rpm_lo, rpm_hi, table="spark_advance_front"):
    return {"table": table, "cylinder": "both",
            "rpm_band": [rpm_lo, rpm_hi], "tps_band": [80, 100],
            "direction": direction, "magnitude": mag, "unit": "deg"}


def _ve_change(mag, direction, rpm_lo, rpm_hi, table="ve_front"):
    return {"table": table, "cylinder": "both",
            "rpm_band": [rpm_lo, rpm_hi], "tps_band": [80, 100],
            "direction": direction, "magnitude": mag, "unit": "ve_pct"}


# -- known-answer: injector duty, recomputed from first principles -----------

def _independent_duty_pct(rpm, ve, afr, displacement_ci, cylinders, flow_gps,
                          baro_inhg, air_temp_f):
    """Injector duty recomputed from scratch — deliberately NOT calling any
    virtual_dyno helper, so the comparison is a real check and not a tautology.

    Physical constants written out literally:
      1 cubic inch      = 2.54^3 = 16.387064 cm^3
      R (dry air)       = 287.058 J/(kg*K)
      1 inHg            = 3386.389 Pa
      degF -> K         : (F - 32) * 5/9 + 273.15
      4-stroke intake events per cylinder per second = rpm / 120

      rho[kg/m^3]  = P / (R * T)
      air g/s/cyl  = cc_per_cyl * VE * rho[g/cm^3] * rpm/120
      fuel g/s     = air / AFR
      duty %       = fuel / injector_flow * 100        (capped at 100)
    """
    cc_per_cyl = float(displacement_ci) * (2.54 ** 3) / float(cylinders)
    t_k = (float(air_temp_f) - 32.0) * 5.0 / 9.0 + 273.15
    p_pa = float(baro_inhg) * 3386.389
    rho_kg_m3 = p_pa / (287.058 * t_k)
    rho_g_cm3 = rho_kg_m3 / 1000.0
    air_gps = cc_per_cyl * float(ve) * rho_g_cm3 * float(rpm) / 120.0
    fuel_gps = air_gps / float(afr)
    return min(100.0, fuel_gps / float(flow_gps) * 100.0)


def _check_injector_duty_closed_form(cal):
    eng = cal["engine"]
    cond = vd.merge_conditions(None)
    air_f = cond["iat_f"] if cond["iat_f"] is not None else (
        cond["ambient_f"] + cond["iat_rise_f"])
    flow = float(cal["injectors"]["flow_gps"])
    worst = 0.0
    rows = []
    for rpm, ve, afr in ((3000, 0.90, 12.7), (5000, 0.95, 12.7),
                         (5500, 1.02, 13.2), (2500, 0.80, 14.6)):
        mine = _independent_duty_pct(rpm, ve, afr, eng["displacement_ci"],
                                     eng["cylinders"], flow,
                                     cond["baro_inhg"], air_f)
        theirs = vd.injector_duty_pct(rpm, ve, afr, cond)
        worst = max(worst, abs(mine - theirs))
        rows.append(f"{rpm}rpm/VE{ve}/AFR{afr}: mine {mine:.6f}% vs dyno "
                    f"{theirs:.6f}%")
    return _check(
        "known_answer.injector_duty_closed_form", worst < 1e-9,
        "independent mass-flow arithmetic matches virtual_dyno.injector_duty_pct "
        "to < 1e-9 %",
        f"max deviation {worst:.3e} %",
        ("Recomputed inside this check from literal physical constants "
         "(2.54^3 cm^3/ci, R=287.058, 3386.389 Pa/inHg, rpm/120 intake events) "
         "using {} g/s injectors. ".format(flow) + " | ".join(rows)))


def _check_injector_flow_matches_profile(cal):
    prof = _profile()
    prof_inj = prof.get("injectors")
    # bike_profile.json may hold a plain-text injector note (e.g. "6.3 (current) ...")
    # instead of a {"flow_gps": ...} object; treat that as "no flow declared".
    prof_flow = prof_inj.get("flow_gps") if isinstance(prof_inj, dict) else None
    used = cal["injectors"].get("flow_gps")
    src = cal["injectors"].get("from") or cal["injectors"].get("source")
    ok = (prof_flow is not None and used is not None
          and abs(float(used) - float(prof_flow)) < 1e-9)
    return _check(
        "consistency.injector_flow_matches_bike_profile", ok,
        f"injector flow == bike_profile.json ({prof_flow} g/s)",
        f"{used} g/s (from: {src})",
        ("Duty scales INVERSELY with injector flow, so a stale value mis-reads "
         "every simulated pull. dyno_baseline.json still carries the superseded "
         "5.5 g/s from base map ZZSSQXETDN100720; bike_profile.json is the "
         "hardware of record (S&S 550 / CAM2, base map HXSSEDCAAN061617) and "
         "must win. Source in use: {}".format(src)))


def _check_published_anchors(cal):
    tq_anchor = cal["baseline"]["anchor_peak_torque"]
    hp_anchor = cal["baseline"]["anchor_peak_hp"]
    tq = vd.baseline_torque(float(tq_anchor["rpm"]))
    hp = (vd.baseline_torque(float(hp_anchor["rpm"])) * float(hp_anchor["rpm"])
          / vd.HP_TORQUE_CONST)
    base = vd.simulate_pull([])
    s = base["summary"]
    ok = (_close(tq, tq_anchor["value"], 0.05)
          and _close(hp, hp_anchor["value"], 0.5)
          and _close(s["peak_torque"], tq_anchor["value"], 1.0)
          and _close(s["peak_hp"], hp_anchor["value"], 1.0)
          and abs(s["peak_torque_rpm"] - tq_anchor["rpm"]) <= 150
          and abs(s["peak_hp_rpm"] - hp_anchor["rpm"]) <= 300)
    return _check(
        "known_answer.published_anchors_on_the_baseline_curve", ok,
        f"~{tq_anchor['value']} lb-ft @ {tq_anchor['rpm']} and "
        f"~{hp_anchor['value']} hp @ {hp_anchor['rpm']}",
        (f"curve: {tq:.2f} lb-ft @ {tq_anchor['rpm']}, {hp:.2f} hp @ "
         f"{hp_anchor['rpm']} | baseline pull peaks: {s['peak_torque']} lb-ft @ "
         f"{s['peak_torque_rpm']}, {s['peak_hp']} hp @ {s['peak_hp_rpm']}"),
        ("The PCHIP curve must pass through the published Screamin' Eagle 131 "
         "anchors, and an actual baseline pull must land on them too. Both "
         "anchors are flagged needs_confirmation — they are chart values, not "
         "a dyno sheet for THIS bike."))


def _check_hp_torque_identity():
    base = vd.simulate_pull([])
    worst, at = 0.0, None
    for f in base["samples"]:
        expect = f["torque"] * f["rpm"] / 5252.0
        d = abs(expect - f["hp"])
        if d > worst:
            worst, at = d, f["rpm"]
    return _check(
        "known_answer.hp_equals_torque_times_rpm_over_5252", worst <= 0.11,
        "every frame satisfies hp = lb-ft * rpm / 5252 within rounding (0.05 "
        "on hp + 0.05 on torque -> 0.11 tolerance)",
        f"max deviation {worst:.4f} hp at {at} rpm",
        "Frames are published rounded to 0.1, so the tolerance is the rounding, "
        "not slack in the model.")


def _check_zero_change_zero_delta():
    res = vd.simulate_pull([])
    s = res["summary"]
    ok = (s["delta_hp_range"] == [0, 0] and s["delta_torque_range"] == [0, 0]
          and s["peak_hp"] == s["baseline_peak_hp"]
          and s["peak_torque"] == s["baseline_peak_torque"]
          and s["peak_injector_duty_pct"] == s["baseline_peak_injector_duty_pct"]
          and s["max_knock_risk"] == s["baseline_max_knock_risk"])
    return _check(
        "invariant.zero_changes_zero_delta", ok,
        "delta_hp_range == [0,0], delta_torque_range == [0,0], every peak "
        "identical to baseline",
        (f"delta_hp_range={s['delta_hp_range']} "
         f"delta_torque_range={s['delta_torque_range']} "
         f"peak_hp={s['peak_hp']}/{s['baseline_peak_hp']} "
         f"peak_tq={s['peak_torque']}/{s['baseline_peak_torque']}"),
        ("The identity property. AFR and timing enter as RATIOS against the "
         "baseline state, so with no changes both ratios are exactly 1.0 and "
         "the model must reproduce the published curve bit-for-bit."))


def _check_duty_monotonicity(cal):
    cond = vd.merge_conditions(None)
    d_lo_rpm = vd.injector_duty_pct(3000, 0.95, 12.7, cond)
    d_hi_rpm = vd.injector_duty_pct(5000, 0.95, 12.7, cond)
    d_lo_ve = vd.injector_duty_pct(4000, 0.80, 12.7, cond)
    d_hi_ve = vd.injector_duty_pct(4000, 1.00, 12.7, cond)
    d_rich = vd.injector_duty_pct(4000, 0.95, 12.0, cond)
    d_lean = vd.injector_duty_pct(4000, 0.95, 14.0, cond)
    ok = (d_hi_rpm > d_lo_rpm and d_hi_ve > d_lo_ve and d_rich > d_lean)
    return _check(
        "invariant.duty_rises_with_rpm_and_ve_and_inversely_with_afr", ok,
        "duty(5000) > duty(3000); duty(VE 1.00) > duty(VE 0.80); "
        "duty(AFR 12.0) > duty(AFR 14.0)",
        (f"rpm {d_lo_rpm:.2f} -> {d_hi_rpm:.2f} | "
         f"VE {d_lo_ve:.2f} -> {d_hi_ve:.2f} | "
         f"AFR12.0 {d_rich:.2f} vs AFR14.0 {d_lean:.2f}"),
        ("Duty is proportional to rpm and VE and INVERSELY proportional to the "
         "AFR value: richer means a LOWER AFR number, more fuel, and therefore "
         "HIGHER duty. If this ever inverts, the duty gauge is lying about "
         "injector headroom."))


def _check_afr_curve_continuous():
    eps = 1e-6
    knots = (12.2, 12.6, 13.0, 13.2)
    worst, at = 0.0, None
    for k in knots:
        jump = abs(vd.afr_power_factor(k - eps) - vd.afr_power_factor(k + eps))
        if jump > worst:
            worst, at = jump, k
    lo_p, hi_p = vd.AFR_PLATEAU
    # Sweep for the argmax.
    best_afr, best_f = None, -1.0
    a = 11.0
    while a <= 14.5 + 1e-9:
        f = vd.afr_power_factor(a)
        if f > best_f + 1e-12:
            best_f, best_afr = f, a
        a = round(a + 0.01, 4)
    peaks_on_plateau = (lo_p - 1e-9) <= best_afr <= (hi_p + 1e-9) and \
        abs(best_f - 1.0) < 1e-12
    ok = worst < 1e-5 and peaks_on_plateau
    return _check(
        "invariant.afr_power_curve_continuous_and_peaks_on_plateau", ok,
        f"no discontinuity > 1e-5 at {knots}; argmax inside the "
        f"{lo_p}-{hi_p} plateau at factor 1.0",
        f"max jump {worst:.3e} at AFR {at}; argmax AFR {best_afr} -> {best_f}",
        ("A kink or a step in this curve would make the dyno report a torque "
         "cliff for a fuel change of a hundredth of an AFR point. The plateau "
         "must be the maximum, so the model never pays torque for running "
         "outside the shop's best-torque band."))


def _check_past_mbt_no_gain():
    # Timing factor: at load, advance past MBT buys nothing.
    f_at = vd.timing_power_factor(0.0, 100.0)
    f_past = vd.timing_power_factor(4.0, 100.0)
    f_low_load = vd.timing_power_factor(4.0, 5.0)
    # End to end: +2 deg in a band that already sits at model-MBT+2.
    ch = [_spark_change(2.0, "increase", 4600, 5800)]
    res = vd.simulate_pull(ch)
    s = res["summary"]
    base_knock = s["baseline_max_knock_risk"]
    ok = (_close(f_at, 1.0, 1e-12) and _close(f_past, 1.0, 1e-12)
          and f_low_load < 1.0
          and s["delta_hp_range"] == [0, 0] and s["delta_torque_range"] == [0, 0]
          and s["max_knock_risk"] > base_knock)
    return _check(
        "invariant.advance_past_mbt_gains_no_torque_but_raises_knock", ok,
        "timing_power_factor(+4 deg, 100% TPS) == 1.0 (no gain); a +2 deg "
        "high-rpm WOT change yields delta [0,0] hp/lb-ft while max_knock_risk "
        "rises above baseline",
        (f"factor at MBT {f_at:.6f}, +4 deg at WOT {f_past:.6f}, +4 deg at 5% "
         f"TPS {f_low_load:.6f} | delta_hp={s['delta_hp_range']} "
         f"delta_tq={s['delta_torque_range']} knock "
         f"{base_knock} -> {s['max_knock_risk']}"),
        ("The single most important asymmetry in the model: above "
         f"{vd.KNOCK_LIMITED_TPS:.0f}% TPS the cylinder is knock-limited, so "
         "extra advance must buy detonation, never power. At light load "
         "over-advance IS penalised, because a lightly-loaded cylinder really "
         "does lose torque when the burn peaks too early."))


def _check_all_issues_are_warn():
    scenarios = [
        ("baseline", []),
        ("lean +8% VE removed", [_ve_change(8.0, "decrease", 2500, 5800)]),
        ("rich +8% VE added", [_ve_change(8.0, "increase", 2500, 5800)]),
        ("+6 deg spark", [_spark_change(6.0, "increase", 2500, 5800)]),
        ("hot start", []),
    ]
    bad, total = [], 0
    for label, ch in scenarios:
        cond = {"cht_f": 300.0} if label == "hot start" else None
        for it in vd.simulate_pull(ch, cond)["issues"]:
            total += 1
            if it["severity"] != "warn":
                bad.append(f"{label}: {it['code']} -> {it['severity']}")
    return _check(
        "invariant.every_dyno_issue_is_severity_warn", not bad and total > 0,
        "every issue across 5 scenarios (including deliberately unsafe ones) "
        "has severity 'warn', and at least one issue was produced",
        f"{total} issues, {len(bad)} non-warn" + (f": {bad}" if bad else ""),
        ("AUTHORITY SPLIT. A simulated pull can never block and never clear a "
         "proposal — only guardrails.check_change() can hard-block, on the "
         "proposed steady-state values. If the dyno ever emits a 'block' it "
         "has taken authority it does not have."))


def _check_authority_split():
    ch = _ve_change(8.0, "decrease", 2500, 5800)
    dyno_sev = {i["severity"] for i in vd.simulate_pull([ch])["issues"]}
    verdict = G.check_proposal([ch])
    ok = dyno_sev <= {"warn"} and verdict["blocks"] > 0 and not verdict["passed"]
    return _check(
        "invariant.guardrails_blocks_what_the_dyno_only_warns_about", ok,
        "the same -8% VE change: dyno severities == {'warn'}, "
        "guardrails.check_proposal() blocks > 0",
        f"dyno severities {sorted(dyno_sev)}; guardrails blocks="
        f"{verdict['blocks']} passed={verdict['passed']}",
        ("Proof the two halves keep their roles: the dyno advises, guardrails "
         "decides. -8% VE is past the provisional +/-5% hard limit."))


def _check_integer_delta_ranges():
    ch = [_ve_change(3.0, "increase", 3000, 4500)]
    s = vd.simulate_pull(ch)["summary"]
    pat = re.compile(r"^(≈ [+-]?\d+ (hp|lb-ft)|≈ [+-]\d+ to [+-]\d+ (hp|lb-ft)"
                     r"|≈ 0 (hp|lb-ft) \(no change\))$")
    ok = (bool(pat.match(s["delta_hp"])) and bool(pat.match(s["delta_torque"]))
          and all(isinstance(v, int) for v in s["delta_hp_range"])
          and all(isinstance(v, int) for v in s["delta_torque_range"]))
    return _check(
        "invariant.summary_deltas_are_integer_ranges_only", ok,
        "delta_hp / delta_torque are integer-range strings with no decimal "
        "point, and delta_*_range entries are ints",
        f"delta_hp={s['delta_hp']!r} range={s['delta_hp_range']} | "
        f"delta_torque={s['delta_torque']!r} range={s['delta_torque_range']}",
        ("The model is +/-{}% — it is not accurate enough to justify a decimal "
         "point, so it is not allowed to print one.".format(
             s["uncertainty_pct"])))


def _check_calibration_honesty(cal):
    status = vd.simulate_pull([])["baseline_status"]
    needs = status.get("needs_confirmation") or []
    ok = (isinstance(status.get("uncertainty_pct"), int)
          and status["uncertainty_pct"] > 0
          and bool(str(status.get("calibration_status", "")).strip())
          and bool(str(status.get("banner", "")).strip())
          and len(needs) > 0
          and status.get("llm_involved") is False
          and status.get("deterministic") is True)
    return _check(
        "honesty.calibration_status_is_surfaced", ok,
        "baseline_status carries uncertainty_pct, a calibration_status string, "
        "the DIRECTIONAL-ONLY banner, a non-empty needs_confirmation list, "
        "deterministic=True and llm_involved=False",
        (f"uncertainty=+/-{status.get('uncertainty_pct')}%, "
         f"{len(needs)} needs_confirmation entries, "
         f"deterministic={status.get('deterministic')}, "
         f"llm_involved={status.get('llm_involved')}"),
        "Unconfirmed calibration: " + "; ".join(needs) if needs else
        "NO calibration item is flagged unconfirmed — that is itself suspicious "
        "for an UNCALIBRATED model.",
        severity="warn" if ok else "critical")


def _check_scale_knowledge_declared():
    """The bridge must not have quietly grown a scale factor nobody measured."""
    bad = [n for n, s in KNOWN_SCALES.items()
           if s["basis"] not in ("measured", "assumed") or not s.get("source")]
    absolute_not_measured = [n for n in ABSOLUTE_SCALE_BANDS
                             if KNOWN_SCALES.get(n, {}).get("basis") != "measured"]
    readopted = sorted(set(KNOWN_SCALES) & set(CONTRADICTED_SCALES))
    unmodellable = sorted(n for n in BAND_TO_DYNO_TABLES if n not in KNOWN_SCALES)
    ok = (not bad and not absolute_not_measured and not BAND_AXES
          and not readopted and not unmodellable)
    return _check(
        "honesty.tbw_scale_knowledge_is_declared_and_bounded", ok,
        "every KNOWN_SCALES entry has a basis of 'measured' or 'assumed' plus a "
        "source; every ABSOLUTE_SCALE_BANDS entry is 'measured'; no band is in "
        "both KNOWN_SCALES and CONTRADICTED_SCALES; every dyno-modelled band "
        "has a known scale; BAND_AXES is empty (no fabricated rpm/TPS mapping)",
        (f"{len(KNOWN_SCALES)} declared scale(s) "
         f"({', '.join(sorted(KNOWN_SCALES))}); "
         f"absolute-capable: {sorted(ABSOLUTE_SCALE_BANDS)}; "
         f"rejected assumptions: {sorted(CONTRADICTED_SCALES)}; "
         f"dyno-modelled bands: {sorted(BAND_TO_DYNO_TABLES)}; "
         f"BAND_AXES entries: {len(BAND_AXES)}"),
        ("Guards the honesty constraint at runtime. If someone adds an "
         "offset -> rpm/TPS guess to BAND_AXES, promotes an assumed scale to "
         "absolute, re-adopts a scale the data rejected, or wires a band into "
         "the dyno without a scale, this check fails before the number reaches "
         "Joshua."))


def self_test():
    """Run the dyno's physics through known-answer and invariant checks.

    Callable from HTTP and from the CLI, not just pytest. Pure arithmetic: no
    NAS, no Ollama, no network, no `.tbw` file needed.

    Returns:
    {
      "passed": bool,                       # no critical check failed
      "checks": [ {name, passed, expected, got, detail, severity}, ... ],
      "summary": {total, passed, failed, critical_failed, warn_failed, verdict},
      "calibration": {...}                  # how much to trust the numbers
    }
    """
    cal = vd.load_baseline()
    checks = [
        _check_injector_duty_closed_form(cal),
        _check_published_anchors(cal),
        _check_hp_torque_identity(),
        _check_zero_change_zero_delta(),
        _check_duty_monotonicity(cal),
        _check_afr_curve_continuous(),
        _check_past_mbt_no_gain(),
        _check_all_issues_are_warn(),
        _check_authority_split(),
        _check_integer_delta_ranges(),
        _check_injector_flow_matches_profile(cal),
        _check_calibration_honesty(cal),
        _check_scale_knowledge_declared(),
    ]

    failed = [c for c in checks if not c["passed"]]
    crit = [c for c in failed if c["severity"] == "critical"]
    warn = [c for c in failed if c["severity"] != "critical"]
    passed = not crit

    status = vd.simulate_pull([])["baseline_status"]
    inj = cal["injectors"]

    return {
        "passed": passed,
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "critical_failed": len(crit),
            "warn_failed": len(warn),
            "failed_names": [c["name"] for c in failed],
            "verdict": ("PASS — the dyno's arithmetic is self-consistent with "
                        "its own documented physics and with bike_profile.json."
                        if passed else
                        "FAIL — " + "; ".join(c["name"] for c in crit)),
            "scope_note": ("Self-consistency only. These checks prove the model "
                           "computes what it claims to compute; they cannot "
                           "prove the model matches the real engine. Nothing "
                           "here replaces a validation ride or a real dyno."),
        },
        "calibration": {
            "uncertainty_pct": status["uncertainty_pct"],
            "calibration_status": status["calibration_status"],
            "banner": status["banner"],
            "needs_confirmation": status["needs_confirmation"],
            "injector_flow_gps": inj.get("flow_gps"),
            "injector_flow_unit": inj.get("unit"),
            "injector_flow_source": inj.get("from") or inj.get("source"),
            "injector_flow_note": inj.get("note"),
            "deterministic": status["deterministic"],
            "llm_involved": status["llm_involved"],
            "tbw_scale_knowledge": {
                "known_scales": {
                    n: {"raw_per_unit": s["raw_per_unit"], "unit": s["unit"],
                        "basis": s["basis"], "confidence": s["confidence"]}
                    for n, s in KNOWN_SCALES.items()},
                "absolute_capable_bands": sorted(ABSOLUTE_SCALE_BANDS),
                "contradicted_scales": dict(CONTRADICTED_SCALES),
                "bands_the_dyno_can_model": sorted(BAND_TO_DYNO_TABLES),
                "offset_to_rpm_tps_mapping": "NONE — no band has one",
                "statement": (
                    "From a real `.tbw` the dyno can read: raw cell "
                    "distributions per named band, which bands a change "
                    "touched, and the DIRECTION of every change. It can size "
                    "exactly one band in engineering units (timing_limit_array "
                    "at 49 raw/deg) and that band is a clamp, not a commanded "
                    "map — so today NO real tune diff produces a change the "
                    "dyno can simulate. It cannot read an absolute AFR, VE or "
                    "fuel value out of a `.tbw`, and it cannot say which "
                    "rpm/TPS cell anything belongs to."),
            },
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_selftest(res):
    out = []
    out.append("VIRTUAL DYNO SELF-TEST — " + ("PASS" if res["passed"] else "FAIL"))
    out.append("=" * 72)
    for c in res["checks"]:
        mark = "PASS" if c["passed"] else ("FAIL" if c["severity"] == "critical"
                                           else "WARN")
        out.append(f"[{mark}] {c['name']}")
        out.append(f"       expected: {c['expected']}")
        out.append(f"       got:      {c['got']}")
        out.append(f"       why:      {c['detail']}")
    s = res["summary"]
    out.append("-" * 72)
    out.append(f"{s['passed']}/{s['total']} checks passed "
               f"({s['critical_failed']} critical failures)")
    out.append(s["verdict"])
    out.append(s["scope_note"])
    cal = res["calibration"]
    out.append("")
    out.append(f"CALIBRATION: +/-{cal['uncertainty_pct']}%  |  injectors "
               f"{cal['injector_flow_gps']} {cal['injector_flow_unit']} "
               f"(source: {cal['injector_flow_source']})")
    out.append(cal["calibration_status"])
    out.append(cal["banner"])
    if cal["needs_confirmation"]:
        out.append("Unconfirmed calibration inputs:")
        for n in cal["needs_confirmation"]:
            out.append(f"  - {n}")
    out.append("")
    out.append("WHAT THE DYNO CAN READ OUT OF A .tbw:")
    out.append("  " + cal["tbw_scale_knowledge"]["statement"])
    return "\n".join(out)


def _fmt_read(res):
    out = []
    f = res["file"]
    out.append(f"TUNE: {f['name']}")
    out.append(f"  base map ID : {res['base_map_id']}"
               + ("  (this bike's setup)" if res["is_my_setup"]
                  else "  (NOT a known base map for this bike)"))
    out.append(f"  size        : {f['size_bytes']} bytes "
               + ("(expected)" if f["size_ok"] else "*** UNEXPECTED ***"))
    out.append(f"  header      : {' '.join(res['header_hex'])}")
    out.append(f"  valid       : {res['valid']}")
    out.append(f"  band coverage: {res['coverage']['mapped_bytes']} of "
               f"{res['coverage']['file_bytes']} bytes "
               f"({res['coverage']['mapped_pct']}%) fall in a named band")
    out.append("")
    out.append("RAW CELL CENSUS PER NAMED BAND (raw device units)")
    hdr = (f"{'band':24} {'cat':9} {'conf':6} {'cells':>6} {'min':>7} "
           f"{'max':>7} {'mean':>9} {'mode':>7}")
    out.append(hdr)
    out.append("-" * len(hdr))
    for b in res["bands"]:
        c = b["raw_cells"]
        out.append(f"{b['name']:24} {b['category']:9} {b['confidence']:6} "
                   f"{c['count']:>6} {str(c['min']):>7} {str(c['max']):>7} "
                   f"{str(c['mean']):>9} {str(c['mode']):>7}")
    out.append("")
    eng = [b for b in res["bands"] if b["engineering"]]
    if eng:
        out.append("ENGINEERING-UNIT ESTIMATES (only where a scale was MEASURED)")
        for b in eng:
            e = b["engineering"]
            out.append(f"  {b['name']}: {e['min']} .. {e['max']} "
                       f"{e['unit_label']} (mean {e['mean']}, mode {e['mode']}) "
                       f"at {e['scale_raw_per_unit']:.0f} raw/{e['unit']}, "
                       f"{e['confidence']} confidence, basis '{e['scale_basis']}'")
            out.append(f"      {e['note']}")
            out.append(f"      caveat: {e['caveat']}")
    else:
        out.append("ENGINEERING-UNIT ESTIMATES: none available for this file.")
    out.append("")
    out.append(f"UNKNOWN — cannot be stated in engineering units "
               f"({len(res['unknown'])} of {res['honesty']['bands_total']} bands):")
    for u in res["unknown"]:
        out.append(f"  - {u['band']} [{u['category']}, {u['confidence']}]")
        out.append(f"      {u['reason']}")
        if u["needs_ground_truth"]:
            out.append(f"      to close it: {u['needs_ground_truth']}")
    out.append("")
    out.append("HONESTY: " + res["honesty"]["statement"])
    out.append("         " + res["honesty"]["axis_mapping"])
    out.append("         " + res["honesty"]["timing_scale"])
    for w in res["warnings"]:
        out.append(f"WARNING: {w}")
    return "\n".join(out)


def _fmt_compare(res, sim=None):
    out = []
    d = res["diff_summary"]
    out.append(f"DIFF: {d['file_a']}  ->  {d['file_b']}")
    out.append(f"  base maps   : {d['base_map_a']} -> {d['base_map_b']} "
               + ("(match)" if d["base_maps_match"] else "*** DIFFERENT ***"))
    out.append(f"  changed     : {d['changed_bytes_total']} bytes across "
               f"{d['regions_total']} regions")
    for cat, v in d["by_category"].items():
        out.append(f"      {cat:10} {v['changed_bytes']:>7} bytes in "
                   f"{v['regions']} regions")
    out.append(f"  -> {d['regions_as_changes']} change(s), "
               f"{d['dyno_usable_changes']} usable by the dyno; "
               f"{d['regions_excluded']} excluded, {d['regions_unmapped']} unmapped")
    out.append(f"  overall confidence: {res['confidence'].upper()}"
               + ("  [DIRECTIONAL ONLY — believe the sign, not the size]"
                  if res["directional_only"] else ""))
    out.append(f"      {res['confidence_reason']}")
    out.append("")
    if res["changes"]:
        out.append("DERIVED CHANGES")
        for c in res["changes"]:
            sign = -1 if c["direction"] == "decrease" else 1
            mag = (f"{sign * c['magnitude']:+g}" if c["unit"] != "raw"
                   else f"{sign * int(c['magnitude']):+d}")
            out.append(f"  {c['offset_hex']} {c['band']:24} -> table "
                       f"{c['table']}")
            out.append(f"      {c['direction']}  {mag} {c['unit']}  "
                       f"(magnitude confidence: {c['magnitude_confidence']}, "
                       f"band confidence: {c['band_confidence']})")
            if c.get("engineering"):
                e = c["engineering"]
                out.append(f"      reads as {e['signed_magnitude']:+g} "
                           f"{e['unit_label']} at {e['confidence']} confidence "
                           f"(basis '{e['basis']}', estimate)")
            out.append(f"      rpm_band={c['rpm_band']} tps_band={c['tps_band']} "
                       f"dyno_usable={c['dyno_usable']}")
            rd = c["raw_delta"]
            out.append(f"      raw delta: mode {rd['mode']:+d} over "
                       f"{rd['changed_cells']} cells "
                       f"(min {rd['min']:+d}, max {rd['max']:+d}, "
                       f"{'uniform' if rd['uniform'] else str(rd['distinct']) + ' distinct'})")
            for cv in c["caveats"]:
                out.append(f"      ! {cv}")
    else:
        out.append("DERIVED CHANGES: none.")
    if res["excluded"]:
        out.append("")
        out.append("EXCLUDED (churn, not deliberate edits)")
        for e in res["excluded"]:
            out.append(f"  {e['offset_hex']} {e['band']} [{e['category']}] "
                       f"{e['changed_bytes']} bytes")
            out.append(f"      {e['reason']}")
            out.append(f"      include with: {e['include_with']}")
    if res["unmapped"]:
        out.append("")
        out.append("UNMAPPED (no band explains these bytes)")
        for u in res["unmapped"][:20]:
            out.append(f"  {u['offset_hex']} {u['changed_bytes']} bytes "
                       f"[{u['category']}] {u['reason']}")
        if len(res["unmapped"]) > 20:
            out.append(f"  ... and {len(res['unmapped']) - 20} more")
    for w in res["warnings"]:
        out.append(f"WARNING: {w}")

    if sim is not None:
        s = sim["summary"]
        out.append("")
        out.append("VIRTUAL DYNO PULL WITH THESE CHANGES")
        out.append("  " + s["banner"])
        out.append(f"  gear {s['gear']} · {s['pull_seconds']}s · "
                   f"{s['rpm_range'][0]}-{s['rpm_range'][1]} rpm")
        out.append(f"  peak {s['peak_hp']} hp @ {s['peak_hp_rpm']} · "
                   f"{s['peak_torque']} lb-ft @ {s['peak_torque_rpm']}")
        out.append(f"  delta vs baseline: {s['delta_hp']} / {s['delta_torque']}")
        out.append(f"  peak injector duty {s['peak_injector_duty_pct']}% "
                   f"(baseline {s['baseline_peak_injector_duty_pct']}%) · "
                   f"max knock index {s['max_knock_risk']} "
                   f"(baseline {s['baseline_max_knock_risk']})")
        for i in sim["issues"]:
            out.append(f"    [{i['severity']}] t={i['t']}s {i['code']}: "
                       f"{i['message']}")
        if res["directional_only"]:
            out.append("  NOTE: no change had a known scale, so this pull is "
                       "the BASELINE pull. The dyno could not quantify this "
                       "diff.")
    return "\n".join(out)


def _main(argv=None):
    p = argparse.ArgumentParser(
        description="Bridge real .tbw tunes into the virtual dyno "
                    "(read-only; never writes a .tbw).")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="prove the dyno's physics is self-consistent")

    pr = sub.add_parser("read", help="what the dyno can see in one tune")
    pr.add_argument("tune")

    pc = sub.add_parser("compare", help="derive dyno changes from an A->B diff "
                                        "and run the pull")
    pc.add_argument("tune_a")
    pc.add_argument("tune_b")
    pc.add_argument("--include-autotune", action="store_true",
                    help="also emit AutoTune/learned-data bands (they churn on "
                         "every ride and are not deliberate edits)")
    pc.add_argument("--include-metadata", action="store_true",
                    help="also emit metadata/checksum regions")
    pc.add_argument("--no-sim", action="store_true",
                    help="derive the changes but do not run the dyno")

    a = p.parse_args(argv)

    if a.cmd == "selftest":
        res = self_test()
        print(json.dumps(res, indent=2) if a.json else _fmt_selftest(res))
        return 0 if res["passed"] else 1

    if a.cmd == "read":
        res = read_tune(a.tune)
        print(json.dumps(res, indent=2) if a.json else _fmt_read(res))
        return 0 if res["valid"] else 1

    if a.cmd == "compare":
        res = changes_from_diff(a.tune_a, a.tune_b,
                                include_autotune=a.include_autotune,
                                include_metadata=a.include_metadata)
        sim = None if a.no_sim else vd.simulate_pull(res["changes"])
        if a.json:
            print(json.dumps({"bridge": res, "pull": sim}, indent=2))
        else:
            print(_fmt_compare(res, sim))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except BrokenPipeError:
        raise SystemExit(0)
    except (tmx.TbwError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)
