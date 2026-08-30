#!/usr/bin/env python3
"""Tests for ecm_state (temp dirs only — no NAS, no real flash)."""
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "src"))

import ecm_state as ecm
import test_thundermax as tt


class TestEcmState(tt.Base):
    def setUp(self):
        super().setUp()
        self.state_path = self.dir / "ecm_state.json"
        good = self.write("good.tbw", tt.synthetic_tbw(map_id=b"HXSSEDCAAN061617"))
        rec, _ = ecm.describe(good)
        rec.update(source="claimed", confidence="file-claimed")
        self.state = {
            "schema": 1,
            "complaint": None,
            "on_ecm": rec,
            "last_known_good": rec,
            "last_pull": rec,
            "last_bagged": None,
            "staged_flash": None,
            "history": [],
        }
        ecm.save_state(self.state, self.state_path)

    def test_show_runs(self):
        st = ecm.load_state(self.state_path)
        self.assertEqual(ecm.cmd_show(st), 0)

    def test_complaint_roundtrip(self):
        st = ecm.load_state(self.state_path)
        ecm.cmd_complaint(st, "launch bog from a stop", self.state_path)
        st2 = ecm.load_state(self.state_path)
        self.assertEqual(st2["complaint"]["text"], "launch bog from a stop")

    def test_bag_no_overwrite(self):
        st = ecm.load_state(self.state_path)
        src = self.write("incoming.tbw", tt.synthetic_tbw(map_id=b"HXSSEDCAAN061617"))
        old_ex = ecm.EXCHANGE
        ecm.EXCHANGE = self.dir / "ex"
        try:
            self.assertEqual(ecm.cmd_bag(st, src, self.state_path, nas=False), 0)
            bagged = ecm.EXCHANGE / "bagged" / "incoming.tbw"
            self.assertTrue(bagged.is_file())
            self.assertEqual(ecm.sha256_of(bagged), ecm.sha256_of(src))
            # second bag of identical file is ok
            st = ecm.load_state(self.state_path)
            self.assertEqual(ecm.cmd_bag(st, src, self.state_path, nas=False), 0)
            other = self.write("incoming.tbw", tt.synthetic_tbw(map_id=b"HXSSEDCAAN061617", fill=0x11))
            other_named = self.dir / "incoming.tbw"
            # dest already exists with different hash under bagged/
            st = ecm.load_state(self.state_path)
            with self.assertRaises(ecm.EcmError):
                ecm.cmd_bag(st, other_named, self.state_path, nas=False)
        finally:
            ecm.EXCHANGE = old_ex

    def test_compare_vs_good(self):
        st = ecm.load_state(self.state_path)
        data = tt.synthetic_tbw(map_id=b"HXSSEDCAAN061617")
        data[0x1A00] ^= 0x01
        new = self.write("new.tbw", data)
        self.assertEqual(ecm.cmd_compare(st, new, "last_known_good"), 0)

    def test_stage_blocks_17aug_map(self):
        st = ecm.load_state(self.state_path)
        bad = self.write("17aug.tbw", tt.synthetic_tbw(map_id=b"ZZSSQXETDN100720"))
        old_ex = ecm.EXCHANGE
        ecm.EXCHANGE = self.dir / "ex"
        try:
            with self.assertRaises(ecm.EcmError):
                ecm.cmd_stage_flash(st, bad, self.state_path)
        finally:
            ecm.EXCHANGE = old_ex

    def test_record_flash_updates_on_ecm(self):
        st = ecm.load_state(self.state_path)
        src = Path(st["last_known_good"]["path"])
        self.assertEqual(ecm.cmd_record_flash(st, src, also_good=True, state_path=self.state_path), 0)
        st2 = ecm.load_state(self.state_path)
        self.assertEqual(st2["on_ecm"]["confidence"], "operator-confirmed-flash")
        self.assertIsNone(st2["staged_flash"])
        self.assertEqual(st2["last_known_good"]["sha256"], st2["on_ecm"]["sha256"])


if __name__ == "__main__":
    unittest.main()
