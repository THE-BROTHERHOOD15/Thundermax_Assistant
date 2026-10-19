#!/usr/bin/env python3
"""Analysis half of the TBW one-cell ground-truth experiment.

COLLABORATION.md's top open task: per-cell engineering-unit scaling and the
offset -> (rpm, TPS) axis mapping are UNCONFIRMED for essentially every band in
`tables.json`. `dyno_bridge.BAND_AXES` is deliberately empty and a self-check
FAILS if anyone fills it with a guess, so today no real tune diff produces a
change the virtual dyno can size.

The experiment that closes it is run by hand, in TMax Tuner, on Windows, by
Joshua. He never flashes anything; he only edits maps and saves files:

    GT_A.tbw   baseline opened and saved with NO edits
    GT_A2.tbw  a SECOND no-edit save          (optional but strongly advised --
               see "the control save" below; without it, save-to-save churn
               cannot be separated from the edit)
    GT_B.tbw   exactly ONE cell changed, at a known (rpm, TPS), old -> new
    GT_C.tbw   the SAME cell changed again to a different value  (linearity)
    GT_D.tbw   a DIFFERENT single cell, far corner                (axis order)
    GT_E.tbw   optional whole-map uniform offset                  (band extent)

This module reads those saves and answers four questions, in this order of
increasing fragility:

    1. WHICH BYTES MOVED           -- and was the edit isolated, or dirty?
    2. WHAT IS THE SCALE           -- raw units per engineering unit (a DELTA
                                      scale; survives an unknown axis order,
                                      an unknown cell offset inside the record,
                                      and an unknown zero point)
    3. WHERE IS ZERO               -- what turns a DELTA scale into an ABSOLUTE
                                      one. Depends additionally on the field
                                      width and anchor being right, so it is
                                      reported per candidate width.
    4. WHAT IS THE AXIS ORDER      -- the most fragile result, and the one that
                                      silently mis-scopes every safety check if
                                      it is wrong. Emitted ONLY when forced.

HONESTY RULES (the same posture dyno_bridge.py already enforces at runtime)
--------------------------------------------------------------------------
  * A scale derived from ONE edit is a 2-point affine fit with NO test of
    linearity. It is labelled as such and can never be promoted to an ABSOLUTE
    scale on its own.
  * B and C touch the same cell, so they MUST yield the same scale and the same
    zero point. Disagreement is reported as a contradiction, never averaged
    away, and it SUPPRESSES the tables.json patch.
  * A DELTA scale and an ABSOLUTE scale are different objects and are never
    conflated. `kind` says which.
  * No axis mapping is emitted that is not forced by the data. A guessed
    rpm/TPS band mis-scopes the guardrails, which is worse than no band.
  * A result that contradicts tables.json (as already happened for
    `timing_map_main`, whose assumed 49 raw/deg was tested and REJECTED) is
    stated loudly and lands in the proposed CONTRADICTED_SCALES.
  * `propose_tables_json_patch()` NEVER writes a file. It returns the exact
    patch plus its evidence so a human approves it.

THE CONTROL SAVE (read before running the experiment)
-----------------------------------------------------
GT_A exists so that save-vs-save cancels the metadata/checksum churn TMax
writes on every save. That cancels the SYSTEMATIC difference between "the
original file" and "a file TMax wrote", but it does NOT cancel save-to-save
NON-DETERMINISM: a timestamp, a save counter, a re-seeded checksum. Those move
on every save regardless of what was edited, and against GT_A alone they are
indistinguishable from the edit.

One extra no-edit save (GT_A2) fixes this for free: every offset that differs
between GT_A and GT_A2 changed for NO reason, so it can be subtracted from
every edit's diff by measurement instead of by assumption. Declare it as
`control` and this module uses it. Without it, churn exclusion falls back to
the structural rule (category METADATA / below 0x3C0), which cannot see churn
that lands inside the `shared_churn_unresolved` block.

NEVER WRITES A `.tbw`. Reads only. (Project hard rule #2.)

Stdlib only. No NAS, no Ollama, no Elasticsearch, no network.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import table_map                 # noqa: E402
import thundermax_parser as tmx  # noqa: E402


SCHEMA_VERSION = "tmax-ground-truth/1"

# A raw cell read out of a binary is an integer, so each reading carries up to
# +/-0.5 of rounding against the engineering value TMax displayed. A DELTA is
# the difference of two readings, so it carries up to +/-1.0.
ROUNDING_RAW = 1.0

# How far from 0 the derived zero point may sit and still be called "zero".
ZERO_TOL_RAW = 1.0

# Categories/bands that are structural churn: they move whenever ANY table
# moves and are never the edit itself.
STRUCTURAL_CHURN_CATEGORIES = {"METADATA"}


class ExperimentError(ValueError):
    """A declaration that cannot be trusted to describe the experiment."""


# ---------------------------------------------------------------------------
# The declaration format
# ---------------------------------------------------------------------------

EXAMPLE_EXPERIMENT = {
    "schema": SCHEMA_VERSION,
    "title": "Timing map ground truth, run 1",
    "recorded_by": "Joshua",
    "date": "2026-08-21",
    "tuner_version": "TMax Tuner (fill in the version from Help -> About)",
    "notes": (
        "Every value below must be what TMAX TUNER DISPLAYED, not what this "
        "repo guessed. Read old BEFORE editing and new AFTER editing, off the "
        "same cell, and copy the units string verbatim from the page header. "
        "Files are resolved relative to this declaration file unless "
        "'dir' is set or a path is absolute."
    ),
    "dir": ".",
    "baseline": {
        "file": "GT_A.tbw",
        "notes": "opened the baseline tune and saved it with NO edits",
    },
    "control": {
        "file": "GT_A2.tbw",
        "notes": (
            "a SECOND no-edit save. Diffing it against the baseline measures "
            "the save-to-save churn footprint so it can be subtracted from "
            "every edit instead of assumed away. Omit this key if you did not "
            "make the extra save -- churn exclusion then falls back to a "
            "structural rule that cannot see churn inside the shared block."
        ),
    },
    "axes": {
        "Ignition Timing - Timing vs TPS @ RPM": {
            "rpm_rows": [1024, 1536, 1792, 2048, 2304, 2560, 2816, 3072,
                         3328, 3584, 3840, 4096, 4352, 4608],
            "tps_cols": [0, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100],
            "notes": (
                "REPLACE BOTH LISTS with the row and column labels TMax Tuner "
                "actually shows for this page, in the order it shows them, "
                "top-to-bottom and left-to-right. These placeholders come from "
                "this repo's report template and are a GUESS. Without correct "
                "axis labels no axis mapping can be derived at all -- the "
                "analysis will say so rather than invent one."
            ),
        },
    },
    "edits": [
        {
            "id": "B",
            "file": "GT_B.tbw",
            "from": "baseline",
            "table": "Ignition Timing - Timing vs TPS @ RPM",
            "units": "deg BTDC",
            "expect_band": "timing_map_main",
            "cells": [
                {"rpm": 3072, "tps": 20, "old": 28.0, "new": 30.0},
            ],
            "notes": "one cell, mid-map, +2 deg",
        },
        {
            "id": "C",
            "file": "GT_C.tbw",
            "from": "baseline",
            "table": "Ignition Timing - Timing vs TPS @ RPM",
            "units": "deg BTDC",
            "expect_band": "timing_map_main",
            "cells": [
                {"rpm": 3072, "tps": 20, "old": 28.0, "new": 24.0},
            ],
            "notes": (
                "THE SAME CELL as B, a different amount and the OTHER "
                "direction. Tests scale linearity and the zero point. If you "
                "edited C on top of the already-edited B instead of on top of "
                "the baseline, set \"from\": \"B\" and set old to B's new."
            ),
        },
        {
            "id": "D",
            "file": "GT_D.tbw",
            "from": "baseline",
            "table": "Ignition Timing - Timing vs TPS @ RPM",
            "units": "deg BTDC",
            "expect_band": "timing_map_main",
            "cells": [
                {"rpm": 1024, "tps": 100, "old": 22.0, "new": 24.0},
            ],
            "notes": "a DIFFERENT cell, far corner -- reveals the axis order",
        },
        {
            "id": "D2",
            "file": "GT_D2.tbw",
            "from": "baseline",
            "table": "Ignition Timing - Timing vs TPS @ RPM",
            "units": "deg BTDC",
            "expect_band": "timing_map_main",
            "cells": [
                {"rpm": 3072, "tps": 25, "old": 28.0, "new": 30.0},
            ],
            "notes": (
                "STRONGLY RECOMMENDED and cheap: the cell IMMEDIATELY NEXT to "
                "B's cell, same rpm row, one TPS column over. Two far-apart "
                "cells alone leave the cell stride and the row stride "
                "entangled; one adjacent cell measures the cell stride "
                "directly instead of inferring it. Add a second neighbour one "
                "RPM row down at the same TPS to measure the row stride the "
                "same way."
            ),
        },
        {
            "id": "E",
            "file": "GT_E.tbw",
            "from": "baseline",
            "table": "Ignition Timing - Timing vs TPS @ RPM",
            "units": "deg BTDC",
            "uniform_delta": 1.0,
            "expect_band": "timing_map_main",
            "notes": (
                "OPTIONAL. The whole map shifted by one uniform amount "
                "(uniform_delta, in the units above). Confirms the band's real "
                "extent and its cell count. Use 'uniform_delta' INSTEAD of "
                "'cells' for this one."
            ),
        },
    ],
}

EXAMPLE_FIELD_GUIDE = """\
FIELD GUIDE -- ground-truth experiment declaration
==================================================
Top level
  schema        required, must be "{schema}"
  dir           optional, folder holding the .tbw saves (default: the folder
                the declaration file itself lives in)
  baseline      required, {{"file": "GT_A.tbw"}} -- the NO-EDIT save
  control       optional but strongly advised, {{"file": "GT_A2.tbw"}} -- a
                SECOND no-edit save. It measures save-to-save churn so the
                analysis can subtract it instead of assuming it away.
  axes          optional, per TMax table name:
                  {{"rpm_rows": [...], "tps_cols": [...]}}
                The row and column labels TMax Tuner displays, IN ORDER.
                Without these NO axis mapping can be derived.
  edits         required, a list

Each edit
  id            required, unique short label ("B", "C", "D")
  file          required, the .tbw save
  from          optional, "baseline" (default) or another edit's id -- name
                whichever file the 'old' values were read out of
  table         required, the table name AS TMAX TUNER SPELLS IT
  units         required, the unit string TMax displays ("deg BTDC", "AFR",
                "%", ...)
  cells         a list of {{"rpm":, "tps":, "old":, "new":}} -- normally
                exactly one. Optional "row_index"/"col_index" (0-based, as
                displayed) let the axis be derived without the 'axes' block.
  uniform_delta a number, INSTEAD of 'cells', for a whole-map uniform offset
  expect_band   optional, the tables.json band you expect it to land in; a
                mismatch is reported as a finding, not an error
  notes         optional free text

Rules the validator enforces
  * exactly one of 'cells' or 'uniform_delta' per edit
  * 'new' must differ from 'old' (a zero-delta edit derives nothing)
  * 'from' must name the baseline or an earlier edit, and must not cycle
  * if edit X has from == Y, X's 'old' must equal Y's 'new' for the same cell
  * ids are unique; every referenced file must exist at analyze time

Run it
  python3 src/ground_truth.py --example > experiment.json
  python3 src/ground_truth.py check   experiment.json     # BEFORE walking away
  python3 src/ground_truth.py analyze experiment.json
  python3 src/ground_truth.py analyze experiment.json --patch
