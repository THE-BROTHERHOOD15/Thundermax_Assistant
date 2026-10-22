#!/usr/bin/env python3
"""Training pipeline drills — no NAS/Ollama required for the synthetic path."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import tbw_train


class TestTrain(unittest.TestCase):
    def test_drill_passes(self):
        result = tbw_train.drill()
        failed = [c["name"] for c in result["checks"] if not c["ok"]]
        self.assertTrue(result["passed"], failed)

    def test_brief_and_quiz_nonempty(self):
        self.assertIn("tmax read", tbw_train.brief_text())
        self.assertIn("0b00100000", tbw_train.quiz_text())

    def test_cli_brief(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tbw_train.main(["brief"])
        self.assertEqual(rc, 0)
        self.assertIn("tmax read", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
