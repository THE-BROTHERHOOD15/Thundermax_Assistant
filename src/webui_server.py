#!/usr/bin/env python3
"""TMax Command Center — web front end for the ThunderMax tuning assistant.

Serves the SPA from web/ plus a JSON API, with resumable SSE chat streaming.
Stdlib only, one process. Supersedes api_server.py (:8181) — Phase 7 retires
that service and this server keeps its NDJSON endpoints for compatibility.

Run: python3 src/webui_server.py [--host 0.0.0.0] [--port 8181]
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


def h_tune_read(h, sha1):
    q = parse_qs(urlparse(h.path).query)
    full = (q.get("full") or ["0"])[0] in ("1", "true", "yes")
    vs = (q.get("vs") or [""])[0]
    d = core.tune_read(sha1, full=full)
    if not d.get("ok"):
        return h._json(d, 404)
    if vs and len(vs) == 40:
        other = core.tune_read(vs)
        if other.get("ok"):
            try:
                import tbw_read
                da = tbw_read.read_tune(str(core.cache_path(sha1)))
                db = tbw_read.read_tune(str(core.cache_path(vs)))
                d["vs"] = vs
                d["afr_diff"] = tbw_read.afr_diff(da, db)
            except Exception as e:
                d["afr_diff_error"] = str(e)
    h._json(d)


def h_claimed_read(h):
    d = core.claimed_tune_read()
    h._json(d, 200 if d.get("ok") else 404)


def h_tune_propose(h, sha1):
    q = parse_qs(urlparse(h.path).query)
    kind = (q.get("kind") or ["afr-audit"])[0]
    if kind not in ("afr-audit", "decel-pop", "all"):
        kind = "afr-audit"
    d = core.tune_read(sha1)
    if not d.get("ok"):
        return h._json(d, 404)
    import tbw_read
    decoded = tbw_read.read_tune(str(core.cache_path(sha1)))
    h._json(tbw_read.propose_from_read(decoded, kind))


def h_tunes_diff(h):
    q = parse_qs(urlparse(h.path).query)
    a, b = (q.get("a") or [""])[0], (q.get("b") or [""])[0]
    if not (a and b):
        return h._json({"error": "a and b sha1 params required"}, 400)
    h._json(core.diff_tunes(a, b))


def h_tunes_sync(h):
    h._json(core.sync_tunes_from_cache())


def h_journal(h):
    h._json(core.list_journal())


def h_journal_add(h):
    b = h._body()
    obj = core.add_journal(b.get("title"), b.get("body"), b.get("kind") or "ride")
    h._json(obj, 400 if obj.get("error") else 200)


def h_journal_one(h, nid):
    n = core.journal_note(nid)
    h._json(n if n else {"error": "not found"}, 200 if n else 404)


def h_kb_one(h, stem):
    d = core.kb_doc(stem)
    h._json(d if d else {"error": "not found"}, 200 if d else 404)


def h_proposals(h):
    h._json({"proposals": core.list_proposals()})


def h_proposal_add(h):
    b = h._body()
    h._json(core.add_proposal(b.get("title"), b.get("claim"), b.get("changes") or []))


def h_proposal_vet(h, pid):
    b = h._body() if h.command == "POST" else {}
    obj = core.vet_proposal(pid, b.get("changes"))
    h._json(obj if obj else {"error": "not found"}, 200 if obj else 404)


def h_proposal_status(h, pid):
    b = h._body()
    obj = core.set_proposal_status(pid, (b.get("status") or "").strip())
    if obj is None:
        return h._json({"error": "not found"}, 404)
    h._json(obj, 400 if obj.get("error") else 200)


def h_dyno(h):
    q = parse_qs(urlparse(h.path).query)
    rpm = (q.get("rpm") or ["2500"])[0]
    tps = (q.get("tps") or ["20"])[0]
    cht = (q.get("cht") or ["240"])[0]
    h._json(core.dyno_snapshot(rpm, tps, cht))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8181)
    args = ap.parse_args(argv)

    core.ensure_dirs()
    core.KEYWORD_LEG_OK = core.docs_dir_is_local()
    if not core.KEYWORD_LEG_OK:
        print(f"WARNING: corpus resolved to {ta.DOCS_DIR} (NAS!) — keyword "
              "retrieval DISABLED to keep CIFS out of the request path.",
              file=sys.stderr)

    core.start_index_refresh()   # non-blocking; index serves stale meanwhile
    core.start_index_timer()

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
    ("GET", r"/api/tunes/claimed/read", h_claimed_read),
    ("GET", r"/api/tunes/([0-9a-f]{40})/read", h_tune_read),
    ("GET", r"/api/tunes/([0-9a-f]{40})/propose", h_tune_propose),
    ("GET", r"/api/tunes/([0-9a-f]{40})", h_tune),
    ("GET", r"/api/tunes/diff", h_tunes_diff),
    ("POST", r"/api/tunes/sync", h_tunes_sync),
    ("GET", r"/api/journal", h_journal),
    ("POST", r"/api/journal", h_journal_add),
    ("GET", r"/api/journal/([\w-]+)", h_journal_one),
    ("GET", r"/api/kb/([\w.-]+)", h_kb_one),
    ("GET", r"/api/proposals", h_proposals),
    ("POST", r"/api/proposals", h_proposal_add),
    ("POST", r"/api/proposals/([\w-]+)/vet", h_proposal_vet),
    ("POST", r"/api/proposals/([\w-]+)/status", h_proposal_status),
    ("GET", r"/api/dyno", h_dyno),
]

if __name__ == "__main__":
    sys.exit(main())