""".format(schema=SCHEMA_VERSION)


def _err(path, msg):
    raise ExperimentError(f"{path}: {msg}")


def _require(obj, key, path, types, what):
    if key not in obj:
        _err(f"{path}.{key}", f"missing -- {what}")
    v = obj[key]
    if not isinstance(v, types) or isinstance(v, bool) and bool not in (types,):
        _err(f"{path}.{key}", f"must be {what}, got {type(v).__name__}")
    return v


def _num(obj, key, path):
    if key not in obj:
        _err(f"{path}.{key}", "missing -- a number is required")
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _err(f"{path}.{key}", f"must be a number, got {type(v).__name__}")
    return float(v)


def validate_experiment(doc, base_dir=None):
    """Validate and normalise a declaration dict. Raises ExperimentError.

    Returns a normalised experiment: absolute paths resolved, defaults filled,
    `from` links resolved, edits in declaration order.
    """
    if not isinstance(doc, dict):
        _err("<root>", f"must be a JSON object, got {type(doc).__name__}")

    schema = doc.get("schema")
    if schema != SCHEMA_VERSION:
        _err("schema", f"must be {SCHEMA_VERSION!r}, got {schema!r}. Run "
                       "`python3 src/ground_truth.py --example` for a template.")

    root = Path(base_dir or ".").resolve()
    sub = doc.get("dir")
    if sub is not None:
        if not isinstance(sub, str):
            _err("dir", "must be a string path")
        root = (root / sub).resolve()

    def _resolve(fname, path):
        if not isinstance(fname, str) or not fname.strip():
            _err(path, "must be a non-empty file name")
        p = Path(fname)
        return p if p.is_absolute() else (root / p)

    def _fileref(obj, path, what):
        if not isinstance(obj, dict):
            _err(path, f"must be an object like {{\"file\": \"...\"}} -- {what}")
        f = _require(obj, "file", path, str, "a .tbw file name")
        return {"file": f, "path": _resolve(f, f"{path}.file"),
                "notes": obj.get("notes", "")}

    if "baseline" not in doc:
        _err("baseline", "missing -- the no-edit save every edit is diffed "
                         "against")
    baseline = _fileref(doc["baseline"], "baseline", "the no-edit save")
    control = (_fileref(doc["control"], "control", "the second no-edit save")
               if doc.get("control") else None)

    axes = doc.get("axes") or {}
    if not isinstance(axes, dict):
        _err("axes", "must be an object keyed by TMax table name")
    norm_axes = {}
    for tname, ax in axes.items():
        p = f"axes[{tname!r}]"
        if not isinstance(ax, dict):
            _err(p, "must be an object with rpm_rows and tps_cols")
        out = {"notes": ax.get("notes", "")}
        for key in ("rpm_rows", "tps_cols"):
            vals = ax.get(key)
            if vals is None:
                _err(f"{p}.{key}", "missing -- the axis labels TMax displays, "
                                   "in order")
            if not isinstance(vals, list) or len(vals) < 2:
                _err(f"{p}.{key}", "must be a list of at least 2 axis labels")
            nums = []
            for i, v in enumerate(vals):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    _err(f"{p}.{key}[{i}]", "axis labels must be numbers")
                nums.append(float(v))
            if len(set(nums)) != len(nums):
                _err(f"{p}.{key}", "axis labels must be distinct")
            out[key] = nums
        norm_axes[tname] = out

    raw_edits = doc.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        _err("edits", "missing or empty -- at least one edit is required")

    edits, seen_ids = [], {}
    for i, e in enumerate(raw_edits):
        p = f"edits[{i}]"
        if not isinstance(e, dict):
            _err(p, "must be an object")
        eid = _require(e, "id", p, str, "a unique short label like \"B\"")
        if not eid.strip():
            _err(f"{p}.id", "must not be blank")
        if eid in seen_ids:
            _err(f"{p}.id", f"duplicate id {eid!r} (also at edits[{seen_ids[eid]}])")
        if eid == "baseline":
            _err(f"{p}.id", "\"baseline\" is reserved")
        seen_ids[eid] = i

        fname = _require(e, "file", p, str, "the .tbw save for this edit")
        table = _require(e, "table", p, str,
                         "the table name as TMax Tuner spells it")
        units = _require(e, "units", p, str,
                         "the unit string TMax Tuner displays")
        if not table.strip():
            _err(f"{p}.table", "must not be blank")
        if not units.strip():
            _err(f"{p}.units", "must not be blank")

        has_cells = "cells" in e and e["cells"] is not None
        has_uniform = "uniform_delta" in e and e["uniform_delta"] is not None
        if has_cells == has_uniform:
            _err(p, "exactly one of 'cells' or 'uniform_delta' is required "
                    "(cells for a per-cell edit, uniform_delta for a "
                    "whole-map offset)")

        cells = []
        uniform = None
        if has_uniform:
            uniform = _num(e, "uniform_delta", p)
            if uniform == 0:
                _err(f"{p}.uniform_delta", "must not be 0 -- a zero-delta edit "
                                           "derives no scale")
        else:
            raw_cells = e["cells"]
            if not isinstance(raw_cells, list) or not raw_cells:
                _err(f"{p}.cells", "must be a non-empty list")
            for j, c in enumerate(raw_cells):
                cp = f"{p}.cells[{j}]"
                if not isinstance(c, dict):
                    _err(cp, "must be an object with rpm, tps, old, new")
                cell = {
                    "rpm": _num(c, "rpm", cp),
                    "tps": _num(c, "tps", cp),
                    "old": _num(c, "old", cp),
                    "new": _num(c, "new", cp),
                }
                if cell["new"] == cell["old"]:
                    _err(f"{cp}.new", "equals 'old' -- a zero-delta edit "
                                      "derives no scale and cannot be checked")
                for k in ("row_index", "col_index"):
                    if c.get(k) is not None:
                        v = c[k]
                        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                            _err(f"{cp}.{k}",
                                 "must be a non-negative 0-based integer index")
                        cell[k] = v
                cells.append(cell)

        edits.append({
            "id": eid,
            "file": fname,
            "path": _resolve(fname, f"{p}.file"),
            "from": e.get("from", "baseline"),
            "table": table,
            "units": units,
            "cells": cells,
            "uniform_delta": uniform,
            "expect_band": e.get("expect_band"),
            "notes": e.get("notes", ""),
        })

    # `from` links: must reference the baseline or an EARLIER edit (which makes
    # cycles structurally impossible).
    by_id = {}
    for idx, e in enumerate(edits):
        src = e["from"]
        if not isinstance(src, str):
            _err(f"edits[{idx}].from", "must be a string id")
        if src == "baseline":
            e["from_path"] = baseline["path"]
            e["from_file"] = baseline["file"]
        elif src in by_id:
            prev = edits[by_id[src]]
            e["from_path"] = prev["path"]
            e["from_file"] = prev["file"]
        elif src in seen_ids:
            _err(f"edits[{idx}].from",
                 f"refers to {src!r}, which is declared LATER. 'from' must "
                 "name the baseline or an earlier edit.")
        else:
            _err(f"edits[{idx}].from",
                 f"unknown id {src!r} -- use \"baseline\" or an earlier "
                 "edit's id")
        by_id[e["id"]] = idx

    # Chained edits must agree on the value they started from.
    for idx, e in enumerate(edits):
        if e["from"] == "baseline" or not e["cells"]:
            continue
        prev = edits[by_id[e["from"]]]
        for j, c in enumerate(e["cells"]):
            match = next((pc for pc in prev["cells"]
                          if pc["rpm"] == c["rpm"] and pc["tps"] == c["tps"]),
                         None)
            if match and match["new"] != c["old"]:
                _err(f"edits[{idx}].cells[{j}].old",
                     f"is {c['old']}, but edit {e['from']!r} left that cell at "
                     f"{match['new']}. One of the two readings is wrong -- fix "
                     "it before analysing, or the derived scale will be wrong "
                     "by exactly that discrepancy.")

    return {
        "schema": SCHEMA_VERSION,
        "title": doc.get("title", ""),
        "recorded_by": doc.get("recorded_by", ""),
        "date": doc.get("date", ""),
        "tuner_version": doc.get("tuner_version", ""),
        "notes": doc.get("notes", ""),
        "dir": str(root),
        "baseline": baseline,
        "control": control,
        "axes": norm_axes,
        "edits": edits,
    }


def load_experiment(path):
    """Load + validate a declaration file. Raises ExperimentError / OSError."""
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise ExperimentError(f"cannot read {p}: {e}") from e
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ExperimentError(f"{p}: not valid JSON -- {e}") from e
    exp = validate_experiment(doc, base_dir=p.resolve().parent)
    exp["declaration_path"] = str(p.resolve())
    return exp


# ---------------------------------------------------------------------------
# Byte-level helpers  (read-only, always)
# ---------------------------------------------------------------------------

def _read_bytes(path):
    return Path(path).read_bytes()


def _changed_offsets(a, b):
    """Offsets where two same-length buffers differ. Chunked, so the common
    case (a handful of changed bytes in 214470) is fast."""
    if len(a) != len(b):
        raise ExperimentError(
            f"files are different sizes ({len(a)} vs {len(b)}); cannot diff")
    out = []
    step = 4096
    for base in range(0, len(a), step):
        sa = a[base:base + step]
        sb = b[base:base + step]
        if sa == sb:
            continue
        for i, (x, y) in enumerate(zip(sa, sb)):
            if x != y:
                out.append(base + i)
    return out


def _band_value_width(stride):
    """Bytes of the value word on a record of pitch `stride`.

    Mirrors thundermax_parser._grid_for: stride 8 carries a 4-byte value, and
    anything narrower carries a value as wide as the stride, capped at 4.
    """
    s = int(stride or 1)
    if s == 8:
        return 4
    return max(1, min(s, 4))


def _wrap_signed(delta, width):
    """Re-read an unsigned delta as signed when it obviously wrapped.

    tables.json: correction cells are SIGNED, so -1 appears in an unsigned diff
    as +65535. Exactly half the modulus is genuinely ambiguous and is left
    alone. Returns (delta, wrapped_bool).
    """
    mod = 256 ** width
    d = int(delta)
    if d > mod // 2:
        return d - mod, True
    if d < -(mod // 2):
        return d + mod, True
    return d, False


def _read_le(data, off, width):
    fmt = {1: "<B", 2: "<H", 4: "<I"}[width]
    return struct.unpack_from(fmt, data, off)[0]


def _record_of(band, offset):
    """(record_index, record_offset) for a byte offset inside a band."""
    stride = max(1, int(band.get("stride") or 1))
    idx = (offset - band["_lo"]) // stride
    return idx, band["_lo"] + idx * stride


def _readings(a, b, rec_off, stride, changed_lo, changed_hi):
    """Candidate (anchor, width) readings of one record in both files.

    Which word inside a record holds the value is UNCONFIRMED (tables.json:
    "a u16 value, often followed by a flag/limit word"), so instead of picking
    one we enumerate the plausible readings and let the caller see whether they
    agree. A DELTA usually survives the ambiguity; an ABSOLUTE value does not,
    which is exactly why the zero point is the weaker of the two results.

    Only readings whose byte span covers every changed byte in the record are
    returned -- a reading that misses the change would report a delta of 0.
    """
    stride = max(1, int(stride))
    anchors = sorted({0, changed_lo - rec_off})
    out = []
    seen = set()
    for anchor in anchors:
        if anchor < 0 or anchor >= stride:
            continue
        for width in (1, 2, 4):
            if anchor + width > stride:
                continue
            lo = rec_off + anchor
            hi = lo + width
            if lo > changed_lo or hi < changed_hi:
                continue          # does not cover the change
            if hi > len(a):
                continue
            key = (anchor, width)
            if key in seen:
                continue
            seen.add(key)
            va = _read_le(a, lo, width)
            vb = _read_le(b, lo, width)
            delta, wrapped = _wrap_signed(vb - va, width)
            out.append({
                "anchor_in_record": anchor,
                "width_bytes": width,
                "offset": lo,
                "offset_hex": f"0x{lo:05X}",
                "raw_from": va,
                "raw_to": vb,
                "raw_delta": delta,
                "signed_wrap_applied": wrapped,
            })
    return out


def _pick_reading(readings, band):
    """The reading tables.json's own record grid implies, if it is viable.

    Falls back to the narrowest viable reading and says so, because a band
    whose declared stride/width does not cover the bytes that actually moved is
    itself a finding about tables.json.
    """
    if not readings:
        return None, "no reading covers the changed bytes"
    want_w = _band_value_width(band.get("stride") if band else 1)
    for r in readings:
        if r["anchor_in_record"] == 0 and r["width_bytes"] == want_w:
            return r, (f"tables.json record grid for this band "
                       f"(stride {band.get('stride')}, {want_w}-byte LE value "
                       f"at the record start)")
    best = min(readings, key=lambda r: (r["width_bytes"], r["anchor_in_record"]))
    return best, (
        "tables.json's declared grid for this band (stride "
        f"{band.get('stride') if band else '?'}, {want_w}-byte value at the "
        "record start) does NOT cover the bytes that moved. Fell back to a "
        f"{best['width_bytes']}-byte LE value at +{best['anchor_in_record']} "
        "inside the record. THAT IS A FINDING: the band's stride or its value "
        "position in tables.json is probably wrong.")


# ---------------------------------------------------------------------------
# check -- the fast preflight Joshua runs before leaving the Windows machine
# ---------------------------------------------------------------------------

def check_experiment(exp, tables=None):
    """Fast file-level preflight. Returns {ok, problems, files, ...}.

    Catches, per file: missing, wrong size, unparseable, a base-map ID that
    differs from the baseline's, and byte-identical-to-its-source (the edit did
    not save, or TMax refused the value).
    """
    if isinstance(exp, (str, Path)):
        exp = load_experiment(exp)
    tables = tables or table_map.load_map()

    problems = []
    files = {}
    cache = {}

    def note(sev, fid, msg, fix=""):
        problems.append({"severity": sev, "file": fid, "message": msg,
                         "fix": fix})

    def load(fid, name, path):
        entry = {"id": fid, "name": name, "path": str(path), "exists": False,
                 "size_bytes": None, "size_ok": False, "base_map_id": None,
                 "parses": False}
        files[fid] = entry
        try:
            data = _read_bytes(path)
        except OSError as e:
            note("error", fid, f"cannot read {path}: {e}",
                 "copy the save off the Windows machine before analysing")
            return None
        entry["exists"] = True
        entry["size_bytes"] = len(data)
        entry["size_ok"] = len(data) == tmx.EXPECTED_SIZE
        if not entry["size_ok"]:
            note("error", fid,
                 f"is {len(data)} bytes, expected {tmx.EXPECTED_SIZE}",
                 "re-save from TMax Tuner; a partial copy or an AppleDouble "
                 "sidecar (._name.tbw) will look like this")
        try:
            t = tmx.TbwFile(path)
            entry["parses"] = True
            entry["base_map_id"] = t.base_map_id
            entry["id_ok"] = t.id_ok
            if not t.id_ok:
                note("error", fid, "base-map ID at 0x10 is not clean uppercase "
                                   "ASCII -- this may not be a ThunderMax TBW",
                     "check you copied the right file")
        except (tmx.TbwError, OSError) as e:
            note("error", fid, f"does not parse as a TBW file: {e}", "")
        cache[fid] = data
        return data

    base_data = load("baseline", exp["baseline"]["file"], exp["baseline"]["path"])
    base_id = files["baseline"].get("base_map_id")

    if exp["control"]:
        ctrl = load("control", exp["control"]["file"], exp["control"]["path"])
        if ctrl is not None and base_data is not None and len(ctrl) == len(base_data):
            n = len(_changed_offsets(base_data, ctrl))
            files["control"]["churn_bytes_vs_baseline"] = n
            if n == 0:
                note("warn", "control",
                     "is byte-identical to the baseline. Either TMax writes a "
                     "fully deterministic save (good news -- churn is purely "
                     "edit-driven) or the same file was copied twice.",
                     "confirm you really made two separate no-edit saves")
    else:
        note("warn", "control",
             "no control save declared. Save-to-save churn (timestamps, save "
             "counters, re-seeded checksums) cannot be measured, so it is "
             "excluded structurally instead -- which cannot see churn landing "
             "inside the shared_churn_unresolved block.",
             "make one extra no-edit save as GT_A2.tbw and add "
             "\"control\": {\"file\": \"GT_A2.tbw\"}")

    seen_files = {exp["baseline"]["file"]: "baseline"}
    if exp["control"]:
        seen_files.setdefault(exp["control"]["file"], "control")

    for e in exp["edits"]:
        fid = e["id"]
        if e["file"] in seen_files:
            note("error", fid,
                 f"reuses the file {e['file']!r} already declared as "
                 f"{seen_files[e['file']]!r}", "each save needs its own file")
        seen_files.setdefault(e["file"], fid)
        data = load(fid, e["file"], e["path"])
        if data is None:
            continue
        if base_id and files[fid].get("base_map_id") not in (None, base_id):
            note("error", fid,
                 f"base map is {files[fid]['base_map_id']!r} but the baseline "
                 f"is {base_id!r}. These are not two revisions of one tune; "
                 "table layouts may not align at all and any scale derived "
                 "from this pair would be meaningless.",
                 "re-do the edit starting from the same baseline tune")
        src = cache.get("baseline" if e["from"] == "baseline" else e["from"])
        if src is None or len(src) != len(data):
            continue
        changed = _changed_offsets(src, data)
        files[fid]["changed_bytes_vs_from"] = len(changed)
        payload = [o for o in changed if not _is_structural_churn(o, tables)]
        files[fid]["payload_bytes_vs_from"] = len(payload)
        if not changed:
            note("error", fid,
                 f"is BYTE-IDENTICAL to {e['from_file']}. The edit did not "
                 "save -- either the value was never committed (click off the "
                 "cell / press Enter before saving) or TMax Tuner refused it.",
                 "redo this edit and save again")
        elif not payload:
            note("error", fid,
                 f"differs from {e['from_file']} in {len(changed)} bytes, but "
                 "ALL of them are metadata/checksum churn below 0x3C0. No "
                 "table data moved -- the edit did not reach a map.",
                 "redo this edit; confirm the new value is showing in the "
                 "cell before saving")

    ok = not any(p["severity"] == "error" for p in problems)
    return {
        "ok": ok,
        "problems": problems,
        "files": files,
        "errors": sum(1 for p in problems if p["severity"] == "error"),
        "warnings": sum(1 for p in problems if p["severity"] == "warn"),
        "verdict": ("READY -- every declared save exists, is the right size, "
                    "shares the baseline's base map, and actually changed "
                    "table data."
                    if ok else
                    "NOT READY -- fix the errors below BEFORE leaving the "
                    "Windows machine; every one of them needs TMax Tuner."),
        "never_writes_tbw": True,
    }


def _is_structural_churn(offset, tables):
    if offset < tmx.METADATA_BOUNDARY:
        return True
    band = table_map.band_for_offset(offset, tables)
    return bool(band and band["category"] in STRUCTURAL_CHURN_CATEGORIES)


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

def _cell_key(table, rpm, tps):
    return f"{table} @ {_n(rpm)} rpm / {_n(tps)}% TPS"


def _n(x):
    """Compact number: 3.0 -> '3', 3.25 -> '3.25'."""
    f = float(x)
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def _group_payload(offsets, tables):
    """Group changed payload offsets into candidate CELLS.

    Two changed bytes in the same record of the same band are one cell -- that
    is what the band's own stride is for, and it is why this does not use a
    byte-gap heuristic. Offsets in no band are grouped into contiguous runs.
    """
    groups = {}
    unmapped = []
    for off in offsets:
        band = table_map.band_for_offset(off, tables)
        if band is None:
            unmapped.append(off)
            continue
        idx, rec_off = _record_of(band, off)
        key = (band["name"], idx)
        g = groups.setdefault(key, {
            "band": band["name"], "band_def": band,
            "category": band["category"],
            "band_confidence": band["confidence"],
            "record_index": idx, "record_offset": rec_off,
            "stride": max(1, int(band.get("stride") or 1)),
            "offsets": [],
        })
        g["offsets"].append(off)

    runs = []
    for off in sorted(unmapped):
        if runs and off - runs[-1]["offsets"][-1] <= 3:
            runs[-1]["offsets"].append(off)
        else:
            runs.append({"band": None, "band_def": None, "category": "UNMAPPED",
                         "band_confidence": "-", "record_index": None,
                         "record_offset": off, "stride": 2, "offsets": [off]})
    out = [groups[k] for k in sorted(groups, key=lambda k: groups[k]["record_offset"])]
    return out + runs


def _analyze_edit(edit, data_from, data_to, tables, churn_offsets):
    """Byte-level analysis of ONE edit against the file it was made from."""
    changed = _changed_offsets(data_from, data_to)

    structural, control_churn, payload = [], [], []
    for off in changed:
        if _is_structural_churn(off, tables):
            structural.append(off)
        elif off in churn_offsets:
            control_churn.append(off)
        else:
            payload.append(off)

    cells = []
    for g in _group_payload(payload, tables):
        lo, hi = min(g["offsets"]), max(g["offsets"]) + 1
        if g["band_def"] is not None:
            readings = _readings(data_from, data_to, g["record_offset"],
                                 g["stride"], lo, hi)
            preferred, basis = _pick_reading(readings, g["band_def"])
        else:
            readings = _readings(data_from, data_to, lo, max(2, hi - lo), lo, hi)
            preferred, basis = (readings[0] if readings else None), (
                "no band contains this offset, so there is no record grid to "
                "read it on; the reading below is a plain LE word anchored at "
                "the first changed byte")
        deltas = sorted({r["raw_delta"] for r in readings})
        abs_from = sorted({r["raw_from"] for r in readings})
        cells.append({
            "band": g["band"],
            "category": g["category"],
            "band_confidence": g["band_confidence"],
            "record_index": g["record_index"],
            "record_offset": g["record_offset"],
            "record_offset_hex": f"0x{g['record_offset']:05X}",
            "stride": g["stride"],
            "changed_offsets": g["offsets"],
            "changed_range_hex": [f"0x{lo:05X}", f"0x{hi:05X}"],
            "changed_bytes": len(g["offsets"]),
            "readings": readings,
            "preferred_reading": preferred,
            "reading_basis": basis,
            "delta_is_width_robust": len(deltas) == 1,
            "delta_candidates": deltas,
            "absolute_is_width_robust": len(abs_from) == 1,
        })

    bands_touched = sorted({c["band"] for c in cells if c["band"]})
    declared_n = len(edit["cells"]) if edit["cells"] else None

    issues = []
    if not changed:
        issues.append(
            f"NO BYTE CHANGED between {edit['from_file']} and {edit['file']}. "
            "The edit never reached the file: either the cell was not "
            "committed before saving, or TMax Tuner refused the value. "
            "Nothing can be derived from this save.")
    elif not payload:
        issues.append(
            f"{len(changed)} bytes changed but every one of them is churn "
            f"({len(structural)} structural metadata/checksum, "
            f"{len(control_churn)} measured save-to-save churn). No table data "
            "moved, so the edit did not reach a map.")
    if len(bands_touched) > 1:
        issues.append(
            "DIRTY: the edit moved cells in {} different bands ({}). A "
            "single-cell edit must land in exactly ONE band. Either more than "
            "one map was edited, or a band boundary in tables.json is wrong. "
            "Any scale derived from this edit is UNSAFE."
            .format(len(bands_touched), ", ".join(bands_touched)))
    if declared_n is not None and len(cells) != declared_n:
        extra = ""
        if declared_n == 1 and len(cells) == 2:
            d = [c["preferred_reading"]["raw_delta"] for c in cells
                 if c["preferred_reading"]]
            gap = abs(cells[1]["record_offset"] - cells[0]["record_offset"])
            if len(d) == 2 and d[0] == d[1]:
                extra = (" NOTE: both cells moved by the SAME delta ({:+d}) and "
                         "sit {} bytes apart. On this engine that is what a "
                         "FRONT/REAR pair looks like -- TMax may have applied "
                         "one displayed edit to both cylinders. If so this is a "
                         "format finding, not a mistake, but the band map has "
                         "to record the pairing before any scale is trusted."
                         .format(d[0], gap))
        issues.append(
            "DIRTY: {} cell(s) declared but {} cell(s) moved. A single-cell "
            "edit must move exactly one cell once checksum churn is excluded. "
            "Either the edit touched more than the declared cell, or a "
            "neighbouring record is not really a separate cell (the band's "
            "stride in tables.json may be wrong). Any scale derived from this "
            "edit is UNSAFE.{}".format(declared_n, len(cells), extra))
    if edit["uniform_delta"] is not None and len(cells) < 2:
        issues.append(
            "A whole-map uniform offset was declared but only {} cell(s) "
            "moved. Either the map is that small, or the offset did not apply "
            "everywhere.".format(len(cells)))
    for c in cells:
        if c["category"] == "AUTOTUNE":
            issues.append(
                "AutoTune-category data moved at {} (band {}). AutoTune "
                "learned cells churn on every ride and every save, so this "
                "cannot be distinguished from the edit. Treat this experiment "
                "as contaminated unless a control save proves otherwise."
                .format(c["record_offset_hex"], c["band"]))
        if c["preferred_reading"] is None:
            issues.append(
                "No viable reading covers the bytes that moved at {} -- the "
                "band's stride/value position in tables.json cannot explain "
                "them.".format(c["changed_range_hex"][0]))
    if edit["expect_band"] and bands_touched and \
            edit["expect_band"] not in bands_touched:
        issues.append(
            "The edit was expected to land in band {!r} but landed in {}. That "
            "is a finding about tables.json, not necessarily an error -- but "
            "the expectation and the bytes disagree."
            .format(edit["expect_band"], ", ".join(bands_touched)))

    clean = (bool(payload) and len(bands_touched) == 1 and not issues)

    return {
        "id": edit["id"],
        "file": edit["file"],
        "from": edit["from"],
        "from_file": edit["from_file"],
        "table": edit["table"],
        "units": edit["units"],
        "expect_band": edit["expect_band"],
        "notes": edit["notes"],
        "declared": {"cells": edit["cells"],
                     "uniform_delta": edit["uniform_delta"]},
        "byte_diff": {
            "total_changed": len(changed),
            "structural_churn_excluded": len(structural),
            "control_churn_excluded": len(control_churn),
            "payload_changed": len(payload),
        },
        "payload_cells": cells,
        "bands_touched": bands_touched,
        "no_change": not changed,
        "clean": clean,
        "issues": issues,
    }


def _eng_delta(edit):
    """The engineering-unit delta this edit declares, or None if ambiguous."""
    if edit["uniform_delta"] is not None:
        return edit["uniform_delta"], "uniform_delta"
    deltas = {c["new"] - c["old"] for c in edit["cells"]}
    if len(deltas) == 1:
        return deltas.pop(), "declared cell(s)"
    return None, ("the edit declares cells with DIFFERENT deltas, so no single "
                  "engineering delta describes it")


def _scale_interval(raw_delta, eng_delta):
    """Scale bounds allowed by +/-{ROUNDING_RAW} raw of reading error.

    Each raw cell is an integer, so it carries up to +/-0.5 of rounding against
    the value TMax displayed; a delta of two readings carries up to +/-1.0.
    Two edits "disagree" only when these intervals do not intersect -- a strict
    equality test would cry contradiction over a single least-significant bit.
    """
    lo = (abs(raw_delta) - ROUNDING_RAW) / abs(eng_delta)
    hi = (abs(raw_delta) + ROUNDING_RAW) / abs(eng_delta)
    sign = 1.0 if (raw_delta > 0) == (eng_delta > 0) else -1.0
    if sign < 0:
        lo, hi = -hi, -lo
    return [round(lo, 6), round(hi, 6)]


def _affine_fit(points, scale_hint=None):
    """Fit raw = eng * slope + intercept over (eng, raw) observations.

    Slope comes from the WIDEST engineering span, which is the pair with the
    smallest relative rounding error. Every other point is then a RESIDUAL --
    a real test the caller did not get for free.
    """
    pts = sorted(points, key=lambda p: p["eng"])
    lo, hi = pts[0], pts[-1]
    if hi["eng"] == lo["eng"]:
        return None
    slope = (hi["raw"] - lo["raw"]) / (hi["eng"] - lo["eng"])
    intercept = lo["raw"] - lo["eng"] * slope
    residuals = []
    for p in pts:
        pred = p["eng"] * slope + intercept
        residuals.append({**p, "predicted_raw": round(pred, 3),
                          "residual_raw": round(p["raw"] - pred, 3)})
    worst = max((abs(r["residual_raw"]) for r in residuals), default=0.0)
    return {
        "slope": slope,
        "intercept": intercept,
        "span_eng": [lo["eng"], hi["eng"]],
        "span_raw": [lo["raw"], hi["raw"]],
        "interval": _scale_interval(hi["raw"] - lo["raw"], hi["eng"] - lo["eng"]),
        "residuals": residuals,
        "max_residual_raw": round(worst, 3),
        "n_points": len(pts),
        "scale_hint": scale_hint,
    }


def _analyze_band_scale(band_name, per_edit, observations, tables):
    """Turn one band's per-edit arithmetic + pooled points into a scale."""
    units = sorted({p["unit"] for p in per_edit})
    unit = units[0] if len(units) == 1 else "/".join(units)
    n_edits = len({p["edit"] for p in per_edit})
    clean_edits = sorted({p["edit"] for p in per_edit if p["clean"]})
    dirty_edits = sorted({p["edit"] for p in per_edit if not p["clean"]})

    # Pairwise consistency: intervals that do not intersect are a real
    # contradiction, not a rounding difference.
    disagreements = []
    for i in range(len(per_edit)):
        for j in range(i + 1, len(per_edit)):
            a, b = per_edit[i], per_edit[j]
            same_cell = a["cell_key"] == b["cell_key"]
            ia, ib = a["interval"], b["interval"]
            if max(ia[0], ib[0]) <= min(ia[1], ib[1]):
                continue
            why = (
                "Edits {} and {} changed the SAME cell and must yield the "
                "same scale.".format(a["edit"], b["edit"]) if same_cell else
                "Edits {} and {} changed DIFFERENT cells of the same band. "
                "Different cells may sit at different values, but one table "
                "has ONE unit scale, so they must still agree."
                .format(a["edit"], b["edit"]))
            disagreements.append({
                "edits": [a["edit"], b["edit"]],
                "same_cell": same_cell,
                "cell": [a["cell_key"], b["cell_key"]],
                "scales": [a["scale"], b["scale"]],
                "intervals": [ia, ib],
                "detail": (
                    "{} {} says {} raw/{} (allowed {}..{}), {} says {} "
                    "(allowed {}..{}). The intervals do not overlap even "
                    "allowing +/-{} raw of reading error, so this is NOT "
                    "rounding: either the relationship is non-linear, the cell "
                    "is not where these bytes are, or one of the recorded TMax "
                    "values is wrong. NOT AVERAGED."
                    .format(why, a["edit"], _n(a["scale"]), unit, _n(ia[0]),
                            _n(ia[1]), b["edit"], _n(b["scale"]), _n(ib[0]),
                            _n(ib[1]), _n(ROUNDING_RAW))),
            })

    fit = _affine_fit(observations) if len(observations) >= 2 else None
    linear = fit is not None and fit["max_residual_raw"] <= ROUNDING_RAW
    # Linearity is only TESTED when a third point exists; two points always fit
    # a line exactly, which proves nothing.
    linearity_tested = bool(fit and fit["n_points"] >= 3 and not disagreements)
    if disagreements:
        linear = False

    zero = None
    if fit is not None:
        z = fit["intercept"]
        # Uncertainty in the intercept, propagated from the scale interval at
        # the anchor point furthest from eng == 0.
        anchor = min(observations, key=lambda p: abs(p["eng"]))
        span = abs(anchor["eng"])
        slack = span * (fit["interval"][1] - fit["interval"][0]) / 2.0 + 0.5
        zero = {
            "zero_raw": round(z, 3),
            "interval": [round(z - slack, 3), round(z + slack, 3)],
            "is_zero": abs(z) <= ZERO_TOL_RAW + slack,
            "arithmetic": (
                "zero_raw = raw({} {}) - {} * scale = {} - {} * {} = {}"
                .format(_n(anchor["eng"]), unit, _n(anchor["eng"]),
                        _n(anchor["raw"]), _n(anchor["eng"]),
                        _n(round(fit["slope"], 4)), _n(round(z, 3)))),
        }
        zero["meaning"] = (
            "raw 0 == {} 0 within +/-{} raw, so an ABSOLUTE reading is just "
            "raw / scale.".format(unit, _n(round(slack + ZERO_TOL_RAW, 2)))
            if zero["is_zero"] else
            "raw 0 does NOT correspond to {} 0. The zero point is {} raw, so "
            "an absolute reading is (raw - {}) / scale. Any code that divides "
            "a raw cell by the scale and calls the result an engineering value "
            "-- including the read_tune() estimate for a band promoted this way "
            "-- would be wrong by {} {}."
            .format(unit, _n(zero["zero_raw"]), _n(zero["zero_raw"]),
                    _n(round(abs(z) / fit["slope"], 3)) if fit["slope"] else "?",
                    unit))

    widths = sorted({p["width_bytes"] for p in per_edit})
    anchors = sorted({p["anchor_in_record"] for p in per_edit})
    width_robust = all(p["delta_is_width_robust"] for p in per_edit)
    # Field width is resolved by POOLING: a (anchor, width) reading survives
    # only if it covers the bytes that moved in EVERY observation of the band.
    # One edit alone often leaves the width open (a +2 deg step may only touch
    # the low byte); a second edit that carries into the next byte rules the
    # narrow reading out. That is exactly what GT_C buys beyond linearity.
    viable = _viable_readings(per_edit)
    consistent = _consistent_readings(per_edit, viable)
    abs_width_robust = _absolute_unambiguous(per_edit, consistent)

    blockers = []
    if dirty_edits:
        blockers.append(
            "edit(s) {} were not clean -- see their issues[]. A scale from a "
            "dirty edit is unsafe.".format(", ".join(dirty_edits)))
    if disagreements:
        blockers.append(
            "edits disagree on the scale for the same cell; the relationship "
            "is not linear or a reading is wrong.")
    if not clean_edits:
        blockers.append("no clean edit contributed to this band.")
    if len(anchors) > 1:
        blockers.append(
            "cells in this band were read at DIFFERENT positions inside their "
            "record (+{}). The value word's position is not consistent, so the "
            "readings are not comparable.".format(", +".join(map(str, anchors))))
    pref_key = ((per_edit[0]["anchor_in_record"], per_edit[0]["width_bytes"])
                if per_edit else None)
    if consistent and pref_key is not None and pref_key not in consistent:
        blockers.append(
            "the reading actually used ({}-byte at +{} in the record) does NOT "
            "put the pooled observations on one line, while {} does. The value "
            "word this band is being read on is the wrong one -- fix the "
            "band's stride/width in tables.json before anything is derived "
            "from it.".format(pref_key[1], pref_key[0],
                              ", ".join("{}-byte at +{}".format(w, a)
                                        for a, w in sorted(consistent))))
    if fit is not None and fit["n_points"] >= 3 and not linear and not disagreements:
        blockers.append(
            "the pooled observations do NOT fall on one line: worst residual "
            "{} raw against a +/-{} rounding tolerance. One table has one "
            "scale and one zero point, so this band does not behave as a "
            "single affine map -- the readings, the cell locations, or the "
            "recorded TMax values disagree with each other. Nothing is "
            "patched off a model the data refutes."
            .format(fit["max_residual_raw"], _n(ROUNDING_RAW)))

    # A DELTA scale needs the delta only. An ABSOLUTE scale needs, additionally:
    # linearity actually tested, the zero point pinned, and the field width
    # unambiguous (a wrong width shifts the absolute reading by whole multiples
    # of 65536 while leaving the delta untouched).
    absolute_ready = bool(
        not blockers and linearity_tested and linear and zero is not None
        and abs_width_robust)
    absolute_blockers = []
    if not linearity_tested:
        absolute_blockers.append(
            "linearity is UNTESTED: {} independent observation(s) of this band. "
            "Two points always fit a line exactly. A third value at the same "
            "cell (the GT_C save) is what tests it."
            .format(len(observations)))
    if not abs_width_robust:
        absolute_blockers.append(
            "the field WIDTH is still ambiguous after pooling every edit: the "
            "readings that survive ({}) give DIFFERENT absolute values, even "
            "though they all give the same delta. The zero point therefore "
            "rests on a width nobody has confirmed -- see zero_point_by_width. "
            "Two things rule the narrow readings out, and both are single "
            "extra saves: an edit at this cell big enough to CARRY into the "
            "next byte, and a cell somewhere else in the map whose value has a "
            "different high byte (the far-corner GT_D save, or the whole-map "
            "GT_E offset)."
            .format(", ".join("{}-byte at +{}".format(w, a)
                              for a, w in sorted(consistent)) or "none"))
    if zero is None:
        absolute_blockers.append("no zero point could be fitted.")

    scale_value = fit["slope"] if fit else (
        per_edit[0]["scale"] if per_edit else None)
    interval = fit["interval"] if fit else (
        per_edit[0]["interval"] if per_edit else None)

    confidence = "unknown"
    if scale_value is not None and not blockers:
        if linearity_tested and linear:
            confidence = "high"
        elif len(per_edit) >= 2:
            confidence = "medium"
        else:
            confidence = "medium"

    return {
        "band": band_name,
        "tables_json_band": next((b for b in tables["bands"]
                                  if b["name"] == band_name), None) is not None,
        "unit": unit,
        "tables": sorted({p["table"] for p in per_edit}),
        "cells": sorted({p["cell_key"] for p in per_edit}),
        "edits_used": sorted({p["edit"] for p in per_edit}),
        "clean_edits": clean_edits,
        "dirty_edits": dirty_edits,
        "n_edits": n_edits,
        "per_edit": per_edit,
        "observations": observations,
        "fit": fit,
        "linearity": {
            "tested": linearity_tested,
            "consistent": linear,
            "max_residual_raw": fit["max_residual_raw"] if fit else None,
            "tolerance_raw": ROUNDING_RAW,
            "detail": (
                "TESTED across {} observations; worst residual {} raw against "
                "a +/-{} rounding tolerance -> {}."
                .format(fit["n_points"], fit["max_residual_raw"],
                        _n(ROUNDING_RAW), "LINEAR" if linear else "NOT LINEAR")
                if linearity_tested and fit else
                "NOT TESTED -- {} observation(s). Two points always fit a line "
                "exactly, so a scale from one edit is a 2-point affine fit "
                "with no linearity evidence at all."
                .format(len(observations))),
        },
        "disagreements": disagreements,
        "scale": {
            "raw_per_unit": round(scale_value, 6) if scale_value is not None else None,
            "unit": unit,
            "interval": interval,
            "kind": "absolute" if absolute_ready else "delta",
            "kind_note": (
                "ABSOLUTE: raw = eng * scale + zero_raw, and both halves are "
                "supported by the data."
                if absolute_ready else
                "DELTA ONLY: this converts a CHANGE in raw to a CHANGE in {}. "
                "It does NOT license reading an absolute cell value -- that "
                "additionally needs the zero point and the field width, which "
                "are not both settled here.".format(unit)),
            "basis": "ground_truth_experiment",
            "observations": len(observations),
            "confidence": confidence,
        },
        "zero_point": zero,
        "zero_point_by_width": _zero_by_width(per_edit, fit, viable, consistent),
        "viable_readings": sorted([{"anchor_in_record": a, "width_bytes": w}
                                   for a, w in viable],
                                  key=lambda r: (r["width_bytes"],
                                                 r["anchor_in_record"])),
        "consistent_readings": sorted([{"anchor_in_record": a, "width_bytes": w}
                                       for a, w in consistent],
                                      key=lambda r: (r["width_bytes"],
                                                     r["anchor_in_record"])),
        "absolute_ready": absolute_ready,
        "absolute_blockers": absolute_blockers,
        "record": {
            "stride_bytes": per_edit[0]["stride"] if per_edit else None,
            "value_width_bytes": widths[0] if len(widths) == 1 else widths,
            "anchor_in_record": anchors[0] if len(anchors) == 1 else anchors,
            "delta_is_width_robust": width_robust,
            "absolute_is_width_robust": abs_width_robust,
            "reading_basis": per_edit[0]["reading_basis"] if per_edit else None,
        },
        "safe_for_patch": not blockers,
        "blockers": blockers,
    }


