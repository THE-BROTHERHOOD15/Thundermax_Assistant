#!/usr/bin/env python3
"""Tests for the TBW ground-truth experiment analysis (src/ground_truth.py).

Self-contained: builds synthetic 214470-byte TBW images in a temp dir exactly
like tests/test_thundermax.py and tests/test_dyno_bridge.py do. No NAS, no real
tunes, no Ollama, no Elasticsearch, no network.

    python3 -m unittest discover -s tests -v

The point of this module is to derive a NUMBER that will be written into
tables.json and then read by the virtual dyno and the guardrails as fact. So
the important tests here are the ones that prove it REFUSES:

  * a dirty experiment (the edit moved cells in several bands) -> no patch
  * two edits of the same cell that disagree                   -> no patch
  * a scale from a single edit                                 -> never ABSOLUTE
  * an axis that the data does not force                       -> never emitted

A wrong scale here is a wrong degree figure in front of Joshua, and the engine
pays for it.

NOTHING in this file writes a `.tbw` anywhere but a TemporaryDirectory.
"""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ground_truth as gt        # noqa: E402
import table_map                 # noqa: E402
import thundermax_parser as tmx  # noqa: E402


# Band anchors straight out of tables.json, so these tests break loudly if the
# frozen band map ever moves.
TABLES = table_map.load_map()
BAND = {b["name"]: b for b in TABLES["bands"]}
TM = BAND["timing_map_main"]                 # 0x01FC9 .. 0x02182, stride 4
TM_LO, TM_HI, TM_STRIDE = TM["_lo"], TM["_hi"], TM["stride"]
TM_RECORDS = (TM_HI - TM_LO) // TM_STRIDE    # 110
AFR = BAND["afr_target"]                     # 0x01989 .. 0x01BCA, stride 8
AFR_LO, AFR_STRIDE = AFR["_lo"], AFR["stride"]
SHARED_OFFSET = 0x4500                       # inside shared_churn_unresolved
META_OFFSET = 0x100                          # inside section_metadata_checksums

# A 10 x 11 grid fills timing_map_main's 110 records exactly.
ROWS = [1024, 1536, 2048, 2560, 3072, 3584, 4096, 4608, 5120, 5632]
COLS = [0, 2, 5, 10, 15, 20, 25, 30, 40, 50, 100]
TABLE = "Ignition Timing - Timing vs TPS @ RPM"

SCALE = 24.5          # raw units per degree, injected
BASE_MAP = b"HXSSEDCAAN061617"


