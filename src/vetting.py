#!/usr/bin/env python3
"""Proposal store, state machine, and the three-stage vetting pipeline.

A proposal is a *structured* tuning change written in TMax Tuner page language
(guardrails.TABLES), with rpm/tps bands, a signed magnitude, a plain-language
claim, citations, a vet report, attached dyno runs, and an append-only history.
The app NEVER writes to the ECM: an approved proposal is a checklist Joshua
applies by hand in the ThunderMax TMax Tuner software.

WHAT THIS MODULE IS FOR
-----------------------
Making sure a change is safe BEFORE it is typed into TMax Tuner. Three stages:

  1. CODE GUARDRAILS  -- guardrails.check_proposal(). Deterministic, offline,
     and the SOLE hard-block authority. No model can block, and no model can
     clear, a stage-1 block.
  2. CITATION CROSS-CHECK -- core.unified_retrieve() per claim; a claim the
     corpus does not support is marked "unsupported by corpus" (warn).
  3. ADVERSARIAL REVIEW -- an LLM asked to REFUTE the proposal, pinned to a
     DIFFERENT (and never weaker) tier than the one that generated it, with
     the refuting model recorded in the report. Its verdict is advisory: an
     OBJECT is a loud warn, never an auto-block.

STACKING GUARD
--------------
Both stage 1 and the `approved` transition recompute the net signed delta
across every still-active proposal (approved / applied_on_bike) that overlaps
this one's table + rpm/tps band, INCLUDING this proposal's own delta. Without
it, three separately-"safe" +2 deg steps ladder into +6 deg one approval at a
time. guardrails owns the numeric limit; this module only sums the deltas.

STATE MACHINE
-------------
  draft -> vetted -> approved -> applied_on_bike -> validated_by_ride
  (any non-terminal state -> rejected; vetted/approved -> draft on recall or
   on dyno attach/detach, which invalidates the vet report)

Loopholes deliberately closed (adversarial review of the design):
  (a) `vetted` is enterable only by vet_proposal() itself, on a zero-block
      result; a generic transition() call asking for it is refused (409).
  (b) `approved` needs a vet report that EXISTS, matches the current changes
      and dyno set, and has zero blocks. An absent report is not a pass. When
      checks_unverifiable > 0 (per-cell .tbw scaling is still unconfirmed, so
      absolute cell values are unknowable from here) approval also needs the
      explicit acknowledge_unverifiable flag: "I will confirm the absolute
      values in TMax Tuner".
  (c) `changes` are immutable after creation. There is no update/patch path
      for them -- revise_proposal() forks a NEW draft that supersedes the old
      one -- and every read re-verifies changes_hash.
  (d) Attaching or detaching a dyno run invalidates the vet report and forces
      a re-vet.
  (e) `applied_on_bike` requires a confirmation note (Joshua applied it by
      hand; the app never touches the ECM).
  (f) `validated_by_ride` links a journal entry and promotes it to vetted
      knowledge via webui_journal.upgrade_entry().

Stdlib only. Ollama at 127.0.0.1:11434; core.GEN_LOCK is held around
generation ONLY (never around retrieval or embedding).
"""

import hashlib
import json
import re
import threading
import urllib.request
from datetime import datetime

import guardrails
import tune_assistant as ta
import webui_core as core
import webui_journal as journal

# --- states ------------------------------------------------------------------

DRAFT = "draft"
VETTED = "vetted"
APPROVED = "approved"
APPLIED = "applied_on_bike"
VALIDATED = "validated_by_ride"
REJECTED = "rejected"

STATES = (DRAFT, VETTED, APPROVED, APPLIED, VALIDATED, REJECTED)
#: states whose changes are live on the bike (or cleared to go on it) and so
#: count toward the cross-proposal stacking guard
ACTIVE_STATES = (APPROVED, APPLIED)
TERMINAL_STATES = (VALIDATED, REJECTED)

ALLOWED_TRANSITIONS = {
    DRAFT: {VETTED, REJECTED},
    VETTED: {APPROVED, DRAFT, REJECTED},
    APPROVED: {APPLIED, DRAFT, REJECTED},
    APPLIED: {VALIDATED, REJECTED},
    VALIDATED: set(),
    REJECTED: set(),
}
#: (a) only the vet handler may put a proposal here
VET_ONLY_STATES = {VETTED}

# --- stage 2 threshold -------------------------------------------------------
# Fraction of a claim's significant words that must appear in the retrieved
# passages for the claim to count as corpus-supported. Deliberately generous:
# this stage produces warns, never blocks, and a false "unsupported" is cheap.
CITATION_SUPPORT_MIN = 0.35
CITATION_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when",
    "then", "than", "will", "should", "would", "have", "has", "was", "are",
    "its", "it's", "about", "after", "before", "more", "less", "not", "but",
    "add", "per", "get", "gets", "over", "under", "any", "all", "some",
}

# --- stage 3 tier pinning ----------------------------------------------------
# A proposal must never be refuted by the model that wrote it, nor by anything
# weaker than it. fast/smart -> deep (hermes3:70b); deep -> smart
# (qwen2.5-coder:32b). The 14b fast tier NEVER refutes anything.
REFUTE_TIER = {"fast": "deep", "smart": "deep", "deep": "smart"}
DEFAULT_REFUTE_TIER = "deep"
ORIGIN_TIERS = ("fast", "smart", "deep", "human")

