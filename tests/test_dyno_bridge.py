#!/usr/bin/env python3
"""Tests for the tune -> virtual-dyno bridge (src/dyno_bridge.py).

Self-contained: builds synthetic 214470-byte TBW images in a temp dir exactly
like tests/test_thundermax.py does. No NAS, no real tunes, no Ollama, no
Elasticsearch, no network. Run from the repo root:

    python3 -m unittest discover -s tests -v

The most important tests in here are the HONESTY tests
(TestHonestyConstraint). They are safety tests: they assert the bridge never
puts an engineering-unit number in front of Joshua for a band whose scale
nobody has measured, and never invents an rpm/TPS band. A fabricated number
here is a number he could tune on, and the engine pays for it.

NOTHING in this file writes a `.tbw` anywhere but a TemporaryDirectory.
"""
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import dyno_bridge as db          # noqa: E402
import guardrails as G            # noqa: E402
import table_map                  # noqa: E402
import thundermax_parser as tmx   # noqa: E402
import virtual_dyno as vd         # noqa: E402


def synthetic_tbw(map_id=b"HXSSEDCAAN061617", fill=0x55):
    """A structurally valid fake tune image (same helper as test_thundermax)."""
    data = bytearray([fill] * tmx.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[tmx.MAP_ID_OFFSET] = len(map_id)
    data[tmx.MAP_ID_OFFSET + 1: tmx.MAP_ID_OFFSET + 1 + len(map_id)] = map_id
    return data


# Band anchors, straight out of tables.json, so the tests break loudly if the
# frozen band map ever moves.
TABLES = table_map.load_map()
BAND = {b["name"]: b for b in TABLES["bands"]}
TIMING_LIMIT_LO = BAND["timing_limit_array"]["_lo"]      # 0x0C3C9, stride 8
TIMING_LIMIT_STRIDE = BAND["timing_limit_array"]["stride"]
TIMING_MAP_LO = BAND["timing_map_main"]["_lo"]           # 0x01FC9, stride 4
AFR_TARGET_LO = BAND["afr_target"]["_lo"]                # 0x01989, stride 8
AUTOTUNE_LO = BAND["autotune_learned"]["_lo"]            # 0x0DDC5, stride 2
METADATA_LO = BAND["section_metadata_checksums"]["_lo"]  # 0x00021, stride 1


def seed_band(data, lo, stride, values):
    """Write `values` as LE u16 on the band's own record grid."""
    for i, v in enumerate(values):
        struct.pack_into("<H", data, lo + i * stride, v)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(bytes(data))
        return p

    def timing_pair(self, raw_delta=-49, n=8):
        """A/B pair differing ONLY in the timing_limit_array, by `raw_delta`
        in every cell — i.e. the shape of the real global-timing edit."""
        base = [1666] * n
        a = synthetic_tbw()
        seed_band(a, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, base)
        b = synthetic_tbw()
        seed_band(b, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE,
                  [v + raw_delta for v in base])
        return self.write("a.tbw", a), self.write("b.tbw", b)


# ---------------------------------------------------------------------------
# read_tune
# ---------------------------------------------------------------------------

class TestReadTune(Base):
    def test_reports_file_identity_and_validity(self):
        p = self.write("t.tbw", synthetic_tbw())
        res = db.read_tune(p)
        self.assertEqual(res["base_map_id"], "HXSSEDCAAN061617")
        self.assertTrue(res["valid"])
        self.assertTrue(res["file"]["size_ok"])
        self.assertEqual(res["file"]["size_bytes"], tmx.EXPECTED_SIZE)
        self.assertEqual(res["file"]["size_expected"], tmx.EXPECTED_SIZE)
        self.assertEqual(res["header"], [0x87, 0x4000, 0x1, 0x147])
        # HXSSEDCAAN061617 is one of the bike's own base maps.
        self.assertTrue(res["is_my_setup"])

    def test_flags_a_foreign_base_map(self):
        p = self.write("t.tbw", synthetic_tbw(map_id=b"AAAAAAAAAAAAAAAA"))
        res = db.read_tune(p)
        self.assertFalse(res["is_my_setup"])
        self.assertTrue(any("not one of this bike's known base maps" in w
                            for w in res["warnings"]))

    def test_flags_an_unexpected_size(self):
        p = self.write("short.tbw", synthetic_tbw()[:100000])
        res = db.read_tune(p)
        self.assertFalse(res["file"]["size_ok"])
        self.assertTrue(any("expected 214470" in w for w in res["warnings"]))

    def test_every_band_gets_raw_cell_statistics(self):
        p = self.write("t.tbw", synthetic_tbw())
        res = db.read_tune(p)
        names = {b["name"] for b in res["bands"]}
        self.assertEqual(names, set(BAND))
        for b in res["bands"]:
            c = b["raw_cells"]
            self.assertGreater(c["count"], 0, b["name"])
            for key in ("min", "max", "mean", "mode", "distinct"):
                self.assertIsNotNone(c[key], f"{b['name']}.{key}")
            self.assertIn(b["confidence"], ("high", "medium", "low"))
            self.assertEqual(b["length_bytes"], b["end"] - b["offset"])

    def test_raw_statistics_are_actually_read_from_the_file(self):
        data = synthetic_tbw()
        seed_band(data, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE,
                  [100, 200, 300, 400])
        res = db.read_tune(self.write("t.tbw", data))
        band = next(b for b in res["bands"] if b["name"] == "timing_limit_array")
        self.assertEqual(band["raw_cells"]["min"], 100)
        self.assertEqual(band["raw_cells"]["max"], max(
            [400] + [0x5555]))  # the rest of the band is still fill

    def test_engineering_estimate_only_for_the_measured_band(self):
        data = synthetic_tbw()
        # 34 deg * 49 raw/deg = 1666, the value COLLABORATION.md documents.
        seed_band(data, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, [1666] * 127)
        res = db.read_tune(self.write("t.tbw", data))
        eng = {b["name"]: b["engineering"] for b in res["bands"]
               if b["engineering"]}
        self.assertEqual(set(eng), {"timing_limit_array"},
                         "only the band with a MEASURED scale may be stated in "
                         "engineering units")
        e = eng["timing_limit_array"]
        self.assertEqual(e["unit"], "deg")
        self.assertEqual(e["scale_raw_per_unit"], 49.0)
        self.assertEqual(e["scale_basis"], "measured")
        self.assertTrue(e["is_estimate"])
        self.assertAlmostEqual(e["mode"], 34.0, places=1)
        self.assertTrue(e["caveat"])
        self.assertTrue(e["source"])

    def test_unknown_list_names_every_unresolvable_band_with_a_reason(self):
        res = db.read_tune(self.write("t.tbw", synthetic_tbw()))
        unknown = {u["band"]: u for u in res["unknown"]}
        # Every band except the one measured band must be listed as unknown.
        self.assertEqual(set(unknown), set(BAND) - db.ABSOLUTE_SCALE_BANDS)
        for name, u in unknown.items():
            self.assertTrue(u["reason"].strip(), name)
            self.assertIn(u["confidence"], ("high", "medium", "low"))
        # timing_map_main's reason must carry the rejected-assumption evidence,
        # not a bland "no scale" line.
        self.assertIn("49 raw/deg", unknown["timing_map_main"]["reason"])

    def test_honesty_block_is_present_and_counts_correctly(self):
        res = db.read_tune(self.write("t.tbw", synthetic_tbw()))
        h = res["honesty"]
        self.assertEqual(h["bands_total"], len(res["bands"]))
        self.assertEqual(h["bands_raw_only"], len(res["unknown"]))
        self.assertEqual(h["bands_with_absolute_engineering"]
                         + h["bands_raw_only"], h["bands_total"])
        self.assertTrue(h["never_writes_tbw"])
        self.assertIn("NO band", h["axis_mapping"])

    def test_does_not_modify_the_tune_file(self):
        p = self.write("t.tbw", synthetic_tbw())
        before = p.read_bytes()
        db.read_tune(p)
        self.assertEqual(p.read_bytes(), before,
                         "read_tune must never write a .tbw")


# ---------------------------------------------------------------------------
# changes_from_diff
# ---------------------------------------------------------------------------

class TestChangesFromDiff(Base):
    def test_detects_an_injected_delta_with_the_right_sign(self):
        a, b = self.timing_pair(raw_delta=-49)
        res = db.changes_from_diff(a, b)
        ch = [c for c in res["changes"] if c["band"] == "timing_limit_array"]
        self.assertEqual(len(ch), 1)
        c = ch[0]
        self.assertEqual(c["direction"], "decrease")
        self.assertEqual(c["raw_delta"]["mode"], -49)
        self.assertTrue(c["raw_delta"]["uniform"])
        self.assertEqual(c["category"], "TIMING")

    def test_positive_delta_reads_as_an_increase(self):
        a, b = self.timing_pair(raw_delta=+98)
        c = next(c for c in db.changes_from_diff(a, b)["changes"]
                 if c["band"] == "timing_limit_array")
        self.assertEqual(c["direction"], "increase")
        self.assertEqual(c["raw_delta"]["mode"], +98)
        self.assertAlmostEqual(c["engineering"]["magnitude"], 2.0, places=3)

    def test_timing_raw_to_degree_conversion_at_49_raw_per_degree(self):
        """The one documented scale: ~49 raw units per degree."""
        for raw, deg in ((-49, 1.0), (-98, 2.0), (+147, 3.0)):
            with self.subTest(raw=raw):
                a, b = self.timing_pair(raw_delta=raw)
                c = next(c for c in db.changes_from_diff(a, b)["changes"]
                         if c["band"] == "timing_limit_array")
                e = c["engineering"]
                self.assertEqual(e["unit"], "deg")
                self.assertAlmostEqual(e["magnitude"], deg, places=3)
                self.assertAlmostEqual(abs(e["signed_magnitude"]), deg, places=3)
                self.assertEqual(e["signed_magnitude"] < 0, raw < 0)
                self.assertTrue(e["is_estimate"])
                self.assertEqual(c["scale"]["raw_per_unit"], 49.0)
                self.assertEqual(c["scale"]["basis"], "measured")
                self.assertEqual(e["confidence"], "medium")

    def test_a_known_scale_alone_does_not_make_a_change_dyno_facing(self):
        """timing_limit_array has a MEASURED scale but is a clamp array, not a
        commanded map. Its degrees are reported in `engineering`; what crosses
        to the dyno stays raw so virtual_dyno cannot apply a limit as if it
        were commanded spark advance."""
        a, b = self.timing_pair(raw_delta=-49)
        c = next(c for c in db.changes_from_diff(a, b)["changes"]
                 if c["band"] == "timing_limit_array")
        self.assertFalse(c["dyno_usable"])
        self.assertEqual(c["unit"], "raw")
        self.assertEqual(c["magnitude"], 49)
        self.assertEqual(c["magnitude_confidence"], "unknown")
        self.assertEqual(c["engineering"]["unit"], "deg")
        self.assertIsNone(vd._unit_of(c))
        self.assertIn("clamp", c["not_usable_reason"])

    def test_deltas_are_read_on_the_band_record_grid_not_even_file_offsets(self):
        """thundermax_parser.region_deltas() used to align to even FILE
        offsets, but every located band starts at an ODD offset, so its deltas
        came out x256 — on the real global -1 deg pair a true -49 was reported
        as -12544, and that number reached both `tmax compare` and the web
        tune-diff. The bridge reads on the band's own record grid; the parser
        has since been fixed to do the same, so both now agree."""
        a, b = self.timing_pair(raw_delta=-49)
        c = next(c for c in db.changes_from_diff(a, b)["changes"]
                 if c["band"] == "timing_limit_array")
        self.assertEqual(c["raw_delta"]["mode"], -49)
        self.assertNotEqual(c["raw_delta"]["mode"], -49 * 256)
        self.assertIn("band record grid", c["raw_delta"]["alignment"])
        # the parser, reading the same band on its grid, must now agree
        band = table_map.band_for_offset(TIMING_LIMIT_LO, table_map.load_map())
        fixed = tmx.deltas_for_region(tmx.TbwFile(a), tmx.TbwFile(b), band,
                                      TIMING_LIMIT_LO, TIMING_LIMIT_LO + 64)
        self.assertIn(-49, fixed)
        self.assertNotIn(-49 * 256, fixed)

    def test_a_change_off_the_record_grid_is_never_silently_dropped(self):
        """The grid read must not hide a changed region. A single byte flipped
        inside a record (not on its boundary) still has to surface — reporting
        a changed region with NO deltas would be a worse failure than the
        misaligned magnitude the grid read exists to fix."""
        a, b = self.timing_pair(raw_delta=-49)
        band = table_map.band_for_offset(TIMING_LIMIT_LO, table_map.load_map())
        fa, fb = tmx.TbwFile(a), tmx.TbwFile(b)
        odd = TIMING_LIMIT_LO + 3           # deliberately mid-record
        self.assertTrue(tmx.deltas_for_region(fa, fb, band, odd, odd + 8),
                        "a changed region must never come back empty")

    def test_change_carries_the_guardrails_vocabulary(self):
        a, b = self.timing_pair()
        for c in db.changes_from_diff(a, b)["changes"]:
            for key in ("table", "rpm_band", "tps_band", "direction",
                        "magnitude", "unit"):
                self.assertIn(key, c)
            self.assertIn(c["direction"], ("increase", "decrease"))
            self.assertGreaterEqual(c["magnitude"], 0)

    def test_signed_cells_are_re_read_as_signed_16_bit(self):
        """tables.json: correction cells are SIGNED, so 0x0000 -> 0xFFxx is a
        small negative trim, not a +65535 jump."""
        a = synthetic_tbw()
        seed_band(a, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, [0] * 4)
        b = synthetic_tbw()
        seed_band(b, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, [0xFFFF - 48] * 4)
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        c = next(c for c in res["changes"] if c["band"] == "timing_limit_array")
        self.assertEqual(c["raw_delta"]["mode"], -49)
        self.assertEqual(c["direction"], "decrease")
        self.assertEqual(c["raw_delta"]["signed_wrap_applied"], 4)

    def test_shaped_edit_uses_the_worst_cell_and_says_so(self):
        a = synthetic_tbw()
        seed_band(a, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, [1000] * 4)
        b = synthetic_tbw()
        seed_band(b, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE,
                  [1049, 1049, 1049, 1147])  # +49, +49, +49, +147
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        c = next(c for c in res["changes"] if c["band"] == "timing_limit_array")
        self.assertFalse(c["raw_delta"]["uniform"])
        self.assertEqual(c["raw_delta"]["mode"], 49)
        # Worst cell in the dominant direction, not the mode: over-stating is
        # the safe way to be wrong for a knock check.
        self.assertEqual(c["raw_representative_delta"], 147)
        self.assertAlmostEqual(c["engineering"]["magnitude"], 3.0, places=3)
        self.assertTrue(any("SHAPED edit" in cv for cv in c["caveats"]))

    def test_autotune_band_is_excluded_by_default(self):
        a = synthetic_tbw()
        b = synthetic_tbw()
        seed_band(b, AUTOTUNE_LO, BAND["autotune_learned"]["stride"], [7, 7, 7])
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        self.assertEqual(res["changes"], [])
        self.assertTrue(any(e["category"] == "AUTOTUNE" for e in res["excluded"]))
        e = next(e for e in res["excluded"] if e["category"] == "AUTOTUNE")
        self.assertIn("every ride", e["reason"])
        self.assertEqual(e["include_with"], "include_autotune=True")

    def test_autotune_band_can_be_opted_back_in(self):
        a = synthetic_tbw()
        b = synthetic_tbw()
        seed_band(b, AUTOTUNE_LO, BAND["autotune_learned"]["stride"], [7, 7, 7])
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b), include_autotune=True)
        self.assertTrue(any(c["category"] == "AUTOTUNE" for c in res["changes"]))
        self.assertEqual(res["excluded"], [])

    def test_metadata_band_is_excluded_by_default(self):
        a = synthetic_tbw()
        b = synthetic_tbw()
        b[METADATA_LO] ^= 0xFF
        b[METADATA_LO + 1] ^= 0xFF
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        self.assertEqual(res["changes"], [])
        e = next(e for e in res["excluded"] if e["category"] == "METADATA")
        self.assertIn("checksum", e["reason"].lower())
        self.assertEqual(e["include_with"], "include_metadata=True")

    def test_metadata_band_can_be_opted_back_in(self):
        a = synthetic_tbw()
        b = synthetic_tbw()
        b[METADATA_LO] ^= 0xFF
        b[METADATA_LO + 1] ^= 0xFF
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b),
                                   include_metadata=True)
        self.assertTrue(any(c["category"] == "METADATA" for c in res["changes"]))

    def test_learned_data_band_is_excluded_by_default(self):
        """fuel_rich_correction: tables.json says treat it as AutoTune data
        until proven otherwise, so it is churn, not a deliberate edit."""
        lo = BAND["fuel_rich_correction"]["_lo"]
        a = synthetic_tbw()
        b = synthetic_tbw()
        seed_band(b, lo, BAND["fuel_rich_correction"]["stride"], [9, 9, 9])
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        self.assertEqual(res["changes"], [])
        e = next(e for e in res["excluded"]
                 if e["band"] == "fuel_rich_correction")
        self.assertIn("AutoTune", e["reason"])

    def test_unmapped_offset_yields_a_null_band_not_a_fabricated_one(self):
        """An offset outside every named band must land in unmapped[] — and
        anything that DOES become a change must still carry a null rpm/tps
        band, because no offset -> (rpm, TPS) mapping exists at all."""
        a = synthetic_tbw()
        b = synthetic_tbw()
        b[0x30000] ^= 0xFF          # outside every band in tables.json
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        self.assertEqual(res["changes"], [])
        self.assertTrue(res["unmapped"])
        u = res["unmapped"][0]
        self.assertIsNone(u["band"])
        self.assertEqual(u["category"], "UNMAPPED")
        self.assertIn("outside every band", u["reason"])

    def test_shared_churn_region_is_reported_as_unmapped(self):
        a = synthetic_tbw()
        b = synthetic_tbw()
        b[0x4500] ^= 0xFF           # inside shared_churn_unresolved
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        self.assertEqual(res["changes"], [])
        u = next(u for u in res["unmapped"] if u["category"] == "SHARED")
        self.assertIn("do not interpret its values", u["reason"])

    def test_identical_files_produce_nothing(self):
        p = self.write("a.tbw", synthetic_tbw())
        q = self.write("b.tbw", synthetic_tbw())
        res = db.changes_from_diff(p, q)
        self.assertEqual(res["changes"], [])
        self.assertTrue(res["diff_summary"]["identical"])
        self.assertEqual(res["confidence"], "unknown")
        self.assertTrue(any("byte-identical" in w for w in res["warnings"]))

    def test_different_base_maps_force_confidence_unknown(self):
        a = synthetic_tbw(map_id=b"HXSSEDCAAN061617")
        b = synthetic_tbw(map_id=b"ZZSSQXETDN100720")
        seed_band(b, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, [1617] * 4)
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        self.assertEqual(res["confidence"], "unknown")
        self.assertFalse(res["diff_summary"]["base_maps_match"])
        self.assertTrue(any("DIFFERENT BASE MAPS" in w for w in res["warnings"]))

    def test_size_mismatch_raises(self):
        a = self.write("a.tbw", synthetic_tbw())
        b = self.write("b.tbw", synthetic_tbw()[:1000])
        with self.assertRaises(tmx.TbwError):
            db.changes_from_diff(a, b)

    def test_diff_summary_rolls_up_by_category(self):
        a = synthetic_tbw()
        b = synthetic_tbw()
        seed_band(b, TIMING_LIMIT_LO, TIMING_LIMIT_STRIDE, [1617] * 4)
        seed_band(b, AUTOTUNE_LO, BAND["autotune_learned"]["stride"], [3, 3])
        res = db.changes_from_diff(self.write("a.tbw", a),
                                   self.write("b.tbw", b))
        d = res["diff_summary"]
        self.assertIn("TIMING", d["by_category"])
        self.assertIn("AUTOTUNE", d["by_category"])
        self.assertGreater(d["changed_bytes_total"], 0)
        self.assertEqual(d["base_map_a"], d["base_map_b"])
        self.assertEqual(d["regions_excluded"], len(res["excluded"]))
        self.assertEqual(d["regions_unmapped"], len(res["unmapped"]))
        self.assertEqual(d["regions_as_changes"], len(res["changes"]))

    def test_return_shape(self):
        a, b = self.timing_pair()
        res = db.changes_from_diff(a, b)
        for key in ("changes", "confidence", "unmapped", "warnings",
                    "diff_summary", "excluded", "directional_only", "honesty"):
            self.assertIn(key, res)
        self.assertIn(res["confidence"], db.CONFIDENCE_ORDER)

    def test_does_not_modify_either_tune_file(self):
        a, b = self.timing_pair()
        ba, bb = a.read_bytes(), b.read_bytes()
        db.changes_from_diff(a, b, include_autotune=True, include_metadata=True)
        self.assertEqual(a.read_bytes(), ba)
        self.assertEqual(b.read_bytes(), bb)


