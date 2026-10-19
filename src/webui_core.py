#!/usr/bin/env python3
"""Core services for the TMax Command Center web UI (webui_server.py).

Holds everything that isn't HTTP plumbing: health probes, the unified
retrieval layer (ES vector + corpus keyword), resumable chat jobs, session
persistence, and the tune index/cache. Stdlib only.

Concurrency model:
- GEN_LOCK: single slot around all Ollama *generation* (the 70b and the qwen
  tiers contend for 16 GB VRAM). classify()/embeddings run OUTSIDE it.
- STORE_LOCK: around every JSON read-modify-write.
- NAS I/O only ever happens on worker threads with join-timeouts; requests
  are served from local caches.
"""

import json
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))
HERMES = Path.home() / "hermes-rag"
sys.path.insert(0, str(HERMES))

import tune_assistant as ta  # noqa: E402
import guardrails  # noqa: E402

# hermes_rag is imported for its CONSTANTS (ES endpoint, index name, tiers);
# we re-implement its two HTTP calls with short explicit timeouts rather than
# calling retrieve() (whose 600 s default timeout could hang a chat request).
try:
    import hermes_rag as hr
    ES_URL, ES_INDEX, EMBED_MODEL = hr.ES, hr.INDEX, hr.EMBED_MODEL
    TIERS = dict(hr.MODELS)
except Exception:  # hermes-rag missing: vector leg simply stays degraded
    hr = None
    ES_URL, ES_INDEX, EMBED_MODEL = "http://127.0.0.1:9200", "brain-knowledge", "nomic-embed-text"
    TIERS = {"fast": ta.FAST_MODEL, "smart": "qwen2.5-coder:32b", "deep": ta.BIG_MODEL}

OLLAMA = ta.OLLAMA_URL
DATA = ROOT / "data"
SESSIONS_DIR = DATA / "sessions"
TUNE_CACHE = DATA / "tune_cache"
DIFF_CACHE = DATA / "diff_cache"
JOURNAL_DIR = DATA / "journal"
PROPOSALS_DIR = DATA / "proposals"
TUNELOG_DIR = HERMES / "tune-log"
# The NAS share layout moved (2026-07-13: THROTTLE LOGIC no longer at its
# documented path) — override with TMAX_TUNES_DIR until the new home is fixed.
NAS_TUNES = Path(os.environ.get("TMAX_TUNES_DIR",
                                "/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC"))
MAX_CONTEXT = ta.MAX_CONTEXT_CHARS

GEN_LOCK = threading.Semaphore(1)
STORE_LOCK = threading.Lock()
NAS_LOCK = threading.Lock()


