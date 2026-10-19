#!/usr/bin/env python3
"""Tests for the proposal store, state machine, and vetting pipeline (vetting).

Self-contained: the proposal store, journal, and corpus are redirected into a
temp tree, retrieval and the adversarial LLM call are stubbed. No Ollama, no
Elasticsearch, no NAS, no network — and Joshua's real corpus is never touched.

    python3 -m unittest discover -s tests -v

What these tests actually defend, in order of how much a mistake would cost:

  * deterministic guardrail blocks cannot be approved away, by anyone,
  * three "safe" +/-2 deg steps cannot ladder into +6 deg one approval at a
    time (the cross-proposal stacking guard),
  * a proposal whose absolute cell values are unknowable (per-cell .tbw
    scaling is still unconfirmed) cannot be approved without Joshua explicitly
    taking on the job of reading those values in TMax Tuner,
  * the six loopholes an adversarial design review found: (a) forged `vetted`,
    (b) approval with no report, (c) mutated changes, (d) evidence swapped
    under a stale report, (e) silent "applied", (f) an unpromoted ride entry,
  * and the adversarial reviewer can never be the weak model, nor the one that
    wrote the proposal.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import guardrails  # noqa: E402
import tune_assistant as ta  # noqa: E402
import webui_core as core  # noqa: E402
import webui_journal as jr  # noqa: E402
import vetting as v  # noqa: E402


def spark_change(magnitude=2.0, direction="increase", rpm=(2048, 2816),
                 tps=(0, 2), target=30.0, table="spark_advance_front", **kw):
    ch = {"table": table, "cylinder": "both", "rpm_band": list(rpm),
          "tps_band": list(tps), "direction": direction,
          "magnitude": magnitude, "unit": "deg", "target_value": target,
          "current_value": None if target is None else target - magnitude,
          "claim": "pulls the knock-prone belly cells back a step"}
    ch.update(kw)
    return ch


def ve_change(magnitude=2.0, direction="increase", rpm=(3840, 4608), tps=(0, 2),
              table="ve_front", **kw):
    ch = {"table": table, "cylinder": "both", "rpm_band": list(rpm),
          "tps_band": list(tps), "direction": direction,
          "magnitude": magnitude, "unit": "ve_pct", "target_value": None,
          "current_value": None,
          "claim": "house decel-pop protocol: +2% VE at closed throttle"}
    ch.update(kw)
    return ch


class VettingTestCase(unittest.TestCase):
    """Shared fixture: temp store + stubbed retrieval + stubbed LLM."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.corpus = root / "corpus"
        self.corpus.mkdir()
        self.props = root / "proposals"
        self.props.mkdir()
        self.journal = root / "journal"
        self.journal.mkdir()
        self._docs, ta.DOCS_DIR = ta.DOCS_DIR, self.corpus
        self._pdir, core.PROPOSALS_DIR = core.PROPOSALS_DIR, self.props
        self._jdir, core.JOURNAL_DIR = core.JOURNAL_DIR, self.journal
        self._ingest, jr.start_es_ingest = jr.start_es_ingest, lambda jid: None

        # --- stage 2: retrieval. Records that GEN_LOCK is NOT held while it
        # runs (the VRAM slot must never be held around retrieval).
        self.retrieval_saw_gen_lock_held = []
        self.support_text = None   # set per test; None => echo the claim back
        self._retrieve, core.unified_retrieve = core.unified_retrieve, self._fake_retrieve

        # --- stage 3: the adversarial LLM. Records the model it was asked for.
        self.llm_calls = []
        self.llm_reply = "The rear cylinder heat-soaks first.\nVERDICT: CONCUR"
        self._llm, v._llm_generate = v._llm_generate, self._fake_llm

    def tearDown(self):
        ta.DOCS_DIR = self._docs
        core.PROPOSALS_DIR = self._pdir
        core.JOURNAL_DIR = self._jdir
        jr.start_es_ingest = self._ingest
        core.unified_retrieve = self._retrieve
        v._llm_generate = self._llm
        self.tmp.cleanup()
        # a leaked generation slot would wedge every later request
        self.assertTrue(core.GEN_LOCK.acquire(blocking=False),
                        "GEN_LOCK must be released by the time vetting returns")
        core.GEN_LOCK.release()

    # --- stubs --------------------------------------------------------------

    def _fake_retrieve(self, question, profile=None):
        held = not core.GEN_LOCK.acquire(blocking=False)
        if not held:
            core.GEN_LOCK.release()
        self.retrieval_saw_gen_lock_held.append(held)
        text = self.support_text if self.support_text is not None else question
        cites = [{"n": 1, "kind": "reference", "source": "thundermax_nas_manual.md",
                  "path": "", "retriever": "keyword", "score": 1.0, "text": text}]
        return cites, core._build_context(cites), None

    def _fake_llm(self, model, messages, job=None, timeout=900, tier=None):
        self.llm_calls.append({"model": model, "messages": messages, "job": job})
        return self.llm_reply

    # --- helpers ------------------------------------------------------------

    def make(self, changes=None, **kw):
        payload = {"title": "kill the decel pops", "claim": "closed-throttle "
                   "pops above 4k need a touch more fuel",
                   "changes": changes or [ve_change()], "origin_tier": "smart"}
        payload.update(kw)
        p = v.create_proposal(payload)
        self.assertNotIn("error", p, p)
        return p

    def vetted(self, changes=None, **kw):
        """Create + vet, asserting the vet came back clean."""
        p = self.make(changes, **kw)
        out = v.vet_proposal(p["id"])
        self.assertEqual(out["state"], v.VETTED, out.get("vet", {}).get("findings"))
        return out

    def force(self, pid, **fields):
        """Write fields straight into the stored JSON — simulates a hand-edited
        or corrupted store, so defense-in-depth checks are actually reachable."""
        path = v.proposal_path(pid)
        data = json.loads(path.read_text())
        data.update(fields)
        path.write_text(json.dumps(data))
        return data


