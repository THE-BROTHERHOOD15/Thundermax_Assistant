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
import tbw_read  # noqa: E402

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
# Tune libraries (read-only). USB 2026-08-19 pull + Desktop saves + laptop
# drop folder. NAS path is optional and often hung. Never write .tbw.
_DEFAULT_TUNE_DIRS = [
    Path.home() / "tmax-exchange" / "tunes-from-usb-2026-08-19",
    Path.home() / "tmax-exchange" / "from-laptop",
    Path.home() / "Desktop",
    Path("/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC"),
]
MAX_CONTEXT = ta.MAX_CONTEXT_CHARS


def tune_dirs():
    env = os.environ.get("TMAX_TUNES_DIR")
    dirs = [Path(env)] if env else []
    dirs.extend(_DEFAULT_TUNE_DIRS)
    out, seen = [], set()
    for d in dirs:
        try:
            r = d.resolve()
        except OSError:
            continue
        if r in seen or not d.is_dir():
            continue
        seen.add(r)
        out.append(d)
    return out


NAS_TUNES = tune_dirs()[0] if tune_dirs() else _DEFAULT_TUNE_DIRS[0]

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
    visible = 0
    nas_ok = False
    nas_err = None
    for d in tune_dirs():
        listing = _listdir_with_timeout(d)
        if "names" in listing:
            nas_ok = True
            visible += sum(1 for n in listing["names"]
                           if n.lower().endswith(".tbw") and not n.startswith("._"))
        elif nas_err is None:
            nas_err = f"{d}: {listing.get('error')}"
    h["decoder"] = decoder_status()
    h["nas"] = ({"ok": True, "tunes_visible": visible, "dirs": [str(d) for d in tune_dirs()]}
                if nas_ok else {"ok": False, "error": nas_err or "no tune dirs"})
    idx = tune_index_path()
    h["tune_index"] = ({"age_s": int(now - idx.stat().st_mtime),
                        "count": len(load_tune_index().get("tunes", []))}
                       if idx.exists() else {"age_s": None, "count": 0})
    h["corpus_docs"] = len(ta.find_docs()) if KEYWORD_LEG_OK else 0
    h["keyword_leg"] = KEYWORD_LEG_OK
    try:
        from kb.store import status as kb_status
        h["kb"] = kb_status()
    except Exception as e:
        h["kb"] = {"ok": False, "error": str(e)}
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


def unified_retrieve(question, profile=None):
    """Merge KB hybrid + ES vector + corpus keyword. Returns (citations, context, degraded).
    Vector leg failure of ANY kind degrades to keyword-only, visibly."""
    degraded = None
    kb_hits = []
    try:
        from kb.retrieve import citations_for_ui
        kb_hits, _bundle = citations_for_ui(question, profile=profile or ta.PROFILE)
    except Exception as e:
        degraded = f"kb retrieve unavailable ({e.__class__.__name__})"
    vec_hits = []
    try:
        vec_hits = _vector_leg(question)
    except Exception as e:
        extra = f"vector search unavailable ({e.__class__.__name__}) — keyword-only"
        degraded = f"{degraded}; {extra}" if degraded else extra
    kw_hits = []
    if KEYWORD_LEG_OK:
        for score, name, passage in ta.scored_passages(question, profile=profile)[:20]:
            kw_hits.append({"text": passage, "source": name, "path": "",
                            "retriever": "keyword", "score": score})
    elif not vec_hits and not kb_hits:
        degraded = "all retrieval legs unavailable — answering from model knowledge only"
    # KB first (source-chained), then interleave vector + keyword.
    merged, seen, used = [], set(), 0
    for h in kb_hits:
        key = (h.get("source"), (h.get("text") or "")[:80])
        if key in seen:
            continue
        if used + len(h.get("text") or "") > MAX_CONTEXT:
            continue
        seen.add(key)
        used += len(h.get("text") or "")
        merged.append(h)
    a, b = vec_hits, kw_hits
    for i in range(max(len(a), len(b))):
        for leg in (a, b):
            if i < len(leg):
                h = leg[i]
                key = (h["source"], h["text"][:80])
                if key in seen:
                    continue
                if used + len(h["text"]) > MAX_CONTEXT:
                    continue
                seen.add(key)
                used += len(h["text"])
                merged.append(h)
    citations = [{"n": i + 1, **h} for i, h in enumerate(merged)]
    ctx = "\n\n".join(f"[{c['n']}] (source: {c['source']})\n{c['text']}"
                      for c in citations)
    return citations, ctx, degraded