def synthetic_tbw(map_id=BASE_MAP, fill=0x55):
    """A structurally valid fake tune image (same helper as the sibling tests)."""
    data = bytearray([fill] * tmx.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[tmx.MAP_ID_OFFSET] = len(map_id)
    data[tmx.MAP_ID_OFFSET + 1: tmx.MAP_ID_OFFSET + 1 + len(map_id)] = map_id
    return data


def rec_rpm_major(row, col):
    return row * len(COLS) + col


def rec_tps_major(row, col):
    return col * len(ROWS) + row


def raw_for(deg, zero=0.0):
    return int(round(deg * SCALE + zero))


class GTBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # -- image construction -------------------------------------------------

    def tune(self, records=None, map_id=BASE_MAP, meta_bump=0):
        """A tune whose timing_map_main is a clean zeroed grid of u32 records."""
        d = synthetic_tbw(map_id)
        for i in range(TM_LO, TM_HI):
            d[i] = 0
        for idx, raw in (records or {}).items():
            struct.pack_into("<I", d, TM_LO + idx * TM_STRIDE, int(raw))
        for i in range(META_OFFSET, META_OFFSET + meta_bump):
            d[i] = (d[i] + 7) % 256
        return d

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(bytes(data))
        return p

    def declare(self, edits, control=True, axes=True, extra=None):
        doc = {
            "schema": gt.SCHEMA_VERSION,
            "title": "unit test experiment",
            "recorded_by": "test",
            "date": "2026-08-20",
            "tuner_version": "TMax Tuner (test)",
            "dir": ".",
            "baseline": {"file": "GT_A.tbw"},
            "edits": edits,
        }
        if control:
            doc["control"] = {"file": "GT_A2.tbw"}
        if axes:
            doc["axes"] = {TABLE: {"rpm_rows": ROWS, "tps_cols": COLS}}
        doc.update(extra or {})
        p = self.dir / "experiment.json"
        p.write_text(json.dumps(doc, indent=2))
        return p

    def edit(self, eid, fname, rpm, tps, old, new, **kw):
        e = {"id": eid, "file": fname, "table": TABLE, "units": "deg BTDC",
             "cells": [{"rpm": rpm, "tps": tps, "old": old, "new": new}]}
        e.update(kw)
        return e

    # -- the standard clean experiment --------------------------------------

    def clean_experiment(self, zero=0.0, with_c=True, with_d=True,
                         with_d2=True, control=True, axes=True,
                         c_raw_delta=None):
        """GT_A / GT_A2 / GT_B / GT_C / GT_D / GT_D2, all clean.

        B and C edit the SAME cell (3072 rpm / 20% TPS). D is a far corner at a
        much higher value, so its high byte differs and the narrow field-width
        readings get ruled out. D2 is B's immediate neighbour one TPS column
        over, which measures the cell stride directly.
        """
        i_b = rec_rpm_major(4, 5)      # 3072 rpm, 20% TPS
        i_d = rec_rpm_major(0, 10)     # 1024 rpm, 100% TPS
        i_d2 = rec_rpm_major(4, 6)     # 3072 rpm, 25% TPS
        base_records = {
            i_b: raw_for(28.0, zero),
            i_d: raw_for(40.0, zero),
            i_d2: raw_for(28.0, zero),
        }
        a = self.tune(base_records, meta_bump=4)
        self.write("GT_A.tbw", a)
        self.write("GT_A2.tbw", self.tune(base_records, meta_bump=9))

        edits = []
        b = self.tune({**base_records, i_b: raw_for(30.0, zero)}, meta_bump=11)
        self.write("GT_B.tbw", b)
        edits.append(self.edit("B", "GT_B.tbw", 3072, 20, 28.0, 30.0,
                               expect_band="timing_map_main"))
        if with_c:
            c_raw = (raw_for(28.0, zero) + c_raw_delta if c_raw_delta is not None
                     else raw_for(24.0, zero))
            self.write("GT_C.tbw",
                       self.tune({**base_records, i_b: c_raw}, meta_bump=13))
            edits.append(self.edit("C", "GT_C.tbw", 3072, 20, 28.0, 24.0,
                                   expect_band="timing_map_main"))
        if with_d:
            self.write("GT_D.tbw", self.tune(
                {**base_records, i_d: raw_for(42.0, zero)}, meta_bump=6))
            edits.append(self.edit("D", "GT_D.tbw", 1024, 100, 40.0, 42.0,
                                   expect_band="timing_map_main"))
        if with_d2:
            self.write("GT_D2.tbw", self.tune(
                {**base_records, i_d2: raw_for(30.0, zero)}, meta_bump=8))
            edits.append(self.edit("D2", "GT_D2.tbw", 3072, 25, 28.0, 30.0,
                                   expect_band="timing_map_main"))
        return self.declare(edits, control=control, axes=axes)


# ---------------------------------------------------------------------------
# The declaration format
# ---------------------------------------------------------------------------

class TestDeclaration(GTBase):
    def test_example_is_valid_against_its_own_validator(self):
        exp = gt.validate_experiment(json.loads(json.dumps(gt.EXAMPLE_EXPERIMENT)),
                                     base_dir=self.dir)
        self.assertEqual(exp["schema"], gt.SCHEMA_VERSION)
        self.assertEqual(exp["baseline"]["file"], "GT_A.tbw")
        self.assertTrue(exp["control"], "the example must model the control save")
        self.assertGreaterEqual(len(exp["edits"]), 3)
        ids = [e["id"] for e in exp["edits"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_example_cli_prints_loadable_json(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = gt._main(["--example"])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["schema"], gt.SCHEMA_VERSION)

    def test_wrong_schema_rejected(self):
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment({"schema": "nope"}, base_dir=self.dir)
        self.assertIn("schema", str(cm.exception))

    def test_missing_baseline_rejected(self):
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment({"schema": gt.SCHEMA_VERSION, "edits": []},
                                   base_dir=self.dir)
        self.assertIn("baseline", str(cm.exception))

    def test_zero_delta_edit_rejected(self):
        doc = {"schema": gt.SCHEMA_VERSION, "baseline": {"file": "a.tbw"},
               "edits": [{"id": "B", "file": "b.tbw", "table": "T",
                          "units": "deg",
                          "cells": [{"rpm": 3000, "tps": 20,
                                     "old": 28.0, "new": 28.0}]}]}
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment(doc, base_dir=self.dir)
        self.assertIn("equals 'old'", str(cm.exception))

    def test_cells_and_uniform_delta_are_mutually_exclusive(self):
        doc = {"schema": gt.SCHEMA_VERSION, "baseline": {"file": "a.tbw"},
               "edits": [{"id": "E", "file": "e.tbw", "table": "T",
                          "units": "deg", "uniform_delta": 1.0,
                          "cells": [{"rpm": 1, "tps": 1, "old": 1, "new": 2}]}]}
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment(doc, base_dir=self.dir)
        self.assertIn("exactly one of", str(cm.exception))

    def test_duplicate_edit_id_rejected(self):
        doc = {"schema": gt.SCHEMA_VERSION, "baseline": {"file": "a.tbw"},
               "edits": [
                   {"id": "B", "file": "b.tbw", "table": "T", "units": "deg",
                    "cells": [{"rpm": 1, "tps": 1, "old": 1, "new": 2}]},
                   {"id": "B", "file": "c.tbw", "table": "T", "units": "deg",
                    "cells": [{"rpm": 1, "tps": 1, "old": 1, "new": 3}]}]}
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment(doc, base_dir=self.dir)
        self.assertIn("duplicate id", str(cm.exception))

    def test_forward_from_reference_rejected(self):
        doc = {"schema": gt.SCHEMA_VERSION, "baseline": {"file": "a.tbw"},
               "edits": [
                   {"id": "B", "file": "b.tbw", "from": "C", "table": "T",
                    "units": "deg",
                    "cells": [{"rpm": 1, "tps": 1, "old": 1, "new": 2}]},
                   {"id": "C", "file": "c.tbw", "table": "T", "units": "deg",
                    "cells": [{"rpm": 1, "tps": 1, "old": 1, "new": 3}]}]}
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment(doc, base_dir=self.dir)
        self.assertIn("declared LATER", str(cm.exception))

    def test_chained_edit_must_agree_on_the_value_it_started_from(self):
        doc = {"schema": gt.SCHEMA_VERSION, "baseline": {"file": "a.tbw"},
               "edits": [
                   {"id": "B", "file": "b.tbw", "table": "T", "units": "deg",
                    "cells": [{"rpm": 3000, "tps": 20, "old": 28.0,
                               "new": 30.0}]},
                   {"id": "C", "file": "c.tbw", "from": "B", "table": "T",
                    "units": "deg",
                    "cells": [{"rpm": 3000, "tps": 20, "old": 28.0,
                               "new": 32.0}]}]}
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment(doc, base_dir=self.dir)
        self.assertIn("left that cell at", str(cm.exception))

    def test_axis_needs_at_least_two_labels(self):
        doc = {"schema": gt.SCHEMA_VERSION, "baseline": {"file": "a.tbw"},
               "axes": {"T": {"rpm_rows": [1000], "tps_cols": [0, 10]}},
               "edits": [{"id": "B", "file": "b.tbw", "table": "T",
                          "units": "deg",
                          "cells": [{"rpm": 1, "tps": 1, "old": 1, "new": 2}]}]}
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.validate_experiment(doc, base_dir=self.dir)
        self.assertIn("rpm_rows", str(cm.exception))

    def test_load_experiment_resolves_paths_relative_to_the_declaration(self):
        p = self.clean_experiment()
        exp = gt.load_experiment(p)
        self.assertEqual(exp["baseline"]["path"], self.dir / "GT_A.tbw")
        self.assertTrue(exp["baseline"]["path"].exists())

    def test_bad_json_gives_a_clear_error(self):
        p = self.dir / "broken.json"
        p.write_text("{ not json")
        with self.assertRaises(gt.ExperimentError) as cm:
            gt.load_experiment(p)
        self.assertIn("not valid JSON", str(cm.exception))


# ---------------------------------------------------------------------------
# check -- the preflight
# ---------------------------------------------------------------------------

class TestCheck(GTBase):
    def test_clean_experiment_passes_preflight(self):
        res = gt.check_experiment(self.clean_experiment())
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["errors"], 0)
        self.assertIn("READY", res["verdict"])
        # a control save was declared, so no "no control save" warning
        self.assertFalse(any("no control save declared" in p["message"]
                             for p in res["problems"]))

    def test_missing_file_is_an_error(self):
        p = self.clean_experiment()
        (self.dir / "GT_C.tbw").unlink()
        res = gt.check_experiment(p)
        self.assertFalse(res["ok"])
        self.assertTrue(any(p_["file"] == "C" and "cannot read" in p_["message"]
                            for p_ in res["problems"]))

    def test_wrong_size_is_an_error(self):
        p = self.clean_experiment()
        (self.dir / "GT_C.tbw").write_bytes(bytes(self.tune())[:100000])
        res = gt.check_experiment(p)
        self.assertFalse(res["ok"])
        self.assertTrue(any("expected 214470" in x["message"]
                            for x in res["problems"]))

    def test_different_base_map_is_an_error(self):
        p = self.clean_experiment()
        self.write("GT_C.tbw", self.tune({}, map_id=b"ZZSSQXETDN100720"))
        res = gt.check_experiment(p)
        self.assertFalse(res["ok"])
        self.assertTrue(any("base map is" in x["message"]
                            for x in res["problems"]))

    def test_byte_identical_save_is_reported_as_the_edit_not_saving(self):
        p = self.clean_experiment()
        (self.dir / "GT_C.tbw").write_bytes((self.dir / "GT_A.tbw").read_bytes())
        res = gt.check_experiment(p)
        self.assertFalse(res["ok"])
        msg = next(x["message"] for x in res["problems"] if x["file"] == "C")
        self.assertIn("BYTE-IDENTICAL", msg)
        self.assertIn("did not save", msg)

    def test_metadata_only_change_is_reported_as_not_reaching_a_map(self):
        p = self.clean_experiment()
        # a save that only bumped checksum bytes -- no table data moved
        self.write("GT_C.tbw", self.tune(
            {rec_rpm_major(4, 5): raw_for(28.0),
             rec_rpm_major(0, 10): raw_for(40.0),
             rec_rpm_major(4, 6): raw_for(28.0)}, meta_bump=20))
        res = gt.check_experiment(p)
        self.assertFalse(res["ok"])
        msg = next(x["message"] for x in res["problems"] if x["file"] == "C")
        self.assertIn("metadata/checksum churn", msg)

    def test_missing_control_is_a_warning_not_an_error(self):
        res = gt.check_experiment(self.clean_experiment(control=False))
        self.assertTrue(res["ok"])
        self.assertTrue(any("no control save declared" in x["message"]
                            for x in res["problems"]))
        self.assertEqual(res["errors"], 0)

    def test_check_writes_nothing(self):
        p = self.clean_experiment()
        before = {f.name: f.read_bytes() for f in self.dir.iterdir()}
        gt.check_experiment(p)
        after = {f.name: f.read_bytes() for f in self.dir.iterdir()}
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Scale derivation
# ---------------------------------------------------------------------------

class TestScale(GTBase):
    def test_clean_single_cell_edit_yields_the_injected_scale(self):
        res = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                               with_d2=False))
        s = res["scales"]["timing_map_main"]
        self.assertEqual(s["scale"]["raw_per_unit"], SCALE)
        self.assertEqual(s["unit"], "deg BTDC")
        self.assertTrue(s["safe_for_patch"], s["blockers"])

    def test_the_arithmetic_is_shown(self):
        res = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                               with_d2=False))
        p = res["scales"]["timing_map_main"]["per_edit"][0]
        # 30 - 28 = 2 deg moved 2 * 24.5 = 49 raw
        self.assertEqual(p["raw_delta"], 49)
        self.assertEqual(p["eng_delta"], 2.0)
        self.assertEqual(p["arithmetic"],
                         "scale = raw_delta / (new - old) = +49 raw / (30 - 28) "
                         "deg BTDC = +49 / 2 = 24.5 raw per deg BTDC")
        self.assertEqual(p["interval"], [24.0, 25.0])

    def test_a_single_edit_is_a_delta_scale_never_an_absolute_one(self):
        res = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                               with_d2=False))
        s = res["scales"]["timing_map_main"]
        self.assertEqual(s["scale"]["kind"], "delta")
        self.assertFalse(s["absolute_ready"])
        self.assertFalse(s["linearity"]["tested"])
        self.assertEqual(s["scale"]["observations"], 2)
        self.assertIn("NOT TESTED", s["linearity"]["detail"])
        self.assertTrue(any("linearity is UNTESTED" in b
                            for b in s["absolute_blockers"]))

    def test_b_and_c_agreeing_confirms_linearity(self):
        res = gt.analyze(self.clean_experiment())
        s = res["scales"]["timing_map_main"]
        self.assertEqual(s["scale"]["raw_per_unit"], SCALE)
        self.assertEqual(s["disagreements"], [])
        self.assertTrue(s["linearity"]["tested"])
        self.assertTrue(s["linearity"]["consistent"])
        self.assertEqual(s["linearity"]["max_residual_raw"], 0.0)
        self.assertEqual(s["scale"]["confidence"], "high")
        self.assertTrue(s["safe_for_patch"], s["blockers"])

    def test_negative_direction_edit_gives_the_same_positive_scale(self):
        res = gt.analyze(self.clean_experiment())
        by_edit = {p["edit"]: p for p in res["scales"]["timing_map_main"]["per_edit"]}
        self.assertEqual(by_edit["C"]["raw_delta"], -98)
        self.assertEqual(by_edit["C"]["eng_delta"], -4.0)
        self.assertEqual(by_edit["C"]["scale"], SCALE)

    def test_scale_survives_a_rounded_raw_delta(self):
        # A real ECU rounds: 49 raw one way, 97 (not 98) the other. The
        # intervals still intersect, so this is rounding, not a contradiction.
        res = gt.analyze(self.clean_experiment(c_raw_delta=-97))
        s = res["scales"]["timing_map_main"]
        self.assertEqual(s["disagreements"], [])
        self.assertTrue(s["safe_for_patch"], s["blockers"])