# ----------------------------------------------------------------------------
# Store + immutability
# ----------------------------------------------------------------------------

class ProposalStoreTest(VettingTestCase):
    def test_create_writes_a_draft_with_a_frozen_hash(self):
        p = self.make()
        self.assertEqual(p["state"], v.DRAFT)
        self.assertEqual(p["changes_hash"], v.changes_hash(p["changes"]))
        self.assertTrue(v.proposal_path(p["id"]).exists())
        self.assertEqual(v.load_proposal(p["id"])["title"], "kill the decel pops")
        self.assertEqual(p["history"][0]["event"], "created")

    def test_create_validates_its_input(self):
        self.assertEqual(v.create_proposal({"changes": [ve_change()]})["code"],
                         "title_required")
        self.assertEqual(v.create_proposal({"title": "x", "changes": []})["code"],
                         "changes_required")
        self.assertEqual(v.create_proposal({"title": "x", "changes": [{"magnitude": 1}]})["code"],
                         "bad_change")

    def test_unit_is_inferred_from_the_tmax_page_when_omitted(self):
        p = self.make([dict(ve_change(), unit=""), dict(spark_change(), unit="")])
        self.assertEqual(p["changes"][0]["unit"], "ve_pct")
        self.assertEqual(p["changes"][1]["unit"], "deg")

    def test_listing_summarises_without_loading_everything(self):
        a, b = self.make(), self.make(title="second")
        rows = v.list_proposals()
        self.assertEqual({r["id"] for r in rows}, {a["id"], b["id"]})
        self.assertEqual(rows[0]["changes"], 1)

    # --- loophole (c) -------------------------------------------------------

    def test_c_module_exposes_no_way_to_edit_changes(self):
        for name in ("update_proposal", "patch_proposal", "edit_proposal",
                     "set_changes", "update_changes", "replace_changes"):
            self.assertFalse(hasattr(v, name),
                             f"vetting.{name} must not exist — changes are immutable")

    def test_c_revising_forks_a_new_draft_and_leaves_the_original_alone(self):
        original = self.vetted()
        fresh = v.revise_proposal(original["id"], {"changes": [ve_change(magnitude=1.0)]})
        self.assertNotEqual(fresh["id"], original["id"])
        self.assertEqual(fresh["state"], v.DRAFT)
        self.assertEqual(fresh["supersedes"], original["id"])
        self.assertIsNone(fresh["vet"], "a fork starts unvetted")
        kept = v.load_proposal(original["id"])
        self.assertEqual(kept["changes"], original["changes"])
        self.assertEqual(kept["changes_hash"], original["changes_hash"])
        self.assertEqual(kept["superseded_by"], fresh["id"])

    def test_c_tampered_changes_are_blocked_at_vet_time(self):
        p = self.make()
        self.force(p["id"], changes=[ve_change(magnitude=1.0)])
        out = v.vet_proposal(p["id"])
        self.assertEqual(out["state"], v.DRAFT)
        self.assertIn("changes_tampered",
                      [f["rule"] for f in out["vet"]["findings"]])
        self.assertFalse(out["vet"]["passed"])

    def test_c_tampered_changes_are_caught_again_at_approval(self):
        p = self.vetted()
        self.force(p["id"], changes=[ve_change(magnitude=4.0)])
        r = v.transition(p["id"], v.APPROVED)
        self.assertEqual(r["code"], "changes_tampered")
        self.assertEqual(r["status"], 409)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.VETTED)


# ----------------------------------------------------------------------------
# Stage 1 — guardrails are the sole block authority
# ----------------------------------------------------------------------------

