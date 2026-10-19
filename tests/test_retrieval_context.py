#!/usr/bin/env python3
"""Tests for how retrieved excerpts are assembled into model context.

Regression origin (live, 2026-08-20): a validation-ride journal entry was
retrieved correctly and sat at citation [1], yet both qwen2.5-coder:14b and
:32b answered "what AFRs did I record?" from a blank ride-report template
instead — the 32b inventing plausible values and presenting them as the
rider's. With the same entry as the only excerpt, the 32b answered verbatim
and cited it. The failure was context dilution, so the context block now names
what each source IS and separates first-hand records from reference material.

No Ollama, ES, or NAS needed.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tune_assistant as ta
import webui_core as core


def cite(n, source, text="body text", retriever="vector"):
    return {"n": n, "kind": core._provenance(source), "source": source,
            "text": text, "retriever": retriever, "path": "", "score": 1.0}


class ProvenanceTest(unittest.TestCase):
    def test_classification_matches_the_boost_tiers(self):
        self.assertEqual(core._provenance(
            "thundermax_journal_m8-131_20260820-1_ride.md"), "journal_unvetted")
        self.assertEqual(core._provenance(
            "thundermax_learned_m8-131_20260820-1_ride.md"), "mine")
        self.assertEqual(core._provenance(
            "thundermax_nas_2025_08_30_ride_packet.md"), "reference")


class ContextBlockTest(unittest.TestCase):
    def test_first_hand_records_lead_and_are_labelled(self):
        cites = [
            cite(1, "thundermax_nas_ride_packet.md", "Cruise AFR: ______"),
            cite(2, "thundermax_learned_m8_20260820-1_ride.md", "afr wot: 12.6"),
            cite(3, "thundermax_journal_m8_20260820-2_note.md", "pops returned"),
        ]
        ctx = core._build_context(cites)
        mine = ctx.index("MY OWN RECORDS")
        ref = ctx.index("REFERENCE MATERIAL")
        self.assertLess(mine, ref, "first-hand records must precede reference material")
        self.assertLess(ctx.index("afr wot: 12.6"), ref)
        self.assertLess(ctx.index("pops returned"), ref)
        self.assertGreater(ctx.index("Cruise AFR: ______"), ref)

    def test_vetted_and_unvetted_records_are_distinguishable(self):
        ctx = core._build_context([
            cite(1, "thundermax_learned_m8_20260820-1_a.md"),
            cite(2, "thundermax_journal_m8_20260820-2_b.md"),
        ])
        self.assertIn("vetted knowledge for this exact bike", ctx)
        self.assertIn("outside the vetting pipeline", ctx)

    def test_citation_numbers_are_preserved_across_the_split(self):
        """Sectioning reorders the text; the [n] a citation was assigned must
        still point at the same excerpt, or every inline cite is wrong."""
        cites = [cite(1, "ref_a.md", "AAA"), cite(2, "thundermax_learned_x_1_b.md", "BBB"),
                 cite(3, "ref_c.md", "CCC")]
        ctx = core._build_context(cites)
        for c in cites:
            head = ctx.index(f"[{c['n']}]")
            self.assertEqual(ctx[head:].split("\n")[1], c["text"])

    def test_sections_are_omitted_when_empty(self):
        only_ref = core._build_context([cite(1, "ref.md")])
        self.assertNotIn("MY OWN RECORDS", only_ref)
        only_mine = core._build_context([cite(1, "thundermax_learned_x_1_a.md")])
        self.assertNotIn("REFERENCE MATERIAL", only_mine)
        self.assertEqual(core._build_context([]), "")

    def test_cite_rule_warns_against_treating_templates_as_data(self):
        self.assertIn("NEVER", core.CITE_RULE)
        self.assertIn("template", core.CITE_RULE.lower())


class CompetitorDownRankTest(unittest.TestCase):
    """A real-world test once had the assistant walking Joshua through a Power
    Vision flashing workflow — the wrong tuner entirely for a ThunderMax bike.
    Corpus docs mentioning competing tuners must never outrank ThunderMax
    material. The penalty lives in scored_passages(), which is the single
    scoring path: the web co-pilot's keyword leg reads it too, so a regression
    here would leak straight into the citations."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.corpus = Path(self.tmp.name)
        self._docs, ta.DOCS_DIR = ta.DOCS_DIR, self.corpus
        # identical wording, so ONLY the competitor mention differs
        body = "To flash the tune, connect the device and load the map file."
        (self.corpus / "thundermax_clean.md").write_text(body)
        (self.corpus / "thundermax_tainted.md").write_text(
            body + " Use the Power Vision PV3 from Dynojet.")

    def tearDown(self):
        ta.DOCS_DIR = self._docs
        self.tmp.cleanup()

    def test_competitor_passages_are_down_ranked(self):
        scores = {n: s for s, n, _ in ta.scored_passages("flash the tune map file")}
        self.assertIn("thundermax_clean.md", scores)
        self.assertIn("thundermax_tainted.md", scores)
        self.assertLess(scores["thundermax_tainted.md"], scores["thundermax_clean.md"],
                        "competing-tuner content must never outrank ThunderMax content")

    def test_every_competitor_term_triggers_the_penalty(self):
        for term in ta.COMPETITOR_TERMS:
            (self.corpus / "thundermax_tainted.md").write_text(
                f"To flash the tune, load the map file. Use the {term} tool.")
            scores = {n: s for s, n, _ in ta.scored_passages("flash the tune map file")}
            self.assertLess(scores.get("thundermax_tainted.md", 0),
                            scores["thundermax_clean.md"], f"term {term!r} not penalised")


if __name__ == "__main__":
    unittest.main()