class TestDisagreement(GTBase):
    def test_b_and_c_disagreeing_is_a_reported_contradiction(self):
        # C's cell moves -70 raw for a -4 deg edit => 17.5 raw/deg, nowhere
        # near B's 24.5 even allowing a full raw unit of reading error.
        res = gt.analyze(self.clean_experiment(c_raw_delta=-70))
        s = res["scales"]["timing_map_main"]
        self.assertTrue(s["disagreements"], "the mismatch must be reported")
        d = s["disagreements"][0]
        self.assertEqual(sorted(d["edits"]), ["B", "C"])
        self.assertTrue(d["same_cell"])
        self.assertIn("NOT AVERAGED", d["detail"])
        self.assertIn("24.5", d["detail"])
        self.assertIn("17.5", d["detail"])

    def test_disagreement_suppresses_the_patch(self):
        res = gt.analyze(self.clean_experiment(c_raw_delta=-70))
        s = res["scales"]["timing_map_main"]
        self.assertFalse(s["safe_for_patch"])
        self.assertFalse(s["absolute_ready"])
        patch = gt.propose_tables_json_patch(res)
        self.assertFalse(patch["safe_to_apply"])
        self.assertEqual(patch["tables_json"]["bands"], {})
        self.assertEqual(patch["dyno_bridge"]["KNOWN_SCALES"], {})
        self.assertTrue(any(r["band"] == "timing_map_main" and r["what"] == "scale"
                            for r in patch["refused"]))

    def test_disagreement_is_never_averaged_away(self):
        res = gt.analyze(self.clean_experiment(c_raw_delta=-70))
        s = res["scales"]["timing_map_main"]
        scales = {p["edit"]: p["scale"] for p in s["per_edit"]}
        self.assertEqual(scales["B"], 24.5)
        self.assertEqual(scales["C"], 17.5)
        # the mean of the two (21.0) must not appear as the answer
        self.assertNotEqual(s["scale"]["raw_per_unit"], 21.0)