ADVERSARIAL_SYSTEM = """\
You are a hostile tuning reviewer for a 2023 Harley-Davidson Low Rider ST
(131ci Milwaukee-Eight, 2-into-1, ThunderMax TBW). Your ONLY job is to REFUTE
the proposal below: find the reason it is wrong, unsupported, unsafe, or
premature. Do not be agreeable, do not restate it approvingly, and do not
suggest improvements unless they are the refutation.

You cannot block anything -- deterministic code already ran the numeric safety
checks and its findings are final. Your value is the argument, not a verdict
you are hoping to be right about.

Consider at least: the engine damage mechanism (detonation, lean-under-load,
heat soak on the rear cylinder), whether the cited evidence actually supports
the claim or is merely adjacent, whether the change is confounded with
something else (weather, fuel, an earlier unvalidated change), and whether the
rider could not tell this change from placebo.

End your reply with exactly one line:
VERDICT: CONCUR
or
VERDICT: OBJECT
CONCUR means you could not find a substantive objection.
"""


class VetCancelled(Exception):
    """Raised inside the pipeline when cancel_vet() fires."""


# ----------------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------------

def _now():
    return datetime.now().isoformat(timespec="seconds")


def proposals_dir():
    """Read core.PROPOSALS_DIR at call time so tests can redirect the store."""
    d = core.PROPOSALS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def proposal_path(pid):
    return proposals_dir() / f"{pid}.json"


def load_proposal(pid):
    try:
        return json.loads(proposal_path(pid).read_text())
    except (OSError, ValueError):
        return None


def _save(proposal):
    proposal["updated"] = _now()
    with core.STORE_LOCK:
        core.atomic_write(proposal_path(proposal["id"]), proposal)
    return proposal


def _err(code, message, status=409, **extra):
    """Uniform refusal shape: the HTTP layer maps `status` straight through."""
    return {"error": message, "code": code, "status": status, **extra}


def _log(proposal, event, note="", actor="joshua", **extra):
    proposal.setdefault("history", []).append(
        {"at": _now(), "actor": actor, "event": event, "note": note or "", **extra})


# --- change normalisation / hashing ------------------------------------------

def _band(value):
    if value is None:
        return None
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError):
        return None
    return [min(lo, hi), max(lo, hi)]


def _unit_of(change):
    """A change's unit, inferred from the table when not stated. Only ever
    used to GROUP deltas for the stacking guard -- the limits live in
    guardrails."""
    unit = change.get("unit")
    if unit:
        return unit
    table = change.get("table", "")
    if table in guardrails.SPARK_TABLES:
        return "deg"
    if table in guardrails.VE_TABLES:
        return "ve_pct"
    if table in guardrails.AFR_TABLES:
        return "afr"
    return "unknown"


def _pair(raw, band_key, lo_key, hi_key):
    """Accept either spelling of a band. guardrails.check_change() reads
    `rpm_band`/`tps_band` 2-tuples, but the web form (and any JSON extracted
    from a chat answer) naturally emits rpm_min/rpm_max. Silently normalising
    to None here would hand the guardrails an unbounded change and let a WOT
    or idle-window check pass on a band it never actually saw — so accept both
    spellings and only return None when neither is present."""
    band = _band(raw.get(band_key))
    if band is not None:
        return band
    lo, hi = raw.get(lo_key), raw.get(hi_key)
    if lo is None and hi is None:
        return None
    return _band([lo if lo is not None else hi, hi if hi is not None else lo])


def _normalise_change(raw):
    ch = {
        "table": (raw.get("table") or "").strip(),
        "cylinder": raw.get("cylinder") or "both",
        "rpm_band": _pair(raw, "rpm_band", "rpm_min", "rpm_max"),
        "tps_band": _pair(raw, "tps_band", "tps_min", "tps_max"),
        "direction": raw.get("direction") or "increase",
        "magnitude": float(raw.get("magnitude") or 0.0),
        "unit": raw.get("unit") or "",
        "target_value": raw.get("target_value"),
        "current_value": raw.get("current_value"),
        "claim": (raw.get("claim") or "").strip(),
    }
    ch["unit"] = ch["unit"] or _unit_of(ch)
    return ch