def ensure_dirs():
    for d in (DATA, SESSIONS_DIR, TUNE_CACHE, DIFF_CACHE, JOURNAL_DIR, PROPOSALS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def docs_dir_is_local():
    """The plan's NAS-isolation guarantee for the keyword leg holds only
    because DOCS_DIR resolved to the local corpus at import. If it fell back
    to the NAS (empty-corpus edge case), the keyword leg must be disabled
    rather than putting CIFS in the request path."""
    return not str(ta.DOCS_DIR).startswith("/mnt/")


KEYWORD_LEG_OK = True  # set at server start from docs_dir_is_local()


def new_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def atomic_write(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def _http_json(url, body=None, timeout=5, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ----------------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------------

_health_cache = {"at": 0.0, "data": None}


def _listdir_with_timeout(path, timeout=2.0):
    """A hung CIFS mount can block listdir forever — do it on a scratch thread
    and abandon it on timeout (thread leaks are bounded by the 15 s health cache)."""
    out = {}

    def worker():
        try:
            out["names"] = os.listdir(path)
        except OSError as e:
            out["error"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"error": "timeout — NAS not responding"}
    return out


def health():
    now = time.time()
    if _health_cache["data"] and now - _health_cache["at"] < 15:
        return _health_cache["data"]
    h = {"ok": True, "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        ver = _http_json(f"{OLLAMA}/api/version", timeout=2)
        tags = _http_json(f"{OLLAMA}/api/tags", timeout=2)
        have = {m["name"] for m in tags.get("models", [])}
        h["ollama"] = {"ok": True, "version": ver.get("version"),
                       "models": {t: (m in have or f"{m}:latest" in have)
                                  for t, m in TIERS.items()}}
    except Exception as e:
        h["ollama"] = {"ok": False, "error": str(e)}
    try:
        es = _http_json(f"{ES_URL}/_cluster/health", timeout=2)
        cnt = _http_json(f"{ES_URL}/{ES_INDEX}/_count", timeout=2)
        h["es"] = {"ok": es.get("status") in ("green", "yellow"),
                   "status": es.get("status"), "chunks": cnt.get("count", 0)}
    except Exception as e:
        h["es"] = {"ok": False, "error": str(e)}
    nas = _listdir_with_timeout(NAS_TUNES)
    h["nas"] = ({"ok": True, "tunes_visible": sum(1 for n in nas["names"]
                                                  if n.lower().endswith(".tbw"))}
                if "names" in nas else {"ok": False, "error": nas.get("error")})
    idx = tune_index_path()
    h["tune_index"] = ({"age_s": int(now - idx.stat().st_mtime),
                        "count": len(load_tune_index().get("tunes", []))}
                       if idx.exists() else {"age_s": None, "count": 0})
    h["corpus_docs"] = len(ta.find_docs()) if KEYWORD_LEG_OK else 0
    h["keyword_leg"] = KEYWORD_LEG_OK
    du = shutil.disk_usage(DATA if DATA.exists() else ROOT)
    h["disk_free_gb"] = round(du.free / 1e9, 1)
    h["ok"] = h["ollama"]["ok"]
    _health_cache.update(at=now, data=h)
    return h


def profile_payload():
    prof = ta.load_profile()
    return {"profile": prof, "guardrails": guardrails.as_dict(),
            "tiers": TIERS, "keyword_leg": KEYWORD_LEG_OK}


# ----------------------------------------------------------------------------
# Unified retrieval: ES vector leg + corpus keyword leg -> numbered citations
# ----------------------------------------------------------------------------

def _embed(text, timeout=15):
    # 15 s allows a cold nomic-embed model load; runs OUTSIDE GEN_LOCK
    d = _http_json(f"{OLLAMA}/api/embeddings",
                   {"model": EMBED_MODEL, "prompt": text[:8000]}, timeout=timeout)
    return d["embedding"]


def _vector_leg(question, k=10):
    vec = _embed(question)
    body = {"knn": {"field": "vector", "query_vector": vec, "k": k,
                    "num_candidates": 100},
            "_source": ["text", "source", "path", "chunk"], "size": k}
    res = _http_json(f"{ES_URL}/{ES_INDEX}/_search", body, timeout=5)
    hits = []
    for hit in res.get("hits", {}).get("hits", []):
        s = hit["_source"]
        hits.append({"text": s["text"], "source": s["source"],
                     "path": s.get("path", ""), "retriever": "vector",
                     "score": hit.get("_score")})
    return hits


# Ollama on this tower runs with OLLAMA_CONTEXT_LENGTH=4096 (see the service
# Environment=). Anything past the window is silently truncated server-side, so
# a request that packs 24k chars of retrieved context loses most of it WITHOUT
# any error — the model then answers from whatever survived. Observed live: a
# correctly-retrieved ride entry at citation [1] never reached the model, and
# the 32b filled the gap by inventing values off a blank report template. So we
# now send an explicit num_ctx per tier AND size the retrieval budget to fit it.
TIER_CTX = {
    "fast": 16384,    # 14b, fully on the 16 GB GPU — room is cheap
    "smart": 16384,   # 32b, ~16 GB GPU + small CPU spill
    "deep": 8192,     # 70b is already ~0.7 tok/s; a huge window makes it worse
}
DEFAULT_CTX = 8192
CHARS_PER_TOKEN = 3.5   # conservative for English prose + markdown
CTX_RESERVE_TOKENS = 1800  # system prompt + question + room for the answer


def _est_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def context_budget(ctx_window):
    """How many characters of retrieved context actually fit in this window."""
    return max(1000, min(MAX_CONTEXT,
                         int((ctx_window - CTX_RESERVE_TOKENS) * CHARS_PER_TOKEN)))


def _fit_messages(msgs, ctx_window):
    """Drop the oldest history turns until the prompt fits the window. Returns
    (messages, dropped). The system prompt and the current question always
    survive — if those alone overflow, the caller has bigger problems and the
    model would truncate exactly the material we most need."""
    limit = ctx_window - 1024  # leave room for the answer itself
    dropped = 0
    while len(msgs) > 2:
        total = sum(_est_tokens(m["content"]) for m in msgs)
        if total <= limit:
            break
        # msgs[0] is the system prompt, msgs[-1] the current question+context;
        # history sits between them as user/assistant pairs
        end = 3 if len(msgs) > 3 else 2
        del msgs[1:end]
        dropped += 1
    return msgs, dropped


def unified_retrieve(question, profile=None, budget=None):
    """Merge both retrieval brains. Returns (citations, context_block, degraded).
    Vector leg failure of ANY kind degrades to keyword-only, visibly."""
    budget = MAX_CONTEXT if budget is None else budget
    degraded = None
    vec_hits = []
    try:
        vec_hits = _vector_leg(question)
    except Exception as e:
        degraded = f"vector search unavailable ({e.__class__.__name__}) — keyword-only"
    kw_hits = []
    if KEYWORD_LEG_OK:
        for score, name, passage in ta.scored_passages(question, profile=profile)[:20]:
            kw_hits.append({"text": passage, "source": name, "path": "",
                            "retriever": "keyword", "score": score})
    elif not vec_hits:
        degraded = "both retrieval legs unavailable — answering from model knowledge only"
    # Interleave vector-first (it saw the whole brain), dedupe, cap context.
    merged, seen, used = [], set(), 0
    a, b = vec_hits, kw_hits
    for i in range(max(len(a), len(b))):
        for leg in (a, b):
            if i < len(leg):
                h = leg[i]
                key = (h["source"], h["text"][:80])
                if key in seen:
                    continue
                if used + len(h["text"]) > budget:
                    continue
                seen.add(key)
                used += len(h["text"])
                merged.append(h)
    citations = [{"n": i + 1, "kind": _provenance(h["source"]), **h}
                 for i, h in enumerate(merged)]
    return citations, _build_context(citations), degraded


def _provenance(source):
    """Classify a citation by where the knowledge came from. Mirrors the boost
    tiers in scored_passages so the two never disagree about what is mine."""
    if "thundermax_journal_" in source:
        return "journal_unvetted"
    if "_learned_" in source:
        return "mine"
    return "reference"


FIRSTHAND_KINDS = ("journal_unvetted", "mine")

KIND_LABEL = {
    "journal_unvetted": "MY RECORD — logged outside the vetting pipeline, "
                        "treat as an observation, not as authority",
    "mine": "MY RECORD — vetted knowledge for this exact bike",
    "reference": "reference",
}


def _build_context(citations):
    """Split the context into first-hand records and reference material.

    Not cosmetic: with ~20 sources merged, a single journal entry gets buried
    under manuals and — worse — blank ride-report TEMPLATES whose empty
    fill-in fields look exactly like the answer to "what did I record?".
    Observed live: both the 14b and the 32b ignored a correctly-retrieved
    ride entry sitting at [1] and answered from a template, the 32b going as
    far as inventing plausible values. Sectioning the context and naming what
    each source IS gives the model the discriminator it was missing; the same
    entry then comes back verbatim with its citation.
    """
    first = [c for c in citations if c["kind"] in FIRSTHAND_KINDS]
    ref = [c for c in citations if c["kind"] not in FIRSTHAND_KINDS]
    out = []
    if first:
        out.append("=== MY OWN RECORDS (this bike — rides, tunes, learned notes) ===")
        out += [f"[{c['n']}] ({KIND_LABEL[c['kind']]}; source: {c['source']})\n{c['text']}"
                for c in first]
    if ref:
        out.append("=== REFERENCE MATERIAL (manuals, guides, blank templates) ===")
        out += [f"[{c['n']}] (source: {c['source']})\n{c['text']}" for c in ref]
    return "\n\n".join(out)


CITE_RULE = """
Cite your sources inline like [1] or [2], using ONLY the numbered excerpts
provided. If the excerpts do not support a claim, say so.

The excerpts come in two sections. "MY OWN RECORDS" are first-hand records
from THIS bike — what was actually done, ridden, and measured. They are the
authority for any question about what happened, what was recorded, or what
was changed; quote their real values rather than generalising. "REFERENCE
MATERIAL" is manuals, guides, and BLANK report templates: a template's empty
fill-in fields (e.g. "Cruise AFR: ______") are a form to be filled in, NEVER
data. Never present template placeholders, or invented example values, as
something the rider recorded. If no record covers the question, say plainly
that nothing is logged for it.
"""

SAFE_RULES = """
SAFETY GUARDRAILS for the 131ci Milwaukee-Eight — never recommend outside these
without explicitly warning first:
- WOT AFR 12.4-12.8; never leaner than 13.2 under load, never richer than 12.2.
- Cruise AFR 13.8-14.6; idle 13.8-14.2.
- Rear cylinder always ~0.2 richer and equal-or-retarded timing vs front.
- Spark advance: cruise 28-32 deg, WOT 26-30 deg, hard ceiling ~32 deg. Change
  no more than +/-2 deg per step, then data-log to verify before the next step.
- Recommend changes ONLY inside the ThunderMax TMax Tuner software. NEVER edit
  or write .tbw files directly - only the ThunderMax software may write those.
"""


def pick_tier(tier, question):
    """Resolve a requested tier to (model, reason). Auto-classify runs outside
    GEN_LOCK; if the generation slot is busy we skip the classifier entirely
    (it needs the fast model, which contends for the same VRAM) and default
    deep — the reason string keeps any downgrade/upgrade visible in the UI."""
    if tier in TIERS and tier != "auto":
        return TIERS[tier], f"user picked {tier}"
    if not GEN_LOCK.acquire(blocking=False):
        return TIERS["deep"], "auto: classifier skipped (generation busy), defaulted deep"
    GEN_LOCK.release()
    model = ta.classify(question)
    return model, f"auto: classified {'HARD' if model == ta.BIG_MODEL else 'EASY'}"


# ----------------------------------------------------------------------------
# Chat jobs: resumable server-side token buffers
# ----------------------------------------------------------------------------

class ChatJob:
    def __init__(self, message_id, session_id):
        self.message_id = message_id
        self.session_id = session_id
        self.events = []          # [{seq, event, data}]
        self.cond = threading.Condition()
        self.state = "queued"     # queued|generating|done|error
        self.cancel = threading.Event()
        self.response = None      # live urllib response, closed by cancel()
        self.finished_at = None

    def emit(self, event, data):
        with self.cond:
            self.events.append({"seq": len(self.events), "event": event, "data": data})
            self.cond.notify_all()

    def read_from(self, seq):
        """Yield events from seq, blocking for new ones until terminal."""
        while True:
            with self.cond:
                while seq >= len(self.events):
                    if self.state in ("done", "error"):
                        return
                    self.cond.wait(timeout=10)
                    if seq >= len(self.events):
                        yield None  # keepalive tick for the SSE writer
                        continue
                batch = self.events[seq:]
                seq = len(self.events)
            for ev in batch:
                yield ev


JOBS = {}
JOBS_LOCK = threading.Lock()


def _prune_jobs():
    cutoff = time.time() - 1800
    with JOBS_LOCK:
        for mid in [m for m, j in JOBS.items()
                    if j.finished_at and j.finished_at < cutoff]:
            del JOBS[mid]


def get_job(message_id):
    with JOBS_LOCK:
        return JOBS.get(message_id)


# --- sessions ---------------------------------------------------------------

def session_path(sid):
    return SESSIONS_DIR / f"{sid}.json"


def load_session(sid):
    try:
        return json.loads(session_path(sid).read_text())
    except (OSError, ValueError):
        return None


def list_sessions(limit=30):
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            s = json.loads(p.read_text())
            out.append({"id": s["id"], "created": s["created"], "title": s.get("title", ""),
                        "messages": len(s.get("messages", []))})
        except (OSError, ValueError):
            continue
    return out


def _save_message(sid, msg):
    with STORE_LOCK:
        s = load_session(sid) or {"id": sid, "created": datetime.now().isoformat(timespec="seconds"),
                                  "title": "", "tunelog": f"tune-{sid}.md", "messages": []}
        for i, m in enumerate(s["messages"]):
            if m["message_id"] == msg["message_id"]:
                s["messages"][i] = msg
                break
        else:
            s["messages"].append(msg)
        if not s["title"] and msg.get("q"):
            s["title"] = msg["q"][:80]
        atomic_write(session_path(sid), s)
        return s


def _mirror_tunelog(session, msg):
    """Append to ~/hermes-rag/tune-log in the exact hermes-tune log_session()
    format so the existing session corpus stays uniform."""
    try:
        TUNELOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TUNELOG_DIR / session["tunelog"], "a") as f:
            f.write(f"\n### {datetime.now():%Y-%m-%d %H:%M} · {msg['model']}\n\n"
                    f"**Q:** {msg['q']}\n\n{msg['a']}\n")
    except OSError:
        pass  # mirror is best-effort; the session JSON is the record


# --- generation -------------------------------------------------------------

def start_chat(question, session_id=None, tier="auto"):
    """Create the job + spawn the generation thread. Returns (message_id, session_id)."""
    _prune_jobs()
    sid = session_id or new_id()
    mid = new_id()
    job = ChatJob(mid, sid)
    with JOBS_LOCK:
        JOBS[mid] = job
    _save_message(sid, {"message_id": mid, "state": "queued", "q": question,
                        "a": "", "model": None, "citations": [],
                        "ts": datetime.now().isoformat(timespec="seconds")})
    threading.Thread(target=_generate, args=(job, question, tier), daemon=True).start()
    return mid, sid


def _history_messages(sid, question, ctx):
    """System + prior turns + current question (context attached to current)."""
    msgs = [{"role": "system", "content": ta.SYSTEM_PROMPT + SAFE_RULES + CITE_RULE}]
    s = load_session(sid)
    if s:
        for m in s["messages"][-8:]:
            if m["message_id"] and m.get("a") and m.get("state") == "done":
                msgs.append({"role": "user", "content": m["q"]})
                msgs.append({"role": "assistant", "content": m["a"]})
    user = question
    if ctx:
        # The reminder is repeated here, after the excerpts: the smaller tiers
        # reliably ignore a citation instruction that only appears in the
        # system prompt, thousands of tokens earlier.
        user = (f"Reference material:\n\n{ctx}\n\nQuestion: {question}\n\n"
                "Answer using the excerpts above, citing each fact inline as "
                "[n]. Prefer MY OWN RECORDS for anything about what was "
                "actually done, ridden, or measured. Never present a blank "
                "template field or an invented example as something I recorded.")
    msgs.append({"role": "user", "content": user})
    return msgs


def _generate(job, question, tier):
    t0 = time.time()
    try:
        # Tier first: it fixes the context window, which fixes how much
        # retrieved material can actually reach the model.
        model, reason = pick_tier(tier, question)
        tier_name = next((t for t, m in TIERS.items() if m == model), "?")
        ctx_window = TIER_CTX.get(tier_name, DEFAULT_CTX)
        job.emit("status", {"phase": "model", "model": model,
                            "tier": tier_name, "reason": reason})
        job.emit("status", {"phase": "retrieving"})
        citations, ctx, degraded = unified_retrieve(
            question, budget=context_budget(ctx_window))
        if degraded:
            job.emit("status", {"phase": "degraded", "detail": degraded})
        job.emit("citations", {"citations": citations})
        if not GEN_LOCK.acquire(blocking=False):
            job.emit("status", {"phase": "queued"})
            GEN_LOCK.acquire()
        try:
            if job.cancel.is_set():
                raise InterruptedError("cancelled")
            job.state = "generating"
            job.emit("status", {"phase": "generating"})
            msgs = _history_messages(job.session_id, question, ctx)
            msgs, dropped = _fit_messages(msgs, ctx_window)
            if dropped:
                job.emit("status", {"phase": "trimmed",
                                    "detail": f"dropped {dropped} older turn(s) "
                                              "to fit the model's context window"})
            answer = []
            req = urllib.request.Request(
                f"{OLLAMA}/api/chat",
                data=json.dumps({"model": model, "messages": msgs, "stream": True,
                                 "options": {"num_ctx": ctx_window}}).encode(),
                headers={"Content-Type": "application/json"})
            job.response = urllib.request.urlopen(req)
            try:
                for line in job.response:
                    if job.cancel.is_set():
                        raise InterruptedError("cancelled")
                    part = json.loads(line)
                    tok = part.get("message", {}).get("content", "")
                    if tok:
                        answer.append(tok)
                        job.emit("token", {"t": tok})
                    if part.get("done"):
                        break
            finally:
                try:
                    job.response.close()
                except Exception:
                    pass
        finally:
            GEN_LOCK.release()
        text = "".join(answer)
        msg = {"message_id": job.message_id, "state": "done", "q": question,
               "a": text, "model": model, "citations": citations,
               "ts": datetime.now().isoformat(timespec="seconds"),
               "elapsed_s": round(time.time() - t0, 1)}
        s = _save_message(job.session_id, msg)
        _mirror_tunelog(s, msg)
        job.state = "done"
        job.emit("done", {"message_id": job.message_id, "model": model,
                          "elapsed_s": msg["elapsed_s"]})
    except InterruptedError:
        job.state = "error"
        _save_message(job.session_id, {"message_id": job.message_id, "state": "error",
                                       "q": question, "a": "", "model": None,
                                       "citations": [], "error": "cancelled",
                                       "ts": datetime.now().isoformat(timespec="seconds")})
        job.emit("error", {"code": "cancelled", "message": "generation cancelled"})
    except Exception as e:
        job.state = "error"
        _save_message(job.session_id, {"message_id": job.message_id, "state": "error",
                                       "q": question, "a": "", "model": None,
                                       "citations": [], "error": str(e),
                                       "ts": datetime.now().isoformat(timespec="seconds")})
        job.emit("error", {"code": "generation_failed", "message": str(e)})
    finally:
        job.finished_at = time.time()


def cancel_chat(message_id):
    """Set the cancel flag AND close the live Ollama socket — the only way to
    unblock a thread stuck in minutes-long 70b prompt-eval (no tokens flow, so
    a between-tokens flag check never runs). Ollama aborts server-side on
    client disconnect."""
    job = get_job(message_id)
    if not job:
        return False
    job.cancel.set()
    if job.response is not None:
        try:
            job.response.close()
        except Exception:
            pass
    return True


# ----------------------------------------------------------------------------
# Tune index + cache (NAS isolation)
# ----------------------------------------------------------------------------

def tune_index_path():
    return DATA / "tune_index.json"


def load_tune_index():
    try:
        return json.loads(tune_index_path().read_text())
    except (OSError, ValueError):
        return {"tunes": [], "refreshed_at": None, "nas_ok": None, "error": None}


def _hash_bytes(b):
    import hashlib
    return hashlib.sha1(b).hexdigest()


def cache_path(sha1):
    # deliberately .bin, never .tbw — nothing this app writes could be flashed
    return TUNE_CACHE / f"{sha1}.bin"


def refresh_tune_index():
    """Scan the NAS tunes dir; cache new/changed tune bytes locally by sha1.
    All NAS I/O happens HERE (background thread, NAS_LOCK). The index is never
    overwritten with an empty/error result — stale beats wrong."""
    import thundermax_parser as tmx
    if not NAS_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        prev = {t["name"]: t for t in load_tune_index().get("tunes", [])}
        listing = _listdir_with_timeout(NAS_TUNES, timeout=15)
        if "names" not in listing:
            idx = load_tune_index()
            idx["nas_ok"] = False
            idx["error"] = listing.get("error", "NAS unreachable")
            with STORE_LOCK:
                atomic_write(tune_index_path(), idx)
            return idx
        tunes = []
        for name in sorted(listing["names"]):
            if not name.lower().endswith(".tbw") or name.startswith("._"):
                continue
            f = NAS_TUNES / name
            try:
                st = f.stat()
            except OSError:
                continue
            old = prev.get(name)
            if old and old.get("size") == st.st_size and old.get("mtime") == int(st.st_mtime):
                tunes.append(old)
                continue
            try:
                raw = f.read_bytes()
            except OSError as e:
                tunes.append({"name": name, "error": str(e), "valid": False})
                continue
            sha = _hash_bytes(raw)
            cp = cache_path(sha)
            if not cp.exists():
                cp.write_bytes(raw)
            try:
                t = tmx.TbwFile(cp)
                entry = {"name": name, "sha1": sha, "base_map_id": t.base_map_id,
                         "valid": t.valid, "size": st.st_size, "mtime": int(st.st_mtime)}
            except Exception as e:
                entry = {"name": name, "sha1": sha, "error": str(e), "valid": False,
                         "size": st.st_size, "mtime": int(st.st_mtime)}
            tunes.append(entry)
        prof = ta.load_profile()
        mine = set(prof.get("base_map_ids", []))
        for t in tunes:
            t["mine"] = t.get("base_map_id") in mine
        idx = {"tunes": tunes, "nas_ok": True, "error": None,
               "refreshed_at": datetime.now().isoformat(timespec="seconds")}
        with STORE_LOCK:
            atomic_write(tune_index_path(), idx)
        _health_cache["at"] = 0  # index age changed
        return idx
    finally:
        NAS_LOCK.release()


def start_index_refresh():
    threading.Thread(target=refresh_tune_index, daemon=True).start()


def _index_timer():
    while True:
        time.sleep(6 * 3600)
        try:
            refresh_tune_index()
        except Exception:
            pass


def start_index_timer():
    threading.Thread(target=_index_timer, daemon=True).start()


def find_tune(sha1):
    for t in load_tune_index().get("tunes", []):
        if t.get("sha1") == sha1:
            return t
    return None


def tune_detail(sha1):
    import thundermax_parser as tmx
    entry = find_tune(sha1)
    cp = cache_path(sha1)
    if not cp.exists():
        return None
    t = tmx.TbwFile(cp)
    return {"entry": entry, "base_map_id": t.base_map_id, "valid": t.valid,
            "header": ["0x%X" % h for h in t.header],
            "integrity": list(t.integrity_lines())}


def diff_tunes(sha_a, sha_b):
    """classify_diff + summarize + per-region signed deltas, memoized.
    classify_diff takes PATHS; region_deltas takes TbwFile objects — both fed
    from the local cache, never the NAS."""
    import thundermax_parser as tmx
    import table_map
    memo = DIFF_CACHE / f"{sha_a}_{sha_b}.json"
    if memo.exists():
        try:
            return json.loads(memo.read_text())
        except ValueError:
            pass
    pa, pb = cache_path(sha_a), cache_path(sha_b)
    if not (pa.exists() and pb.exists()):
        return {"error": "tune bytes not cached — refresh the library"}
    rows = table_map.classify_diff(str(pa), str(pb))
    ta_f, tb_f = tmx.TbwFile(pa), tmx.TbwFile(pb)
    for r in rows:
        # read each region on its band's own record grid — an even-offset u16
        # sweep reports 256x the real delta for the odd-offset bands
        band = table_map.band_for_offset(r["offset"], table_map.load_map())
        deltas = tmx.deltas_for_region(ta_f, tb_f, band, r["offset"], r["end"])
        if deltas:
            from collections import Counter
            r["delta_min"] = min(deltas)
            r["delta_max"] = max(deltas)
            r["delta_mode"] = Counter(deltas).most_common(1)[0][0]
            r["cells"] = len(deltas)
        r["offset_hex"] = "0x%05X" % r["offset"]
    summary = {}
    for cat, v in table_map.summarize(rows).items():
        # summarize() returns a Python set for confidences — JSON can't
        summary[cat] = {**v, "confidences": sorted(v["confidences"])}
    out = {"a": {"sha1": sha_a, "base_map_id": ta_f.base_map_id},
           "b": {"sha1": sha_b, "base_map_id": tb_f.base_map_id},
           "identical": not rows, "rows": rows, "summary": summary}
    with STORE_LOCK:
        atomic_write(memo, out)
    return out


def sync_tunes_from_cache():
    """Fold cached tunes matching MY setup into the KB — the web equivalent of
    `tmax sync`, but reading from the local cache so the NAS stays out of it."""
    prof = ta.load_profile()
    names = {}
    for t in load_tune_index().get("tunes", []):
        if t.get("sha1"):
            names[str(cache_path(t["sha1"]))] = t["name"]
    if not names:
        return {"ok": False, "error": "tune cache is empty — refresh the library first"}
    results = {"ok": True, "matched": [], "skipped": []}
    known = set(prof.get("base_map_ids", []))
    import thundermax_parser as tmx
    seen = set()
    for path, name in names.items():
        t = tmx.TbwFile(path)
        if t.base_map_id in known:
            doc = ta._tune_doc(t, Path(name), profile=prof)
            results["matched"].append({"file": name, "base_map_id": t.base_map_id,
                                       "doc": doc.name})
            seen.add(t.base_map_id)
        else:
            results["skipped"].append({"file": name, "base_map_id": t.base_map_id})
    current = ta.load_profile()
    all_ids = sorted(set(current.get("base_map_ids", [])) | known | seen)
    if all_ids != sorted(current.get("base_map_ids", [])):
        current["base_map_ids"] = all_ids
        with STORE_LOCK:
            ta.save_profile(current)
    results["base_map_ids"] = all_ids
    return results
