#!/usr/bin/env python3
"""HTTP API for the ThunderMax tuning assistant.

A thin stdlib wrapper around tune_assistant so a web front-end can drive it.
No pip deps. CORS is open. Answers stream token-by-token as NDJSON (one JSON
object per line) so a browser `fetch()` reader can render them live.

Endpoints (JSON request bodies):
  GET  /api/health   -> {ok, models:{fast,deep}, profile, modes}
  GET  /api/profile  -> the bike-setup profile
  POST /api/ask      {question, mode?}                 -> NDJSON stream
  POST /api/chat     {messages:[{role,content}], mode?} -> NDJSON stream
  POST /api/analyze  {file, baseline?, mode?}          -> NDJSON stream (server-side .tbw path)
  POST /api/learn    {text, title?, setup?}            -> {ok, doc, setup}
  POST /api/sync     {path, baseline?}                 -> {ok, matched, skipped, base_map_ids}

Stream line types: {"type":"model","model":..,"tier":"fast|deep"} then many
{"type":"token","t":".."} then {"type":"done"} (or {"type":"error","error":..}).

Run: python3 src/api_server.py [--host 0.0.0.0] [--port 8181]
"""
import argparse
import io
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import tune_assistant as ta

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

# Static frontend: drop a built web app (Fable5 output) here and it is served
# same-origin alongside /api/* — no CORS/CSP to fight.
WEB_DIR = (Path(__file__).resolve().parent.parent / "web").resolve()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TMaxAssistant/1.0"

    def log_message(self, *a):  # keep the console quiet
        pass

    def _cors(self):
        for k, v in CORS.items():
            self.send_header(k, v)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True  # client reads until EOF

    def _emit(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode())
        self.wfile.flush()

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/health":
            return self._json({
                "ok": True,
                "models": {"fast": ta.FAST_MODEL, "deep": ta.BIG_MODEL},
                "profile": ta.PROFILE,
                "modes": ["auto", "fast", "deep"],
            })
        if path == "/api/profile":
            return self._json(ta.PROFILE)
        if path.startswith("/api/"):
            return self._json({"ok": False, "error": "not found"}, 404)
        return self._serve_static(self.path.split("?")[0])

    def _serve_static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        try:
            target = (WEB_DIR / rel).resolve()
            target.relative_to(WEB_DIR)          # blocks ../ path traversal (post-decode)
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "forbidden"}, 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            idx = WEB_DIR / "index.html"         # SPA fallback for client routing
            if idx.is_file():
                target = idx
            else:
                return self._json({"ok": False, "error": "no frontend deployed "
                                   "(drop files in web/)"}, 404)
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _stream_answer(self, messages, mode, route_q):
        self._begin_stream()
        try:
            model = ta.pick_model(mode, route_q)
            tier = "deep" if model == ta.BIG_MODEL else "fast"
            self._emit({"type": "model", "model": model, "tier": tier})
            for tok in ta.stream_chat(messages, model):
                self._emit({"type": "token", "t": tok})
            self._emit({"type": "done"})
        except Exception as e:
            try:
                self._emit({"type": "error", "error": str(e)})
            except Exception:
                pass

    def do_POST(self):
        try:
            body = self._read_json()
        except Exception as e:
            return self._json({"ok": False, "error": f"bad json: {e}"}, 400)
        path = self.path.split("?")[0].rstrip("/")

        if path == "/api/ask":
            q = (body.get("question") or "").strip()
            if not q:
                return self._json({"ok": False, "error": "question required"}, 400)
            return self._stream_answer(ta.build_messages(q), body.get("mode", "auto"), q)

        if path == "/api/chat":
            msgs = body.get("messages") or []
            if not msgs:
                return self._json({"ok": False, "error": "messages required"}, 400)
            if not any(m.get("role") == "system" for m in msgs):
                msgs = [{"role": "system", "content": ta.SYSTEM_PROMPT}] + msgs
            # ground the first user turn in the corpus, like the CLI chat does
            if not any(m.get("role") == "assistant" for m in msgs):
                for m in reversed(msgs):
                    if m.get("role") == "user":
                        ctx = ta.relevant_context(m["content"])
                        if ctx:
                            m["content"] = f"Reference material:\n\n{ctx}\n\nQuestion: {m['content']}"
                        break
            last_user = next((m["content"] for m in reversed(msgs)
                              if m.get("role") == "user"), "")
            return self._stream_answer(msgs, body.get("mode", "auto"), last_user)

        if path == "/api/analyze":
            f = body.get("file")
            if not f:
                return self._json({"ok": False, "error": "file (server-side .tbw path) required"}, 400)
            try:
                tbw = ta.tmx.TbwFile(f)
                buf = io.StringIO()
                buf.write(f"Tune file: {tbw.path.name}\n")
                for line in tbw.integrity_lines():
                    buf.write(line + "\n")
                if body.get("baseline"):
                    buf.write("\n" + ta._diff_facts(body["baseline"], f) + "\n")
                facts = buf.getvalue()
            except Exception as e:
                return self._json({"ok": False, "error": f"analyze failed: {e}"}, 400)
            question = (
                "Describe ONLY what the tune-diff facts above show was changed - do not "
                "invent features or modes that are not listed. Explain what the rider "
                "should feel and what to validate on the next ride per the house protocol."
                if body.get("baseline") else
                "Explain what this tune analysis means for the rider and what to validate "
                "on the next ride."
            )
            msgs = [
                {"role": "system", "content": ta.SYSTEM_PROMPT},
                {"role": "user", "content":
                    f"Tune-diff facts (authoritative - describe only these):\n\n{facts}\n\n{question}"},
            ]
            return self._stream_answer(msgs, body.get("mode", "deep"), question)

        if path == "/api/learn":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "text required"}, 400)
            title = body.get("title") or " ".join(text.split()[:8])
            doc = ta.learn_write(title, text, source="api", setup=body.get("setup"))
            return self._json({"ok": True, "doc": doc.name,
                               "setup": body.get("setup") or ta.PROFILE.get("setup_key")})

        if path == "/api/sync":
            p = body.get("path")
            if not p:
                return self._json({"ok": False, "error": "path required"}, 400)
            res = ta.sync_folder(p, baseline=body.get("baseline"))
            return self._json(res, 200 if res.get("ok") else 400)

        return self._json({"ok": False, "error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="ThunderMax tuning assistant HTTP API")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8181)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"ThunderMax assistant API -> http://{a.host}:{a.port}"
          f"  (fast={ta.FAST_MODEL}, deep={ta.BIG_MODEL})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
