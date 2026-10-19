#!/usr/bin/env python3
"""TMax Command Center — web front end for the ThunderMax tuning assistant.

Serves the SPA from web/ plus a JSON API, with resumable SSE chat streaming.
Stdlib only, one process. Supersedes api_server.py (:8181) — Phase 7 retires
that service and this server keeps its NDJSON endpoints for compatibility.

Run: python3 src/webui_server.py [--host 0.0.0.0] [--port 8092]
Auth: set TMAX_TOKEN to require a token; POST /api/auth {token} sets a cookie
(EventSource sends same-origin cookies, so streaming works unchanged).
"""

import argparse
import hmac
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webui_core as core  # noqa: E402
import webui_journal as journal  # noqa: E402
import vetting  # noqa: E402
import tune_assistant as ta  # noqa: E402

WEB_DIR = (Path(__file__).resolve().parent.parent / "web").resolve()
TOKEN = os.environ.get("TMAX_TOKEN", "")
COOKIE = "tmax_auth"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TMaxCommandCenter/1.0"

    def log_message(self, *a):
        pass

    # --- plumbing -----------------------------------------------------------

    def _json(self, obj, code=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except ValueError:
            return {}

    def _authed(self):
        if not TOKEN:
            return True
        cookies = self.headers.get("Cookie", "")
        m = re.search(rf"{COOKIE}=([\w-]+)", cookies)
        if m and hmac.compare_digest(m.group(1), TOKEN):
            return True
        # first-contact fallback: ?token= on GET sets the cookie (handy for
        # pinning the PWA); never required per-request after that
        q = parse_qs(urlparse(self.path).query)
        t = (q.get("token") or [""])[0]
        return bool(t) and hmac.compare_digest(t, TOKEN)

    def _auth_cookie_header(self):
        return {"Set-Cookie": f"{COOKIE}={TOKEN}; Path=/; Max-Age=31536000; "
                              "SameSite=Lax; HttpOnly"}

    # --- SSE ----------------------------------------------------------------

    def _sse_begin(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event, data, seq=None):
        buf = []
        if seq is not None:
            buf.append(f"id: {seq}\n")
        buf.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")
        self.wfile.write("".join(buf).encode())
        self.wfile.flush()

    def _sse_ping(self):
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    # --- routing ------------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def _route(self, method):
        path = urlparse(self.path).path
        try:
            for m, rx, fn in ROUTES:
                if m != method:
                    continue
                match = re.fullmatch(rx, path)
                if match:
                    if path != "/api/auth" and path.startswith("/api/") and not self._authed():
                        return self._json({"error": "auth required"}, 401)
                    return fn(self, *match.groups())
            if method == "GET":
                if not self._authed():
                    # let the shell load; the app shows the token prompt
                    if path not in ("/", "/index.html"):
                        return self._json({"error": "auth required"}, 401)
                return self._static(path)
            self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass  # phone dropped mid-stream; the ChatJob buffer has the data
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def _static(self, path):
        name = "index.html" if path in ("/", "") else unquote(path).lstrip("/")
        f = (WEB_DIR / name).resolve()
        if not str(f).startswith(str(WEB_DIR)) or not f.is_file():
            # SPA fallback: unknown non-file paths get the shell
            f = WEB_DIR / "index.html"
            if not f.is_file():
                return self._json({"error": "not found"}, 404)
        mime = MIME.get(f.suffix.lower())
        if not mime:
            return self._json({"error": "type not served"}, 404)
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# --- handlers ----------------------------------------------------------------

def h_auth(h):
    body = h._body()
    if TOKEN and hmac.compare_digest(body.get("token", ""), TOKEN):
        return h._json({"ok": True}, extra=h._auth_cookie_header())
    if not TOKEN:
        return h._json({"ok": True, "note": "no token configured"})
    h._json({"ok": False, "error": "bad token"}, 403)


def h_health(h):
    h._json(core.health())


def h_profile(h):
    h._json(core.profile_payload())


def h_chat_post(h):
    body = h._body()
    q = (body.get("question") or "").strip()
    if not q:
        return h._json({"error": "question required"}, 400)
    mid, sid = core.start_chat(q, session_id=body.get("session_id"),
                               tier=body.get("tier", "auto"))
    h._json({"message_id": mid, "session_id": sid})


def h_chat_cancel(h, mid):
    h._json({"ok": core.cancel_chat(mid)})


def h_chat_stream(h, mid):
    job = core.get_job(mid)
    last = h.headers.get("Last-Event-ID")
    since = int(last) + 1 if last and last.isdigit() else 0
    h._sse_begin()
    if job is None:
        # expired/unknown id: emit a terminal event rather than 404 — a bare
        # 404 either kills native reconnect or makes a wrapper retry forever
        h._sse("error", {"code": "expired", "message": "message no longer buffered"})
        return
    if job.state in ("done", "error") and since >= len(job.events):
        h._sse("done", {"message_id": mid, "note": "already complete"})
        return
    for ev in job.read_from(since):
        if ev is None:
            h._sse_ping()
        else:
            h._sse(ev["event"], ev["data"], seq=ev["seq"])


def h_sessions(h):
    h._json({"sessions": core.list_sessions()})


def h_session(h, sid):
    s = core.load_session(sid)
    h._json(s if s else {"error": "not found"}, 200 if s else 404)


def h_tunes(h):
    h._json(core.load_tune_index())


def h_tunes_refresh(h):
    if core.NAS_LOCK.locked():
        return h._json({"busy": True}, 409)
    core.start_index_refresh()
    h._json({"started": True})


def h_tune(h, sha1):
    d = core.tune_detail(sha1)
    h._json(d if d else {"error": "not cached"}, 200 if d else 404)


def h_tunes_diff(h):
    q = parse_qs(urlparse(h.path).query)
    a, b = (q.get("a") or [""])[0], (q.get("b") or [""])[0]
    if not (a and b):
        return h._json({"error": "a and b sha1 params required"}, 400)
    h._json(core.diff_tunes(a, b))


def h_tunes_sync(h):
    h._json(core.sync_tunes_from_cache())


def h_journal_list(h):
    h._json({"entries": journal.list_entries(),
             "types": list(journal.ENTRY_TYPES)})


def h_journal_create(h):
    e = journal.create_entry(h._body())
    h._json(e, 400 if e.get("error") else 200)


def h_journal_get(h, jid):
    e = journal.load_entry(jid)
    h._json(e if e else {"error": "not found"}, 200 if e else 404)


def h_journal_patch(h, jid):
    body = h._body()
    # vetted is not a client-settable field: promotion to citable authority
    # happens only via the proposal state machine (upgrade_entry).
    if "vetted" in body:
        return h._json({"error": "vetted is set by the proposal pipeline, "
                                 "not by editing the entry"}, 409)
    e = journal.update_entry(jid, body)
    h._json(e, 404 if e.get("error") == "not found"
            else (400 if e.get("error") else 200))


def h_journal_retry(h):
    h._json(journal.retry_pending_es())


def h_journal_retract(h, jid):
    """Pull an entry out of BOTH retrieval legs. The JSON record survives as
    history — this un-publishes knowledge, it does not erase what happened."""
    e = journal.delete_entry_docs(jid)
    h._json(e, 404 if e.get("error") == "not found" else 200)


# --- proposals + vetting ------------------------------------------------------
# A vet run can take minutes (a 70b refutation at ~0.7 tok/s), so it runs on a
# worker thread and streams stage progress over SSE. ChatJob is reused verbatim
# as the buffer: same replay-from-0 resume behaviour the chat stream needs when
# Joshua's phone locks mid-run.

VET_JOBS = {}
VET_JOBS_LOCK = threading.Lock()


def _err(h, r):
    """vetting returns {'error','code','status'} — map status straight to HTTP."""
    return h._json(r, r.get("status", 400))


def h_proposals(h):
    q = parse_qs(urlparse(h.path).query)
    rows = vetting.list_proposals(state=(q.get("state") or [None])[0])
    for r in rows:
        # the list chip needs counts without an N+1 fetch; a missing summary
        # would render a blocked proposal as merely "unvetted"
        r["summary"] = {"blocks": r.get("blocks"), "warns": r.get("warns"),
                        "checks_unverifiable": r.get("checks_unverifiable")}
    h._json({"proposals": rows, "machine": vetting.state_payload()})


def h_proposal_create(h):
    r = vetting.create_proposal(h._body())
    h._json(r) if not r.get("error") else _err(h, r)


def h_proposal(h, pid):
    p = vetting.load_proposal(pid)
    if not p:
        return h._json({"error": "not found"}, 404)
    p["overlaps"] = vetting.overlap_net(p)
    running = vetting.vet_running(pid)
    with VET_JOBS_LOCK:
        job = VET_JOBS.get(pid)
    # Without this the UI cannot know to re-attach the progress stream after a
    # phone lock or an iPad handover — it would show a stale report and a
    # "re-run" button while a 70b refutation was still grinding away.
    stage = None
    if job:
        for ev in reversed(job.events):
            if ev["event"] == "progress":
                stage = ev["data"].get("stage")
                break
    p["vet_running"] = running
    p["vetting"] = {"running": running, "stage": stage,
                    "buffered": bool(job), "state": job.state if job else None}
    h._json(p)


def h_proposal_revise(h, pid):
    """`changes` are immutable, so an edit forks a NEW draft rather than
    mutating a proposal that may already carry a vet report."""
    r = vetting.revise_proposal(pid, h._body())
    h._json(r) if not r.get("error") else _err(h, r)


def h_proposal_vet(h, pid):
    if not vetting.load_proposal(pid):
        return h._json({"error": "not found"}, 404)
    if vetting.vet_running(pid):
        return h._json({"error": "vet already running", "code": "already_running"}, 409)
    job = core.ChatJob(pid, pid)
    with VET_JOBS_LOCK:
        VET_JOBS[pid] = job

    def run():
        try:
            r = vetting.vet_proposal(pid, progress=lambda ev: job.emit("progress", ev))
            job.state = "done"
            job.emit("done", r)
        except Exception as e:
            job.state = "error"
            job.emit("error", {"code": "vet_failed", "message": str(e)})
        finally:
            job.finished_at = __import__("time").time()

    threading.Thread(target=run, daemon=True).start()
    h._json({"started": True, "proposal_id": pid})


def h_proposal_vet_stream(h, pid):
    with VET_JOBS_LOCK:
        job = VET_JOBS.get(pid)
    last = h.headers.get("Last-Event-ID")
    since = int(last) + 1 if last and last.isdigit() else 0
    h._sse_begin()
    if job is None:
        # same contract as the chat stream: a terminal event, never a bare 404,
        # so a reconnect wrapper stops instead of retrying forever
        h._sse("error", {"code": "expired", "message": "no vet run buffered"})
        return
    if job.state in ("done", "error") and since >= len(job.events):
        return h._sse("done", {"proposal_id": pid, "note": "already complete"})
    for ev in job.read_from(since):
        if ev is None:
            h._sse_ping()
        else:
            h._sse(ev["event"], ev["data"], seq=ev["seq"])


def h_proposal_vet_cancel(h, pid):
    h._json({"ok": vetting.cancel_vet(pid)})


def h_proposal_transition(h, pid):
    body = h._body()
    to = body.get("to")
    if not to:
        return h._json({"error": "to required"}, 400)
    # Accept both spellings for each field. The vetting engine and the web form
    # were built to the same spec but landed on different key names; a silently
    # dropped `ack_unverifiable` would look like a refused approval, and a
    # dropped `journal_id` would fail to promote the ride entry at all.
    note = body.get("note") or body.get("reason") or ""
    entry_id = body.get("entry_id") or body.get("journal_id")
    ack = bool(body.get("acknowledge_unverifiable") or body.get("ack_unverifiable"))
    r = vetting.transition(pid, to, note=note, entry_id=entry_id,
                           acknowledge_unverifiable=ack)
    h._json(r) if not r.get("error") else _err(h, r)


def h_proposal_dyno_attach(h, pid):
    r = vetting.attach_dyno_run(pid, h._body())
    h._json(r) if not r.get("error") else _err(h, r)


def h_proposal_dyno_detach(h, pid, run_id):
    r = vetting.detach_dyno_run(pid, run_id)
    h._json(r) if not r.get("error") else _err(h, r)


def h_dyno_run(h):
    """Run a simulated pull. Deterministic physics, no LLM — and it can never
    block anything: every issue it raises is advisory (see virtual_dyno)."""
    import virtual_dyno
    body = h._body()
    try:
        gear = int(body.get("gear") or 5)
    except (TypeError, ValueError):
        gear = 5
    try:
        out = virtual_dyno.simulate_pull(body.get("changes") or [],
                                         conditions=body.get("conditions"),
                                         gear=gear)
    except Exception as e:
        return h._json({"error": f"{e.__class__.__name__}: {e}"}, 400)
    h._json(out)


def h_dyno_baseline(h):
    import virtual_dyno
    h._json(virtual_dyno.load_baseline())


def h_dyno_selftest(h):
    """Proof the dyno is thinking correctly: known-answer + invariant checks on
    the physics, run at runtime rather than only in pytest. Self-consistency
    only — it cannot prove the model matches the real engine."""
    import dyno_bridge
    h._json(dyno_bridge.self_test())


def h_tune_read(h, sha1):
    """What the dyno can actually see inside one tune — per band, with the
    confidence and the explicit list of what it CANNOT determine."""
    import dyno_bridge
    p = core.cache_path(sha1)
    if not p.exists():
        return h._json({"error": "tune bytes not cached — refresh the library"}, 404)
    out = dyno_bridge.read_tune(str(p))
    entry = core.find_tune(sha1)
    if entry:
        out["file"]["name"] = entry.get("name", out["file"]["name"])
    h._json(out)


def h_tune_bridge(h):
    """Derive dyno changes from a REAL A->B tune diff, and (unless ?sim=0) run
    the pull. This is the honest path from 'here is my tune' to the gauges."""
    import dyno_bridge, virtual_dyno
    q = parse_qs(urlparse(h.path).query)
    a, b = (q.get("a") or [""])[0], (q.get("b") or [""])[0]
    if not (a and b):
        return h._json({"error": "a and b sha1 params required"}, 400)
    pa, pb = core.cache_path(a), core.cache_path(b)
    if not (pa.exists() and pb.exists()):
        return h._json({"error": "tune bytes not cached — refresh the library"}, 404)
    out = dyno_bridge.changes_from_diff(
        str(pa), str(pb),
        include_autotune=(q.get("autotune") or ["0"])[0] == "1")
    usable = [c for c in out["changes"] if c.get("dyno_usable")]
    out["simulated"] = False
    if usable and (q.get("sim") or ["1"])[0] != "0":
        try:
            out["pull"] = virtual_dyno.simulate_pull(usable, gear=5)
            out["simulated"] = True
        except Exception as e:
            out["pull_error"] = f"{e.__class__.__name__}: {e}"
    h._json(out)


def h_learn(h):
    """Write a note straight into the corpus. Kept from the plan's API list;
    unlike a journal entry this is explicitly Joshua-authored reference
    material, so it is written as a normal learned doc."""
    body = h._body()
    content = (body.get("content") or "").strip()
    title = (body.get("title") or "").strip()
    if not (content and title):
        return h._json({"error": "title and content required"}, 400)
    path = ta.learn_write(title, content, source=body.get("source", "web"),
                          profile=ta.load_profile())
    h._json({"ok": True, "doc": path.name})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    # NOT 8090: that port belongs to AI Operator on this tower (and something
    # is listening on it). 8181 is the existing shop UI's tmax-api.service.
    ap.add_argument("--port", type=int, default=8092)
    args = ap.parse_args(argv)

    core.ensure_dirs()
    core.KEYWORD_LEG_OK = core.docs_dir_is_local()
    if not core.KEYWORD_LEG_OK:
        print(f"WARNING: corpus resolved to {ta.DOCS_DIR} (NAS!) — keyword "
              "retrieval DISABLED to keep CIFS out of the request path.",
              file=sys.stderr)

    core.start_index_refresh()   # non-blocking; index serves stale meanwhile
    core.start_index_timer()
    journal.start_es_retry_timer()  # re-ingest entries stranded by an ES outage

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"TMax Command Center on http://{args.host}:{args.port}/ "
          f"(web: {WEB_DIR}, auth: {'token' if TOKEN else 'open'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


ROUTES = [
    ("POST", r"/api/auth", h_auth),
    ("GET", r"/api/health", h_health),
    ("GET", r"/api/profile", h_profile),
    ("POST", r"/api/chat/messages", h_chat_post),
    ("POST", r"/api/chat/messages/([\w-]+)/cancel", h_chat_cancel),
    ("GET", r"/api/chat/stream/([\w-]+)", h_chat_stream),
    ("GET", r"/api/chat/sessions", h_sessions),
    ("GET", r"/api/chat/sessions/([\w-]+)", h_session),
    ("GET", r"/api/tunes", h_tunes),
    ("POST", r"/api/tunes/refresh", h_tunes_refresh),
    ("GET", r"/api/tunes/([0-9a-f]{40})", h_tune),
    ("GET", r"/api/tunes/diff", h_tunes_diff),
    ("POST", r"/api/tunes/sync", h_tunes_sync),
    ("GET", r"/api/journal", h_journal_list),
    ("POST", r"/api/journal", h_journal_create),
    ("POST", r"/api/journal/retry-index", h_journal_retry),
    ("POST", r"/api/journal/([\w-]+)/retract", h_journal_retract),
    ("GET", r"/api/journal/([\w-]+)", h_journal_get),
    ("PATCH", r"/api/journal/([\w-]+)", h_journal_patch),
    ("GET", r"/api/proposals", h_proposals),
    ("POST", r"/api/proposals", h_proposal_create),
    ("GET", r"/api/proposals/([\w-]+)", h_proposal),
    ("POST", r"/api/proposals/([\w-]+)/revise", h_proposal_revise),
    ("POST", r"/api/proposals/([\w-]+)/vet", h_proposal_vet),
    ("GET", r"/api/proposals/([\w-]+)/vet/stream", h_proposal_vet_stream),
    ("POST", r"/api/proposals/([\w-]+)/vet/cancel", h_proposal_vet_cancel),
    ("POST", r"/api/proposals/([\w-]+)/transition", h_proposal_transition),
    ("POST", r"/api/proposals/([\w-]+)/dyno", h_proposal_dyno_attach),
    ("POST", r"/api/proposals/([\w-]+)/dyno/([\w-]+)/detach", h_proposal_dyno_detach),
    ("POST", r"/api/dyno/run", h_dyno_run),
    ("GET", r"/api/dyno/baseline", h_dyno_baseline),
    ("GET", r"/api/dyno/selftest", h_dyno_selftest),
    ("GET", r"/api/tunes/bridge", h_tune_bridge),
    ("GET", r"/api/tunes/([0-9a-f]{40})/read", h_tune_read),
    ("POST", r"/api/learn", h_learn),
]

if __name__ == "__main__":
    sys.exit(main())
