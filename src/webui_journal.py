#!/usr/bin/env python3
"""Ride & tune journal -> knowledge base, with vetting provenance.

Every entry lands in BOTH retrieval legs: a markdown doc in the corpus (the
keyword leg) and embedded chunks in Elasticsearch (the vector leg).

PROVENANCE RULE (the point of this whole module): an entry linked to a
proposal that reached validated_by_ride is written as vetted knowledge — its
filename carries the `_learned_` marker, which earns the setup boost in
tune_assistant.scored_passages(). Everything else — hand-applied changes,
ad-hoc notes, advice taken from chat without running it through the pipeline —
is written with an explicit UNVETTED header, `unvetted: true` in ES, and a
filename WITHOUT `_learned_`, so it scores boost 1.0. Hand-applied advice must
not be able to launder itself into citable authority just by being written
down. upgrade_entry() is the only path that flips an entry to vetted, and the
proposal state machine (Phase 5) is its only caller.

Stdlib only. ES ingest runs on background threads with short timeouts; a
failed ingest is recorded on the entry and retried, never raised — the corpus
doc is already on disk, so the keyword leg has the entry regardless.
"""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import tune_assistant as ta
import webui_core as core

ENTRY_TYPES = ("validation_ride", "tune_change", "note")
ES_CHUNK_CHARS = getattr(core.hr, "CHUNK_CHARS", 1500) if core.hr else 1500
ES_DIMS = getattr(core.hr, "DIMS", 768) if core.hr else 768

UNVETTED_HEADER = (
    "> **UNVETTED — recorded outside the proposal pipeline.**\n"
    "> This entry is a record of what was done and observed. It has NOT passed\n"
    "> guardrail checks, citation cross-check, or adversarial review, so it is\n"
    "> not authority for any tuning change. Cite it as an observation only.\n"
)


def journal_path(jid):
    return core.JOURNAL_DIR / f"{jid}.json"


def load_entry(jid):
    try:
        return json.loads(journal_path(jid).read_text())
    except (OSError, ValueError):
        return None