def _viable_readings(per_edit):
    """(anchor, width) readings that cover the moved bytes in EVERY observation.

    A single small edit often moves only the low byte, which leaves a 1-byte
    reading viable and the absolute value ambiguous. A second, larger edit at
    the same cell carries into the next byte and rules that reading out. So the
    width question is answered by pooling, never by one edit.
    """
    sets = [{(r["anchor_in_record"], r["width_bytes"]) for r in p["all_readings"]}
            for p in per_edit if p.get("all_readings")]
    if not sets:
        return set()
    common = set(sets[0])
    for s in sets[1:]:
        common &= s
    return common


def _fit_for_reading(per_edit, anchor, width):
    """The affine fit this band would have under one specific (anchor, width)."""
    pts = []
    for p in per_edit:
        if p.get("eng_from") is None:
            continue
        r = next((x for x in p.get("all_readings", [])
                  if (x["anchor_in_record"], x["width_bytes"]) == (anchor, width)),
                 None)
        if r is None:
            return None
        for eng, raw in ((p["eng_from"], r["raw_from"]),
                         (p["eng_to"], r["raw_to"])):
            if not any(q["eng"] == eng and q["raw"] == raw
                       and q["cell"] == p["cell_key"] for q in pts):
                pts.append({"eng": eng, "raw": raw, "cell": p["cell_key"],
                            "source": p["edit"]})
    if len({p["eng"] for p in pts}) < 2:
        return None
    return _affine_fit(pts)