def changes_hash(changes):
    """Stable fingerprint of the changes array — (c)'s tamper detector."""
    blob = json.dumps(changes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()


def dyno_hash(runs):
    ids = sorted(str(r.get("id", "")) for r in (runs or []))
    return hashlib.sha1(json.dumps(ids).encode()).hexdigest()


def changes_intact(proposal):
    return changes_hash(proposal.get("changes") or []) == proposal.get("changes_hash")


# ----------------------------------------------------------------------------
# Create / list / revise
# ----------------------------------------------------------------------------

def create_proposal(payload):
    """Create a draft proposal. `changes` is frozen here and forever after:
    there is no code path in this module that edits it (loophole c)."""
    title = (payload.get("title") or "").strip()
    if not title:
        return _err("title_required", "title required", 400)
    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        return _err("changes_required",
                    "changes must be a non-empty list of structured changes", 400)
    changes = []
    for i, raw in enumerate(raw_changes):
        if not isinstance(raw, dict):
            return _err("bad_change", f"change {i} is not an object", 400)
        if not (raw.get("table") or "").strip():
            return _err("bad_change", f"change {i} needs a table name "
                                      "(TMax Tuner page language)", 400)
        changes.append(_normalise_change(raw))
    origin_tier = payload.get("origin_tier") or "human"
    if origin_tier not in ORIGIN_TIERS:
        origin_tier = "human"
    prof = ta.load_profile()
    proposal = {
        "id": core.new_id(),
        "created": _now(),
        "updated": _now(),
        "state": DRAFT,
        "title": title,
        "claim": (payload.get("claim") or "").strip(),
        "changes": changes,
        "changes_hash": changes_hash(changes),
        "citations": payload.get("citations") or [],
        "origin_tier": origin_tier,
        "origin_model": payload.get("origin_model"),
        "source_message_id": payload.get("source_message_id"),
        "setup_key": prof.get("setup_key"),
        "dyno_runs": [],
        "dyno_hash": dyno_hash([]),
        "vet": None,
        "acknowledgment": None,
        "applied": None,
        "validation": None,
        "supersedes": payload.get("supersedes"),
        "superseded_by": None,
        "history": [],
    }
    _log(proposal, "created", note=title, actor=payload.get("actor", "joshua"))
    return _save(proposal)


def revise_proposal(pid, payload):
    """(c) The ONLY way to 'edit' changes: fork a new draft that supersedes the
    original. The original is never mutated beyond a superseded_by pointer."""
    original = load_proposal(pid)
    if not original:
        return _err("not_found", "proposal not found", 404)
    body = dict(payload or {})
    body.setdefault("title", original["title"])
    body.setdefault("claim", original["claim"])
    body.setdefault("changes", original["changes"])
    body.setdefault("citations", original["citations"])
    body.setdefault("origin_tier", original["origin_tier"])
    body["supersedes"] = pid
    fresh = create_proposal(body)
    if fresh.get("error"):
        return fresh
    cur = load_proposal(pid)
    if cur and not cur.get("superseded_by"):
        cur["superseded_by"] = fresh["id"]
        _log(cur, "superseded", note=f"revised as {fresh['id']}")
        _save(cur)
    return fresh


def list_proposals(limit=100, state=None):
    out = []
    for p in sorted(proposals_dir().glob("*.json"), reverse=True):
        try:
            pr = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if state and pr.get("state") != state:
            continue
        vet = pr.get("vet") or {}
        out.append({
            "id": pr.get("id"), "created": pr.get("created"),
            "updated": pr.get("updated"), "state": pr.get("state"),
            "title": pr.get("title"), "claim": pr.get("claim"),
            "changes": len(pr.get("changes") or []),
            "origin_tier": pr.get("origin_tier"),
            "dyno_runs": len(pr.get("dyno_runs") or []),
            "vetted_at": vet.get("at"),
            "blocks": vet.get("blocks"), "warns": vet.get("warns"),
            "checks_unverifiable": vet.get("checks_unverifiable"),
            "refuted_by": ((vet.get("stages") or {}).get("adversarial") or {}).get("model"),
            "supersedes": pr.get("supersedes"),
            "superseded_by": pr.get("superseded_by"),
        })
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------
# Cross-proposal stacking guard
# ----------------------------------------------------------------------------

def _bands_overlap(a, b):
    """Missing band == covers everything (a whole-table change stacks with
    every band in that table)."""
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def _cylinders_overlap(a, b):
    a = a or "both"
    b = b or "both"
    return a == b or "both" in (a, b)


def changes_overlap(a, b):
    return (a.get("table") == b.get("table")
            and _cylinders_overlap(a.get("cylinder"), b.get("cylinder"))
            and _bands_overlap(_band(a.get("rpm_band")), _band(b.get("rpm_band")))
            and _bands_overlap(_band(a.get("tps_band")), _band(b.get("tps_band"))))


def overlap_net(proposal):
    """Net signed delta per unit across this proposal PLUS every still-active
    (approved / applied_on_bike) proposal overlapping the same table+bands.

    Own delta is included on purpose: the guard exists to catch "+2 today,
    +2 tomorrow" laddering, which is only visible in the sum.

    Returns {"net": {unit: signed_total}, "overlaps": [...]}.
    """
    mine = proposal.get("changes") or []
    net = {}
    for ch in mine:
        net[_unit_of(ch)] = net.get(_unit_of(ch), 0.0) + guardrails._signed(ch)
    overlaps = []
    for meta in list_proposals(limit=1000):
        if meta["id"] == proposal.get("id") or meta["state"] not in ACTIVE_STATES:
            continue
        other = load_proposal(meta["id"])
        if not other:
            continue
        for och in other.get("changes") or []:
            if not any(changes_overlap(ch, och) for ch in mine):
                continue
            unit = _unit_of(och)
            delta = guardrails._signed(och)
            net[unit] = net.get(unit, 0.0) + delta
            overlaps.append({"proposal_id": other["id"], "state": other["state"],
                             "title": other.get("title"), "table": och.get("table"),
                             "rpm_band": och.get("rpm_band"),
                             "tps_band": och.get("tps_band"),
                             "unit": unit, "delta": delta})
    return {"net": {k: round(v, 4) for k, v in net.items()}, "overlaps": overlaps}


# ----------------------------------------------------------------------------
# Vetting stage 1 — code guardrails (sole hard-block authority)
# ----------------------------------------------------------------------------

def _stage_guardrails(proposal):
    stack = overlap_net(proposal)
    # only feed the stacking net to guardrails when something actually overlaps;
    # a lone proposal is already covered by the per-change step checks
    net = stack["net"] if stack["overlaps"] else None
    result = guardrails.check_proposal(proposal.get("changes") or [], overlapping_net=net)
    findings = [{**f, "stage": "guardrails"} for f in result["findings"]]
    if not changes_intact(proposal):
        findings.append({"rule": "changes_tampered", "severity": "block",
                         "stage": "guardrails",
                         "message": "stored changes no longer match changes_hash — "
                                    "the proposal was edited outside the pipeline; "
                                    "create a new proposal"})
    blocks = sum(1 for f in findings if f["severity"] == "block")
    return {
        "status": "done",
        "findings": findings,
        "blocks": blocks,
        "warns": len(findings) - blocks,
        "checks_unverifiable": result["checks_unverifiable"],
        "passed": blocks == 0,
        "stacking": stack,
    }


# ----------------------------------------------------------------------------
# Vetting stage 2 — citation cross-check
# ----------------------------------------------------------------------------

def _claim_words(claim):
    words = re.findall(r"[a-z0-9.%+-]{3,}", (claim or "").lower())
    return [w for w in words if w not in CITATION_STOPWORDS]


def claim_support(claim, citations):
    """Fraction of the claim's significant words that appear in the retrieved
    passages, plus the sources that carried them."""
    words = _claim_words(claim)
    if not words:
        return 0.0, []
    blob = " ".join((c.get("text") or "").lower() for c in citations)
    if not blob:
        return 0.0, []
    hit_sources = []
    matched = 0
    for w in set(words):
        if w in blob:
            matched += 1
    for c in citations:
        text = (c.get("text") or "").lower()
        if any(w in text for w in set(words)):
            hit_sources.append({"n": c.get("n"), "source": c.get("source"),
                                "kind": c.get("kind"), "retriever": c.get("retriever")})
    return round(matched / len(set(words)), 3), hit_sources[:5]


def _proposal_claims(proposal):
    claims = []
    if proposal.get("claim"):
        claims.append({"text": proposal["claim"], "change_idx": None})
    for i, ch in enumerate(proposal.get("changes") or []):
        if ch.get("claim"):
            claims.append({"text": ch["claim"], "change_idx": i})
    return claims


def _stage_citations(proposal, profile=None):
    claims = _proposal_claims(proposal)
    rows, findings, degraded, all_cites = [], [], None, []
    for c in claims:
        try:
            cites, _ctx, deg = core.unified_retrieve(c["text"], profile=profile)
        except Exception as e:
            rows.append({"claim": c["text"], "change_idx": c["change_idx"],
                         "supported": False, "support": 0.0, "sources": [],
                         "error": f"{e.__class__.__name__}: {e}"})
            findings.append({"rule": "citation_unavailable", "severity": "warn",
                             "stage": "citations", "change_idx": c["change_idx"],
                             "message": f"retrieval failed for a claim ({e.__class__.__name__}) "
                                        "— corpus support unknown"})
            continue
        degraded = degraded or deg
        all_cites.extend(cites)
        score, sources = claim_support(c["text"], cites)
        supported = score >= CITATION_SUPPORT_MIN and bool(sources)
        rows.append({"claim": c["text"], "change_idx": c["change_idx"],
                     "supported": supported, "support": score, "sources": sources,
                     "error": None})
        if not supported:
            findings.append({"rule": "citation_support", "severity": "warn",
                             "stage": "citations", "change_idx": c["change_idx"],
                             "message": f"claim unsupported by corpus (support {score:.2f} "
                                        f"< {CITATION_SUPPORT_MIN:.2f}): "
                                        f"\"{c['text'][:120]}\""})
    if not claims:
        findings.append({"rule": "citation_support", "severity": "warn",
                         "stage": "citations", "change_idx": None,
                         "message": "proposal states no claim — nothing to cross-check "
                                    "against the corpus"})
    # dedupe the citations we hand to stage 3
    seen, ctx_cites = set(), []
    for c in all_cites:
        key = (c.get("source"), (c.get("text") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        ctx_cites.append(c)
    return {"status": "done", "claims": rows, "findings": findings,
            "unsupported": sum(1 for r in rows if not r["supported"]),
            "degraded": degraded, "citations": ctx_cites[:12]}


# ----------------------------------------------------------------------------
# Vetting stage 3 — adversarial review (advisory only)
# ----------------------------------------------------------------------------

def refute_tier(origin_tier):
    """(mandatory tier pinning) fast/smart -> deep, deep -> smart, unknown ->
    deep. Never fast: the 14b does not get to sign off on the 70b's work."""
    tier = REFUTE_TIER.get(origin_tier, DEFAULT_REFUTE_TIER)
    return "deep" if tier == "fast" else tier


def refute_model(origin_tier):
    tier = refute_tier(origin_tier)
    model = core.TIERS.get(tier) or core.TIERS.get(DEFAULT_REFUTE_TIER)
    # belt and braces: if the tier table is ever misconfigured so that the
    # refuting model IS the fast model, escalate rather than review weakly.
    if model == core.TIERS.get("fast"):
        tier = DEFAULT_REFUTE_TIER
        model = core.TIERS.get(DEFAULT_REFUTE_TIER)
    return tier, model


def _render_changes(proposal):
    lines = []
    for i, ch in enumerate(proposal.get("changes") or []):
        sign = "+" if ch.get("direction") != "decrease" else "-"
        lines.append(
            f"{i + 1}. {ch['table']} ({ch.get('cylinder', 'both')}): "
            f"{sign}{abs(ch.get('magnitude', 0)):g} {ch.get('unit')} @ "
            f"rpm {ch.get('rpm_band')} tps {ch.get('tps_band')}; "
            f"current={ch.get('current_value')} target={ch.get('target_value')}"
            + (f"\n   claim: {ch['claim']}" if ch.get("claim") else ""))
    return "\n".join(lines)


def _adversarial_prompt(proposal, guard, cites):
    ctx = core._build_context(cites) if cites else "(no corpus excerpts retrieved)"
    guard_lines = "\n".join(
        f"- [{f['severity']}] {f['rule']}: {f['message']}" for f in guard["findings"]
    ) or "- (no findings)"
    user = f"""PROPOSAL: {proposal.get('title')}

RIDER'S CLAIM: {proposal.get('claim') or '(none stated)'}

CHANGES (to be applied by hand in TMax Tuner — nothing writes to the ECM):
{_render_changes(proposal)}

DETERMINISTIC GUARDRAIL FINDINGS (final; you cannot add to or remove from these):
{guard_lines}
unverifiable absolute-value checks: {guard['checks_unverifiable']}

ATTACHED DYNO RUNS: {len(proposal.get('dyno_runs') or [])}

CORPUS EXCERPTS:
{ctx}

Refute this proposal."""
    return [{"role": "system", "content": ADVERSARIAL_SYSTEM + core.SAFE_RULES},
            {"role": "user", "content": user}]


def _llm_generate(model, messages, job=None, timeout=900, tier=None):
    """Streamed Ollama call. Isolated so tests can stub it, and so the live
    response object is reachable by cancel_vet().

    `num_ctx` IS A SAFETY PARAMETER HERE, not a tuning knob. This tower runs
    OLLAMA_CONTEXT_LENGTH=4096, and a request that omits `options.num_ctx` is
    truncated server-side with no error of any kind — the model simply answers
    from whatever survived the cut. The adversarial prompt carries the whole
    proposal, every guardrail finding and the citations, which routinely
    exceeds 4096 tokens.

    A refuting model that cannot see the change it is meant to refute is
    strictly more likely to return CONCUR, and CONCUR is the verdict that lets
    a proposal move toward `approved`. So the failure mode is not a worse
    review, it is a FALSE PASS on the one gate that exists to catch a bad
    change — silently, with the report still looking complete.

    The window is sized from the same TIER_CTX table the chat path uses, so
    the reviewer and the assistant can never disagree about how much context a
    tier affords.
    """
    ctx = core.TIER_CTX.get(tier, core.DEFAULT_CTX)
    req = urllib.request.Request(
        f"{core.OLLAMA}/api/chat",
        data=json.dumps({"model": model, "messages": messages, "stream": True,
                         "options": {"num_ctx": ctx}}).encode(),
        headers={"Content-Type": "application/json"})
    out = []
    resp = urllib.request.urlopen(req, timeout=timeout)
    if job is not None:
        job.response = resp
    try:
        for line in resp:
            if job is not None and job.cancel.is_set():
                raise VetCancelled("cancelled")
            part = json.loads(line)
            tok = part.get("message", {}).get("content", "")
            if tok:
                out.append(tok)
            if part.get("done"):
                break
    finally:
        if job is not None:
            job.response = None
        try:
            resp.close()
        except Exception:
            pass
    return "".join(out)


VERDICT_RE = re.compile(r"VERDICT:\s*(CONCUR|OBJECT)", re.I)


def parse_verdict(text):
    """Last VERDICT line wins. Unparseable but non-empty output is treated as
    OBJECT — a review nobody can read is not a pass."""
    matches = VERDICT_RE.findall(text or "")
    if matches:
        return matches[-1].upper()
    return "OBJECT" if (text or "").strip() else None


def _stage_adversarial(proposal, guard, cites, job=None):
    tier, model = refute_model(proposal.get("origin_tier"))
    out = {"status": "done", "tier": tier, "model": model,
           "origin_tier": proposal.get("origin_tier"),
           "origin_model": proposal.get("origin_model"),
           "verdict": None, "text": "", "error": None, "findings": []}
    msgs = _adversarial_prompt(proposal, guard, cites)
    core.GEN_LOCK.acquire()          # generation only — never around retrieval
    try:
        if job is not None and job.cancel.is_set():
            raise VetCancelled("cancelled")
        out["text"] = _llm_generate(model, msgs, job=job, tier=tier)
    except VetCancelled:
        raise
    except Exception as e:
        if job is not None and job.cancel.is_set():
            raise VetCancelled("cancelled")
        out["status"] = "error"
        out["error"] = f"{e.__class__.__name__}: {e}"
        out["findings"].append({
            "rule": "adversarial_unavailable", "severity": "warn",
            "stage": "adversarial", "change_idx": None,
            "message": f"adversarial review did not run ({out['error']}) — "
                       "no second opinion on this proposal"})
        return out
    finally:
        core.GEN_LOCK.release()
    out["verdict"] = parse_verdict(out["text"])
    if out["verdict"] == "OBJECT":
        out["findings"].append({
            "rule": "adversarial_objection", "severity": "warn",
            "stage": "adversarial", "change_idx": None,
            "message": f"{model} OBJECTS to this proposal — read the refutation "
                       "before approving"})
    elif out["verdict"] is None:
        out["status"] = "error"
        out["error"] = "empty response"
        out["findings"].append({
            "rule": "adversarial_unavailable", "severity": "warn",
            "stage": "adversarial", "change_idx": None,
            "message": "adversarial review returned nothing — no second opinion"})
    return out


# ----------------------------------------------------------------------------
# Cancellation (mirrors core.cancel_chat: flag AND close the live socket, since
# a 70b prompt-eval blocks for minutes with no tokens flowing)
# ----------------------------------------------------------------------------

class VetJob:
    def __init__(self, pid):
        self.proposal_id = pid
        self.cancel = threading.Event()
        self.response = None


_VET_JOBS = {}
_VET_JOBS_LOCK = threading.Lock()


def cancel_vet(proposal_id):
    with _VET_JOBS_LOCK:
        job = _VET_JOBS.get(proposal_id)
    if not job:
        return False
    job.cancel.set()
    if job.response is not None:
        try:
            job.response.close()
        except Exception:
            pass
    return True


def vet_running(proposal_id):
    with _VET_JOBS_LOCK:
        return proposal_id in _VET_JOBS


# ----------------------------------------------------------------------------
# The pipeline
# ----------------------------------------------------------------------------

def _progress(cb, stage, status, detail=None):
    if not cb:
        return
    try:
        cb({"stage": stage, "status": status, "detail": detail})
    except Exception:
        pass  # a broken SSE writer must never take the vet down


def vet_proposal(proposal_id, progress=None, profile=None):
    """Run all three stages and store the report on the proposal.

    Only stage 1 can block. A zero-block result is the ONLY way a proposal
    enters `vetted` (loophole a). Blocks leave it in `draft`.
    """
    proposal = load_proposal(proposal_id)
    if not proposal:
        return _err("not_found", "proposal not found", 404)
    if proposal["state"] not in (DRAFT, VETTED):
        return _err("bad_state",
                    f"cannot vet a proposal in state '{proposal['state']}' — "
                    "revise it into a new draft instead")
    with _VET_JOBS_LOCK:
        if proposal_id in _VET_JOBS:
            return _err("already_running", "a vet is already running for this proposal")
        job = VetJob(proposal_id)
        _VET_JOBS[proposal_id] = job

    try:
        _progress(progress, "guardrails", "running")
        guard = _stage_guardrails(proposal)
        if job.cancel.is_set():
            raise VetCancelled("cancelled")
        _progress(progress, "guardrails", "done",
                  {"blocks": guard["blocks"], "warns": guard["warns"],
                   "checks_unverifiable": guard["checks_unverifiable"],
                   "overlaps": len(guard["stacking"]["overlaps"])})

        _progress(progress, "citations", "running")
        cites = _stage_citations(proposal, profile=profile)
        if job.cancel.is_set():
            raise VetCancelled("cancelled")
        _progress(progress, "citations", "done",
                  {"claims": len(cites["claims"]), "unsupported": cites["unsupported"],
                   "degraded": cites["degraded"]})

        tier, model = refute_model(proposal.get("origin_tier"))
        _progress(progress, "adversarial", "running", {"tier": tier, "model": model})
        adv = _stage_adversarial(proposal, guard, cites["citations"], job=job)
        _progress(progress, "adversarial", "done",
                  {"verdict": adv["verdict"], "model": adv["model"],
                   "tier": adv["tier"], "error": adv["error"]})
    except VetCancelled:
        with _VET_JOBS_LOCK:
            _VET_JOBS.pop(proposal_id, None)
        return _err("cancelled", "vetting cancelled", 499)
    finally:
        with _VET_JOBS_LOCK:
            _VET_JOBS.pop(proposal_id, None)

    findings = guard["findings"] + cites["findings"] + adv["findings"]
    blocks = sum(1 for f in findings if f["severity"] == "block")
    # invariant, not decoration: only stage 1 may ever produce a block
    assert blocks == guard["blocks"], "only the guardrail stage may hard-block"
    report = {
        "at": _now(),
        "proposal_id": proposal_id,
        "changes_hash": proposal["changes_hash"],
        "dyno_hash": proposal["dyno_hash"],
        "stages": {"guardrails": guard, "citations": cites, "adversarial": adv},
        "findings": findings,
        "blocks": blocks,
        "warns": len(findings) - blocks,
        "checks_unverifiable": guard["checks_unverifiable"],
        "stacking": guard["stacking"],
        "refuted_by": {"model": adv["model"], "tier": adv["tier"],
                       "verdict": adv["verdict"]},
        "passed": blocks == 0,
    }
    fresh = load_proposal(proposal_id) or proposal
    fresh["vet"] = report
    _log(fresh, "vetted", note=f"{blocks} block(s), {report['warns']} warn(s)",
         blocks=blocks, warns=report["warns"], verdict=adv["verdict"],
         refuted_by=adv["model"])
    if report["passed"]:
        fresh["state"] = VETTED
        _log(fresh, "state", **{"from": DRAFT, "to": VETTED,
                                "note": "guardrails clean"})
    else:
        # a failed re-vet must not leave a stale `vetted` standing
        if fresh["state"] == VETTED:
            _log(fresh, "state", **{"from": VETTED, "to": DRAFT,
                                    "note": "re-vet found blocks"})
        fresh["state"] = DRAFT
    _save(fresh)
    _progress(progress, "report", "done",
              {"passed": report["passed"], "blocks": blocks,
               "warns": report["warns"], "state": fresh["state"]})
    return fresh


# ----------------------------------------------------------------------------
# Dyno runs — (d) attach/detach invalidates the vet report
# ----------------------------------------------------------------------------

def _invalidate_vet(proposal, reason):
    had = proposal.get("vet") is not None
    proposal["vet"] = None
    proposal["acknowledgment"] = None
    if proposal["state"] in (VETTED, APPROVED):
        _log(proposal, "state", **{"from": proposal["state"], "to": DRAFT,
                                   "note": reason})
        proposal["state"] = DRAFT
    if had:
        _log(proposal, "vet_invalidated", note=reason)
    return proposal


def attach_dyno_run(proposal_id, run):
    """Attach a dyno run. Invalidates any vet report — new evidence means the
    refutation and the citation check were argued against a different case."""
    proposal = load_proposal(proposal_id)
    if not proposal:
        return _err("not_found", "proposal not found", 404)
    if proposal["state"] in (APPLIED, VALIDATED, REJECTED):
        return _err("bad_state",
                    f"cannot change the evidence of a proposal in state "
                    f"'{proposal['state']}' — record it in the journal, or "
                    "revise into a new proposal")
    if not isinstance(run, dict):
        return _err("bad_run", "dyno run must be an object", 400)
    entry = {"id": str(run.get("id") or core.new_id()),
             "label": run.get("label") or "",
             "source": run.get("source") or "",
             "attached_at": _now(),
             "data": run.get("data") or {}}
    if any(r["id"] == entry["id"] for r in proposal["dyno_runs"]):
        return _err("duplicate_run", f"dyno run {entry['id']} already attached")
    proposal["dyno_runs"].append(entry)
    proposal["dyno_hash"] = dyno_hash(proposal["dyno_runs"])
    _log(proposal, "dyno_attached", note=entry["id"])
    _invalidate_vet(proposal, f"dyno run {entry['id']} attached — re-vet required")
    return _save(proposal)


def detach_dyno_run(proposal_id, run_id):
    proposal = load_proposal(proposal_id)
    if not proposal:
        return _err("not_found", "proposal not found", 404)
    if proposal["state"] in (APPLIED, VALIDATED, REJECTED):
        return _err("bad_state",
                    f"cannot change the evidence of a proposal in state "
                    f"'{proposal['state']}'")
    keep = [r for r in proposal["dyno_runs"] if r["id"] != str(run_id)]
    if len(keep) == len(proposal["dyno_runs"]):
        return _err("not_found", f"dyno run {run_id} is not attached", 404)
    proposal["dyno_runs"] = keep
    proposal["dyno_hash"] = dyno_hash(keep)
    _log(proposal, "dyno_detached", note=str(run_id))
    _invalidate_vet(proposal, f"dyno run {run_id} detached — re-vet required")
    return _save(proposal)


# ----------------------------------------------------------------------------
# State machine
# ----------------------------------------------------------------------------

def _check_approval(proposal, acknowledge_unverifiable):
    """(b) + the stacking guard, recomputed at approval time."""
    vet = proposal.get("vet")
    if not vet:
        return _err("vet_required",
                    "approval requires a vet report — an absent report is not a pass; "
                    "run the vetting pipeline first")
    if not changes_intact(proposal):
        return _err("changes_tampered",
                    "stored changes no longer match changes_hash — create a new proposal")
    if vet.get("changes_hash") != proposal["changes_hash"]:
        return _err("vet_stale", "the vet report was written against different changes — re-vet")
    if vet.get("dyno_hash") != proposal["dyno_hash"]:
        return _err("vet_stale",
                    "the attached dyno runs changed after vetting — re-vet")
    if vet.get("blocks") or not vet.get("passed"):
        return _err("vet_blocked",
                    f"vet report has {vet.get('blocks')} hard block(s) — "
                    "guardrail blocks cannot be approved away")
    # stacking guard again: another proposal may have been approved since
    stack = overlap_net(proposal)
    if stack["overlaps"]:
        recheck = guardrails.check_proposal(proposal["changes"], overlapping_net=stack["net"])
        if recheck["blocks"]:
            msgs = [f["message"] for f in recheck["findings"] if f["severity"] == "block"]
            return _err("stacking_blocked",
                        "approval would stack with still-active proposals: "
                        + "; ".join(msgs), 409,
                        stacking=stack, findings=recheck["findings"])
    if vet.get("checks_unverifiable", 0) > 0 and not acknowledge_unverifiable:
        return _err("acknowledgment_required",
                    f"{vet['checks_unverifiable']} check(s) could not be verified from "
                    "here (per-cell .tbw scaling is unconfirmed, so absolute cell "
                    "values are unknowable) — approve again with "
                    "acknowledge_unverifiable to confirm you will read the absolute "
                    "values in TMax Tuner before applying",
                    409, checks_unverifiable=vet["checks_unverifiable"])
    return None


def transition(proposal_id, to_state, note="", actor="joshua", entry_id=None,
               acknowledge_unverifiable=False, _via_vet=False):
    """The single public state-change entry point.

    Refusals return {"error", "code", "status"} — status is the HTTP code the
    server should send (409 for a refused transition, 404/400 otherwise).
    """
    if to_state not in STATES:
        return _err("bad_state", f"unknown state '{to_state}'", 400)
    proposal = load_proposal(proposal_id)
    if not proposal:
        return _err("not_found", "proposal not found", 404)
    frm = proposal["state"]
    if to_state == frm:
        return _err("no_op", f"proposal is already {frm}")
    if to_state not in ALLOWED_TRANSITIONS.get(frm, set()):
        return _err("bad_transition",
                    f"{frm} -> {to_state} is not a legal transition"
                    + (f" (terminal state)" if frm in TERMINAL_STATES else ""))
    if to_state in VET_ONLY_STATES and not _via_vet:
        # (a) the only door into `vetted` is a zero-block vet report
        return _err("vet_only",
                    "'vetted' is entered only by the vetting pipeline on a "
                    "zero-block result — POST the vet endpoint instead")

    if to_state == APPROVED:
        refusal = _check_approval(proposal, acknowledge_unverifiable)
        if refusal:
            return refusal
        if proposal["vet"].get("checks_unverifiable", 0) > 0:
            proposal["acknowledgment"] = {
                "unverifiable_ack": True, "at": _now(), "actor": actor,
                "checks_unverifiable": proposal["vet"]["checks_unverifiable"],
                "text": "I will confirm the absolute values in TMax Tuner "
                        "before applying."}

    elif to_state == APPLIED:
        # (e) the app never touches the ECM: this records that Joshua typed the
        # change into TMax Tuner himself
        if not (note or "").strip():
            return _err("note_required",
                        "applied_on_bike requires a confirmation note describing what "
                        "you actually entered in TMax Tuner (the app never writes to "
                        "the ECM)", 400)
        proposal["applied"] = {"at": _now(), "actor": actor, "note": note.strip()}

    elif to_state == VALIDATED:
        # (f) promote the linked journal entry to vetted knowledge
        if not entry_id:
            return _err("entry_required",
                        "validated_by_ride requires the journal entry id of the "
                        "validation ride", 400)
        entry = journal.load_entry(entry_id)
        if not entry:
            return _err("not_found", f"journal entry {entry_id} not found", 404)
        upgraded = journal.upgrade_entry(entry_id, proposal_id)
        if isinstance(upgraded, dict) and upgraded.get("error"):
            return _err("upgrade_failed",
                        f"could not promote journal entry: {upgraded['error']}")
        proposal["validation"] = {"at": _now(), "actor": actor, "entry_id": entry_id,
                                  "note": (note or "").strip(),
                                  "entry_doc": upgraded.get("doc")}

    elif to_state == DRAFT:
        _invalidate_vet(proposal, note or "recalled to draft")
        proposal["state"] = DRAFT   # _invalidate_vet already logged the move
        _log(proposal, "state", **{"from": frm, "to": DRAFT, "note": note or ""})
        return _save(proposal)

    elif to_state == REJECTED:
        proposal["rejected"] = {"at": _now(), "actor": actor,
                                "note": (note or "").strip()}

    proposal["state"] = to_state
    _log(proposal, "state", actor=actor, **{"from": frm, "to": to_state,
                                            "note": (note or "").strip()})
    return _save(proposal)


def approve(proposal_id, note="", actor="joshua", acknowledge_unverifiable=False):
    return transition(proposal_id, APPROVED, note=note, actor=actor,
                      acknowledge_unverifiable=acknowledge_unverifiable)


def mark_applied(proposal_id, note, actor="joshua"):
    return transition(proposal_id, APPLIED, note=note, actor=actor)


def mark_validated(proposal_id, entry_id, note="", actor="joshua"):
    return transition(proposal_id, VALIDATED, note=note, actor=actor,
                      entry_id=entry_id)


def reject(proposal_id, note="", actor="joshua"):
    return transition(proposal_id, REJECTED, note=note, actor=actor)


def state_payload():
    """What the frontend needs to render the state machine without hardcoding
    it (and without ever offering the `vetted` button)."""
    return {"states": list(STATES),
            "transitions": {s: sorted(t) for s, t in ALLOWED_TRANSITIONS.items()},
            "vet_only": sorted(VET_ONLY_STATES),
            "active": list(ACTIVE_STATES),
            "terminal": list(TERMINAL_STATES),
            "tables": list(guardrails.TABLES),
            "refute_tier": dict(REFUTE_TIER),
            "citation_support_min": CITATION_SUPPORT_MIN}