# ---------------------------------------------------------------------------
# THE HONESTY CONSTRAINT — safety tests
# ---------------------------------------------------------------------------

class TestHonestyConstraint(Base):
    """A confident number for a band nobody has ground-truthed is a lie Joshua
    could tune on. These tests exist to make that impossible to ship."""

    def _every_band_pair(self):
        """A/B pair with a change seeded in EVERY named band."""
        a = synthetic_tbw()
        b = synthetic_tbw()
        for name, band in BAND.items():
            lo = band["_lo"]
            stride = max(1, band.get("stride", 1))
            if stride == 1:
                b[lo] = (a[lo] ^ 0x33)
                b[lo + 1] = (a[lo + 1] ^ 0x33)
            else:
                seed_band(a, lo, stride, [1000, 1000, 1000])
                seed_band(b, lo, stride, [1049, 1049, 1049])
        return self.write("a.tbw", a), self.write("b.tbw", b)

    def test_no_engineering_value_for_a_band_with_no_known_scale(self):
        """THE safety assertion. Every change whose band is not in
        KNOWN_SCALES must come back in raw units with unknown confidence and
        no scale block — never a degree, an AFR point or a VE percent."""
        a, b = self._every_band_pair()
        res = db.changes_from_diff(a, b, include_autotune=True,
                                   include_metadata=True)
        self.assertTrue(res["changes"])
        for c in res["changes"]:
            if c["band"] in db.KNOWN_SCALES:
                continue
            self.assertEqual(c["unit"], "raw", c["band"])
            self.assertEqual(c["magnitude_confidence"], "unknown", c["band"])
            self.assertIsNone(c["scale"], c["band"])
            self.assertIsNone(c["engineering"], c["band"])
            self.assertFalse(c["dyno_usable"], c["band"])
            self.assertEqual(c["magnitude"], abs(c["raw_representative_delta"]),
                             c["band"])

    def test_an_engineering_unit_reaches_the_dyno_only_when_modellable(self):
        """THE structural invariant: unit != 'raw' implies dyno_usable. A
        change the dyno may not model must cross in raw units, or
        virtual_dyno._unit_of() will honour the explicit unit and apply it."""
        a, b = self._every_band_pair()
        res = db.changes_from_diff(a, b, include_autotune=True,
                                   include_metadata=True)
        self.assertTrue(res["changes"])
        for c in res["changes"]:
            if c["unit"] != "raw":
                self.assertTrue(c["dyno_usable"], c["band"])
                self.assertIsNotNone(c["scale"], c["band"])
            else:
                self.assertIsNone(vd._unit_of(c), c["table"])

    def test_read_tune_emits_no_engineering_value_for_unknown_scale_bands(self):
        a, _ = self._every_band_pair()
        res = db.read_tune(a)
        for band in res["bands"]:
            if band["name"] in db.ABSOLUTE_SCALE_BANDS:
                self.assertIsNotNone(band["engineering"], band["name"])
                continue
            self.assertIsNone(band["engineering"], band["name"])
            self.assertFalse(band["absolute_engineering_available"],
                             band["name"])

    def test_absolute_scale_bands_are_a_subset_of_measured_scales(self):
        for name in db.ABSOLUTE_SCALE_BANDS:
            self.assertIn(name, db.KNOWN_SCALES)
            self.assertEqual(db.KNOWN_SCALES[name]["basis"], "measured", name)

    def test_no_scale_is_declared_without_a_source_and_a_caveat(self):
        for name, s in db.KNOWN_SCALES.items():
            self.assertIn(s["basis"], ("measured", "assumed"), name)
            self.assertTrue(s["source"].strip(), name)
            self.assertTrue(s["caveat"].strip(), name)
            self.assertIn(s["confidence"], db.CONFIDENCE_ORDER, name)

    def test_a_rejected_scale_assumption_is_never_re_adopted(self):
        self.assertFalse(set(db.KNOWN_SCALES) & set(db.CONTRADICTED_SCALES))
        # timing_map_main's assumed 49 raw/deg did not survive the real data.
        self.assertIn("timing_map_main", db.CONTRADICTED_SCALES)

    def test_band_axes_stays_empty_so_no_rpm_tps_band_is_fabricated(self):
        self.assertEqual(db.BAND_AXES, {},
                         "populating BAND_AXES with a guess would silently "
                         "mis-scope every safety check that reads rpm_band")

    def test_every_change_has_a_null_rpm_and_tps_band_today(self):
        a, b = self._every_band_pair()
        res = db.changes_from_diff(a, b, include_autotune=True,
                                   include_metadata=True)
        for c in res["changes"]:
            self.assertIsNone(c["rpm_band"], c["band"])
            self.assertIsNone(c["tps_band"], c["band"])
            self.assertTrue(any("needs_ground_truth" in cv
                                for cv in c["caveats"]), c["band"])
        self.assertTrue(res["honesty"]["rpm_tps_bands_are_null"])
        self.assertFalse(res["honesty"]["axis_mapping_available"])

    def test_raw_changes_never_borrow_a_guardrails_table_name(self):
        """virtual_dyno._unit_of() infers a unit from a bare table name, so a
        raw-unit change called 'afr_target' would be read as AFR points."""
        a, b = self._every_band_pair()
        res = db.changes_from_diff(a, b, include_autotune=True,
                                   include_metadata=True)
        for c in res["changes"]:
            if c["dyno_usable"]:
                continue
            self.assertNotIn(c["table"], G.TABLES, c["band"])
            self.assertTrue(c["table"].startswith(db.NON_DYNO_TABLE_PREFIX))
            self.assertIsNone(vd._unit_of(c),
                              "the dyno must not infer a unit for " + c["table"])

    def test_the_dyno_ignores_unquantified_changes_entirely(self):
        """Feeding the derived changes to simulate_pull must be safe: an
        unquantified change may never move a single modelled number."""
        a, b = self._every_band_pair()
        res = db.changes_from_diff(a, b, include_autotune=True,
                                   include_metadata=True)
        self.assertFalse(any(c["dyno_usable"] for c in res["changes"]))
        with_changes = vd.simulate_pull(res["changes"])["summary"]
        baseline = vd.simulate_pull([])["summary"]
        self.assertEqual(with_changes["peak_hp"], baseline["peak_hp"])
        self.assertEqual(with_changes["peak_torque"], baseline["peak_torque"])
        self.assertEqual(with_changes["max_knock_risk"],
                         baseline["max_knock_risk"])
        self.assertTrue(res["directional_only"])
        self.assertTrue(any("directional only" in w.lower()
                            for w in res["warnings"]))

    def test_a_band_wired_to_the_dyno_must_have_a_known_scale(self):
        for name in db.BAND_TO_DYNO_TABLES:
            self.assertIn(name, db.KNOWN_SCALES, name)
            for table in db.BAND_TO_DYNO_TABLES[name]:
                self.assertIn(table, G.TABLES, table)

    def test_dyno_usable_path_produces_a_real_guardrails_change(self):
        """BAND_TO_DYNO_TABLES is empty today. This documents (and locks) what
        happens the moment the ground-truth experiment fills it in."""
        original = dict(db.BAND_TO_DYNO_TABLES)
        db.BAND_TO_DYNO_TABLES["timing_limit_array"] = (
            "spark_advance_front", "spark_advance_rear")
        try:
            a, b = self.timing_pair(raw_delta=+98)
            res = db.changes_from_diff(a, b)
            usable = [c for c in res["changes"] if c["dyno_usable"]]
            self.assertEqual(len(usable), 2)
            self.assertEqual({c["table"] for c in usable},
                             {"spark_advance_front", "spark_advance_rear"})
            for c in usable:
                self.assertEqual(c["unit"], "deg")
                self.assertAlmostEqual(c["magnitude"], 2.0, places=3)
                self.assertEqual(c["magnitude_confidence"], "medium")
                self.assertEqual(vd._unit_of(c), "deg")
                self.assertEqual(vd._signed(c), +2.0)
            self.assertEqual(res["confidence"], "low")
            self.assertFalse(res["directional_only"])
            # ...and it really moves the dyno.
            sim = vd.simulate_pull(res["changes"])["summary"]
            base = vd.simulate_pull([])["summary"]
            self.assertNotEqual(sim["max_knock_risk"], base["max_knock_risk"])
        finally:
            db.BAND_TO_DYNO_TABLES.clear()
            db.BAND_TO_DYNO_TABLES.update(original)