CITE_RULE = (
    "\nCite your sources inline like [1] or [2], using ONLY the numbered "
    "reference excerpts provided. If the excerpts do not support a claim, say so."
)

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
        user = f"Reference material:\n\n{ctx}\n\nQuestion: {question}"
    msgs.append({"role": "user", "content": user})
    return msgs


def _generate(job, question, tier):
    t0 = time.time()
    try:
        override = ta.shop_override(question) if hasattr(ta, "shop_override") else None
        if override:
            job.emit("status", {"phase": "shop-override"})
            job.emit("token", {"t": override})
            msg = {"message_id": job.message_id, "state": "done", "q": question,
                   "a": override, "model": "shop_override", "citations": [],
                   "ts": datetime.now().isoformat(timespec="seconds"),
                   "elapsed_s": round(time.time() - t0, 1)}
            s = _save_message(job.session_id, msg)
            _mirror_tunelog(s, msg)
            job.state = "done"
            job.emit("done", {"message_id": job.message_id, "model": "shop_override",
                              "elapsed_s": msg["elapsed_s"]})
            return
        job.emit("status", {"phase": "retrieving"})
        citations, ctx, degraded = unified_retrieve(question)
        decoded_ctx = decoder_context_for_question(question)
        if decoded_ctx:
            ctx = (decoded_ctx + "\n\n" + (ctx or "")).strip()
        if degraded:
            job.emit("status", {"phase": "degraded", "detail": degraded})
        job.emit("citations", {"citations": citations})
        model, reason = pick_tier(tier, question)
        job.emit("status", {"phase": "model", "model": model,
                            "tier": next((t for t, m in TIERS.items() if m == model), "?"),
                            "reason": reason})
        if not GEN_LOCK.acquire(blocking=False):
            job.emit("status", {"phase": "queued"})
            GEN_LOCK.acquire()
        try:
            if job.cancel.is_set():
                raise InterruptedError("cancelled")
            job.state = "generating"
            job.emit("status", {"phase": "generating"})
            msgs = _history_messages(job.session_id, question, ctx)
            answer = []
            req = urllib.request.Request(
                f"{OLLAMA}/api/chat",
                data=json.dumps({"model": model, "messages": msgs, "stream": True}).encode(),
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


def read_memo_path(sha1):
    return TUNE_CACHE / f"{sha1}.read.json"


def decoder_status():
    """Health for the 8-bit AFR decoder. Never touches a .tbw write."""
    out = {
        "ok": False,
        "afr_scale": tbw_read.AFR_SCALE,
        "landmark": "0b00100000",
        "claimed": None,
        "error": None,
    }
    try:
        payload = claimed_tune_read()
        if payload and payload.get("ok"):
            out["ok"] = True
            out["claimed"] = {
                "file": payload.get("file"),
                "summary": payload.get("afr", {}).get("summary"),
            }
        else:
            out["ok"] = True  # decoder imported; claimed file may be uncached
            out["error"] = (payload or {}).get("error") or "claimed tune not cached"
    except Exception as e:
        out["error"] = str(e)
    return out


def claimed_sha1():
    """Map ecm_state on_ecm to a library sha1 (cache), if indexed."""
    try:
        st = json.loads((DATA / "ecm_state.json").read_text())
    except (OSError, ValueError):
        return None
    name = (st.get("on_ecm") or {}).get("name") or ""
    if not name:
        return None
    for t in load_tune_index().get("tunes", []):
        n = t.get("name") or ""
        if n.endswith("/" + name) or n == name or n.endswith(name):
            return t.get("sha1")
    return None


def tune_read(sha1, full=False):
    """Decode a cached tune into AFR engineering units. Read-only."""
    if not sha1 or len(sha1) != 40:
        return {"error": "sha1 required", "ok": False}
    cp = cache_path(sha1)
    if not cp.exists():
        return {"error": "tune bytes not cached — refresh the library", "ok": False}
    memo = read_memo_path(sha1)
    decoded = None
    if memo.exists():
        try:
            decoded = json.loads(memo.read_text())
        except ValueError:
            decoded = None
    if decoded is None:
        decoded = tbw_read.read_tune(str(cp))
        decoded["file"] = (find_tune(sha1) or {}).get("name") or decoded.get("file")
        with STORE_LOCK:
            atomic_write(memo, decoded)
    pub = tbw_read.public_payload(decoded, full=full)
    pub["entry"] = find_tune(sha1)
    pub["sha1"] = sha1
    return pub


def claimed_tune_read(full=False):
    sha = claimed_sha1()
    if not sha:
        path = None
        try:
            st = json.loads((DATA / "ecm_state.json").read_text())
            path = (st.get("on_ecm") or {}).get("path")
        except (OSError, ValueError):
            path = None
        if path and Path(path).is_file():
            decoded = tbw_read.read_tune(path)
            return tbw_read.public_payload(decoded, full=full)
        return {"ok": False, "error": "claimed on_ecm tune is not in the library cache"}
    return tune_read(sha, full=full)


def decoder_context_for_question(question):
    return tbw_read.claimed_file_context(question)


def refresh_tune_index():
    """Scan tune dirs; cache new/changed tune bytes locally by sha1.
    All library I/O happens HERE (background thread, NAS_LOCK). The index is
    never overwritten with an empty/error result — stale beats wrong.
    Cache files are .bin, never .tbw — nothing this app writes can be flashed."""
    import thundermax_parser as tmx
    if not NAS_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        prev = {t["name"]: t for t in load_tune_index().get("tunes", [])}
        dirs = tune_dirs()
        if not dirs:
            idx = load_tune_index()
            idx["nas_ok"] = False
            idx["error"] = "no readable tune directories"
            with STORE_LOCK:
                atomic_write(tune_index_path(), idx)
            return idx
        tunes = []
        for d in dirs:
            listing = _listdir_with_timeout(d, timeout=15)
            if "names" not in listing:
                continue
            tag = d.name
            files = []
            for name in sorted(listing["names"]):
                if name.startswith(".") or name.startswith("._"):
                    continue
                p = d / name
                if name.lower().endswith(".tbw"):
                    files.append((f"{tag}/{name}", p))
                elif p.is_dir():
                    sub = _listdir_with_timeout(p, timeout=10)
                    if "names" not in sub:
                        continue
                    for n2 in sorted(sub["names"]):
                        if n2.lower().endswith(".tbw") and not n2.startswith("._"):
                            files.append((f"{tag}/{name}/{n2}", p / n2))
            for key, f in files:
                try:
                    st = f.stat()
                except OSError:
                    continue
                old = prev.get(key) or prev.get(name)
                if old and old.get("size") == st.st_size and old.get("mtime") == int(st.st_mtime):
                    old = dict(old)
                    old["name"] = key
                    tunes.append(old)
                    continue
                try:
                    raw = f.read_bytes()
                except OSError as e:
                    tunes.append({"name": key, "error": str(e), "valid": False})
                    continue
                sha = _hash_bytes(raw)
                cp = cache_path(sha)
                if not cp.exists():
                    cp.write_bytes(raw)
                try:
                    t = tmx.TbwFile(cp)
                    entry = {"name": key, "sha1": sha, "base_map_id": t.base_map_id,
                             "valid": t.valid, "size": st.st_size, "mtime": int(st.st_mtime)}
                except Exception as e:
                    entry = {"name": key, "sha1": sha, "error": str(e), "valid": False,
                             "size": st.st_size, "mtime": int(st.st_mtime)}
                tunes.append(entry)
        try:
            est = json.loads((DATA / "ecm_state.json").read_text())
            cpath = Path((est.get("on_ecm") or {}).get("path") or "")
            cname = (est.get("on_ecm") or {}).get("name") or cpath.name
            if cpath.is_file() and not any((t.get("name") or "").endswith(cname) for t in tunes):
                stt = cpath.stat()
                raw = cpath.read_bytes()
                sha = _hash_bytes(raw)
                cp = cache_path(sha)
                if not cp.exists():
                    cp.write_bytes(raw)
                t = tmx.TbwFile(cp)
                tunes.append({"name": f"claimed/{cname}", "sha1": sha,
                              "base_map_id": t.base_map_id, "valid": t.valid,
                              "size": stt.st_size, "mtime": int(stt.st_mtime)})
        except Exception:
            pass
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
    out = {"entry": entry, "base_map_id": t.base_map_id, "valid": t.valid,
            "header": ["0x%X" % h for h in t.header],
            "integrity": list(t.integrity_lines())}
    decoded = tune_read(sha1)
    if decoded.get("ok"):
        out["afr"] = decoded.get("afr")
        out["landmark"] = decoded.get("landmark")
        out["never_write_tbw"] = True
    else:
        out["afr"] = {"error": decoded.get("error")}
    return out


def diff_tunes(sha_a, sha_b):
    """classify_diff + summarize + per-region signed deltas, memoized.
    classify_diff takes PATHS; region_deltas takes TbwFile objects — both fed
    from the local cache, never the NAS."""
    import thundermax_parser as tmx
    import table_map
    memo = DIFF_CACHE / f"{sha_a}_{sha_b}.json"
    if memo.exists():
        try:
            cached = json.loads(memo.read_text())
            if "afr_diff" in cached:
                return cached
        except ValueError:
            pass
    pa, pb = cache_path(sha_a), cache_path(sha_b)
    if not (pa.exists() and pb.exists()):
        return {"error": "tune bytes not cached — refresh the library"}
    rows = table_map.classify_diff(str(pa), str(pb))
    ta_f, tb_f = tmx.TbwFile(pa), tmx.TbwFile(pb)
    for r in rows:
        deltas = tmx.region_deltas(ta_f, tb_f, r["offset"], r["end"])
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
    afr_changed = []
    try:
        da, db = tbw_read.read_tune(str(pa)), tbw_read.read_tune(str(pb))
        afr_changed = tbw_read.afr_diff(da, db)
    except Exception:
        afr_changed = []
    out = {"a": {"sha1": sha_a, "base_map_id": ta_f.base_map_id},
           "b": {"sha1": sha_b, "base_map_id": tb_f.base_map_id},
           "identical": not rows, "rows": rows, "summary": summary,
           "afr_diff": afr_changed,
           "afr_scale": tbw_read.AFR_SCALE,
           "never_write_tbw": True}
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


# ----------------------------------------------------------------------------
# Phase 4 — journal + KB
# ----------------------------------------------------------------------------

def list_journal():
    notes = []
    for p in sorted(JOURNAL_DIR.glob("*.json"), reverse=True):
        try:
            notes.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    learned = []
    docs = ta.DOCS_DIR
    if docs.is_dir():
        for p in sorted(docs.glob("thundermax_learned_*.md"), reverse=True)[:50]:
            learned.append({
                "id": p.stem,
                "title": p.stem.replace("thundermax_learned_", "").replace("-", " ")[:80],
                "kind": "kb",
                "mtime": int(p.stat().st_mtime),
            })
    return {"notes": notes, "learned": learned, "sessions": list_sessions()}


def add_journal(title, body, kind="ride"):
    title = (title or "").strip() or "untitled"
    body = (body or "").strip()
    if not body:
        return {"error": "body required"}
    nid = new_id()
    obj = {"id": nid, "title": title, "body": body, "kind": kind,
           "ts": datetime.now().isoformat(timespec="seconds")}
    with STORE_LOCK:
        atomic_write(JOURNAL_DIR / f"{nid}.json", obj)
        try:
            ta.learn_write(title, body, source="journal")
        except Exception:
            pass
    return obj


def journal_note(nid):
    p = JOURNAL_DIR / f"{nid}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def kb_doc(stem):
    p = ta.DOCS_DIR / f"{stem}.md"
    if not p.is_file():
        return None
    text = p.read_text(errors="replace")
    return {"id": stem, "title": stem, "body": text[:12000], "kind": "kb"}


# ----------------------------------------------------------------------------
# Phase 5 — proposals + guardrail vetting
# ----------------------------------------------------------------------------

def list_proposals():
    out = []
    for p in sorted(PROPOSALS_DIR.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


def add_proposal(title, claim, changes):
    changes = changes if isinstance(changes, list) else []
    vet = guardrails.check_proposal(changes)
    pid = new_id()
    obj = {
        "id": pid,
        "title": (title or "").strip() or "untitled proposal",
        "claim": (claim or "").strip(),
        "changes": changes,
        "vet": vet,
        "status": "blocked" if not vet.get("passed") else "open",
        "never_write_tbw": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    with STORE_LOCK:
        atomic_write(PROPOSALS_DIR / f"{pid}.json", obj)
    return obj


def vet_proposal(pid, extra_changes=None):
    p = PROPOSALS_DIR / f"{pid}.json"
    try:
        obj = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    changes = extra_changes if extra_changes is not None else obj.get("changes") or []
    vet = guardrails.check_proposal(changes)
    obj["changes"] = changes
    obj["vet"] = vet
    obj["status"] = "blocked" if not vet.get("passed") else obj.get("status") or "open"
    if obj["status"] == "blocked" and vet.get("passed"):
        obj["status"] = "open"
    with STORE_LOCK:
        atomic_write(p, obj)
    return obj


def set_proposal_status(pid, status):
    allowed = {"open", "applied-in-tmax", "rejected", "blocked"}
    if status not in allowed:
        return {"error": f"status must be one of {sorted(allowed)}"}
    p = PROPOSALS_DIR / f"{pid}.json"
    try:
        obj = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if status == "applied-in-tmax" and not obj.get("vet", {}).get("passed"):
        return {"error": "blocked proposal cannot be marked applied — re-vet first"}
    obj["status"] = status
    with STORE_LOCK:
        atomic_write(p, obj)
    return obj


# ----------------------------------------------------------------------------
# Phase 6 — virtual dyno (house-limit gauges, not live ECU)
# ----------------------------------------------------------------------------

def _interp_backbone(rpm):
    pts = guardrails.TIMING_BACKBONE
    if rpm <= pts[0][0]:
        return pts[0][1]
    if rpm >= pts[-1][0]:
        return pts[-1][1]
    for (r0, d0), (r1, d1) in zip(pts, pts[1:]):
        if r0 <= rpm <= r1:
            t = (rpm - r0) / (r1 - r0) if r1 != r0 else 0
            return round(d0 + t * (d1 - d0), 1)
    return pts[-1][1]


def dyno_snapshot(rpm=2500, tps=20, cht=240):
    try:
        rpm = int(rpm)
        tps = int(tps)
        cht = int(cht)
    except (TypeError, ValueError):
        rpm, tps, cht = 2500, 20, 240
    rpm = max(800, min(6500, rpm))
    tps = max(0, min(100, tps))
    cht = max(60, min(400, cht))
    spark = _interp_backbone(rpm)
    in_belly = (guardrails.BELLY_RPM[0] <= rpm <= guardrails.BELLY_RPM[1]
                and guardrails.BELLY_TPS[0] <= tps <= guardrails.BELLY_TPS[1])
    if in_belly:
        spark = round(spark - guardrails.BELLY_DERATE_DEG, 1)
    if cht >= guardrails.HEAT_RETARD_CHT_F:
        extra = min(4.0, (cht - guardrails.HEAT_RETARD_CHT_F) / 25.0)
        spark = round(spark - extra, 1)
    wot = tps >= 80
    afr = 12.6 if wot else (14.0 if tps >= 8 else 14.0)
    duty = min(95.0, 18 + tps * 0.55 + max(0, rpm - 2000) / 80.0)
    at_ok = guardrails.AUTOTUNE_ENABLE_F <= cht <= guardrails.AUTOTUNE_DISABLE_F
    return {
        "mode": "virtual — not live ECU telemetry. Apply changes only in TMax Tuner.",
        "never_write_tbw": True,
        "inputs": {"rpm": rpm, "tps": tps, "cht": cht},
        "gauges": [
            {"name": "CHT", "value": cht, "unit": "°F", "min": 160, "max": 360,
             "warn": guardrails.AUTOTUNE_DISABLE_F, "red": 320,
             "ok": cht < guardrails.AUTOTUNE_DISABLE_F,
             "note": "AutoTune disable >280°F (heat-soak trims are garbage)"},
            {"name": "AFR", "value": afr, "unit": "", "min": 11.5, "max": 15.5,
             "warn": 13.2 if wot else 14.6, "red": 13.4 if wot else 15.0,
             "ok": True,
             "note": "WOT 12.4–12.8 · cruise 13.8–14.6"},
            {"name": "Spark", "value": spark, "unit": "°", "min": 0, "max": 36,
             "warn": guardrails.SPARK_CEILING - 2, "red": guardrails.SPARK_CEILING,
             "ok": spark <= guardrails.SPARK_CEILING,
             "note": "backbone + belly derate + heat retard; ceiling 32°"},
            {"name": "Injector duty", "value": round(duty, 1), "unit": "%",
             "min": 0, "max": 100,
             "warn": guardrails.INJECTOR_DUTY_AMBER_PCT,
             "red": guardrails.INJECTOR_DUTY_RED_PCT,
             "ok": duty < guardrails.INJECTOR_DUTY_AMBER_PCT,
             "note": "6.3 injectors — amber 80% / red 90%"},
        ],
        "autotune_window": at_ok,
        "belly": in_belly,
        "backbone": guardrails.TIMING_BACKBONE,
        "flash_first": "M8_131_550CAM_63inj.tbw",
        "no_flash": "17AUG2025ATLRUNNINGGREATv6TODAY.tbw (SE8-517 / 5.5 inj history)",
    }