# ---------------------------------------------------------------------------
# Zero point
# ---------------------------------------------------------------------------

class TestZeroPoint(GTBase):
    def test_zero_at_zero_is_recovered_and_unlocks_absolute(self):
        res = gt.analyze(self.clean_experiment(zero=0.0))
        s = res["scales"]["timing_map_main"]
        self.assertIsNotNone(s["zero_point"])
        self.assertEqual(s["zero_point"]["zero_raw"], 0.0)
        self.assertTrue(s["zero_point"]["is_zero"])
        self.assertTrue(s["absolute_ready"], s["absolute_blockers"])
        self.assertEqual(s["scale"]["kind"], "absolute")

    def test_a_nonzero_zero_point_is_recovered_and_stated(self):
        # raw = deg * 24.5 + 100. The DELTA scale is unchanged; only the
        # absolute reading moves, which is exactly the trap this catches.
        res = gt.analyze(self.clean_experiment(zero=100.0))
        s = res["scales"]["timing_map_main"]
        self.assertEqual(s["scale"]["raw_per_unit"], SCALE)
        self.assertEqual(s["zero_point"]["zero_raw"], 100.0)
        self.assertFalse(s["zero_point"]["is_zero"])
        self.assertIn("does NOT correspond", s["zero_point"]["meaning"])

    def test_zero_point_arithmetic_is_shown(self):
        res = gt.analyze(self.clean_experiment(zero=100.0))
        z = res["scales"]["timing_map_main"]["zero_point"]
        self.assertIn("zero_raw =", z["arithmetic"])
        self.assertIn("* scale", z["arithmetic"])
        self.assertIn("100", z["arithmetic"])

    def test_zero_point_is_reported_per_candidate_field_width(self):
        res = gt.analyze(self.clean_experiment())
        s = res["scales"]["timing_map_main"]
        widths = {w["width_bytes"]: w for w in s["zero_point_by_width"]}
        self.assertIn(2, widths)
        self.assertIn(4, widths)
        # the 1-byte reading cannot cover an edit that carries into byte 1, so
        # pooling rules it out rather than silently averaging over it
        if 1 in widths:
            self.assertFalse(widths[1]["survives_pooling"])
            self.assertTrue(widths[1]["ruled_out_by"])

    def test_field_width_ambiguity_blocks_an_absolute_reading(self):
        # B alone only moves the low byte, so a 1-byte reading survives and the
        # absolute value is genuinely ambiguous. The delta is not.
        res = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                               with_d2=False))
        s = res["scales"]["timing_map_main"]
        self.assertEqual(s["scale"]["raw_per_unit"], SCALE)
        self.assertFalse(s["absolute_ready"])
        self.assertEqual(s["scale"]["kind"], "delta")