def _consistent_readings(per_edit, viable):
    """Viable readings that ALSO put every observation on one straight line.

    A reading that is too narrow survives coincidentally while the high bytes
    happen to be equal across the cells that were edited; pooling cells at
    different values breaks it. This is the second filter, and it is why a
    far-corner cell (GT_D) and a whole-map offset (GT_E) are worth the extra
    saves even though neither adds a new scale.
    """
    out = set()
    for anchor, width in viable:
        f = _fit_for_reading(per_edit, anchor, width)
        if f is not None and f["max_residual_raw"] <= ROUNDING_RAW:
            out.add((anchor, width))
    return out


def _absolute_unambiguous(per_edit, readings):
    """True when every surviving reading agrees on the ABSOLUTE cell value."""
    if not readings:
        return False
    for p in per_edit:
        vals = {r["raw_from"] for r in p.get("all_readings", [])
                if (r["anchor_in_record"], r["width_bytes"]) in readings}
        if len(vals) != 1:
            return False
    return True


def _zero_by_width(per_edit, fit, viable=None, consistent=None):
    """Zero point under each candidate field width.

    The delta -- and therefore the scale -- survives an unknown field width.
    The zero point does not: reading a u16 value as a u32 folds the neighbouring
    flag word in at x65536. Listing them makes that dependence visible instead
    of hiding it behind one arbitrary choice.
    """
    if not fit or not per_edit:
        return []
    slope = fit["slope"]
    ref = per_edit[0]
    out = []
    for r in ref["all_readings"]:
        z = r["raw_from"] - ref["eng_from"] * slope
        key = (r["anchor_in_record"], r["width_bytes"])
        in_viable = viable is None or key in viable
        survives = in_viable and (consistent is None or key in consistent)
        why_out = None
        if not in_viable:
            why_out = ("does not cover the bytes that moved in every edit of "
                       "this band")
        elif not survives:
            why_out = ("covers every edit, but pooling the cells does not put "
                       "its readings on one line -- too narrow, so a "
                       "neighbouring high byte is being dropped")
        out.append({
            "width_bytes": r["width_bytes"],
            "anchor_in_record": r["anchor_in_record"],
            "raw_at_{}".format(_n(ref["eng_from"])): r["raw_from"],
            "zero_raw": round(z, 3),
            "is_zero": abs(z) <= ZERO_TOL_RAW,
            "survives_pooling": survives,
            "ruled_out_by": why_out,
            "preferred": (r["width_bytes"] == ref["width_bytes"]
                          and r["anchor_in_record"] == ref["anchor_in_record"]),
        })
    return out