# ---------------------------------------------------------------------------
# self_test
# ---------------------------------------------------------------------------

class TestSelfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = db.self_test()

    def test_passes_on_the_real_module(self):
        self.assertTrue(self.res["passed"],
                        "self_test failed: " + self.res["summary"]["verdict"])
        self.assertEqual(self.res["summary"]["critical_failed"], 0)

    def test_every_check_has_the_documented_shape(self):
        self.assertTrue(self.res["checks"])
        names = set()
        for c in self.res["checks"]:
            for key in ("name", "passed", "expected", "got", "detail",
                        "severity"):
                self.assertIn(key, c)
            self.assertIsInstance(c["passed"], bool)
            self.assertTrue(str(c["expected"]).strip(), c["name"])
            self.assertTrue(str(c["got"]).strip(), c["name"])
            self.assertTrue(str(c["detail"]).strip(), c["name"])
            self.assertIn(c["severity"], ("critical", "warn"))
            self.assertNotIn(c["name"], names, "duplicate check name")
            names.add(c["name"])

    def test_covers_the_required_ground(self):
        names = " ".join(c["name"] for c in self.res["checks"])
        for token in ("injector_duty_closed_form", "published_anchors",
                      "hp_equals_torque", "zero_changes_zero_delta",
                      "duty_rises_with_rpm", "afr_power_curve_continuous",
                      "advance_past_mbt", "every_dyno_issue_is_severity_warn",
                      "summary_deltas_are_integer_ranges",
                      "injector_flow_matches_bike_profile",
                      "calibration_status_is_surfaced",
                      "tbw_scale_knowledge"):
            self.assertIn(token, names, f"self_test lost coverage of {token}")

    def test_summary_counts_are_consistent(self):
        s = self.res["summary"]
        self.assertEqual(s["total"], len(self.res["checks"]))
        self.assertEqual(s["passed"] + s["failed"], s["total"])
        self.assertEqual(s["failed"], len(s["failed_names"]))
        self.assertTrue(s["verdict"].startswith("PASS"))
        self.assertIn("validation ride", s["scope_note"])

    def test_calibration_block_reports_the_profile_injectors(self):
        cal = self.res["calibration"]
        self.assertEqual(cal["injector_flow_gps"], 6.3,
                         "a stale 5.5 g/s would mis-read every pull")
        self.assertEqual(cal["injector_flow_unit"], "g/s")
        self.assertEqual(cal["injector_flow_source"], "bike_profile.json")

    def test_calibration_block_surfaces_uncertainty_and_confirmations(self):
        cal = self.res["calibration"]
        self.assertEqual(cal["uncertainty_pct"], 15)
        self.assertTrue(cal["calibration_status"].strip())
        self.assertTrue(cal["banner"].strip())
        self.assertTrue(cal["needs_confirmation"])
        self.assertFalse(cal["llm_involved"])
        self.assertTrue(cal["deterministic"])

    def test_calibration_block_states_the_tbw_scale_knowledge(self):
        k = self.res["calibration"]["tbw_scale_knowledge"]
        self.assertIn("timing_limit_array", k["known_scales"])
        self.assertEqual(k["known_scales"]["timing_limit_array"]["raw_per_unit"],
                         49.0)
        self.assertEqual(k["offset_to_rpm_tps_mapping"], "NONE — no band has one")
        self.assertIn("timing_map_main", k["contradicted_scales"])
        self.assertTrue(k["statement"].strip())

    def test_needs_no_nas_ollama_or_network(self):
        """self_test is pure arithmetic over local JSON — re-running it must be
        deterministic and identical."""
        again = db.self_test()
        self.assertEqual([c["name"] for c in again["checks"]],
                         [c["name"] for c in self.res["checks"]])
        self.assertEqual([c["passed"] for c in again["checks"]],
                         [c["passed"] for c in self.res["checks"]])

    def test_the_injector_duty_check_is_independent_arithmetic(self):
        """The known-answer check must recompute duty itself, not just call the
        dyno twice. Perturb one input and the two must disagree."""
        mine = db._independent_duty_pct(5000, 0.95, 12.7, 131.0, 2, 6.3,
                                        29.92, 75.0)
        theirs = vd.injector_duty_pct(5000, 0.95, 12.7,
                                      vd.merge_conditions(None))
        self.assertAlmostEqual(mine, theirs, places=9)
        wrong = db._independent_duty_pct(5000, 0.95, 12.7, 131.0, 2, 5.5,
                                         29.92, 75.0)
        self.assertNotAlmostEqual(wrong, theirs, places=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli(Base):
    def _run(self, argv):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            code = db._main(argv)
        finally:
            sys.stdout = old
        return code, buf.getvalue()

    def test_selftest_command(self):
        code, out = self._run(["selftest"])
        self.assertEqual(code, 0)
        self.assertIn("VIRTUAL DYNO SELF-TEST — PASS", out)
        self.assertIn("CALIBRATION:", out)

    def test_selftest_json(self):
        import json
        code, out = self._run(["--json", "selftest"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["passed"])

    def test_read_command(self):
        p = self.write("t.tbw", synthetic_tbw())
        code, out = self._run(["read", str(p)])
        self.assertEqual(code, 0)
        self.assertIn("RAW CELL CENSUS", out)
        self.assertIn("UNKNOWN", out)

    def test_compare_command_runs_the_pull(self):
        a, b = self.timing_pair()
        code, out = self._run(["compare", str(a), str(b)])
        self.assertEqual(code, 0)
        self.assertIn("DERIVED CHANGES", out)
        self.assertIn("VIRTUAL DYNO PULL", out)

    def test_compare_json(self):
        import json
        a, b = self.timing_pair()
        code, out = self._run(["--json", "compare", str(a), str(b)])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("bridge", payload)
        self.assertIn("pull", payload)
        self.assertIn("samples", payload["pull"])

    def test_compare_no_sim(self):
        a, b = self.timing_pair()
        code, out = self._run(["compare", str(a), str(b), "--no-sim"])
        self.assertEqual(code, 0)
        self.assertNotIn("VIRTUAL DYNO PULL", out)


if __name__ == "__main__":
    unittest.main()