# ---------------------------------------------------------------------------
# Cleanliness
# ---------------------------------------------------------------------------

class TestCleanliness(GTBase):
    def test_a_clean_edit_is_clean(self):
        res = gt.analyze(self.clean_experiment())
        for e in res["edits"]:
            self.assertTrue(e["clean"], f"{e['id']}: {e['issues']}")
            # one cell, so 1-2 bytes: a small edit only moves the low byte
            self.assertIn(e["byte_diff"]["payload_changed"], (1, 2))
            self.assertGreater(e["byte_diff"]["structural_churn_excluded"], 0)
        self.assertEqual(res["headline"]["experiment_quality"], "CLEAN")

    def test_metadata_churn_is_excluded_not_counted_as_the_edit(self):
        res = gt.analyze(self.clean_experiment())
        e = next(x for x in res["edits"] if x["id"] == "B")
        self.assertEqual(len(e["payload_cells"]), 1)
        self.assertEqual(e["payload_cells"][0]["band"], "timing_map_main")
        self.assertEqual(e["bands_touched"], ["timing_map_main"])

    def test_an_edit_touching_two_bands_is_flagged_and_yields_no_patch(self):
        p = self.clean_experiment()
        # GT_C also moves an afr_target cell: two maps were edited, not one
        dirty = self.tune({rec_rpm_major(4, 5): raw_for(24.0),
                           rec_rpm_major(0, 10): raw_for(40.0),
                           rec_rpm_major(4, 6): raw_for(28.0)}, meta_bump=13)
        struct.pack_into("<H", dirty, AFR_LO + 3 * AFR_STRIDE, 0x1234)
        self.write("GT_C.tbw", dirty)
        res = gt.analyze(p)
        e = next(x for x in res["edits"] if x["id"] == "C")
        self.assertFalse(e["clean"])
        self.assertIn("timing_map_main", e["bands_touched"])
        self.assertIn("afr_target", e["bands_touched"])
        self.assertTrue(any("DIRTY" in i and "different bands" in i
                            for i in e["issues"]))
        patch = gt.propose_tables_json_patch(res)
        self.assertFalse(patch["safe_to_apply"])
        self.assertEqual(patch["tables_json"]["bands"], {})

    def test_an_edit_moving_extra_cells_in_one_band_is_flagged(self):
        p = self.clean_experiment()
        self.write("GT_C.tbw", self.tune(
            {rec_rpm_major(4, 5): raw_for(24.0),
             rec_rpm_major(0, 10): raw_for(40.0),
             rec_rpm_major(4, 6): raw_for(28.0),
             rec_rpm_major(7, 2): 999},          # an extra cell nobody declared
            meta_bump=13))
        res = gt.analyze(p)
        e = next(x for x in res["edits"] if x["id"] == "C")
        self.assertFalse(e["clean"])
        self.assertTrue(any("1 cell(s) declared but 2 cell(s) moved" in i
                            for i in e["issues"]))
        self.assertFalse(res["scales"]["timing_map_main"]["safe_for_patch"])

    def test_two_cells_moving_by_the_same_delta_are_named_as_a_possible_pair(self):
        # One displayed edit landing on two records with an IDENTICAL delta is
        # what a front/rear cylinder pair looks like -- a format finding, but
        # still dirty until the band map records the pairing.
        p = self.clean_experiment()
        self.write("GT_C.tbw", self.tune(
            {rec_rpm_major(4, 5): raw_for(24.0),
             rec_rpm_major(0, 10): raw_for(40.0),
             rec_rpm_major(4, 6): raw_for(24.0)}, meta_bump=13))
        res = gt.analyze(p)
        e = next(x for x in res["edits"] if x["id"] == "C")
        self.assertFalse(e["clean"])
        issue = next(i for i in e["issues"] if "DIRTY" in i)
        self.assertIn("FRONT/REAR pair", issue)
        self.assertIn("-98", issue)
        self.assertFalse(res["scales"]["timing_map_main"]["safe_for_patch"])

    def test_an_edit_with_no_byte_change_at_all_is_reported_clearly(self):
        p = self.clean_experiment()
        (self.dir / "GT_C.tbw").write_bytes((self.dir / "GT_A.tbw").read_bytes())
        res = gt.analyze(p)
        e = next(x for x in res["edits"] if x["id"] == "C")
        self.assertTrue(e["no_change"])
        self.assertFalse(e["clean"])
        self.assertEqual(e["byte_diff"]["total_changed"], 0)
        self.assertTrue(any("NO BYTE CHANGED" in i for i in e["issues"]))
        self.assertTrue(any("never reached the file" in i for i in e["issues"]))
        self.assertIn("C", res["headline"]["dead_edits"])
        self.assertIn("NOTHING LEARNED", res["headline"]["scale"])

    def test_the_control_save_measures_churn_so_a_shared_block_edit_stays_clean(self):
        """A byte that moves between two NO-EDIT saves is not the edit."""
        records = {rec_rpm_major(4, 5): raw_for(28.0)}
        a = self.tune(records, meta_bump=4)
        self.write("GT_A.tbw", a)
        a2 = self.tune(records, meta_bump=9)
        a2[SHARED_OFFSET] ^= 0xFF               # churns on every save
        self.write("GT_A2.tbw", a2)
        b = self.tune({rec_rpm_major(4, 5): raw_for(30.0)}, meta_bump=11)
        b[SHARED_OFFSET] ^= 0xFF                # same free churn, plus the edit
        self.write("GT_B.tbw", b)
        edits = [self.edit("B", "GT_B.tbw", 3072, 20, 28.0, 30.0)]

        with_ctrl = gt.analyze(self.declare(edits, control=True))
        e = next(x for x in with_ctrl["edits"] if x["id"] == "B")
        self.assertEqual(e["byte_diff"]["control_churn_excluded"], 1)
        self.assertEqual(e["bands_touched"], ["timing_map_main"])
        self.assertTrue(e["clean"], e["issues"])

        without = gt.analyze(self.declare(edits, control=False))
        e2 = next(x for x in without["edits"] if x["id"] == "B")
        self.assertEqual(e2["byte_diff"]["control_churn_excluded"], 0)
        self.assertIn("shared_churn_unresolved", e2["bands_touched"])
        self.assertFalse(e2["clean"])
        self.assertTrue(any("No control save" in w for w in without["warnings"]))


