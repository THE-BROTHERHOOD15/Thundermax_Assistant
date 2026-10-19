#!/usr/bin/env python3
"""Tests for the journal -> knowledge-base provenance loop (webui_journal).

Self-contained: redirects the corpus and journal dirs into a temp tree and
stubs the two network calls (embed + HTTP), so no Ollama, Elasticsearch, or
NAS access is needed and Joshua's real corpus/brain index is never touched.

    python3 -m unittest discover -s tests -v

The behaviour under test is the one rule that matters: an entry recorded
outside the proposal pipeline must not be able to launder itself into citable
authority. It stays retrievable, but unboosted and banner-flagged, until a
linked proposal reaches validated_by_ride.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import tune_assistant as ta
import webui_core as core
import webui_journal as jr


class JournalProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.corpus = root / "corpus"
        self.corpus.mkdir()
        self.journal = root / "journal"
        self.journal.mkdir()
        # redirect both stores; scored_passages reads ta.DOCS_DIR at call time
        self._docs, ta.DOCS_DIR = ta.DOCS_DIR, self.corpus
        self._jdir, core.JOURNAL_DIR = core.JOURNAL_DIR, self.journal
        # ES ingest is exercised separately; keep it out of the unit tests
        self._ingest, jr.start_es_ingest = jr.start_es_ingest, lambda jid: None
        self.profile = ta.load_profile()

    def tearDown(self):
        ta.DOCS_DIR = self._docs
        core.JOURNAL_DIR = self._jdir
        jr.start_es_ingest = self._ingest
        self.tmp.cleanup()

    def _entry(self, **kw):
        payload = {"type": "validation_ride", "title": "decel pop check ride",
                   "body": "Closed-throttle 4000-2000 decels in 4th, no pops.",
                   "observations": {"decel_pop": "none", "afr_wot": 12.6}}
        payload.update(kw)
        e = jr.create_entry(payload)
        self.assertNotIn("error", e, e)
        return e

    # --- provenance ---------------------------------------------------------

    def test_new_entry_is_unvetted_regardless_of_what_the_caller_asks_for(self):
        e = self._entry(vetted=True, proposal_id="p-1")
        self.assertFalse(e["vetted"],
                         "create_entry must never honour a caller-supplied vetted flag")

    def test_unvetted_doc_carries_the_banner_and_earns_no_boost(self):
        e = self._entry()
        name = e["doc"]
        text = (self.corpus / name).read_text()
        self.assertIn("UNVETTED", text)
        self.assertNotIn("_learned_", name,
                         "unvetted docs must not carry the boost marker")
        # ...but they ARE retrievable: the name still matches DOC_GLOB
        self.assertIn(self.corpus / name, ta.find_docs())
        hits = ta.scored_passages("decel pop", profile=self.profile)
        self.assertTrue(any(h[1] == name for h in hits),
                        "an unvetted entry must still be findable")

    def test_vetted_upgrade_flips_marker_banner_and_boost(self):
        e = self._entry()
        unvetted_score = self._score_of(e["doc"], "decel pop")
        up = jr.upgrade_entry(e["id"], proposal_id="20260820-000000-abcd")
        self.assertTrue(up["vetted"])
        self.assertIn("_learned_", up["doc"])
        self.assertIn(self.profile["setup_key"], up["doc"])
        text = (self.corpus / up["doc"]).read_text()
        self.assertNotIn("UNVETTED", text)
        # old doc is gone — two retrievable copies would let the stale banner live on
        self.assertFalse((self.corpus / e["doc"]).exists())
        vetted_score = self._score_of(up["doc"], "decel pop")
        self.assertGreater(vetted_score, unvetted_score,
                           "vetted entries must outrank the unvetted version they replace")

    def test_upgrade_requires_a_linked_proposal(self):
        e = self._entry()
        r = jr.upgrade_entry(e["id"])
        self.assertIn("error", r)
        self.assertFalse(jr.load_entry(e["id"])["vetted"])

    def test_update_cannot_promote_an_entry(self):
        e = self._entry()
        up = jr.update_entry(e["id"], {"vetted": True, "title": "edited title"})
        self.assertFalse(up["vetted"])
        self.assertEqual(up["title"], "edited title")
        self.assertIn("UNVETTED", (self.corpus / up["doc"]).read_text())

    def test_edit_leaves_no_orphan_doc(self):
        e = self._entry()
        up = jr.update_entry(e["id"], {"title": "a completely different title"})
        self.assertNotEqual(up["doc"], e["doc"])
        self.assertFalse((self.corpus / e["doc"]).exists())
        self.assertEqual(len(list(self.corpus.glob("*.md"))), 1)

    # --- validation ---------------------------------------------------------

    def test_type_and_title_are_required(self):
        self.assertIn("error", jr.create_entry({"type": "wat", "title": "x"}))
        self.assertIn("error", jr.create_entry({"type": "note", "title": "  "}))

    def test_entry_round_trips_and_lists(self):
        e = self._entry(title="ride one")
        self.assertEqual(jr.load_entry(e["id"])["title"], "ride one")
        rows = jr.list_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], e["id"])
        self.assertFalse(rows[0]["es_indexed"])

    def test_retracting_removes_the_doc_but_keeps_the_record(self):
        e = self._entry()
        real, jr._es_drop_paths = jr._es_drop_paths, lambda *a, **k: 0
        try:
            r = jr.delete_entry_docs(e["id"])
        finally:
            jr._es_drop_paths = real
        self.assertTrue(r["retracted"])
        self.assertIsNone(r["doc"])
        self.assertFalse(list(self.corpus.glob("*.md")))
        self.assertIsNotNone(jr.load_entry(e["id"]))
        # a doc-less entry must not be re-queued forever by the retry sweep
        self.assertNotIn(e["id"], jr.retry_pending_es()["pending"])

    # --- ES ingest bookkeeping (transport stubbed) --------------------------

    def test_es_failure_is_recorded_not_raised_and_retried(self):
        e = self._entry()
        calls = []

        def boom(entry, profile=None):
            calls.append(entry["id"])
            raise ConnectionRefusedError("ES down")

        real, jr.es_index_entry = jr.es_index_entry, boom
        try:
            jr._es_worker(e["id"])
            rec = jr.load_entry(e["id"])
            self.assertFalse(rec["es"]["indexed"])
            self.assertIn("ConnectionRefusedError", rec["es"]["error"])
            # the corpus doc is untouched, so the keyword leg still has it
            self.assertTrue((self.corpus / rec["doc"]).exists())
            out = jr.retry_pending_es()
            self.assertIn(e["id"], out["retried"])
            self.assertIn(e["id"], out["pending"])
            self.assertEqual(len(calls), 2, "retry must re-attempt the failed entry")
        finally:
            jr.es_index_entry = real

    def test_es_success_records_chunk_count(self):
        e = self._entry()
        real, jr.es_index_entry = jr.es_index_entry, lambda entry, profile=None: 3
        try:
            jr._es_worker(e["id"])
        finally:
            jr.es_index_entry = real
        rec = jr.load_entry(e["id"])
        self.assertTrue(rec["es"]["indexed"])
        self.assertEqual(rec["es"]["chunks"], 3)
        self.assertNotIn(e["id"], jr.retry_pending_es()["pending"])

    def test_es_payload_marks_unvetted_and_carries_a_vector(self):
        """The bulk body is what the vector leg actually stores — assert the
        provenance flag and the embedding both make it in."""
        e = self._entry()
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["body"] = req.data.decode()
            class R:
                def read(self_inner): return b'{"errors": false}'
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
            return R()

        stubs = {
            "_embed": (core._embed, lambda text, timeout=15: [0.1] * 768),
            "ensure": (jr._es_ensure_index, lambda timeout=5: None),
            "drop": (jr._es_drop_paths, lambda *a, **k: 0),
            "http": (core._http_json, lambda *a, **k: {}),
        }
        core._embed = stubs["_embed"][1]
        jr._es_ensure_index = stubs["ensure"][1]
        jr._es_drop_paths = stubs["drop"][1]
        core._http_json = stubs["http"][1]
        real_urlopen = jr.urllib.request.urlopen
        jr.urllib.request.urlopen = fake_urlopen
        try:
            n = jr.es_index_entry(jr.load_entry(e["id"]))
        finally:
            jr.urllib.request.urlopen = real_urlopen
            core._embed = stubs["_embed"][0]
            jr._es_ensure_index = stubs["ensure"][0]
            jr._es_drop_paths = stubs["drop"][0]
            core._http_json = stubs["http"][0]

        self.assertGreaterEqual(n, 1)
        docs = [json.loads(l) for l in sent["body"].strip().split("\n")]
        payload = docs[1]
        self.assertTrue(payload["unvetted"])
        self.assertEqual(payload["entry_id"], e["id"])
        self.assertEqual(len(payload["vector"]), 768)
        self.assertIn("UNVETTED", payload["text"])

    # --- stale-chunk cleanup ------------------------------------------------
    # Regression: cleanup used to key on entry_id, but `brain-knowledge` is a
    # shared index created by hermes_rag with no entry_id in its mapping, so ES
    # typed it dynamically as analyzed `text` and the term query matched
    # nothing. Every upgrade silently left the old UNVETTED chunks retrievable.

    def test_rename_queues_the_old_doc_for_vector_cleanup(self):
        e = self._entry()
        old = e["doc"]
        up = jr.upgrade_entry(e["id"], proposal_id="p-9")
        self.assertIn(old, up["prev_doc"],
                      "the pre-upgrade doc must be queued for ES cleanup")

    def test_cleanup_targets_old_path_and_survives_an_es_outage(self):
        e = self._entry()
        old = e["doc"]
        jr.upgrade_entry(e["id"], proposal_id="p-9")
        dropped = []

        def fail_once(entry, profile=None):
            dropped.append([str(p) for p in
                            [ta.DOCS_DIR / entry["doc"]] +
                            [ta.DOCS_DIR / n for n in (entry.get("prev_doc") or [])]])
            if len(dropped) == 1:
                raise ConnectionRefusedError("ES down mid-upgrade")
            return 1

        real, jr.es_index_entry = jr.es_index_entry, fail_once
        try:
            jr._es_worker(e["id"])
            # outage: prev_doc must SURVIVE, or the stale unvetted copy is orphaned
            self.assertIn(old, jr.load_entry(e["id"])["prev_doc"])
            jr._es_worker(e["id"])
        finally:
            jr.es_index_entry = real
        self.assertIn(str(ta.DOCS_DIR / old), dropped[1],
                      "the retry must still target the old path")
        self.assertEqual(jr.load_entry(e["id"])["prev_doc"], [],
                         "a successful reindex clears the cleanup queue")

    def test_drop_paths_uses_a_keyword_field_query(self):
        """Guards the field choice itself: `path` is keyword in hermes_rag's
        base mapping, `entry_id` is not in it at all."""
        sent = {}

        def capture(url, body=None, timeout=None, method=None):
            sent["url"], sent["body"] = url, body
            return {"deleted": 2}

        real, core._http_json = core._http_json, capture
        try:
            n = jr._es_drop_paths([ta.DOCS_DIR / "a.md", ta.DOCS_DIR / "b.md"])
        finally:
            core._http_json = real
        self.assertEqual(n, 2)
        self.assertIn("_delete_by_query", sent["url"])
        self.assertIn("path", sent["body"]["query"]["terms"])
        self.assertEqual(len(sent["body"]["query"]["terms"]["path"]), 2)

    def test_drop_paths_noops_on_empty(self):
        def explode(*a, **k):
            self.fail("must not call ES with an empty path list")
        real, core._http_json = core._http_json, explode
        try:
            self.assertEqual(jr._es_drop_paths([]), 0)
            self.assertEqual(jr._es_drop_paths([None]), 0)
        finally:
            core._http_json = real

    # --- helpers ------------------------------------------------------------

    def _score_of(self, docname, question):
        for score, name, _ in ta.scored_passages(question, profile=self.profile):
            if name == docname:
                return score
        self.fail(f"{docname} not retrieved for {question!r}")


if __name__ == "__main__":
    unittest.main()
