#!/usr/bin/env python3
"""Log suggestions and tune notes. Temp files only. Never writes a .tbw."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import studio_caps as sc


def _log(n, afr=14.5, et=180, rpm=1024, tps=20):
    lines = ["rpm,tps,afr,target afr,et"]
    for _ in range(n):
        lines.append(f"{rpm},{tps},{afr},13.2,{et}")
    return "\n".join(lines) + "\n"


class StudioCaps(unittest.TestCase):
    def test_lean_log_suggests_fuel_and_short_log_does_not(self):
        short = sc.suggest_log(_log(9))
        self.assertEqual(short["suggestions"], [])
        hit = sc.suggest_log(_log(12))["suggestions"]
        self.assertEqual(len(hit), 1)
        self.assertGreater(hit[0]["pct"], 0)
        self.assertLessEqual(hit[0]["pct"], 15)

    def test_warmup_rows_are_skipped(self):
        out = sc.suggest_log(_log(12, et=100))
        self.assertEqual(out["suggestions"], [])
        self.assertEqual(out["skipped"]["warmup"], 12)

    def test_notes_follow_the_file_hash(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "notes.json"
            tune = Path(d) / "map.tbw"
            tune.write_bytes(b"not-a-real-tune")
            sha = sc.sha256_file(tune)
            db = {}
            sc.note_set(db, sha, {"weather": "70F", "mods": "", "feel": "pop", "text": ""}, tune.name)
            sc.save_notes(db, path)
            loaded = sc.load_notes(path)
            self.assertEqual(sc.note_get(loaded, sha)["feel"], "pop")
            self.assertEqual(sc.note_get(loaded, sha)["files"], ["map.tbw"])
            self.assertIn(sha, json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