# ---------------------------------------------------------------------------
# Axis derivation
# ---------------------------------------------------------------------------

class TestAxis(GTBase):
    def _axis(self, res):
        self.assertEqual(len(res["axis"]), 1, res["axis"])
        return next(iter(res["axis"].values()))

    def test_rpm_major_layout_and_strides_are_recovered(self):
        a = self._axis(gt.analyze(self.clean_experiment()))
        self.assertTrue(a["forced"], a["reason"])
        m = a["mapping"]
        self.assertEqual(m["layout"], "rpm_major")
        self.assertEqual(m["origin_record_index"], 0)
        self.assertEqual(m["cell_stride_records"], 1)
        self.assertEqual(m["row_stride_records"], len(COLS))
        self.assertTrue(a["layouts"]["rpm_major"]["consistent"])
        self.assertFalse(a["layouts"]["tps_major"]["consistent"])

    def test_cell_stride_is_measured_from_the_adjacent_neighbour(self):
        a = self._axis(gt.analyze(self.clean_experiment()))
        self.assertEqual(a["measured_strides"]["cell_stride_records"], 1)
        self.assertEqual(sorted(a["measured_strides"]["cell_stride_measured_from"]),
                         ["B", "D2"])

    def test_tps_major_layout_is_recovered_when_that_is_what_was_injected(self):
        i_b = rec_tps_major(4, 5)
        i_d = rec_tps_major(0, 10)
        i_d2 = rec_tps_major(4, 6)
        base = {i_b: raw_for(28.0), i_d: raw_for(40.0), i_d2: raw_for(28.0)}
        self.write("GT_A.tbw", self.tune(base, meta_bump=4))
        self.write("GT_A2.tbw", self.tune(base, meta_bump=9))
        self.write("GT_B.tbw", self.tune({**base, i_b: raw_for(30.0)}, meta_bump=11))
        self.write("GT_D.tbw", self.tune({**base, i_d: raw_for(42.0)}, meta_bump=6))
        self.write("GT_D2.tbw", self.tune({**base, i_d2: raw_for(30.0)}, meta_bump=8))
        p = self.declare([
            self.edit("B", "GT_B.tbw", 3072, 20, 28.0, 30.0),
            self.edit("D", "GT_D.tbw", 1024, 100, 40.0, 42.0),
            self.edit("D2", "GT_D2.tbw", 3072, 25, 28.0, 30.0)])
        a = self._axis(gt.analyze(p))
        self.assertTrue(a["forced"], a["reason"])
        self.assertEqual(a["mapping"]["layout"], "tps_major")
        self.assertEqual(a["mapping"]["col_stride_records"], len(ROWS))
        self.assertEqual(a["mapping"]["row_stride_records"], 1)
        self.assertFalse(a["layouts"]["rpm_major"]["consistent"])

    def test_no_axis_without_declared_breakpoints(self):
        a = self._axis(gt.analyze(self.clean_experiment(axes=False)))
        self.assertFalse(a["forced"])
        self.assertIsNone(a["mapping"])
        self.assertIn("No axis breakpoints declared", a["reason"])

    def test_no_axis_from_a_single_cell(self):
        a = self._axis(gt.analyze(self.clean_experiment(with_d=False,
                                                        with_d2=False)))
        self.assertFalse(a["forced"])
        self.assertIsNone(a["mapping"])
        self.assertIn("at least two cells", a["reason"])

    def test_cells_on_one_row_are_reported_but_not_emitted(self):
        # B and D2 share the rpm row: consistent with a layout, but the row
        # stride and the cell stride are still entangled.
        a = self._axis(gt.analyze(self.clean_experiment(with_d=False)))
        self.assertFalse(a["forced"])
        self.assertIsNone(a["mapping"])
        self.assertIn("entangled", a["reason"])
        self.assertEqual(a["confidence"], "low")

    def test_an_axis_that_matches_no_declared_label_is_refused(self):
        p = self.clean_experiment()
        doc = json.loads(p.read_text())
        doc["edits"][0]["cells"][0]["rpm"] = 3077     # not an axis label
        p.write_text(json.dumps(doc))
        a = self._axis(gt.analyze(p))
        self.assertFalse(a["forced"])
        self.assertIn("do not match any declared axis label", a["reason"])

    def test_a_layout_no_simple_grid_explains_is_refused(self):
        # Put D's cell at a record index that neither layout can produce.
        i_b = rec_rpm_major(4, 5)
        i_d2 = rec_rpm_major(4, 6)
        base = {i_b: raw_for(28.0), 7: raw_for(40.0), i_d2: raw_for(28.0)}
        self.write("GT_A.tbw", self.tune(base, meta_bump=4))
        self.write("GT_A2.tbw", self.tune(base, meta_bump=9))
        self.write("GT_B.tbw", self.tune({**base, i_b: raw_for(30.0)}, meta_bump=11))
        self.write("GT_D.tbw", self.tune({**base, 7: raw_for(42.0)}, meta_bump=6))
        self.write("GT_D2.tbw", self.tune({**base, i_d2: raw_for(30.0)}, meta_bump=8))
        p = self.declare([
            self.edit("B", "GT_B.tbw", 3072, 20, 28.0, 30.0),
            self.edit("D", "GT_D.tbw", 4608, 50, 40.0, 42.0),
            self.edit("D2", "GT_D2.tbw", 3072, 25, 28.0, 30.0)])
        a = self._axis(gt.analyze(p))
        self.assertFalse(a["forced"])
        self.assertIn("NEITHER", a["reason"])

    def test_next_cells_to_confirm_names_a_concrete_offset(self):
        a = self._axis(gt.analyze(self.clean_experiment()))
        self.assertTrue(a["next_cells_to_confirm"])
        nxt = a["next_cells_to_confirm"][0]
        for key in ("rpm", "tps", "predicted_record_index", "predicted_offset",
                    "predicted_offset_hex", "instruction"):
            self.assertIn(key, nxt)
        self.assertTrue(TM_LO <= nxt["predicted_offset"] < TM_HI)
        self.assertIn("change ONLY", nxt["instruction"])