# --- axis ------------------------------------------------------------------

def _axis_index(labels, value, what, path):
    for i, v in enumerate(labels):
        if v == value:
            return i, True
    # nearest, but flagged inexact -- an inexact axis match is never "forced"
    best = min(range(len(labels)), key=lambda i: abs(labels[i] - value))
    return best, False


def _analyze_axis(table, band_name, obs, axes, tables):
    """Derive cell stride, row stride and major order -- or refuse to."""
    out = {
        "table": table,
        "band": band_name,
        "axis_declared": table in axes,
        "observations": obs,
        "forced": False,
        "mapping": None,
        "confidence": "unknown",
        "measured_strides": {},
        "layouts": {},
        "next_cells_to_confirm": [],
        "reason": "",
    }

    if len(obs) < 2:
        out["reason"] = (
            "Only {} located cell(s) in this band. An axis mapping needs at "
            "least two cells at known (rpm, TPS). Nothing is emitted -- a "
            "guessed rpm/TPS band silently mis-scopes every safety check that "
            "reads it.".format(len(obs)))
        return out

    ax = axes.get(table)
    if not ax:
        out["reason"] = (
            "No axis breakpoints declared for table {!r}. The record indices "
            "of the edited cells are known ({}), but converting an rpm/TPS "
            "VALUE into a row/column INDEX requires the list of row and column "
            "labels TMax Tuner shows. Without them the cell stride and the row "
            "stride cannot be separated. Add an \"axes\" entry with rpm_rows "
            "and tps_cols, or record row_index/col_index on each cell, and "
            "re-run -- no re-editing needed."
            .format(table, ", ".join(str(o["record_index"]) for o in obs)))
        return out

    rows, cols = ax["rpm_rows"], ax["tps_cols"]
    exact = True
    for o in obs:
        if "row_index" not in o or o["row_index"] is None:
            r, ok_r = _axis_index(rows, o["rpm"], "rpm", table)
            o["row_index"], o["row_index_exact"] = r, ok_r
            exact = exact and ok_r
        else:
            o["row_index_exact"] = True
        if "col_index" not in o or o["col_index"] is None:
            c, ok_c = _axis_index(cols, o["tps"], "tps", table)
            o["col_index"], o["col_index_exact"] = c, ok_c
            exact = exact and ok_c
        else:
            o["col_index_exact"] = True

    if not exact:
        bad = [o for o in obs
               if not o.get("row_index_exact") or not o.get("col_index_exact")]
        out["reason"] = (
            "{} edited cell(s) do not match any declared axis label exactly "
            "({}). The declared rpm_rows/tps_cols are not the axis TMax "
            "Tuner is actually showing, so no index can be assigned and no "
            "axis is emitted."
            .format(len(bad),
                    "; ".join("{} rpm / {}% TPS".format(_n(o["rpm"]), _n(o["tps"]))
                              for o in bad)))
        return out

    n_rpm, n_tps = len(rows), len(cols)

    # Direct measurement beats inference: two cells sharing a row measure the
    # cell stride, two sharing a column measure the row stride.
    for i in range(len(obs)):
        for j in range(i + 1, len(obs)):
            a, b = obs[i], obs[j]
            di = b["record_index"] - a["record_index"]
            if a["row_index"] == b["row_index"] and a["col_index"] != b["col_index"]:
                dc = b["col_index"] - a["col_index"]
                if di % dc == 0:
                    out["measured_strides"]["cell_stride_records"] = di // dc
                    out["measured_strides"]["cell_stride_measured_from"] = \
                        [a["edit"], b["edit"]]
            if a["col_index"] == b["col_index"] and a["row_index"] != b["row_index"]:
                dr = b["row_index"] - a["row_index"]
                if di % dr == 0:
                    out["measured_strides"]["row_stride_records"] = di // dr
                    out["measured_strides"]["row_stride_measured_from"] = \
                        [a["edit"], b["edit"]]

    def _try(name, predict, describe):
        base = obs[0]["record_index"] - predict(obs[0])
        bad = [o for o in obs if o["record_index"] - predict(o) != base]
        out["layouts"][name] = {
            "consistent": not bad,
            "origin_record_index": base,
            "description": describe,
            "mismatches": [
                {"edit": o["edit"], "expected_record_index": base + predict(o),
                 "actual_record_index": o["record_index"]} for o in bad],
        }

    _try("rpm_major",
         lambda o: o["row_index"] * n_tps + o["col_index"],
         "one contiguous run per RPM row; consecutive records step through TPS "
         "columns. record = origin + rpm_row * {} + tps_col".format(n_tps))
    _try("tps_major",
         lambda o: o["col_index"] * n_rpm + o["row_index"],
         "one contiguous run per TPS column; consecutive records step through "
         "RPM rows. record = origin + tps_col * {} + rpm_row".format(n_rpm))

    fits = [k for k, v in out["layouts"].items() if v["consistent"]]
    distinct_rows = len({o["row_index"] for o in obs})
    distinct_cols = len({o["col_index"] for o in obs})

    if not fits:
        out["reason"] = (
            "NEITHER a rpm-major nor a tps-major {}x{} grid explains the "
            "observed record indices ({}). The band is not a plain contiguous "
            "grid on this stride -- it may be paged, padded, or the band's "
            "start/stride in tables.json is wrong. No axis is emitted."
            .format(n_rpm, n_tps,
                    ", ".join(str(o["record_index"]) for o in obs)))
        return out
    if len(fits) > 1:
        out["reason"] = (
            "AMBIGUOUS: both layouts ({}) explain these cells equally well. "
            "The edited cells do not distinguish them. Edit one more cell in "
            "the SAME rpm row but a different TPS column -- that separates the "
            "two immediately.".format(" and ".join(fits)))
        return out

    layout = fits[0]
    lay = out["layouts"][layout]
    if distinct_rows < 2 or distinct_cols < 2:
        out["reason"] = (
            "Only {} distinct rpm row(s) and {} distinct TPS column(s) were "
            "edited. {} is the only layout consistent with them, but with the "
            "cells all on one line the row stride and the cell stride are "
            "still entangled. This is REPORTED, NOT EMITTED."
            .format(distinct_rows, distinct_cols, layout))
        out["confidence"] = "low"
        out["candidate_layout"] = layout
        out["next_cells_to_confirm"] = _next_cells(
            obs, rows, cols, layout, lay["origin_record_index"], n_rpm, n_tps)
        return out

    out["forced"] = True
    out["confidence"] = "high" if len(obs) >= 3 else "medium"
    out["mapping"] = {
        "layout": layout,
        "description": lay["description"],
        "origin_record_index": lay["origin_record_index"],
        "n_rpm_rows": n_rpm,
        "n_tps_cols": n_tps,
        "rpm_rows": rows,
        "tps_cols": cols,
        "cell_stride_records": 1,
        "row_stride_records": n_tps if layout == "rpm_major" else 1,
        "col_stride_records": 1 if layout == "rpm_major" else n_rpm,
        "evidence": [
            "edit {}: {} rpm / {}% TPS = row {}, col {} -> record index {}"
            .format(o["edit"], _n(o["rpm"]), _n(o["tps"]), o["row_index"],
                    o["col_index"], o["record_index"]) for o in obs],
        "caveat": (
            "Forced by {} cells at {} distinct rpm rows and {} distinct TPS "
            "columns. It is the ONLY simple contiguous layout consistent with "
            "them; it is not proof that the whole band is that grid. Confirm "
            "with the cells listed in next_cells_to_confirm before anything "
            "safety-critical reads it."
            .format(len(obs), distinct_rows, distinct_cols)),
    }
    out["next_cells_to_confirm"] = _next_cells(
        obs, rows, cols, layout, lay["origin_record_index"], n_rpm, n_tps)
    return out