class GuardrailStageTest(VettingTestCase):
    def test_three_degree_spark_proposal_is_blocked_and_cannot_be_approved(self):
        p = self.make([spark_change(magnitude=3.0, target=31.0)])
        out = v.vet_proposal(p["id"])
        self.assertFalse(out["vet"]["passed"])
        self.assertEqual(out["vet"]["blocks"], 1)
        self.assertIn("spark_step", [f["rule"] for f in out["vet"]["findings"]])
        self.assertEqual(out["state"], v.DRAFT, "a blocked proposal stays a draft")
        refusal = v.transition(p["id"], v.APPROVED)
        self.assertEqual(refusal["status"], 409)
        self.assertEqual(refusal["code"], "bad_transition")

    def test_a_blocked_report_cannot_be_approved_even_from_vetted(self):
        """Defense in depth: even if something forced the state, the report's
        own block count refuses the approval."""
        p = self.make([spark_change(magnitude=3.0, target=31.0)])
        v.vet_proposal(p["id"])
        self.force(p["id"], state=v.VETTED)
        r = v.transition(p["id"], v.APPROVED)
        self.assertEqual(r["code"], "vet_blocked")
        self.assertEqual(r["status"], 409)

    def test_the_llm_can_neither_block_nor_clear_a_block(self):
        self.llm_reply = ("This is catastrophic, it will hole a piston.\n"
                          "VERDICT: OBJECT")
        clean = self.make([ve_change(magnitude=2.0)])
        out = v.vet_proposal(clean["id"])
        self.assertEqual(out["vet"]["blocks"], 0, "an OBJECT is a warn, never a block")
        self.assertEqual(out["state"], v.VETTED)
        self.assertIn("adversarial_objection", [f["rule"] for f in out["vet"]["findings"]])

        self.llm_reply = "Looks perfect to me.\nVERDICT: CONCUR"
        blocked = self.make([spark_change(magnitude=3.0, target=31.0)])
        out = v.vet_proposal(blocked["id"])
        self.assertFalse(out["vet"]["passed"],
                         "a CONCUR cannot clear a deterministic block")
        self.assertEqual(out["state"], v.DRAFT)

    def test_rear_timing_advance_is_blocked(self):
        p = self.make([spark_change(table="rear_timing_offset", magnitude=1.0,
                                    direction="increase", target=1.0)])
        out = v.vet_proposal(p["id"])
        self.assertIn("rear_timing", [f["rule"] for f in out["vet"]["findings"]])
        self.assertEqual(out["state"], v.DRAFT)

    def test_findings_record_which_stage_produced_them(self):
        out = self.vetted()
        self.assertTrue(all("stage" in f for f in out["vet"]["findings"]))

    def test_report_shape_is_stable(self):
        out = self.vetted()
        rep = out["vet"]
        for key in ("at", "proposal_id", "changes_hash", "dyno_hash", "stages",
                    "findings", "blocks", "warns", "checks_unverifiable",
                    "stacking", "refuted_by", "passed"):
            self.assertIn(key, rep)
        self.assertEqual(set(rep["stages"]), {"guardrails", "citations", "adversarial"})


# ----------------------------------------------------------------------------
# Cross-proposal stacking guard
# ----------------------------------------------------------------------------

class StackingGuardTest(VettingTestCase):
    def test_two_sequential_two_degree_steps_stack_into_a_block(self):
        first = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        approved = v.transition(first["id"], v.APPROVED)
        self.assertEqual(approved["state"], v.APPROVED)

        second = self.make([spark_change(magnitude=2.0, target=32.0)])
        out = v.vet_proposal(second["id"])
        self.assertFalse(out["vet"]["passed"])
        self.assertIn("spark_stacking", [f["rule"] for f in out["vet"]["findings"]])
        self.assertEqual(out["state"], v.DRAFT)
        self.assertEqual(out["vet"]["stacking"]["net"]["deg"], 4.0)
        self.assertEqual(out["vet"]["stacking"]["overlaps"][0]["proposal_id"],
                         first["id"])

    def test_applied_proposals_still_count_toward_the_net(self):
        first = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        v.transition(first["id"], v.APPROVED)
        v.transition(first["id"], v.APPLIED, note="typed into TMax Tuner")
        second = self.make([spark_change(magnitude=2.0, target=32.0)])
        out = v.vet_proposal(second["id"])
        self.assertIn("spark_stacking", [f["rule"] for f in out["vet"]["findings"]])

    def test_rejected_and_draft_proposals_do_not_count(self):
        first = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        v.transition(first["id"], v.REJECTED, note="changed my mind")
        second = self.make([spark_change(magnitude=2.0, target=32.0)])
        out = v.vet_proposal(second["id"])
        self.assertTrue(out["vet"]["passed"])
        self.assertEqual(out["vet"]["stacking"]["overlaps"], [])

    def test_a_different_band_does_not_stack(self):
        first = self.vetted([spark_change(magnitude=2.0, rpm=(2048, 2816), target=30.0)])
        v.transition(first["id"], v.APPROVED)
        second = self.make([spark_change(magnitude=2.0, rpm=(4000, 4600), target=30.0)])
        out = v.vet_proposal(second["id"])
        self.assertTrue(out["vet"]["passed"])
        self.assertEqual(out["vet"]["stacking"]["overlaps"], [])

    def test_a_different_table_does_not_stack(self):
        first = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        v.transition(first["id"], v.APPROVED)
        second = self.make([spark_change(table="spark_advance_rear",
                                         magnitude=2.0, target=30.0)])
        out = v.vet_proposal(second["id"])
        self.assertTrue(out["vet"]["passed"])

    def test_opposing_steps_net_out(self):
        """Backing out a change is not laddering — the net is signed."""
        first = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        v.transition(first["id"], v.APPROVED)
        second = self.make([spark_change(magnitude=2.0, direction="decrease",
                                         target=28.0)])
        out = v.vet_proposal(second["id"])
        self.assertEqual(out["vet"]["stacking"]["net"]["deg"], 0.0)
        self.assertTrue(out["vet"]["passed"])

    def test_ve_stacking_warns_at_the_house_threshold(self):
        first = self.vetted([ve_change(magnitude=2.0)])
        v.transition(first["id"], v.APPROVED)
        second = self.make([ve_change(magnitude=2.0)])
        out = v.vet_proposal(second["id"])
        self.assertTrue(out["vet"]["passed"], "VE stacking warns, it does not block here")
        self.assertIn("ve_stacking", [f["rule"] for f in out["vet"]["findings"]])
        self.assertEqual(out["vet"]["stacking"]["net"]["ve_pct"], 4.0)

    def test_the_guard_runs_again_at_approval_time(self):
        """Two proposals vetted while both were still drafts: whichever is
        approved second must be caught, or the ladder just needs two tabs."""
        a = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        b = self.vetted([spark_change(magnitude=2.0, target=32.0)])
        self.assertEqual(v.transition(a["id"], v.APPROVED)["state"], v.APPROVED)
        refusal = v.transition(b["id"], v.APPROVED)
        self.assertEqual(refusal["code"], "stacking_blocked")
        self.assertEqual(refusal["status"], 409)
        self.assertEqual(v.load_proposal(b["id"])["state"], v.VETTED)


