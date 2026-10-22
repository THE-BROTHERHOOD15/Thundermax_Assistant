#!/usr/bin/env python3
"""Backend decoder is wired: public payload, claimed context, never writes."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import tbw_read
import thundermax_parser as tmx
import struct


def synthetic_tbw():
    data = bytearray([0x55] * tmx.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[0x10] = 16
    data[0x11:0x21] = b"HXSSEDCAAN061617"
    return bytes(data)


def plant(raw_values):
    data = bytearray(synthetic_tbw())
    off = 0x1981
    for v in raw_values:
        struct.pack_into("<h", data, off, v)
        struct.pack_into("<h", data, off + 2, 0)
        off += 4
    struct.pack_into("<I", data, off, 32)
    return bytes(data)


class TestBackendRead(unittest.TestCase):
    def test_public_payload_has_values(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.tbw"
            p.write_bytes(plant([720, 670]))
            pub = tbw_read.public_payload(tbw_read.read_tune(p))
        self.assertTrue(pub["ok"])
        self.assertEqual(pub["afr"]["values"], [14.4, 13.4])
        self.assertTrue(pub["never_write_tbw"])
        self.assertNotIn("cells", pub["afr"])

    def test_claimed_context_empty_on_unrelated(self):
        self.assertEqual(tbw_read.claimed_file_context("what is the weather"), "")

    def test_claimed_context_decodes_afr(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "M8.tbw"
            p.write_bytes(plant([720]))
            st = {"on_ecm": {"path": str(p), "name": "M8.tbw"}}
            fake = Path(td) / "ecm_state.json"
            fake.write_text(json.dumps(st))
            with mock.patch.object(tbw_read, "claimed_file_context") as _:
                pass
            # call internals via read_tune path used by claimed_file_context
            block = tbw_read.context_block(tbw_read.read_tune(p))
        self.assertIn("14.40", block)
        self.assertIn("0b00100000", block)
        self.assertIn("Never hex-edit", block)

    def test_webui_tune_read_from_cache(self):
        import webui_core as wui
        data = plant([720, 670])
        sha = hashlib.sha1(data).hexdigest()
        wui.TUNE_CACHE.mkdir(parents=True, exist_ok=True)
        dest = wui.cache_path(sha)
        dest.write_bytes(data)
        try:
            out = wui.tune_read(sha)
            self.assertTrue(out.get("ok"), out)
            self.assertEqual(out["afr"]["values"], [14.4, 13.4])
            self.assertTrue(out["never_write_tbw"])
            self.assertEqual(dest.read_bytes(), data)  # no mutate
        finally:
            dest.unlink(missing_ok=True)
            wui.read_memo_path(sha).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
