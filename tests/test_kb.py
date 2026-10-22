#!/usr/bin/env python3
"""Tests for the three-layer ThunderMax knowledge base.

Temp-dir only. No NAS, no Ollama, no live ~/thundermax-kb.
Never writes a real shop .tbw — synthetic images only.
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import thundermax_parser as tmx  # noqa: E402
from kb.claims import add_claim, supersede_claim  # noqa: E402
from kb.classify import classify_file  # noqa: E402
from kb.eval_questions import QUESTIONS  # noqa: E402
from kb.extract import sha256_file  # noqa: E402
from kb.ingest import init_kb, ingest_path  # noqa: E402
from kb.names import archive_name, parse_archive_name  # noqa: E402
from kb.retrieve import retrieve_bundle  # noqa: E402
from kb.store import connect, status  # noqa: E402


def synthetic_tbw(map_id=b"HXSSEDCAAN061617", fill=0x55):
    data = bytearray([fill] * tmx.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[tmx.MAP_ID_OFFSET] = len(map_id)
    data[tmx.MAP_ID_OFFSET + 1: tmx.MAP_ID_OFFSET + 1 + len(map_id)] = map_id
    return bytes(data)


class KbBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "thundermax-kb"
        os.environ["TMAX_KB_ROOT"] = str(self.root)
        init_kb(root=self.root, seed=True)

    def tearDown(self):
        os.environ.pop("TMAX_KB_ROOT", None)
        self.tmp.cleanup()


class TestInit(KbBase):
    def test_tree_and_db(self):
        self.assertTrue((self.root / "01_originals" / "tbw").is_dir())
        self.assertTrue((self.root / "08_quarantine" / "unsupported").is_dir())
        self.assertTrue((self.root / "database" / "knowledge.sqlite").is_file())
        st = status(self.root)
        self.assertGreaterEqual(st["files"], 1)
        self.assertGreaterEqual(st["claims"], 4)
        self.assertGreaterEqual(st["chunks"], 1)

    def test_house_rules_readonly_original(self):
        con = connect(self.root, create=False)
        row = con.execute(
            "SELECT archive_path FROM files WHERE parser_status='parsed' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row["archive_path"])
        p = self.root / row["archive_path"]
        self.assertTrue(p.exists())
        self.assertFalse(os.access(p, os.W_OK))


class TestNames(unittest.TestCase):
    def test_archive_name_rules(self):
        n = archive_name(
            "2026-08-27", "project-014", "2020 FXLR", "114ci",
            "baseline", "datalog", "v01", "csv",
        )
        self.assertEqual(
            n,
            "2026-08-27__project-014__2020-fxlr__114ci__baseline__datalog__v01.csv",
        )
        self.assertIsNotNone(parse_archive_name(n))
        self.assertNotIn(" ", n)
        self.assertNotIn("final", n)


class TestIngest(KbBase):
    def test_markdown_roundtrip(self):
        src = Path(self.tmp.name) / "note.md"
        src.write_text("# Cold start\n\nSymptom: rough idle when CHT is 90F.\n")
        r = ingest_path(
            src, root=self.root, extra={
                "vehicle_key": "2023-fxlrst",
                "engine_key": "131ci",
                "hardware_key": "ss550-63inj-2into1",
                "project_key": "project-014",
                "tags": ["cold-start"],
            },
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "parsed")
        self.assertGreater(r["chunks"], 0)
        con = connect(self.root, create=False)
        row = con.execute("SELECT * FROM files WHERE file_id=?", (r["file_id"],)).fetchone()
        self.assertEqual(row["vehicle_key"], "2023-fxlrst")
        chain = json.loads(con.execute(
            "SELECT metadata_json FROM chunks WHERE file_id=? LIMIT 1", (r["file_id"],)
        ).fetchone()["metadata_json"])
        self.assertEqual(chain["source_chain"]["file_id"], r["file_id"])

    def test_duplicate_preserved(self):
        src = Path(self.tmp.name) / "dup.md"
        src.write_text("same bytes twice")
        a = ingest_path(src, root=self.root)
        b = ingest_path(src, root=self.root)
        self.assertEqual(b["status"], "duplicate")
        self.assertTrue((self.root / "08_quarantine" / "duplicates").exists())
        self.assertTrue(src.exists())
        con = connect(self.root, create=False)
        row = con.execute("SELECT duplicate_of FROM files WHERE file_id=?", (b["file_id"],)).fetchone()
        self.assertEqual(row["duplicate_of"], a["file_id"])

    def test_tbw_copy_readonly_source_untouched(self):
        src = Path(self.tmp.name) / "bike.tbw"
        src.write_bytes(synthetic_tbw())
        before = sha256_file(src)
        mode_before = src.stat().st_mode
        r = ingest_path(src, root=self.root, extra={"vehicle_key": "2023-fxlrst",
                                                    "engine_key": "131ci",
                                                    "validated": True})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "parsed")
        self.assertEqual(sha256_file(src), before)
        self.assertEqual(src.stat().st_mode, mode_before)
        arch = self.root / r["archive_path"]
        self.assertTrue(arch.exists())
        self.assertFalse(os.access(arch, os.W_OK))
        self.assertTrue(arch.name.endswith(".tbw"))
        con = connect(self.root, create=False)
        row = con.execute("SELECT map_id FROM files WHERE file_id=?", (r["file_id"],)).fetchone()
        self.assertEqual(row["map_id"], "HXSSEDCAAN061617")

    def test_high_entropy_quarantine(self):
        src = Path(self.tmp.name) / "mystery.bin"
        src.write_bytes(os.urandom(4096))
        r = ingest_path(src, root=self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "unsupported")
        self.assertTrue(r["archive_path"].startswith("08_quarantine/"))
        why = list((self.root / "08_quarantine" / "unsupported").glob("*.why.json"))
        self.assertTrue(why)

    def test_name_collision_does_not_fail(self):
        a = Path(self.tmp.name) / "same-prefix-one.md"
        b = Path(self.tmp.name) / "same-prefix-two.md"
        a.write_text("# One\nunique body one " + "x" * 20)
        b.write_text("# Two\nunique body two " + "y" * 20)
        extra = {"vehicle_key": "2023-fxlrst", "engine_key": "131ci",
                 "project_key": "corpus", "event": "same-prefix"}
        ra = ingest_path(a, root=self.root, extra=extra)
        rb = ingest_path(b, root=self.root, extra=extra)
        self.assertTrue(ra["ok"], ra)
        self.assertTrue(rb["ok"], rb)
        self.assertNotEqual(ra["archive_path"], rb["archive_path"])

    def test_does_not_invent_map_cells(self):
        src = Path(self.tmp.name) / "bike.tbw"
        src.write_bytes(synthetic_tbw())
        r = ingest_path(src, root=self.root)
        deriv = self.root / "02_normalized" / "map_tables" / f"{r['file_id']}.md"
        text = deriv.read_text()
        self.assertIn("NOT decoded", text)
        self.assertNotRegex(text, r"AFR 1[0-9]\\.[0-9]")


class TestClaims(KbBase):
    def test_supersede_keeps_old(self):
        old = add_claim(
            root=self.root,
            claim="Old wrong note about idle AFR.",
            claim_type="observation",
            status="observed",
        )
        new = supersede_claim(
            old, "Corrected idle AFR note.",
            reason="log showed the old note was heat-soak, not idle",
            evidence="datalog segment idle rows 10-40",
            root=self.root,
        )
        con = connect(self.root, create=False)
        o = con.execute("SELECT * FROM knowledge_claims WHERE claim_id=?", (old,)).fetchone()
        n = con.execute("SELECT * FROM knowledge_claims WHERE claim_id=?", (new,)).fetchone()
        self.assertEqual(o["status"], "superseded")
        self.assertEqual(o["superseded_by"], new)
        self.assertEqual(n["supersedes"], old)
        self.assertIn("heat-soak", o["correction_reason"])


class TestRetrieve(KbBase):
    def test_house_rule_question(self):
        bundle = retrieve_bundle(
            "Can I flash 17AUG2025ATLRUNNINGGREATv6TODAY onto the current 6.3 injector bike?",
            profile={"vehicle_key": "2023-fxlrst", "engine_key": "131ci",
                     "hardware_key": "ss550-63inj-2into1"},
            root=self.root,
            embed=False,
        )
        blob = (bundle.get("context") or "") + " ".join(
            c["claim"] for c in bundle.get("claims") or []
        )
        self.assertTrue(bundle.get("claims") or bundle.get("citations"))
        self.assertIn("6.3", blob)

    def test_wrong_displacement_not_same_vehicle(self):
        src = Path(self.tmp.name) / "114-note.md"
        src.write_text("# 114ci only\nThis note is exclusively for a 114ci FXLRS, not a 131.")
        ingest_path(src, root=self.root, extra={
            "vehicle_key": "2020-fxlrs", "engine_key": "114ci", "project_key": "other",
        })
        bundle = retrieve_bundle(
            "Find validated maps for this exact vehicle and exhaust.",
            profile={"vehicle_key": "2023-fxlrst", "engine_key": "131ci",
                     "hardware_key": "ss550-63inj-2into1", "model": "2023 Low Rider ST"},
            root=self.root,
            embed=False,
        )
        for c in bundle.get("citations") or []:
            if c.get("engine_key"):
                self.assertNotEqual(c["engine_key"], "114ci")

    def test_map_id_fts(self):
        src = Path(self.tmp.name) / "bike.tbw"
        src.write_bytes(synthetic_tbw())
        ingest_path(src, root=self.root, extra={"vehicle_key": "2023-fxlrst",
                                                "engine_key": "131ci", "validated": True})
        bundle = retrieve_bundle(
            "Base map ID HXSSEDCAAN061617 — which files?",
            profile={"vehicle_key": "2023-fxlrst", "engine_key": "131ci"},
            root=self.root,
            embed=False,
        )
        ids = [c.get("map_id") for c in bundle.get("citations") or []]
        self.assertIn("HXSSEDCAAN061617", ids)

    def test_unreadable_not_treated_as_known(self):
        src = Path(self.tmp.name) / "secret.bin"
        src.write_bytes(os.urandom(2048))
        r = ingest_path(src, root=self.root)
        bundle = retrieve_bundle(
            "What AFR values are inside secret.bin?",
            root=self.root, embed=False,
        )
        for c in bundle.get("citations") or []:
            self.assertNotEqual(c.get("file_id"), r["file_id"])


class TestEvalSuite(unittest.TestCase):
    def test_twenty_five_questions(self):
        self.assertEqual(len(QUESTIONS), 25)
        ids = [q["id"] for q in QUESTIONS]
        self.assertEqual(len(ids), len(set(ids)))


class TestClassify(unittest.TestCase):
    def test_tbw_and_datalog(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            tbw = p / "x.tbw"
            tbw.write_bytes(b"x")
            self.assertEqual(classify_file(tbw)[0], "tbw")
            csvp = p / "log.csv"
            csvp.write_text("rpm,tps,afr\n900,1,14.2\n")
            self.assertEqual(classify_file(csvp)[0], "datalog")


if __name__ == "__main__":
    unittest.main()