def _next_cells(obs, rows, cols, layout, origin, n_rpm, n_tps):
    """Exactly which further cells would confirm (or break) the layout."""
    anchor = obs[0]
    band = anchor["band_def"]
    stride = max(1, int(band.get("stride") or 1)) if band else 1
    lo = band["_lo"] if band else 0

    def rec(r, c):
        return origin + (r * n_tps + c if layout == "rpm_major"
                         else c * n_rpm + r)

    picks = []
    r0, c0 = anchor["row_index"], anchor["col_index"]
    for label, r, c in (
            ("one TPS column over, same rpm row", r0, min(c0 + 1, n_tps - 1)),
            ("one rpm row down, same TPS column", min(r0 + 1, n_rpm - 1), c0),
            ("the opposite corner of the map", n_rpm - 1, n_tps - 1)):
        if (r, c) == (r0, c0):
            continue
        if any(o["row_index"] == r and o["col_index"] == c for o in obs):
            continue
        idx = rec(r, c)
        picks.append({
            "why": label,
            "rpm": rows[r], "tps": cols[c],
            "row_index": r, "col_index": c,
            "predicted_record_index": idx,
            "predicted_offset": lo + idx * stride,
            "predicted_offset_hex": f"0x{lo + idx * stride:05X}",
            "instruction": (
                "In TMax Tuner change ONLY the {} rpm / {}% TPS cell of this "
                "table, save as a new .tbw, add it to the declaration and "
                "re-run. If its bytes move at {} the layout is confirmed; if "
                "they move anywhere else the layout above is WRONG."
                .format(_n(rows[r]), _n(cols[c]),
                        f"0x{lo + idx * stride:05X}")),
        })
    return picks


# --- the public entry point ------------------------------------------------

def analyze(experiment, tables=None):
    """Analyse a ground-truth experiment. Never writes anything.

    `experiment` may be a path to a declaration file or an already-loaded
    (validated) experiment dict.
    """
    if isinstance(experiment, (str, Path)):
        experiment = load_experiment(experiment)
    elif "edits" in experiment and "baseline" in experiment and \
            not isinstance(experiment.get("baseline", {}).get("path", ""), Path):
        experiment = validate_experiment(experiment)
    tables = tables or table_map.load_map()

    warnings = []
    data = {}
    try:
        data["baseline"] = _read_bytes(experiment["baseline"]["path"])
    except OSError as e:
        raise ExperimentError(
            f"baseline {experiment['baseline']['file']}: {e}") from e

    churn_offsets = set()
    control_info = {"declared": bool(experiment["control"])}
    if experiment["control"]:
        try:
            ctrl = _read_bytes(experiment["control"]["path"])
        except OSError as e:
            raise ExperimentError(
                f"control {experiment['control']['file']}: {e}") from e
        if len(ctrl) != len(data["baseline"]):
            raise ExperimentError(
                "control save is a different size than the baseline; cannot "
                "measure save churn")
        churn_offsets = set(_changed_offsets(data["baseline"], ctrl))
        payload_churn = sorted(o for o in churn_offsets
                               if not _is_structural_churn(o, tables))
        control_info.update({
            "file": experiment["control"]["file"],
            "churn_offsets": len(churn_offsets),
            "churn_outside_metadata": len(payload_churn),
            "churn_bands": sorted({
                (table_map.band_for_offset(o, tables) or {}).get("name", "UNMAPPED")
                for o in payload_churn}),
            "note": (
                "MEASURED save-to-save churn: {} byte(s) differ between two "
                "no-edit saves, {} of them outside the metadata block. Those "
                "offsets are subtracted from every edit's diff by measurement, "
                "not by assumption."
                .format(len(churn_offsets), len(payload_churn))),
        })
        if payload_churn:
            warnings.append(
                "{} byte(s) change between two NO-EDIT saves in bands {} -- "
                "outside the metadata block. Anything landing there in an edit "
                "cannot be attributed to the edit."
                .format(len(payload_churn),
                        ", ".join(control_info["churn_bands"])))
    else:
        control_info["note"] = (
            "NO control save. Save-to-save churn is excluded structurally "
            "(category METADATA / below 0x{:X}) instead of being measured, so "
            "churn landing inside shared_churn_unresolved -- the block "
            "tables.json says moves on every edit -- is indistinguishable from "
            "the edit itself.".format(tmx.METADATA_BOUNDARY))
        warnings.append(
            "No control save (a SECOND no-edit save) was declared. One extra "
            "save would let this analysis MEASURE the churn instead of "
            "assuming it. Strongly recommended before trusting any patch.")

    edit_results = []
    for e in experiment["edits"]:
        try:
            to = _read_bytes(e["path"])
        except OSError as err:
            raise ExperimentError(f"edit {e['id']} ({e['file']}): {err}") from err
        src = data.get(e["from"]) if e["from"] != "baseline" else data["baseline"]
        if src is None:
            src = _read_bytes(e["from_path"])
        data[e["id"]] = to
        if len(to) != len(src):
            raise ExperimentError(
                f"edit {e['id']} ({e['file']}) is {len(to)} bytes but "
                f"{e['from_file']} is {len(src)}; cannot diff")
        res = _analyze_edit(e, src, to, tables, churn_offsets)
        res["_src"] = src
        res["_edit"] = e
        edit_results.append(res)

    # ---- per-band scale arithmetic ----------------------------------------
    per_band = {}
    per_band_obs = {}
    cells_index = {}
    for res in edit_results:
        e = res["_edit"]
        eng_delta, delta_src = _eng_delta(e)
        for c in res["payload_cells"]:
            r = c["preferred_reading"]
            if r is None or c["band"] is None:
                continue
            if eng_delta is None:
                res["issues"].append(
                    "Cannot derive a scale: " + str(delta_src))
                continue
            # A multi-cell edit can only be attributed 1:1 when every declared
            # cell moved by the same amount AND the same number of cells moved.
            if e["cells"] and len(e["cells"]) > 1 and len(res["payload_cells"]) != 1:
                pass  # still usable: eng_delta is common to all declared cells
            cell = e["cells"][0] if e["cells"] else None
            key = (_cell_key(e["table"], cell["rpm"], cell["tps"]) if cell
                   else f"{e['table']} @ whole map")
            eng_from = cell["old"] if cell else None
            eng_to = cell["new"] if cell else None
            scale = r["raw_delta"] / eng_delta
            entry = {
                "edit": e["id"],
                "table": e["table"],
                "unit": e["units"],
                "cell_key": key,
                "rpm": cell["rpm"] if cell else None,
                "tps": cell["tps"] if cell else None,
                "record_index": c["record_index"],
                "record_offset": c["record_offset"],
                "offset_hex": r["offset_hex"],
                "stride": c["stride"],
                "width_bytes": r["width_bytes"],
                "anchor_in_record": r["anchor_in_record"],
                "raw_from": r["raw_from"],
                "raw_to": r["raw_to"],
                "raw_delta": r["raw_delta"],
                "eng_from": eng_from,
                "eng_to": eng_to,
                "eng_delta": eng_delta,
                "eng_delta_source": delta_src,
                "scale": round(scale, 6),
                "interval": _scale_interval(r["raw_delta"], eng_delta),
                "arithmetic": (
                    "scale = raw_delta / (new - old) = {:+d} raw / ({} - {}) "
                    "{} = {:+d} / {} = {} raw per {}"
                    .format(r["raw_delta"], _n(eng_to), _n(eng_from), e["units"],
                            r["raw_delta"], _n(eng_delta), _n(round(scale, 4)),
                            e["units"])
                    if cell else
                    "scale = raw_delta / uniform_delta = {:+d} raw / {} {} = "
                    "{} raw per {}".format(r["raw_delta"], _n(eng_delta),
                                           e["units"], _n(round(scale, 4)),
                                           e["units"])),
                "clean": res["clean"],
                "reading_basis": c["reading_basis"],
                "delta_is_width_robust": c["delta_is_width_robust"],
                "absolute_is_width_robust": c["absolute_is_width_robust"],
                "all_readings": c["readings"],
            }
            per_band.setdefault(c["band"], []).append(entry)
            if eng_from is not None:
                obs = per_band_obs.setdefault(c["band"], [])
                pt_from = {"eng": eng_from, "raw": r["raw_from"],
                           "source": f"{e['id']} (before)", "cell": key}
                pt_to = {"eng": eng_to, "raw": r["raw_to"],
                         "source": f"{e['id']} (after)", "cell": key}
                for pt in (pt_from, pt_to):
                    if not any(o["eng"] == pt["eng"] and o["raw"] == pt["raw"]
                               and o["cell"] == pt["cell"] for o in obs):
                        obs.append(pt)
                cells_index.setdefault(key, []).append(entry)

    scales = {name: _analyze_band_scale(name, rows, per_band_obs.get(name, []),
                                        tables)
              for name, rows in sorted(per_band.items())}

    # ---- axis -------------------------------------------------------------
    axis = {}
    by_table = {}
    for name, rows in per_band.items():
        for r in rows:
            if r["rpm"] is None:
                continue
            by_table.setdefault((r["table"], name), []).append(r)
    for (table, band_name), rows in sorted(by_table.items()):
        band_def = next((b for b in tables["bands"] if b["name"] == band_name),
                        None)
        seen, obs = set(), []
        for r in rows:
            key = (r["rpm"], r["tps"])
            if key in seen:
                continue
            seen.add(key)
            obs.append({"edit": r["edit"], "rpm": r["rpm"], "tps": r["tps"],
                        "record_index": r["record_index"],
                        "record_offset": r["record_offset"],
                        "offset_hex": r["offset_hex"],
                        "band_def": band_def})
        axis[f"{table} :: {band_name}"] = _analyze_axis(
            table, band_name, obs, experiment["axes"], tables)

    # ---- contradictions with what tables.json already claims ---------------
    contradictions = _contradictions(scales, tables)

    for res in edit_results:
        res.pop("_src", None)
        res.pop("_edit", None)

    headline = _headline(scales, axis, edit_results, contradictions)
    result = {
        "schema": SCHEMA_VERSION,
        "experiment": {
            "title": experiment["title"],
            "recorded_by": experiment["recorded_by"],
            "date": experiment["date"],
            "tuner_version": experiment["tuner_version"],
            "dir": experiment["dir"],
            "declaration_path": experiment.get("declaration_path"),
            "baseline": experiment["baseline"]["file"],
            "control": (experiment["control"]["file"]
                        if experiment["control"] else None),
            "n_edits": len(experiment["edits"]),
        },
        "control": control_info,
        "edits": edit_results,
        "scales": scales,
        "axis": axis,
        "cells": {k: [e["edit"] for e in v] for k, v in cells_index.items()},
        "contradictions": contradictions,
        "headline": headline,
        "warnings": warnings,
        "never_writes_tbw": True,
    }
    result["honesty"] = _honesty_audit(result)
    return result


def _contradictions(scales, tables):
    """Anything the experiment REFUTES about tables.json / dyno_bridge."""
    out = []
    finding = tables.get("timing_scale_finding") or {}
    declared = finding.get("raw_per_degree")
    for name, s in scales.items():
        band = next((b for b in tables["bands"] if b["name"] == name), None)
        got = s["scale"]["raw_per_unit"]
        if got is None or not s["safe_for_patch"]:
            continue
        if (declared and band and band["category"] == "TIMING"
                and str(s["unit"]).lower().startswith("deg")):
            iv = s["scale"]["interval"] or [got, got]
            if not (min(iv) <= float(declared) <= max(iv)):
                out.append({
                    "kind": "scale",
                    "band": name,
                    "target": "tables.json timing_scale_finding.raw_per_degree",
                    "claimed": float(declared),
                    "measured": got,
                    "measured_interval": iv,
                    "severity": "loud",
                    "detail": (
                        "tables.json claims ~{} raw per degree and its {} band "
                        "note repeats it. This experiment measures {} raw/{} "
                        "(allowed {}..{}) on that band -- {} is NOT inside the "
                        "measured interval. The assumed value is REFUTED for "
                        "this band. It must not be quietly kept: record it in "
                        "dyno_bridge.CONTRADICTED_SCALES so it cannot be "
                        "re-adopted, exactly as was already done for "
                        "timing_map_main."
                        .format(_n(declared), name, _n(got), s["unit"],
                                _n(min(iv)), _n(max(iv)), _n(declared))),
                })
        if band and band.get("needs_ground_truth") and s["safe_for_patch"]:
            out.append({
                "kind": "closes_open_question",
                "band": name,
                "target": f"tables.json bands[{name}].needs_ground_truth",
                "claimed": band["needs_ground_truth"],
                "measured": got,
                "severity": "info",
                "detail": (
                    "This band's needs_ground_truth note is now answered for "
                    "the SCALE ({} raw/{}). It is NOT answered for the axis "
                    "order unless the axis section below says forced."
                    .format(_n(got), s["unit"])),
            })
    return out