# ----------------------------------------------------------------------------
# Unverifiable absolute values -> explicit acknowledgment
# ----------------------------------------------------------------------------

class UnverifiableAcknowledgmentTest(VettingTestCase):
    def _delta_only(self):
        """An advance with an unknown baseline: per-cell scaling is
        unconfirmed, so nothing here can know where the cell lands."""
        return self.make([spark_change(magnitude=1.0, target=None)])

    def test_unknown_baseline_warns_and_still_vets(self):
        out = v.vet_proposal(self._delta_only()["id"])
        self.assertTrue(out["vet"]["passed"])
        self.assertEqual(out["state"], v.VETTED)
        self.assertEqual(out["vet"]["checks_unverifiable"], 1)
        self.assertIn("spark_absolute", [f["rule"] for f in out["vet"]["findings"]])

    def test_b_approval_needs_the_acknowledgment(self):
        p = v.vet_proposal(self._delta_only()["id"])
        refusal = v.transition(p["id"], v.APPROVED)
        self.assertEqual(refusal["code"], "acknowledgment_required")
        self.assertEqual(refusal["status"], 409)
        self.assertEqual(refusal["checks_unverifiable"], 1)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.VETTED)

    def test_b_acknowledged_approval_is_recorded(self):
        p = v.vet_proposal(self._delta_only()["id"])
        ok = v.approve(p["id"], acknowledge_unverifiable=True)
        self.assertEqual(ok["state"], v.APPROVED)
        self.assertTrue(ok["acknowledgment"]["unverifiable_ack"])
        self.assertIn("TMax Tuner", ok["acknowledgment"]["text"])
        self.assertEqual(ok["acknowledgment"]["checks_unverifiable"], 1)

    def test_a_fully_verifiable_proposal_needs_no_acknowledgment(self):
        p = self.vetted([ve_change(magnitude=2.0)])
        self.assertEqual(p["vet"]["checks_unverifiable"], 0)
        ok = v.transition(p["id"], v.APPROVED)
        self.assertEqual(ok["state"], v.APPROVED)
        self.assertIsNone(ok["acknowledgment"])


# ----------------------------------------------------------------------------
# State machine loopholes (a), (b), (d), (e), (f)
# ----------------------------------------------------------------------------