# ---------------------------------------------------------------------------
# The proposed patch
# ---------------------------------------------------------------------------

class TestPatch(GTBase):
    def test_clean_experiment_produces_a_patch_with_its_evidence(self):
        res = gt.analyze(self.clean_experiment())
        patch = gt.propose_tables_json_patch(res)
        self.assertTrue(patch["safe_to_apply"], patch["refused"])
        band = patch["tables_json"]["bands"]["timing_map_main"]["set"]
        self.assertEqual(band["scale"]["raw_per_unit"], SCALE)
        self.assertEqual(band["scale"]["kind"], "absolute")
        self.assertEqual(band["scale"]["zero_raw"], 0.0)
        self.assertEqual(band["scale"]["basis"], "ground_truth_experiment")
        self.assertTrue(band["scale"]["measured_on"]["arithmetic"])
        self.assertIn("timing_map_main", patch["dyno_bridge"]["KNOWN_SCALES"])
        self.assertIn("timing_map_main", patch["dyno_bridge"]["ABSOLUTE_SCALE_BANDS"])
        self.assertTrue(patch["evidence"])

    def test_a_delta_only_band_is_never_promoted_to_absolute_in_the_patch(self):
        res = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                               with_d2=False))
        patch = gt.propose_tables_json_patch(res)
        band = patch["tables_json"]["bands"]["timing_map_main"]["set"]
        self.assertEqual(band["scale"]["kind"], "delta")
        self.assertIsNone(band["scale"]["zero_raw"])
        self.assertIsNone(band["scale"]["absolute_formula"])
        self.assertIn("DELTA SCALE ONLY", band["scale"]["caveat"])
        self.assertEqual(patch["dyno_bridge"]["ABSOLUTE_SCALE_BANDS"], [])

    def test_band_axes_stays_empty_when_the_axis_is_not_forced(self):
        res = gt.analyze(self.clean_experiment(axes=False))
        patch = gt.propose_tables_json_patch(res)
        self.assertEqual(patch["dyno_bridge"]["BAND_AXES"], {})
        self.assertEqual(patch["dyno_bridge"]["BAND_AXIS_SPECS"], {})
        self.assertTrue(any(r["what"] == "axis" for r in patch["refused"]))

    def test_band_axes_is_materialized_when_the_axis_is_forced(self):
        res = gt.analyze(self.clean_experiment())
        patch = gt.propose_tables_json_patch(res)
        axes = patch["dyno_bridge"]["BAND_AXES"]
        self.assertTrue(axes)
        # B's cell: 3072 rpm / 20% TPS at record 49
        off = TM_LO + rec_rpm_major(4, 5) * TM_STRIDE
        self.assertIn(off, axes)
        self.assertEqual(axes[off]["rpm_breakpoint"], 3072)
        self.assertEqual(axes[off]["tps_breakpoint"], 20)
        self.assertEqual(len(axes[off]["rpm_band"]), 2)
        self.assertLess(axes[off]["rpm_band"][0], axes[off]["rpm_band"][1])
        spec = patch["dyno_bridge"]["BAND_AXIS_SPECS"]["timing_map_main"]
        self.assertEqual(spec["layout"], "rpm_major")

    def test_the_rejected_49_raw_per_degree_assumption_is_contradicted_loudly(self):
        res = gt.analyze(self.clean_experiment())
        loud = [c for c in res["contradictions"] if c["severity"] == "loud"]
        self.assertTrue(loud, "tables.json claims 49 raw/deg; we measured 24.5")
        c = loud[0]
        self.assertEqual(c["claimed"], 49.0)
        self.assertEqual(c["measured"], SCALE)
        self.assertIn("REFUTED", c["detail"])
        patch = gt.propose_tables_json_patch(res)
        self.assertIn("timing_map_main",
                      patch["dyno_bridge"]["CONTRADICTED_SCALES"])

    def test_the_patch_is_returned_never_written(self):
        p = self.clean_experiment()
        tables_before = (Path(gt._SRC) / "tables.json").read_bytes()
        dyno_before = (Path(gt._SRC) / "dyno_bridge.py").read_bytes()
        files_before = {f.name: f.read_bytes() for f in self.dir.iterdir()}
        res = gt.analyze(p)
        patch = gt.propose_tables_json_patch(res)
        self.assertTrue(patch["never_writes_anything"])
        self.assertEqual((Path(gt._SRC) / "tables.json").read_bytes(),
                         tables_before)
        self.assertEqual((Path(gt._SRC) / "dyno_bridge.py").read_bytes(),
                         dyno_before)
        self.assertEqual({f.name: f.read_bytes() for f in self.dir.iterdir()},
                         files_before)