def _headline(scales, axis, edits, contradictions):
    locked = [n for n, s in scales.items() if s["safe_for_patch"]
              and s["scale"]["raw_per_unit"] is not None]
    absolute = [n for n, s in scales.items() if s["absolute_ready"]]
    forced_axes = [k for k, a in axis.items() if a["forced"]]
    dirty = [e["id"] for e in edits if not e["clean"]]
    dead = [e["id"] for e in edits if e["no_change"]]

    if dead:
        scale_line = ("NOTHING LEARNED from edit(s) {} -- those saves are "
                      "byte-identical to their source.".format(", ".join(dead)))
    elif not scales:
        scale_line = ("NO SCALE. No edit produced an isolable cell change in a "
                      "named band.")
    elif locked:
        parts = []
        for n in locked:
            s = scales[n]
            parts.append("{} = {} raw per {} ({} scale, {} confidence, {} "
                         "observation(s))"
                         .format(n, _n(s["scale"]["raw_per_unit"]), s["unit"],
                                 s["scale"]["kind"].upper(),
                                 s["scale"]["confidence"],
                                 s["scale"]["observations"]))
        scale_line = "SCALE LOCKED: " + "; ".join(parts)
    else:
        scale_line = ("SCALE NOT LOCKED: every band that moved failed its "
                      "cleanliness or consistency check -- see blockers.")

    if absolute:
        zero_line = ("ABSOLUTE READING UNLOCKED for {}: linearity tested, zero "
                     "point pinned, field width unambiguous."
                     .format(", ".join(absolute)))
    elif scales:
        why = []
        for n, s in scales.items():
            if s["absolute_blockers"]:
                why.append("{}: {}".format(n, s["absolute_blockers"][0]))
        zero_line = ("STILL DELTA-ONLY -- no band earned an ABSOLUTE scale. "
                     + " | ".join(why))
    else:
        zero_line = "STILL DELTA-ONLY -- nothing to fit."

    if forced_axes:
        axis_line = "AXIS FORCED for {}: {}".format(
            ", ".join(forced_axes),
            "; ".join(axis[k]["mapping"]["layout"] for k in forced_axes))
    elif axis:
        axis_line = "AXIS NOT DERIVED. " + " | ".join(
            f"{k}: {a['reason']}" for k, a in axis.items())
    else:
        axis_line = ("AXIS NOT DERIVED -- no cell landed in a named band with "
                     "a declared (rpm, TPS).")

    loud = [c for c in contradictions if c["severity"] == "loud"]
    return {
        "scale": scale_line,
        "zero_point": zero_line,
        "axis": axis_line,
        "clean": not dirty and not dead,
        "dirty_edits": dirty,
        "dead_edits": dead,
        "bands_locked": locked,
        "bands_absolute": absolute,
        "axes_forced": forced_axes,
        "contradictions": [c["detail"] for c in loud],
        "experiment_quality": (
            "CLEAN" if not dirty and not dead else "NOT CLEAN"),
    }


# ---------------------------------------------------------------------------
# The proposed patch -- returned, never written
# ---------------------------------------------------------------------------

def propose_tables_json_patch(analysis):
    """The EXACT edits src/tables.json and dyno_bridge.py should receive.

    Returns the patch plus its evidence. It NEVER writes a file: a human reads
    the evidence and applies it. Refuses to emit anything for a band whose
    experiment was not clean, or where two edits of the same cell disagree.
    """
    bands_patch = {}
    known_scales = {}
    absolute_bands = []
    contradicted = {}
    band_axes = {}
    axis_specs = {}
    evidence = []
    refused = []

    exp = analysis.get("experiment", {})
    provenance = ("ground-truth experiment {!r} run in TMax Tuner {} on {} by "
                  "{}; baseline {}, control {}"
                  .format(exp.get("title") or "(untitled)",
                          exp.get("tuner_version") or "(version not recorded)",
                          exp.get("date") or "(date not recorded)",
                          exp.get("recorded_by") or "(unattributed)",
                          exp.get("baseline"),
                          exp.get("control") or "NONE (churn not measured)"))

    for name, s in sorted(analysis["scales"].items()):
        if not s["safe_for_patch"]:
            refused.append({
                "band": name, "what": "scale",
                "reason": "; ".join(s["blockers"]),
                "detail": (
                    "No patch is emitted for this band. A scale derived from a "
                    "dirty or self-contradictory experiment would be written "
                    "into tables.json as fact and then read by the dyno and "
                    "the guardrails as fact."),
            })
            continue

        k = s["scale"]["raw_per_unit"]
        entry = {
            "raw_per_unit": k,
            "unit": s["unit"],
            "kind": s["scale"]["kind"],
            "basis": "ground_truth_experiment",
            "confidence": s["scale"]["confidence"],
            "observations": s["scale"]["observations"],
            "linearity_tested": s["linearity"]["tested"],
            "record": {
                "stride_bytes": s["record"]["stride_bytes"],
                "value_width_bytes": s["record"]["value_width_bytes"],
                "anchor_in_record": s["record"]["anchor_in_record"],
            },
            "measured_on": {
                "edits": s["edits_used"],
                "cells": s["cells"],
                "arithmetic": [p["arithmetic"] for p in s["per_edit"]],
                "provenance": provenance,
            },
        }
        if s["absolute_ready"] and s["zero_point"]:
            entry["zero_raw"] = s["zero_point"]["zero_raw"]
            entry["zero_point_basis"] = s["zero_point"]["arithmetic"]
            entry["absolute_formula"] = "eng = (raw - zero_raw) / raw_per_unit"
            absolute_bands.append(name)
        else:
            entry["zero_raw"] = None
            entry["zero_point_basis"] = (
                "NOT ESTABLISHED: " + "; ".join(s["absolute_blockers"]))
            entry["absolute_formula"] = None
            entry["caveat"] = (
                "DELTA SCALE ONLY. Converts a CHANGE in raw to a CHANGE in {}. "
                "Do NOT divide a raw cell by this number and call the result a "
                "reading.".format(s["unit"]))

        bands_patch[name] = {
            "set": {
                "scale": entry,
                "ground_truth": {
                    "closed_for": (["scale", "zero_point"]
                                   if s["absolute_ready"] else ["scale"]),
                    "still_open": ([] if s["absolute_ready"]
                                   else ["zero_point"]) + (
                        [] if any(a["forced"] and a["band"] == name
                                  for a in analysis["axis"].values())
                        else ["axis_order"]),
                    "provenance": provenance,
                },
            },
            "note_on_needs_ground_truth": (
                "The scale half of this band's needs_ground_truth note is now "
                "answered. Do NOT delete the note -- narrow it to what is still "
                "open (see ground_truth.still_open)."),
        }
        known_scales[name] = {
            "raw_per_unit": k,
            "unit": s["unit"],
            "unit_label": s["unit"],
            "basis": "measured",
            "confidence": s["scale"]["confidence"],
            "source": ("ground_truth.py analysis of " + provenance + ". "
                       + "; ".join(p["arithmetic"] for p in s["per_edit"])),
            "caveat": (entry.get("caveat")
                       or "Absolute readings use eng = (raw - {}) / {}."
                       .format(_n(entry["zero_raw"]), _n(k))),
            "zero_point_basis": entry["zero_point_basis"],
        }
        evidence.append({
            "band": name,
            "claim": "{} raw per {} ({} scale)".format(_n(k), s["unit"],
                                                       s["scale"]["kind"]),
            "arithmetic": [p["arithmetic"] for p in s["per_edit"]],
            "linearity": s["linearity"]["detail"],
            "zero_point": (s["zero_point"]["arithmetic"] if s["zero_point"]
                           else "not fitted"),
            "clean_edits": s["clean_edits"],
        })

    for c in analysis["contradictions"]:
        if c["severity"] != "loud":
            continue
        contradicted[c["band"]] = c["detail"]

    for key, a in sorted(analysis["axis"].items()):
        if not a["forced"]:
            refused.append({
                "band": a["band"], "what": "axis",
                "reason": a["reason"],
                "detail": ("dyno_bridge.BAND_AXES stays empty. A guessed "
                           "offset -> (rpm, TPS) mapping silently mis-scopes "
                           "every safety check that reads rpm_band, and "
                           "dyno_bridge's own self-check FAILS if BAND_AXES is "
                           "populated without evidence."),
            })
            continue
        m = a["mapping"]
        axis_specs[a["band"]] = {
            "table": a["table"],
            "layout": m["layout"],
            "origin_record_index": m["origin_record_index"],
            "rpm_rows": m["rpm_rows"],
            "tps_cols": m["tps_cols"],
            "cell_stride_records": m["cell_stride_records"],
            "row_stride_records": m["row_stride_records"],
            "evidence": m["evidence"],
            "caveat": m["caveat"],
            "confidence": a["confidence"],
        }
        band_axes.update(_materialize_axes(a, analysis))

    dyno_patch = {
        "KNOWN_SCALES": known_scales,
        "ABSOLUTE_SCALE_BANDS": sorted(absolute_bands),
        "CONTRADICTED_SCALES": contradicted,
        "BAND_AXES": band_axes,
        "BAND_AXIS_SPECS": axis_specs,
        "notes": [
            "KNOWN_SCALES entries above are ready to paste, but ONLY the "
            "bands listed in ABSOLUTE_SCALE_BANDS may be read absolutely; "
            "dyno_bridge._check_scale_knowledge_declared() enforces that every "
            "ABSOLUTE_SCALE_BANDS member has basis 'measured'.",
            "BAND_TO_DYNO_TABLES is NOT patched here. A band earns a dyno "
            "table only when it is a COMMANDED map, not a clamp or a learned "
            "store -- that is a judgement about what the table DOES, which no "
            "byte diff can settle.",
            "BAND_AXES is keyed by byte offset today. If BAND_AXIS_SPECS is "
            "non-empty, prefer storing the spec and expanding it, rather than "
            "committing thousands of literal offsets.",
        ],
    }

    safe = bool(bands_patch) and not any(
        not s["safe_for_patch"] for s in analysis["scales"].values())
    return {
        "safe_to_apply": safe,
        "summary": (
            "{} band(s) ready to patch, {} refused."
            .format(len(bands_patch), len(refused))),
        "tables_json": {"bands": bands_patch},
        "dyno_bridge": dyno_patch,
        "evidence": evidence,
        "refused": refused,
        "apply_instructions": (
            "NOTHING HAS BEEN WRITTEN. Review evidence[] and every 'arithmetic' "
            "line against what TMax Tuner showed, then apply by hand: the "
            "tables_json.bands.<band>.set object merges into that band in "
            "src/tables.json, and dyno_bridge.KNOWN_SCALES / "
            "ABSOLUTE_SCALE_BANDS / CONTRADICTED_SCALES / BAND_AXES take the "
            "entries above. Re-run `python3 src/dyno_bridge.py selftest` "
            "afterwards -- honesty.tbw_scale_knowledge_is_declared_and_bounded "
            "is the check that catches a scale promoted past its evidence."),
        "never_writes_tbw": True,
        "never_writes_anything": True,
    }


def _materialize_axes(a, analysis):
    """offset -> {rpm_band, tps_band} for a FORCED axis, using midpoints.

    virtual_dyno wants rpm/tps RANGES, but a table cell sits at a breakpoint,
    not across a range. Midway-to-midway is the standard reading and it is
    stated as an assumption rather than smuggled in.
    """
    m = a["mapping"]
    band_def = a["observations"][0]["band_def"] if a["observations"] else None
    if not band_def:
        return {}
    lo = band_def["_lo"]
    stride = max(1, int(band_def.get("stride") or 1))
    rows, cols = m["rpm_rows"], m["tps_cols"]

    def edges(vals, i):
        lo_v = vals[i] if i == 0 else (vals[i - 1] + vals[i]) / 2.0
        hi_v = vals[i] if i == len(vals) - 1 else (vals[i] + vals[i + 1]) / 2.0
        return [round(lo_v, 3), round(hi_v, 3)]

    out = {}
    for r in range(len(rows)):
        for c in range(len(cols)):
            idx = m["origin_record_index"] + (
                r * len(cols) + c if m["layout"] == "rpm_major"
                else c * len(rows) + r)
            off = lo + idx * stride
            if off >= band_def["_hi"]:
                continue
            out[off] = {
                "rpm_band": edges(rows, r),
                "tps_band": edges(cols, c),
                "rpm_breakpoint": rows[r],
                "tps_breakpoint": cols[c],
                "basis": "forced axis mapping, midpoint-to-midpoint cell edges",
            }
    return out


# ---------------------------------------------------------------------------
# Honesty audit -- the rules, checked against our own output
# ---------------------------------------------------------------------------

