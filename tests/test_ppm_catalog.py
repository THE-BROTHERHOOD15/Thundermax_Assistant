#!/usr/bin/env python3
"""PPM catalog is the 16-char ID answer key — not a .tbw decoder."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import ppm_catalog


class TestPpmCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = ppm_catalog.load()

    def test_count_32(self):
        self.assertEqual(self.cat["count"], 32)
        self.assertEqual(len(self.cat["records"]), 32)

    def test_decode_house_id(self):
        d = ppm_catalog.decode_map_id("HXSSEDCAAN061617")
        self.assertTrue(d["standard"])
        self.assertEqual(d["family"], "HX")
        self.assertEqual(d["family_engine_index"], 23)
        self.assertEqual(d["combo"], "HXSSEDCAAN")
        self.assertEqual(d["minted"], "2017-06-16")
        self.assertTrue(d["ss_m8_catalog"])

    def test_house_exact_lineage(self):
        r = ppm_catalog.lookup("HXSSEDCAAN061617", self.cat)
        self.assertEqual(r["match"], "exact")
        rec = r["record"]
        self.assertEqual(rec["item_id"], 482)
        self.assertEqual(rec["engine_index"], 23)
        self.assertIn("M8117STAGE400000", rec["history_ids"])
        self.assertIn("HXSSEDCAAN041117", rec["history_ids"])
        self.assertEqual(rec["current"], "HXSSEDCAAN061617")

    def test_parent_id_finds_same_record(self):
        r = ppm_catalog.lookup("HXSSEDCAAN041117", self.cat)
        self.assertEqual(r["match"], "exact")
        self.assertEqual(r["record"]["item_id"], 482)

    def test_prefix_newer_suffix(self):
        r = ppm_catalog.lookup("H1SSTJLAAN120822", self.cat)
        self.assertEqual(r["match"], "prefix")
        self.assertEqual(r["record"]["current"], "H1SSTJLAAN113022")

    def test_twincam_not_in_m8(self):
        r = ppm_catalog.lookup("HYSSPVCAHN051320", self.cat)
        self.assertEqual(r["match"], "none")
        self.assertFalse(r["in_m8_catalog_family"])
        text = "\n".join(ppm_catalog.format_lookup(r))
        self.assertIn("not in M8 PPM", text)

    def test_family_hx_is_not_shop_hardware(self):
        r = ppm_catalog.lookup("HXSSEDCAAN061617", self.cat)
        text = "\n".join(ppm_catalog.format_lookup(r))
        self.assertIn("6.3 / S&S 550", text)


if __name__ == "__main__":
    unittest.main()
