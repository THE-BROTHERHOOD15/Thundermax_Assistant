#!/usr/bin/env python3
"""Tests for thundermax_parser and table_map.

Self-contained: builds synthetic 214470-byte TBW images in a temp dir, so no
NAS access, real tunes, or Ollama server are needed. Run from the repo root:

    python3 -m unittest discover -s tests -v
"""
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import table_map
import thundermax_parser as tmx


def synthetic_tbw(map_id=b"HYSSPVCAHN051320", fill=0x55):
    """A structurally valid fake tune image."""
    data = bytearray([fill] * tmx.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[tmx.MAP_ID_OFFSET] = len(map_id)
    data[tmx.MAP_ID_OFFSET + 1 : tmx.MAP_ID_OFFSET + 1 + len(map_id)] = map_id
    return data


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p


class TestParser(Base):
    def test_parse_valid(self):
        t = tmx.TbwFile(self.write("a.tbw", synthetic_tbw()))
        self.assertEqual(t.base_map_id, "HYSSPVCAHN051320")
        self.assertEqual(t.header, (0x87, 0x4000, 0x1, 0x147))
        self.assertTrue(t.size_ok)
        self.assertTrue(t.id_ok)
        self.assertTrue(t.valid)

    def test_wrong_size_flagged(self):
        data = synthetic_tbw()[:100000]
        t = tmx.TbwFile(self.write("short.tbw", data))
        self.assertFalse(t.size_ok)
        self.assertFalse(t.valid)

    def test_too_small_raises(self):
        self.write("tiny.tbw", b"\x00" * 10)
        with self.assertRaises(tmx.TbwError):
            tmx.TbwFile(self.dir / "tiny.tbw")

    def test_diff_regions_merge_and_split(self):
        a = tmx.TbwFile(self.write("a.tbw", synthetic_tbw()))
        data = synthetic_tbw()
        data[0x2000] ^= 0xFF          # region 1: two changes within gap
        data[0x2010] ^= 0xFF
        data[0x9000] ^= 0xFF          # region 2: far away -> separate
        b = tmx.TbwFile(self.write("b.tbw", data))
        regs = tmx.diff_regions(a, b)
        self.assertEqual(regs, [(0x2000, 0x2011), (0x9000, 0x9001)])

    def test_region_deltas_le_u16(self):
        a = tmx.TbwFile(self.write("a.tbw", synthetic_tbw()))
        data = synthetic_tbw()
        struct.pack_into("<H", data, 0x2000, 0x5555 + 49)  # +49 raw
        b = tmx.TbwFile(self.write("b.tbw", data))
        d = tmx.region_deltas(a, b, 0x2000, 0x2002)
        self.assertEqual(d, [49])

    def test_compare_labels_known_band(self):
        a = tmx.TbwFile(self.write("a.tbw", synthetic_tbw()))
        data = synthetic_tbw()
        data[0x1A00] ^= 0x01           # inside afr_target band
        b = tmx.TbwFile(self.write("b.tbw", data))
        out = io.StringIO()
        tmx.compare(a, b, out=out)
        text = out.getvalue()
        self.assertIn("afr_target", text)
        self.assertIn("Changed bytes by table category", text)

    def test_scan_empty_dir_refuses(self):
        # a stale NAS mount must not silently produce an empty index
        with self.assertRaises(tmx.TbwError):
            tmx.scan(self.dir, out=io.StringIO())

    def test_scan_skips_appledouble(self):
        self.write("good.tbw", synthetic_tbw())
        self.write("._good.tbw", b"junk")
        out = io.StringIO()
        tmx.scan(self.dir, out=out)
        text = out.getvalue()
        self.assertIn("good.tbw", text)
        self.assertNotIn("._good.tbw", text)
        self.assertIn("1 files scanned", text)


class TestTableMap(Base):
    def test_map_loads_and_is_sane(self):
        m = table_map.load_map()
        self.assertEqual(m["file_size"], tmx.EXPECTED_SIZE)
        names = [b["name"] for b in m["bands"]]
        self.assertEqual(len(names), len(set(names)), "duplicate band names")
        for b in m["bands"]:
            self.assertLess(b["_lo"], b["_hi"], b["name"])
            self.assertIn(b["confidence"], ("high", "medium", "low"), b["name"])

    def test_narrowest_band_wins(self):
        m = table_map.load_map()
        # afr_target (0x1989-0x1BCA) is nested inside shared_churn_unresolved
        band = table_map.band_for_offset(0x1A00, m)
        self.assertEqual(band["name"], "afr_target")
        # but an offset in the churn block outside any nested band stays SHARED
        band = table_map.band_for_offset(0x4500, m)
        self.assertEqual(band["name"], "shared_churn_unresolved")

    def test_unmapped_offset(self):
        m = table_map.load_map()
        self.assertIsNone(table_map.band_for_offset(0x20000, m))

    def test_classify_diff_and_summary(self):
        a = self.write("a.tbw", synthetic_tbw())
        data = synthetic_tbw()
        data[0xC3D0] ^= 0x01           # timing_limit_array
        data[0x2DA00] ^= 0x01          # fuel_rich_correction
        b = self.write("b.tbw", data)
        rows = table_map.classify_diff(a, b)
        cats = {r["category"] for r in rows}
        self.assertEqual(cats, {"TIMING", "FUEL"})
        summary = table_map.summarize(rows)
        self.assertEqual(summary["TIMING"]["bytes"], 1)
        self.assertEqual(summary["FUEL"]["bytes"], 1)

    def test_identical_files_no_rows(self):
        a = self.write("a.tbw", synthetic_tbw())
        b = self.write("b.tbw", synthetic_tbw())
        self.assertEqual(table_map.classify_diff(a, b), [])

    def test_size_mismatch_raises(self):
        a = self.write("a.tbw", synthetic_tbw())
        b = self.write("b.tbw", synthetic_tbw()[:1000])
        with self.assertRaises(ValueError):
            table_map.classify_diff(a, b)


class TestTbwRead(Base):
    def _plant_afr(self, raw_values, landmark_at=None):
        data = bytearray(synthetic_tbw())
        off = 0x1981
        for v in raw_values:
            struct.pack_into("<h", data, off, v)
            struct.pack_into("<h", data, off + 2, 0)
            off += 4
        if landmark_at is None:
            landmark_at = off
        struct.pack_into("<I", data, landmark_at, 32)  # 0b00100000
        return data

    def test_afr_scale_50(self):
        import tbw_read
        p = self.write("a.tbw", self._plant_afr([0, 720, 720, 670]))
        decoded = tbw_read.read_tune(p)
        afrs = [c["afr"] for c in decoded["afr"]["cells"] if c["kind"] == "afr"]
        self.assertEqual(afrs, [14.4, 14.4, 13.4])
        self.assertEqual(decoded["afr"]["summary"]["min"], 13.4)
        self.assertEqual(decoded["afr"]["summary"]["max"], 14.4)
        self.assertTrue(decoded["never_write_tbw"])

    def test_landmark_stops_scan(self):
        import tbw_read
        data = self._plant_afr([720, 720])
        # garbage after landmark must not become AFR
        struct.pack_into("<h", data, 0x1981 + 4 * 3, 800)
        p = self.write("a.tbw", data)
        decoded = tbw_read.read_tune(p)
        afrs = [c["afr"] for c in decoded["afr"]["cells"] if c["kind"] == "afr"]
        self.assertEqual(afrs, [14.4, 14.4])

    def test_afr_diff_minus_one(self):
        import tbw_read
        a = self.write("old.tbw", self._plant_afr([720, 720]))
        b = self.write("new.tbw", self._plant_afr([670, 670]))
        diff = tbw_read.afr_diff(tbw_read.read_tune(a), tbw_read.read_tune(b))
        self.assertEqual([round(d["delta"], 2) for d in diff], [-1.0, -1.0])

    def test_read_does_not_write(self):
        import tbw_read
        p = self.write("a.tbw", self._plant_afr([720]))
        before = p.read_bytes()
        tbw_read.read_tune(p)
        tbw_read.propose_from_read(tbw_read.read_tune(p), "afr-audit")
        self.assertEqual(p.read_bytes(), before)

    def test_propose_never_write_flag(self):
        import tbw_read
        p = self.write("a.tbw", self._plant_afr([615]))  # 12.30
        prop = tbw_read.propose_from_read(tbw_read.read_tune(p), "all")
        self.assertTrue(prop["never_write_tbw"])
        self.assertIn("TMax", prop["apply_in"])
        self.assertTrue(any(c["table"] == "afr_target" for c in prop["changes"]))

    def test_commands_0x29_labels_afr(self):
        import tbw_read
        p = self.write("a.tbw", self._plant_afr([720, 670]))
        decoded = tbw_read.read_tune(p)
        cmd = decoded["commands"]["0x29"]
        self.assertEqual(cmd["fid"], 0x29)
        self.assertEqual(cmd["name"], "AFR Target Map")
        self.assertEqual(decoded["afr"]["fid"], 0x29)
        self.assertEqual(cmd["summary"]["min"], 13.4)
        self.assertEqual(cmd["summary"]["max"], 14.4)

    def test_commands_legend_has_three_fids(self):
        import tbw_read
        self.assertEqual(set(tbw_read.COMMANDS), {0x20, 0x26, 0x29})
        p = self.write("a.tbw", self._plant_afr([720]))
        decoded = tbw_read.read_tune(p)
        self.assertEqual(set(decoded["commands"]), {"0x20", "0x26", "0x29"})
        self.assertEqual(decoded["commands"]["0x20"]["aka"], "VE")
        self.assertEqual(decoded["commands"]["0x26"]["fid"], 0x26)

    def test_0x29_trailer_after_afr(self):
        """Plant [0x20,0,0x20,0x20, 0x29,0,0x29,0x29] as the AFR-scan tail."""
        import tbw_read
        data = bytearray(self._plant_afr([720, 720]))
        # landmark is already u32 32 at end of cells; extend into a 0x20+0x29 directory
        off = 0x1981 + 4 * 2  # after two AFR cells; _plant_afr put landmark here
        struct.pack_into("<8I", data, off,
                         0x20, 0, 0x20, 0x20,
                         0x29, 0, 0x29, 0x29)
        p = self.write("a.tbw", bytes(data))
        decoded = tbw_read.read_tune(p)
        trailers = decoded["commands"]["0x29"]["trailers"]
        self.assertTrue(any(int(t, 16) == off for t in trailers), trailers)
        afrs = [c["afr"] for c in decoded["afr"]["cells"] if c["kind"] == "afr"]
        self.assertEqual(afrs, [14.4, 14.4])


if __name__ == "__main__":
    unittest.main()