def _honesty_audit(result):
    checks = []

    def chk(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    scales = result["scales"]
    axis = result["axis"]

    bad = [n for n, s in scales.items()
           if s["scale"]["kind"] == "absolute" and not s["absolute_ready"]]
    chk("no_absolute_scale_without_zero_point_and_linearity", not bad,
        "A scale is labelled ABSOLUTE only with linearity tested (>=3 "
        "observations), a fitted zero point, and an unambiguous field width. "
        "Offenders: " + (", ".join(bad) if bad else "none"))

    bad = [n for n, s in scales.items()
           if s["scale"]["observations"] < 3 and s["linearity"]["tested"]]
    chk("linearity_claimed_only_when_a_third_point_exists", not bad,
        "Two points always fit a line exactly, so linearity cannot be claimed "
        "from a single edit. Offenders: " + (", ".join(bad) if bad else "none"))

    bad = [k for k, a in axis.items() if a["mapping"] is not None and not a["forced"]]
    chk("no_axis_emitted_unless_forced", not bad,
        "An rpm/TPS mapping is emitted only when exactly one simple layout "
        "explains the data and the cells span >=2 rows and >=2 columns. "
        "Offenders: " + (", ".join(bad) if bad else "none"))

    bad = [n for n, s in scales.items()
           if s["safe_for_patch"] and (s["dirty_edits"] or s["disagreements"])]
    chk("dirty_or_contradictory_experiments_never_reach_a_patch", not bad,
        "safe_for_patch is False whenever a contributing edit was not clean or "
        "two edits of the same cell disagree. Offenders: "
        + (", ".join(bad) if bad else "none"))

    bad = [n for n, s in scales.items()
           if s["scale"]["raw_per_unit"] is not None
           and (not s["scale"].get("basis") or s["scale"]["observations"] < 2)]
    chk("every_scale_declares_its_basis_and_observation_count", not bad,
        "Offenders: " + (", ".join(bad) if bad else "none"))

    loud = [c for c in result["contradictions"] if c["severity"] == "loud"]
    chk("contradictions_are_surfaced_not_swallowed", True,
        "{} loud contradiction(s) with tables.json are reported in "
        "contradictions[] and in the headline: {}"
        .format(len(loud), "; ".join(c["target"] for c in loud) or "none"))

    chk("never_writes_a_tbw", True,
        "This module opens .tbw files read-only and writes nothing at all -- "
        "not the tunes, not tables.json, not dyno_bridge.py. "
        "propose_tables_json_patch() RETURNS the patch for a human to apply.")

    failed = [c["name"] for c in checks if not c["passed"]]
    return {
        "passed": not failed,
        "checks": checks,
        "failed": failed,
        "statement": (
            "These checks guard the output of THIS analysis against its own "
            "honesty rules. They cannot tell you the numbers Joshua wrote down "
            "were read correctly off the TMax Tuner screen -- nothing here can. "
            "Every scale below is only as good as the values recorded by hand."),
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_check(res):
    out = ["GROUND-TRUTH PREFLIGHT — " + ("READY" if res["ok"] else "NOT READY"),
           "=" * 72, res["verdict"], ""]
    out.append(f"{'file':10} {'name':28} {'size':>8} {'base map':18} changed")
    out.append("-" * 78)
    for fid, f in res["files"].items():
        size = f["size_bytes"] if f["size_bytes"] is not None else "-"
        flag = "" if f["size_ok"] else " !"
        changed = f.get("changed_bytes_vs_from")
        payload = f.get("payload_bytes_vs_from")
        ch = "-" if changed is None else f"{changed} ({payload} payload)"
        if fid == "control" and "churn_bytes_vs_baseline" in f:
            ch = f"{f['churn_bytes_vs_baseline']} churn"
        out.append(f"{fid:10} {f['name'][:28]:28} {str(size):>8}{flag} "
                   f"{str(f.get('base_map_id') or '-'):18} {ch}")
    out.append("")
    if not res["problems"]:
        out.append("No problems found.")
    for p in res["problems"]:
        out.append(f"[{p['severity'].upper():5}] {p['file']}: {p['message']}")
        if p["fix"]:
            out.append(f"          fix: {p['fix']}")
    out.append("")
    out.append(f"{res['errors']} error(s), {res['warnings']} warning(s).")
    if not res["ok"]:
        out.append("DO NOT LEAVE THE WINDOWS MACHINE — every error above needs "
                   "TMax Tuner to fix.")
    return "\n".join(out)


def _fmt_analysis(res, patch=None):
    h = res["headline"]
    out = []
    exp = res["experiment"]
    out.append("GROUND-TRUTH EXPERIMENT — " + (exp["title"] or "(untitled)"))
    out.append("=" * 72)
    out.append("HEADLINE")
    out.append("  SCALE : " + h["scale"])
    out.append("  ZERO  : " + h["zero_point"])
    out.append("  AXIS  : " + h["axis"])
    out.append("  QUALITY: " + h["experiment_quality"]
               + (f"  (dirty: {', '.join(h['dirty_edits'])})"
                  if h["dirty_edits"] else "")
               + (f"  (no change: {', '.join(h['dead_edits'])})"
                  if h["dead_edits"] else ""))
    for c in h["contradictions"]:
        out.append("  ** CONTRADICTION ** " + c)
    out.append("")
    out.append("CHURN CONTROL")
    out.append("  " + res["control"]["note"])
    out.append("")

    out.append("EDITS")
    for e in res["edits"]:
        bd = e["byte_diff"]
        out.append(f"  [{e['id']}] {e['file']}  (from {e['from_file']})  "
                   f"table {e['table']!r}, units {e['units']!r}")
        for c in e["declared"]["cells"]:
            out.append("      declared: {} rpm / {}% TPS  {} -> {} {}"
                       .format(_n(c["rpm"]), _n(c["tps"]), _n(c["old"]),
                               _n(c["new"]), e["units"]))
        if e["declared"]["uniform_delta"] is not None:
            out.append("      declared: whole map {:+g} {}"
                       .format(e["declared"]["uniform_delta"], e["units"]))
        out.append("      bytes: {} changed = {} structural churn + {} measured "
                   "save churn + {} PAYLOAD"
                   .format(bd["total_changed"], bd["structural_churn_excluded"],
                           bd["control_churn_excluded"], bd["payload_changed"]))
        out.append("      verdict: " + ("CLEAN — one cell, one band"
                                        if e["clean"] else "NOT CLEAN"))
        for c in e["payload_cells"]:
            r = c["preferred_reading"]
            out.append("      cell @ {} band {} [{}] record #{} stride {}"
                       .format(c["record_offset_hex"], c["band"] or "UNMAPPED",
                               c["category"], c["record_index"], c["stride"]))
            if r:
                out.append("           raw {} -> {}  (delta {:+d}, {}-byte LE "
                           "at +{} in the record)"
                           .format(r["raw_from"], r["raw_to"], r["raw_delta"],
                                   r["width_bytes"], r["anchor_in_record"]))
                out.append("           delta is {} across candidate widths"
                           .format("STABLE" if c["delta_is_width_robust"]
                                   else "WIDTH-DEPENDENT " + str(c["delta_candidates"])))
            out.append("           reading basis: " + c["reading_basis"])
        for i in e["issues"]:
            out.append("      ! " + i)
        out.append("")

    out.append("DERIVED SCALES")
    if not res["scales"]:
        out.append("  none — no edit produced an isolable cell change in a "
                   "named band.")
    for name, s in res["scales"].items():
        out.append(f"  {name}  [{'PATCHABLE' if s['safe_for_patch'] else 'NOT SAFE'}]")
        for p in s["per_edit"]:
            out.append(f"      {p['edit']}: {p['arithmetic']}")
            out.append("           at {} (record #{}), allowing +/-{} raw of "
                       "reading error the scale is {}..{}"
                       .format(p["offset_hex"], p["record_index"],
                               _n(ROUNDING_RAW), _n(p["interval"][0]),
                               _n(p["interval"][1])))
        for d in s["disagreements"]:
            out.append("      ** DISAGREEMENT ** " + d["detail"])
        out.append("      linearity: " + s["linearity"]["detail"])
        sc = s["scale"]
        out.append("      => {} raw per {}  [{} SCALE, {} confidence, {} "
                   "observations]".format(_n(sc["raw_per_unit"]), sc["unit"],
                                          sc["kind"].upper(), sc["confidence"],
                                          sc["observations"]))
        out.append("         " + sc["kind_note"])
        if s["zero_point"]:
            z = s["zero_point"]
            out.append("      zero point: " + z["arithmetic"])
            out.append("         " + z["meaning"])
            if len(s["zero_point_by_width"]) > 1:
                out.append("         zero point BY FIELD WIDTH (the delta is "
                           "width-robust, the zero point is not):")
                for w in s["zero_point_by_width"]:
                    tag = " <- preferred" if w["preferred"] else ""
                    if not w["survives_pooling"]:
                        tag = " (RULED OUT: " + w["ruled_out_by"] + ")"
                    out.append("            {}-byte at +{}: zero_raw = {}{}"
                               .format(w["width_bytes"], w["anchor_in_record"],
                                       _n(w["zero_raw"]), tag))
        for b in s["absolute_blockers"]:
            out.append("      absolute reading blocked: " + b)
        for b in s["blockers"]:
            out.append("      ! PATCH BLOCKED: " + b)
        out.append("")

    out.append("AXIS MAPPING")
    if not res["axis"]:
        out.append("  none — no cell landed in a named band with a declared "
                   "(rpm, TPS).")
    for key, a in res["axis"].items():
        out.append(f"  {key}: " + ("FORCED" if a["forced"] else "NOT DERIVED"))
        for o in a["observations"]:
            out.append("      {} : {} rpm / {}% TPS -> record #{} at {}"
                       .format(o["edit"], _n(o["rpm"]), _n(o["tps"]),
                               o["record_index"], o["offset_hex"]))
        if a["measured_strides"]:
            out.append("      measured: " + json.dumps(a["measured_strides"]))
        for lname, lay in a["layouts"].items():
            out.append("      {:10} {}  (origin record #{})"
                       .format(lname, "CONSISTENT" if lay["consistent"]
                               else "ruled out", lay["origin_record_index"]))
        if a["forced"]:
            m = a["mapping"]
            out.append("      => {}: {}".format(m["layout"], m["description"]))
            out.append("         cell stride {} record(s), row stride {} "
                       "record(s), origin record #{}"
                       .format(m["cell_stride_records"],
                               m["row_stride_records"],
                               m["origin_record_index"]))
            out.append("         confidence: " + a["confidence"])
            out.append("         caveat: " + m["caveat"])
        else:
            out.append("      reason: " + a["reason"])
        for nc in a["next_cells_to_confirm"]:
            out.append("      next: {} — {} rpm / {}% TPS, predicted {}"
                       .format(nc["why"], _n(nc["rpm"]), _n(nc["tps"]),
                               nc["predicted_offset_hex"]))
        out.append("")

    if res["contradictions"]:
        out.append("CONTRADICTIONS WITH tables.json")
        for c in res["contradictions"]:
            out.append(f"  [{c['severity'].upper()}] {c['target']}")
            out.append("      " + c["detail"])
        out.append("")

    for w in res["warnings"]:
        out.append("WARNING: " + w)
    if res["warnings"]:
        out.append("")

    ho = res["honesty"]
    out.append("HONESTY SELF-CHECK — " + ("PASS" if ho["passed"] else "FAIL"))
    for c in ho["checks"]:
        out.append(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}")
        out.append("        " + c["detail"])
    out.append("  " + ho["statement"])

    if patch is not None:
        out.append("")
        out.append("PROPOSED PATCH — " + ("SAFE TO APPLY" if patch["safe_to_apply"]
                                          else "NOT SAFE / PARTIAL"))
        out.append("  " + patch["summary"])
        for r in patch["refused"]:
            out.append(f"  REFUSED {r['band']} ({r['what']}): {r['reason']}")
        out.append("  " + patch["apply_instructions"])
        out.append("")
        out.append(json.dumps({"tables_json": patch["tables_json"],
                               "dyno_bridge": patch["dyno_bridge"]}, indent=2))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv=None):
    p = argparse.ArgumentParser(
        prog="ground_truth.py",
        description="Analyse the TBW one-cell ground-truth experiment "
                    "(read-only; never writes a .tbw, tables.json, or anything "
                    "else).")
    p.add_argument("--example", action="store_true",
                   help="print a template declaration (JSON on stdout, field "
                        "guide on stderr) and exit")
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("check", help="validate the saves BEFORE walking away "
                                      "from the Windows machine")
    pc.add_argument("experiment")
    pc.add_argument("--json", action="store_true")

    pa = sub.add_parser("analyze", help="derive scale, zero point and axis")
    pa.add_argument("experiment")
    pa.add_argument("--json", action="store_true")
    pa.add_argument("--patch", action="store_true",
                    help="also print the proposed tables.json / dyno_bridge "
                         "patch (printed, never written)")

    a = p.parse_args(argv)

    if a.example:
        print(json.dumps(EXAMPLE_EXPERIMENT, indent=2))
        print(EXAMPLE_FIELD_GUIDE, file=sys.stderr)
        return 0

    if a.cmd == "check":
        res = check_experiment(a.experiment)
        print(json.dumps(res, indent=2, default=str) if a.json
              else _fmt_check(res))
        return 0 if res["ok"] else 1

    if a.cmd == "analyze":
        res = analyze(a.experiment)
        patch = propose_tables_json_patch(res) if a.patch else None
        if a.json:
            print(json.dumps({"analysis": res, "patch": patch}, indent=2,
                             default=str))
        else:
            print(_fmt_analysis(res, patch))
        return 0 if res["honesty"]["passed"] else 1

    p.print_help()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except BrokenPipeError:
        raise SystemExit(0)
    except (ExperimentError, tmx.TbwError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)