def list_entries(limit=50):
    out = []
    for p in sorted(core.JOURNAL_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        meta = {k: e.get(k) for k in
                ("id", "ts", "type", "title", "vetted", "proposal_id", "doc")}
        meta["es_indexed"] = (e.get("es") or {}).get("indexed", False)
        meta["es_error"] = (e.get("es") or {}).get("error")
        meta["summary"] = (e.get("body") or "")[:160]
        out.append(meta)
    return out


# --- corpus doc rendering ----------------------------------------------------

def _fmt_kv(d):
    return [f"- {k.replace('_', ' ')}: {v}" for k, v in d.items()
            if v not in (None, "", [], {})]


def _entry_doc_text(entry, profile):
    """Render an entry as the markdown that actually gets retrieved. The
    UNVETTED banner is part of the retrieved TEXT, not just metadata, so it
    travels into the citation excerpt the model reads."""
    vetted = entry.get("vetted")
    lines = []
    if not vetted:
        lines += [UNVETTED_HEADER, ""]
    lines += [
        f"# {entry['title']}",
        "",
        f"Journal entry ({entry['type'].replace('_', ' ')}) — {entry['ts']}",
        f"Setup: {profile.get('label', profile.get('setup_key', 'unknown'))}",
    ]
    if entry.get("proposal_id"):
        lines.append(f"Linked proposal: {entry['proposal_id']} "
                     f"({'validated by this ride' if vetted else 'not yet validated'})")
    else:
        lines.append("Linked proposal: none — this change did not go through "
                     "the vetting pipeline.")
    for head, key in (("Tune", "tune"), ("Conditions", "conditions"),
                      ("Observations", "observations")):
        rows = _fmt_kv(entry.get(key) or {})
        if rows:
            lines += ["", f"## {head}"] + rows
    if entry.get("body"):
        lines += ["", "## Notes", "", entry["body"].strip()]
    return "\n".join(lines) + "\n"


def _doc_name(entry, profile):
    """Vetted -> `thundermax_learned_<setup>_...`: matches DOC_GLOB *and* the
    `_learned_` boost marker, and carries the setup key so it scores 1.6.
    Unvetted -> `thundermax_journal_...`: still matches DOC_GLOB (so it IS
    retrievable and citable) but has no `_learned_`, so it scores 1.0."""
    setup = profile.get("setup_key", "default")
    slug = ta._slug(entry["title"])
    marker = "learned" if entry.get("vetted") else "journal"
    return f"thundermax_{marker}_{setup}_{entry['id']}_{slug}.md"


def _write_entry_doc(entry, profile):
    """(Re)write the corpus doc, removing the previous one — a vetted upgrade
    changes the filename, and two copies would both be retrievable.

    Mutates `entry["prev_doc"]` so the ES ingest can clear the old chunks too;
    it is cleared only once that ingest succeeds, so an ES outage during an
    upgrade cannot strand a stale UNVETTED copy in the vector index."""
    old = entry.get("doc")
    name = _doc_name(entry, profile)
    (ta.DOCS_DIR / name).write_text(_entry_doc_text(entry, profile))
    if old and old != name:
        try:
            (ta.DOCS_DIR / old).unlink()
        except OSError:
            pass
        stale = set(entry.get("prev_doc") or [])
        stale.add(old)
        entry["prev_doc"] = sorted(stale - {name})
    return name


# --- Elasticsearch single-doc ingest ----------------------------------------
# Composed from hermes_rag's primitives but with short explicit timeouts: its
# _req defaults to 600 s, which would pin a worker thread when ES is wedged.

def _chunk_text(text):
    if core.hr:
        return core.hr.chunk_text(text)
    return [text[i:i + ES_CHUNK_CHARS]
            for i in range(0, len(text), ES_CHUNK_CHARS)] or [text]


def _es_ensure_index(timeout=5):
    try:
        core._http_json(f"{core.ES_URL}/{core.ES_INDEX}", timeout=timeout, method="GET")
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    core._http_json(f"{core.ES_URL}/{core.ES_INDEX}", {
        "mappings": {"properties": {
            "vector": {"type": "dense_vector", "dims": ES_DIMS,
                       "index": True, "similarity": "cosine"},
            "text": {"type": "text"}, "source": {"type": "keyword"},
            "path": {"type": "keyword"}, "chunk": {"type": "integer"},
            "unvetted": {"type": "boolean"}, "entry_id": {"type": "keyword"},
        }}}, timeout=timeout, method="PUT")


def _es_drop_paths(paths, timeout=15):
    """Clear the chunks belonging to these doc paths. Needed because a rename
    (unvetted -> learned) or a shorter edit changes the chunk _ids, which would
    otherwise leave orphaned copies of the OLD text — including a stale UNVETTED
    banner — retrievable forever.

    Deliberately keyed on `path`, not `entry_id`: `brain-knowledge` is a shared
    index created by hermes_rag, whose mapping has no entry_id, so ES typed it
    dynamically as analyzed `text` — a term query against it silently matches
    nothing. `path` is `keyword` in that base mapping, so this works on both a
    pre-existing index and a fresh one.
    """
    paths = [str(p) for p in paths if p]
    if not paths:
        return 0
    res = core._http_json(
        f"{core.ES_URL}/{core.ES_INDEX}/_delete_by_query?refresh=true&conflicts=proceed",
        {"query": {"terms": {"path": paths}}}, timeout=timeout, method="POST")
    return res.get("deleted", 0)


def es_index_entry(entry, profile=None):
    """Embed + index one journal doc. Chunk _ids are deterministic (path:i),
    matching hermes_rag's own scheme so a shared reindex overwrites in place.
    Chunks from the doc's previous name(s) — and from a longer previous version
    of this same doc — are dropped first."""
    path = ta.DOCS_DIR / entry["doc"]
    text = path.read_text()
    _es_ensure_index()
    _es_drop_paths([path] + [ta.DOCS_DIR / n for n in (entry.get("prev_doc") or [])])
    chunks = _chunk_text(text)
    lines = []
    for i, c in enumerate(chunks):
        vec = core._embed(c)
        _id = hashlib.sha1(f"{path}:{i}".encode()).hexdigest()
        lines.append(json.dumps({"index": {"_index": core.ES_INDEX, "_id": _id}}))
        lines.append(json.dumps({
            "text": c, "source": entry["doc"], "path": str(path), "chunk": i,
            "unvetted": not entry.get("vetted"), "entry_id": entry["id"],
            "vector": vec}))
    payload = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(f"{core.ES_URL}/_bulk", data=payload, method="POST",
                                 headers={"Content-Type": "application/x-ndjson"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode())
    if res.get("errors"):
        items = res.get("items", [])
        why = next((i["index"]["error"] for i in items
                    if i.get("index", {}).get("error")), "unknown")
        raise RuntimeError(f"ES bulk rejected chunks: {why}")
    core._http_json(f"{core.ES_URL}/{core.ES_INDEX}/_refresh", timeout=5, method="POST")
    return len(chunks)


def _es_worker(jid):
    entry = load_entry(jid)
    if not entry or not entry.get("doc"):
        return  # retracted entries keep their record but own no doc to index
    ok = False
    try:
        n = es_index_entry(entry)
        es = {"indexed": True, "chunks": n, "error": None,
              "at": datetime.now().isoformat(timespec="seconds")}
        ok = True
    except Exception as e:
        es = {"indexed": False, "chunks": 0,
              "error": f"{e.__class__.__name__}: {e}",
              "at": datetime.now().isoformat(timespec="seconds")}
    with core.STORE_LOCK:
        cur = load_entry(jid)
        if cur:
            cur["es"] = es
            if ok:
                # the stale chunks are gone; stop carrying the old names
                cur["prev_doc"] = []
            core.atomic_write(journal_path(jid), cur)


def start_es_ingest(jid):
    threading.Thread(target=_es_worker, args=(jid,), daemon=True).start()


def retry_pending_es():
    """Re-attempt every entry whose vector-leg ingest failed (ES was down).
    Runs on a timer and on demand. The keyword leg never depended on it."""
    tried = []
    for meta in list_entries(limit=500):
        if not meta.get("es_indexed") and meta.get("doc"):
            tried.append(meta["id"])
            _es_worker(meta["id"])
    return {"retried": tried,
            "pending": [m["id"] for m in list_entries(limit=500)
                        if m.get("doc") and not m.get("es_indexed")]}


def _es_retry_timer():
    while True:
        time.sleep(900)
        try:
            retry_pending_es()
        except Exception:
            pass


def start_es_retry_timer():
    threading.Thread(target=_es_retry_timer, daemon=True).start()


# --- entry lifecycle ---------------------------------------------------------

def create_entry(payload):
    """Create an entry, write its corpus doc, kick off ES ingest.

    `vetted` is NEVER taken from the caller: everything born here is unvetted.
    Only upgrade_entry(), driven by the proposal state machine on a
    validated_by_ride transition, can flip it."""
    etype = payload.get("type", "note")
    if etype not in ENTRY_TYPES:
        return {"error": f"type must be one of {', '.join(ENTRY_TYPES)}"}
    title = (payload.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    prof = ta.load_profile()
    entry = {
        "id": core.new_id(),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "type": etype,
        "title": title,
        "body": (payload.get("body") or "").strip(),
        "tune": payload.get("tune") or {},
        "conditions": payload.get("conditions") or {},
        "observations": payload.get("observations") or {},
        "proposal_id": payload.get("proposal_id") or None,
        "vetted": False,
        "setup_key": prof.get("setup_key"),
        "doc": None,
        "es": {"indexed": False, "chunks": 0, "error": None, "at": None},
    }
    with core.STORE_LOCK:
        entry["doc"] = _write_entry_doc(entry, prof)
        core.atomic_write(journal_path(entry["id"]), entry)
    start_es_ingest(entry["id"])
    return entry


EDITABLE = ("title", "body", "tune", "conditions", "observations", "proposal_id")


def update_entry(jid, patch):
    """Edit the human-authored fields and re-sync both legs. `vetted` is
    deliberately not in EDITABLE — a PATCH must never be able to promote an
    entry to citable authority."""
    prof = ta.load_profile()
    with core.STORE_LOCK:
        entry = load_entry(jid)
        if not entry:
            return {"error": "not found"}
        merged = dict(entry)
        for k in EDITABLE:
            if k in patch:
                merged[k] = patch[k]
        if not (merged.get("title") or "").strip():
            return {"error": "title required"}
        merged["doc"] = _write_entry_doc(merged, prof)
        merged["es"] = {"indexed": False, "chunks": 0, "error": None, "at": None}
        core.atomic_write(journal_path(jid), merged)
    start_es_ingest(jid)
    return merged


def upgrade_entry(jid, proposal_id=None):
    """Promote an entry to vetted knowledge — the ONLY path that sets
    vetted=True. Strips the UNVETTED banner, renames the doc under the
    `_learned_` marker (earning the setup boost), and flips `unvetted` in ES."""
    prof = ta.load_profile()
    with core.STORE_LOCK:
        entry = load_entry(jid)
        if not entry:
            return {"error": "not found"}
        if proposal_id:
            entry["proposal_id"] = proposal_id
        if not entry.get("proposal_id"):
            return {"error": "cannot vet an entry with no linked proposal"}
        entry["vetted"] = True
        entry["doc"] = _write_entry_doc(entry, prof)
        entry["es"] = {"indexed": False, "chunks": 0, "error": None, "at": None}
        core.atomic_write(journal_path(jid), entry)
    start_es_ingest(jid)
    return entry


def delete_entry_docs(jid):
    """Remove an entry's corpus doc + ES chunks WITHOUT deleting the entry
    record. Used when an entry is retracted; the JSON stays as history."""
    entry = load_entry(jid)
    if not entry:
        return {"error": "not found"}
    names = [n for n in ([entry.get("doc")] + (entry.get("prev_doc") or [])) if n]
    for n in names:
        try:
            (ta.DOCS_DIR / n).unlink()
        except OSError:
            pass
    try:
        _es_drop_paths([ta.DOCS_DIR / n for n in names])
        es_err = "retracted"
    except Exception as e:
        # the corpus doc is already gone (keyword leg clean); flag the vector
        # leg so the retry sweep does not quietly leave it retrievable
        es_err = f"retracted, but ES cleanup failed: {e.__class__.__name__}: {e}"
    with core.STORE_LOCK:
        cur = load_entry(jid) or entry
        cur["doc"] = None
        cur["prev_doc"] = []
        cur["retracted"] = True
        cur["es"] = {"indexed": False, "chunks": 0, "error": es_err, "at": None}
        core.atomic_write(journal_path(jid), cur)
    return cur