class StateMachineTest(VettingTestCase):
    # --- (a) ---------------------------------------------------------------

    def test_a_generic_transition_cannot_enter_vetted(self):
        p = self.make()
        r = v.transition(p["id"], v.VETTED, note="trust me")
        self.assertEqual(r["code"], "vet_only")
        self.assertEqual(r["status"], 409)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.DRAFT)
        self.assertIsNone(v.load_proposal(p["id"])["vet"])

    def test_a_only_the_vet_handler_sets_vetted(self):
        self.assertEqual(v.VET_ONLY_STATES, {v.VETTED})
        self.assertIn(v.VETTED, v.state_payload()["vet_only"])
        p = self.make()
        self.assertEqual(v.vet_proposal(p["id"])["state"], v.VETTED)

    def test_a_a_failed_revet_demotes_a_vetted_proposal(self):
        p = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        other = self.vetted([spark_change(magnitude=2.0, target=30.0)])
        v.transition(other["id"], v.APPROVED)   # now the first one stacks
        out = v.vet_proposal(p["id"])
        self.assertEqual(out["state"], v.DRAFT,
                         "a re-vet that finds blocks must not leave `vetted` standing")

    # --- (b) ---------------------------------------------------------------

    def test_b_absent_vet_report_is_not_a_pass(self):
        p = self.make()
        self.force(p["id"], state=v.VETTED, vet=None)
        r = v.transition(p["id"], v.APPROVED)
        self.assertEqual(r["code"], "vet_required")
        self.assertEqual(r["status"], 409)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.VETTED)

    def test_b_a_report_for_different_changes_is_stale(self):
        p = self.vetted()
        stale = dict(p["vet"], changes_hash="deadbeef")
        self.force(p["id"], vet=stale)
        r = v.transition(p["id"], v.APPROVED)
        self.assertEqual(r["code"], "vet_stale")

    # --- (d) ---------------------------------------------------------------

    def test_d_attaching_a_dyno_run_invalidates_the_vet(self):
        p = self.vetted()
        out = v.attach_dyno_run(p["id"], {"id": "run-1", "label": "baseline pull",
                                          "data": {"peak_hp": 121.4}})
        self.assertEqual(out["state"], v.DRAFT)
        self.assertIsNone(out["vet"])
        self.assertEqual(len(out["dyno_runs"]), 1)
        self.assertNotEqual(out["dyno_hash"], p["dyno_hash"])
        self.assertIn("vet_invalidated", [h["event"] for h in out["history"]])
        r = v.transition(out["id"], v.APPROVED)
        self.assertEqual(r["status"], 409)

    def test_d_detaching_a_dyno_run_invalidates_the_vet(self):
        p = self.make()
        v.attach_dyno_run(p["id"], {"id": "run-1"})
        after = v.vet_proposal(p["id"])
        self.assertEqual(after["state"], v.VETTED)
        out = v.detach_dyno_run(p["id"], "run-1")
        self.assertEqual(out["state"], v.DRAFT)
        self.assertIsNone(out["vet"])
        self.assertEqual(out["dyno_runs"], [])

    def test_d_attaching_after_approval_forces_a_revet(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        out = v.attach_dyno_run(p["id"], {"id": "run-2"})
        self.assertEqual(out["state"], v.DRAFT, "approval does not survive new evidence")
        self.assertIsNone(out["vet"])

    def test_d_a_report_whose_dyno_set_moved_is_stale(self):
        p = self.vetted()
        # sneak the runs in without going through attach_dyno_run
        self.force(p["id"], dyno_runs=[{"id": "run-x"}],
                   dyno_hash=v.dyno_hash([{"id": "run-x"}]))
        r = v.transition(p["id"], v.APPROVED)
        self.assertEqual(r["code"], "vet_stale")

    def test_d_evidence_cannot_change_once_it_is_on_the_bike(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        v.transition(p["id"], v.APPLIED, note="entered by hand in TMax Tuner")
        r = v.attach_dyno_run(p["id"], {"id": "run-3"})
        self.assertEqual(r["code"], "bad_state")
        self.assertEqual(r["status"], 409)

    # --- (e) ---------------------------------------------------------------

    def test_e_applied_on_bike_requires_a_confirmation_note(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        r = v.transition(p["id"], v.APPLIED)
        self.assertEqual(r["code"], "note_required")
        self.assertEqual(r["status"], 400)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.APPROVED)
        r = v.transition(p["id"], v.APPLIED, note="   ")
        self.assertEqual(r["code"], "note_required")

    def test_e_the_note_is_recorded_as_a_hand_application(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        out = v.mark_applied(p["id"], note="typed +2% into the VE front page, saved v7")
        self.assertEqual(out["state"], v.APPLIED)
        self.assertIn("VE front page", out["applied"]["note"])
        self.assertTrue(out["applied"]["at"])

    # --- (f) ---------------------------------------------------------------

    def _applied(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        return v.mark_applied(p["id"], note="applied by hand in TMax Tuner")

    def _ride_entry(self):
        e = jr.create_entry({"type": "validation_ride", "title": "decel pop check ride",
                             "body": "Closed-throttle 4000-2000 decels in 4th, no pops.",
                             "observations": {"decel_pop": "none"}})
        self.assertNotIn("error", e, e)
        return e

    def test_f_validation_requires_a_journal_entry_id(self):
        p = self._applied()
        r = v.transition(p["id"], v.VALIDATED)
        self.assertEqual(r["code"], "entry_required")
        self.assertEqual(r["status"], 400)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.APPLIED)

    def test_f_an_unknown_entry_id_is_refused(self):
        p = self._applied()
        r = v.transition(p["id"], v.VALIDATED, entry_id="nope")
        self.assertEqual(r["status"], 404)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.APPLIED)

    def test_f_validation_calls_upgrade_entry_and_promotes_the_record(self):
        p = self._applied()
        entry = self._ride_entry()
        self.assertFalse(entry["vetted"])
        calls = []
        real = jr.upgrade_entry

        def spy(jid, proposal_id=None):
            calls.append((jid, proposal_id))
            return real(jid, proposal_id)

        jr.upgrade_entry = spy
        try:
            out = v.mark_validated(p["id"], entry["id"], note="150 mile shakedown")
        finally:
            jr.upgrade_entry = real
        self.assertEqual(out["state"], v.VALIDATED)
        self.assertEqual(calls, [(entry["id"], p["id"])],
                         "the linked entry must be promoted through upgrade_entry")
        promoted = jr.load_entry(entry["id"])
        self.assertTrue(promoted["vetted"])
        self.assertEqual(promoted["proposal_id"], p["id"])
        self.assertIn("_learned_", promoted["doc"])
        self.assertNotIn("UNVETTED", (self.corpus / promoted["doc"]).read_text())
        self.assertEqual(out["validation"]["entry_id"], entry["id"])
        self.assertEqual(out["validation"]["entry_doc"], promoted["doc"])

    def test_f_a_failed_promotion_leaves_the_state_alone(self):
        p = self._applied()
        entry = self._ride_entry()
        real = jr.upgrade_entry
        jr.upgrade_entry = lambda jid, proposal_id=None: {"error": "ES exploded"}
        try:
            r = v.mark_validated(p["id"], entry["id"])
        finally:
            jr.upgrade_entry = real
        self.assertEqual(r["code"], "upgrade_failed")
        self.assertEqual(v.load_proposal(p["id"])["state"], v.APPLIED)

    # --- general transition legality ---------------------------------------

    def test_illegal_jumps_are_refused(self):
        p = self.make()
        for target in (v.APPROVED, v.APPLIED, v.VALIDATED):
            r = v.transition(p["id"], target, note="n", entry_id="x")
            self.assertEqual(r["code"], "bad_transition", target)
        self.assertEqual(v.load_proposal(p["id"])["state"], v.DRAFT)

    def test_terminal_states_are_terminal(self):
        p = self.make()
        v.transition(p["id"], v.REJECTED, note="bad idea")
        for target in (v.DRAFT, v.APPROVED, v.VETTED):
            r = v.transition(p["id"], target)
            self.assertIn(r["code"], ("bad_transition", "vet_only"))
        self.assertEqual(v.load_proposal(p["id"])["state"], v.REJECTED)

    def test_a_rejected_proposal_cannot_be_revetted(self):
        p = self.make()
        v.transition(p["id"], v.REJECTED, note="no")
        r = v.vet_proposal(p["id"])
        self.assertEqual(r["code"], "bad_state")

    def test_recalling_to_draft_drops_the_vet_report(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        out = v.transition(p["id"], v.DRAFT, note="want another look")
        self.assertEqual(out["state"], v.DRAFT)
        self.assertIsNone(out["vet"])
        self.assertIsNone(out["acknowledgment"])

    def test_unknown_state_and_missing_proposal(self):
        p = self.make()
        self.assertEqual(v.transition(p["id"], "shipped")["status"], 400)
        self.assertEqual(v.transition("no-such-id", v.REJECTED)["status"], 404)
        self.assertEqual(v.vet_proposal("no-such-id")["status"], 404)

    def test_history_is_append_only_and_records_every_move(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        out = v.mark_applied(p["id"], note="by hand")
        events = [h["event"] for h in out["history"]]
        self.assertEqual(events[0], "created")
        self.assertIn("vetted", events)
        moves = [(h.get("from"), h.get("to")) for h in out["history"]
                 if h["event"] == "state"]
        self.assertIn((v.DRAFT, v.VETTED), moves)
        self.assertIn((v.VETTED, v.APPROVED), moves)
        self.assertIn((v.APPROVED, v.APPLIED), moves)

    def test_full_lifecycle_draft_to_validated(self):
        p = self.make([ve_change(magnitude=2.0)])
        self.assertEqual(p["state"], v.DRAFT)
        p = v.vet_proposal(p["id"])
        self.assertEqual(p["state"], v.VETTED)
        p = v.approve(p["id"], note="matches the house decel-pop protocol")
        self.assertEqual(p["state"], v.APPROVED)
        p = v.mark_applied(p["id"], note="+2% VE 3840-4608 @ 0-2% TPS, saved as v8")
        self.assertEqual(p["state"], v.APPLIED)
        entry = self._ride_entry()
        p = v.mark_validated(p["id"], entry["id"], note="pops gone")
        self.assertEqual(p["state"], v.VALIDATED)
        self.assertTrue(jr.load_entry(entry["id"])["vetted"])
        self.assertEqual(v.list_proposals(state=v.VALIDATED)[0]["id"], p["id"])


# ----------------------------------------------------------------------------
# Stage 2 — citation cross-check
# ----------------------------------------------------------------------------

class CitationStageTest(VettingTestCase):
    def test_supported_claims_are_marked_supported(self):
        out = self.vetted()          # stub echoes the claim back verbatim
        stage = out["vet"]["stages"]["citations"]
        self.assertTrue(all(c["supported"] for c in stage["claims"]), stage["claims"])
        self.assertEqual(stage["unsupported"], 0)
        self.assertNotIn("citation_support",
                         [f["rule"] for f in out["vet"]["findings"]])

    def test_unsupported_claims_warn_but_never_block(self):
        self.support_text = "Torque the primary chain inspection cover to 108 in-lbs."
        out = self.vetted()
        stage = out["vet"]["stages"]["citations"]
        self.assertGreater(stage["unsupported"], 0)
        findings = [f for f in out["vet"]["findings"] if f["rule"] == "citation_support"]
        self.assertTrue(findings)
        self.assertTrue(all(f["severity"] == "warn" for f in findings))
        self.assertIn("unsupported by corpus", findings[0]["message"])
        self.assertTrue(out["vet"]["passed"], "stage 2 never blocks")

    def test_every_claim_is_checked_including_per_change_claims(self):
        out = self.vetted([ve_change(), spark_change(claim="belly cells need less")])
        idxs = [c["change_idx"] for c in out["vet"]["stages"]["citations"]["claims"]]
        self.assertIn(None, idxs, "the proposal-level claim is checked")
        self.assertIn(0, idxs)
        self.assertIn(1, idxs)

    def test_a_claimless_proposal_is_warned_about(self):
        out = self.vetted([dict(ve_change(), claim="")], claim="")
        self.assertIn("citation_support", [f["rule"] for f in out["vet"]["findings"]])

    def test_retrieval_failure_degrades_to_a_warn(self):
        def boom(question, profile=None):
            raise ConnectionRefusedError("ES down")
        core.unified_retrieve = boom
        out = self.vetted()
        self.assertIn("citation_unavailable", [f["rule"] for f in out["vet"]["findings"]])
        self.assertTrue(out["vet"]["passed"])

    def test_retrieval_never_runs_under_the_generation_lock(self):
        self.vetted()
        self.assertTrue(self.retrieval_saw_gen_lock_held)
        self.assertFalse(any(self.retrieval_saw_gen_lock_held),
                         "GEN_LOCK is the single VRAM slot — never held around retrieval")

    def test_claim_support_scoring(self):
        cites = [{"text": "decel pops above 4000 rpm want a touch more fuel"}]
        score, sources = v.claim_support("decel pops above 4000 rpm", cites)
        self.assertEqual(score, 1.0)
        self.assertTrue(sources)
        score, sources = v.claim_support("valve lash cold clearance", cites)
        self.assertLess(score, v.CITATION_SUPPORT_MIN)
        self.assertEqual(v.claim_support("anything", [])[0], 0.0)


# ----------------------------------------------------------------------------
# Stage 3 — adversarial review, tier pinning
# ----------------------------------------------------------------------------

class AdversarialStageTest(VettingTestCase):
    def test_tier_pinning_map(self):
        self.assertEqual(v.refute_tier("fast"), "deep")
        self.assertEqual(v.refute_tier("smart"), "deep")
        self.assertEqual(v.refute_tier("deep"), "smart")
        self.assertEqual(v.refute_tier("human"), "deep")
        self.assertEqual(v.refute_tier(None), "deep")

    def test_a_fast_tier_proposal_is_refuted_by_the_deep_model(self):
        out = self.vetted(origin_tier="fast")
        adv = out["vet"]["stages"]["adversarial"]
        self.assertEqual(adv["tier"], "deep")
        self.assertEqual(adv["model"], core.TIERS["deep"])
        self.assertEqual(self.llm_calls[0]["model"], core.TIERS["deep"])

    def test_a_deep_tier_proposal_is_refuted_by_the_smart_model(self):
        out = self.vetted(origin_tier="deep")
        adv = out["vet"]["stages"]["adversarial"]
        self.assertEqual(adv["tier"], "smart")
        self.assertEqual(adv["model"], core.TIERS["smart"])
        self.assertNotEqual(adv["model"], core.TIERS["deep"],
                            "a model must never review its own proposal")

    def test_the_fast_model_never_refutes_anything(self):
        for tier in ("fast", "smart", "deep", "human", None, "bogus"):
            _t, model = v.refute_model(tier)
            self.assertNotEqual(model, core.TIERS["fast"],
                                f"{tier} must not be refuted by the 14b")

    def test_the_refuting_model_is_recorded_in_the_report(self):
        out = self.vetted(origin_tier="smart")
        self.assertEqual(out["vet"]["refuted_by"],
                         {"model": core.TIERS["deep"], "tier": "deep",
                          "verdict": "CONCUR"})
        self.assertEqual(out["vet"]["stages"]["adversarial"]["origin_tier"], "smart")
        self.assertEqual(v.list_proposals()[0]["refuted_by"], core.TIERS["deep"])

    def test_the_prompt_asks_for_a_refutation_and_carries_the_guardrail_findings(self):
        self.vetted([ve_change(magnitude=3.0)])   # warn-level VE step
        msgs = self.llm_calls[0]["messages"]
        self.assertIn("REFUTE", msgs[0]["content"])
        self.assertIn("cannot block", msgs[0]["content"])
        self.assertIn("SAFETY GUARDRAILS", msgs[0]["content"])
        self.assertIn("ve_step", msgs[1]["content"])
        self.assertIn("TMax Tuner", msgs[1]["content"])

    def test_object_is_a_loud_warn_not_a_block(self):
        self.llm_reply = "Confounded with the weather.\nVERDICT: OBJECT"
        out = self.vetted()
        adv = out["vet"]["stages"]["adversarial"]
        self.assertEqual(adv["verdict"], "OBJECT")
        finding = next(f for f in out["vet"]["findings"]
                       if f["rule"] == "adversarial_objection")
        self.assertEqual(finding["severity"], "warn")
        self.assertIn(core.TIERS["deep"], finding["message"])
        self.assertEqual(out["state"], v.VETTED, "an objection does not stop approval")
        self.assertEqual(v.transition(out["id"], v.APPROVED)["state"], v.APPROVED)

    def test_verdict_parsing(self):
        self.assertEqual(v.parse_verdict("blah\nVERDICT: CONCUR"), "CONCUR")
        self.assertEqual(v.parse_verdict("verdict: object"), "OBJECT")
        self.assertEqual(v.parse_verdict("VERDICT: CONCUR\nVERDICT: OBJECT"), "OBJECT")
        self.assertIsNone(v.parse_verdict(""))
        self.assertEqual(v.parse_verdict("I have concerns but no verdict line"),
                         "OBJECT", "an unreadable review is not a pass")

    def test_an_unavailable_model_warns_and_does_not_block(self):
        def boom(model, messages, job=None, timeout=900, tier=None):
            raise ConnectionRefusedError("ollama down")
        v._llm_generate = boom
        out = self.vetted()
        adv = out["vet"]["stages"]["adversarial"]
        self.assertEqual(adv["status"], "error")
        self.assertIn("ConnectionRefusedError", adv["error"])
        self.assertIn("adversarial_unavailable",
                      [f["rule"] for f in out["vet"]["findings"]])
        self.assertTrue(out["vet"]["passed"])
        self.assertEqual(out["state"], v.VETTED)

    def test_generation_holds_the_gen_lock(self):
        held = []

        def check(model, messages, job=None, timeout=900, tier=None):
            got = core.GEN_LOCK.acquire(blocking=False)
            held.append(not got)
            if got:
                core.GEN_LOCK.release()
            return "VERDICT: CONCUR"

        v._llm_generate = check
        self.vetted()
        self.assertEqual(held, [True], "generation must hold the single VRAM slot")


# ----------------------------------------------------------------------------
# Progress + cancellation
# ----------------------------------------------------------------------------

class ProgressAndCancelTest(VettingTestCase):
    def test_progress_reports_every_stage(self):
        seen = []
        self.vetted()  # warm-up not needed, but keeps the store realistic
        p = self.make()
        v.vet_proposal(p["id"], progress=seen.append)
        stages = [(e["stage"], e["status"]) for e in seen]
        for stage in ("guardrails", "citations", "adversarial"):
            self.assertIn((stage, "running"), stages)
            self.assertIn((stage, "done"), stages)
        self.assertEqual(stages[-1][0], "report")
        adv = next(e for e in seen if e["stage"] == "adversarial" and e["status"] == "done")
        self.assertEqual(adv["detail"]["model"], core.TIERS["deep"])
        final = seen[-1]["detail"]
        self.assertTrue(final["passed"])
        self.assertEqual(final["state"], v.VETTED)

    def test_a_broken_progress_callback_cannot_kill_the_vet(self):
        def bad(_event):
            raise RuntimeError("SSE writer went away")
        out = v.vet_proposal(self.make()["id"], progress=bad)
        self.assertEqual(out["state"], v.VETTED)

    def test_cancel_sets_the_flag_and_closes_the_live_socket(self):
        """A 70b prompt-eval blocks for minutes between tokens, so the flag
        alone is not enough — the response socket has to be closed too."""
        closed = []

        class FakeResponse:
            def close(self_inner):
                closed.append(True)

        def stubborn(model, messages, job=None, timeout=900, tier=None):
            job.response = FakeResponse()
            self.assertTrue(v.cancel_vet(job.proposal_id))
            self.assertTrue(job.cancel.is_set())
            raise v.VetCancelled("cancelled")

        v._llm_generate = stubborn
        p = self.make()
        r = v.vet_proposal(p["id"])
        self.assertEqual(r["code"], "cancelled")
        self.assertEqual(closed, [True], "cancel must close the live Ollama socket")
        stored = v.load_proposal(p["id"])
        self.assertEqual(stored["state"], v.DRAFT)
        self.assertIsNone(stored["vet"], "a cancelled vet writes no report")

    def test_cancelling_an_idle_proposal_is_a_no_op(self):
        self.assertFalse(v.cancel_vet(self.make()["id"]))
        self.assertFalse(v.vet_running("whatever"))

    def test_a_second_vet_cannot_run_concurrently(self):
        started = []

        def reentrant(model, messages, job=None, timeout=900, tier=None):
            started.append(v.vet_proposal(job.proposal_id))
            return "VERDICT: CONCUR"

        v._llm_generate = reentrant
        p = self.make()
        v.vet_proposal(p["id"])
        self.assertEqual(started[0]["code"], "already_running")

    def test_the_job_registry_is_cleaned_up(self):
        p = self.make()
        v.vet_proposal(p["id"])
        self.assertFalse(v.vet_running(p["id"]))


# ----------------------------------------------------------------------------
# Wiring contract for the HTTP layer / frontend
# ----------------------------------------------------------------------------

class ContractTest(VettingTestCase):
    def test_state_payload_describes_the_machine(self):
        pay = v.state_payload()
        self.assertEqual(pay["states"], list(v.STATES))
        self.assertEqual(pay["transitions"][v.DRAFT], sorted({v.VETTED, v.REJECTED}))
        self.assertEqual(pay["transitions"][v.VALIDATED], [])
        self.assertEqual(pay["active"], [v.APPROVED, v.APPLIED])
        self.assertEqual(pay["tables"], list(guardrails.TABLES))
        self.assertEqual(pay["refute_tier"]["deep"], "smart")

    def test_refusals_carry_an_http_status(self):
        p = self.make()
        for bad in (v.transition(p["id"], v.VETTED),
                    v.transition(p["id"], v.APPROVED),
                    v.transition("nope", v.REJECTED)):
            self.assertIn("error", bad)
            self.assertIn("code", bad)
            self.assertIn(bad["status"], (400, 404, 409))

    def test_the_store_is_json_serialisable_end_to_end(self):
        p = self.vetted()
        v.transition(p["id"], v.APPROVED)
        raw = v.proposal_path(p["id"]).read_text()
        self.assertEqual(json.loads(raw)["state"], v.APPROVED)

    def test_the_module_can_only_ever_write_json_into_the_proposal_store(self):
        """The app must never be able to touch a .tbw: every write here goes
        through core.atomic_write onto a path this module builds itself."""
        import re
        source = (SRC / "vetting.py").read_text()
        for banned in (r"\bopen\(", r"write_bytes", r"write_text", r"\bshutil\b",
                       r"os\.remove", r"unlink", r"tbw[\"']"):
            self.assertIsNone(re.search(banned, source),
                              f"vetting must not use {banned}")
        self.assertTrue(str(v.proposal_path("x")).endswith("x.json"))
        p = self.make()
        self.assertEqual([f.suffix for f in self.props.iterdir()], [".json"])
        self.assertEqual(json.loads(v.proposal_path(p["id"]).read_text())["id"], p["id"])


if __name__ == "__main__":
    unittest.main()