# ---------------------------------------------------------------------------
# Honesty -- the safety tests
# ---------------------------------------------------------------------------

class TestHonesty(GTBase):
    def test_the_self_audit_passes_on_a_clean_experiment(self):
        res = gt.analyze(self.clean_experiment())
        self.assertTrue(res["honesty"]["passed"], res["honesty"]["failed"])

    def test_the_self_audit_passes_on_a_dirty_experiment_too(self):
        # The audit checks OUR OUTPUT's honesty, not the experiment's quality:
        # a dirty experiment must still be reported honestly.
        res = gt.analyze(self.clean_experiment(c_raw_delta=-70))
        self.assertTrue(res["honesty"]["passed"], res["honesty"]["failed"])
        self.assertEqual(res["headline"]["experiment_quality"], "CLEAN")
        self.assertFalse(res["scales"]["timing_map_main"]["safe_for_patch"])

    def test_no_absolute_scale_from_a_single_observation(self):
        res = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                               with_d2=False))
        s = res["scales"]["timing_map_main"]
        self.assertNotEqual(s["scale"]["kind"], "absolute")
        check = next(c for c in res["honesty"]["checks"]
                     if c["name"] == "no_absolute_scale_without_zero_point_and_linearity")
        self.assertTrue(check["passed"])
        self.assertIn("DELTA ONLY", s["scale"]["kind_note"])

    def test_no_axis_is_emitted_when_it_is_not_forced(self):
        for exp in (self.clean_experiment(axes=False),):
            res = gt.analyze(exp)
            for a in res["axis"].values():
                if not a["forced"]:
                    self.assertIsNone(a["mapping"])
            check = next(c for c in res["honesty"]["checks"]
                         if c["name"] == "no_axis_emitted_unless_forced")
            self.assertTrue(check["passed"])

    def test_delta_and_absolute_are_distinguished_everywhere(self):
        delta = gt.analyze(self.clean_experiment(with_c=False, with_d=False,
                                                 with_d2=False))
        absolute = gt.analyze(self.clean_experiment())
        self.assertEqual(delta["scales"]["timing_map_main"]["scale"]["kind"],
                         "delta")
        self.assertEqual(absolute["scales"]["timing_map_main"]["scale"]["kind"],
                         "absolute")
        self.assertIn("DELTA", delta["headline"]["scale"])
        self.assertIn("ABSOLUTE", absolute["headline"]["scale"])

    def test_analyze_never_writes_a_tbw(self):
        p = self.clean_experiment()
        before = {f.name: f.read_bytes() for f in self.dir.iterdir()}
        gt.analyze(p)
        self.assertEqual({f.name: f.read_bytes() for f in self.dir.iterdir()},
                         before)
        self.assertTrue(gt.analyze(p)["never_writes_tbw"])

    def test_a_scale_always_declares_its_basis_and_observation_count(self):
        res = gt.analyze(self.clean_experiment())
        for s in res["scales"].values():
            self.assertEqual(s["scale"]["basis"], "ground_truth_experiment")
            self.assertGreaterEqual(s["scale"]["observations"], 2)
            self.assertIn(s["scale"]["confidence"],
                          ("unknown", "low", "medium", "high"))


# ---------------------------------------------------------------------------
# CLI / formatting smoke tests
# ---------------------------------------------------------------------------

class TestCli(GTBase):
    def _run(self, argv):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = gt._main(argv)
        return rc, buf.getvalue()

    def test_check_cli_on_a_clean_experiment(self):
        rc, out = self._run(["check", str(self.clean_experiment())])
        self.assertEqual(rc, 0)
        self.assertIn("READY", out)
        self.assertIn("GT_A.tbw", out)

    def test_check_cli_exits_nonzero_on_a_missing_save(self):
        p = self.clean_experiment()
        (self.dir / "GT_D.tbw").unlink()
        rc, out = self._run(["check", str(p)])
        self.assertEqual(rc, 1)
        self.assertIn("NOT READY", out)
        self.assertIn("DO NOT LEAVE THE WINDOWS MACHINE", out)

    def test_analyze_cli_leads_with_the_headline(self):
        rc, out = self._run(["analyze", str(self.clean_experiment())])
        self.assertEqual(rc, 0)
        head = out.splitlines()[:8]
        self.assertTrue(any("HEADLINE" in ln for ln in head))
        self.assertIn("SCALE LOCKED", out)
        self.assertIn("AXIS FORCED", out)
        self.assertIn("scale = raw_delta / (new - old)", out)
        self.assertIn("HONESTY SELF-CHECK", out)

    def test_analyze_cli_reports_a_dirty_experiment_loudly(self):
        p = self.clean_experiment(c_raw_delta=-70)
        rc, out = self._run(["analyze", str(p)])
        self.assertIn("DISAGREEMENT", out)
        self.assertIn("NOT AVERAGED", out)
        self.assertIn("PATCH BLOCKED", out)

    def test_analyze_cli_patch_flag_prints_json(self):
        rc, out = self._run(["analyze", str(self.clean_experiment()), "--patch"])
        self.assertEqual(rc, 0)
        self.assertIn("PROPOSED PATCH", out)
        self.assertIn("NOTHING HAS BEEN WRITTEN", out)
        blob = out[out.index('{\n  "tables_json"'):]
        doc = json.loads(blob)
        self.assertIn("timing_map_main", doc["tables_json"]["bands"])

    def test_analyze_cli_json_flag_is_machine_readable(self):
        rc, out = self._run(["analyze", str(self.clean_experiment()), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertIn("analysis", doc)
        self.assertEqual(doc["analysis"]["schema"], gt.SCHEMA_VERSION)

    def test_check_cli_json_flag_is_machine_readable(self):
        rc, out = self._run(["check", str(self.clean_experiment()), "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()
