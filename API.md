# ThunderMax Assistant — Frontend API

The live shop UI is **TMax Command Center** (`src/webui_server.py` + `web/`),
served by the **`tmax-api`** systemd unit. Stdlib only. Never writes `.tbw`.
`:8090` is AI Operator — do not bind this app there.

`src/api_server.py` is the older NDJSON API. Command Center keeps those
endpoints for compatibility and adds chat sessions, tune library, journal,
proposals, and virtual dyno.

## Base URL

Reachable from LAN or Tailscale (internet is firewalled off):

| From | URL |
|------|-----|
| the tower itself | `http://127.0.0.1:8181` |
| LAN | `http://192.168.1.201:8181/` |

The SPA is served same-origin from `web/` (`index.html`, `web/css/`, `web/js/`).
Use a relative API base (`""`). After changing files under `web/`, no restart
is needed (static files are read per request); after editing `src/`,
`sudo systemctl restart tmax-api`.

> A **claude.ai Artifact** frontend can't reach the tower (sandbox CSP blocks
> external hosts) — hosting it here on the tower sidesteps that entirely.

## Command Center endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET  | `/api/health` | models, profile, ollama/ES status |
| GET  | `/api/profile` | bike-setup profile |
| POST | `/api/chat/messages` | start a chat turn |
| GET  | `/api/chat/stream/{id}` | **SSE** tokens |
| GET  | `/api/tunes` | library (USB + Desktop + NAS) |
| GET  | `/api/tunes/diff` | visual/table-classified diff |
| GET/POST | `/api/journal` | shop notes |
| GET/POST | `/api/proposals` | proposed edits |
| POST | `/api/proposals/{id}/vet` | `guardrails.py` verdict |
| GET  | `/api/dyno` | house-limit virtual dyno (not live ECU) |

Brick-the-bike shop facts (`shop_override`) short-circuit the LLM: never flash
`17AUG…v6TODAY` onto 6.3 injectors; do not lock 330–345°F CH-home AutoTune
trims (above the 280°F gate).

## Legacy NDJSON endpoints (still served)

| Method | Path | Body | Returns |
|--------|------|------|---------|
| POST | `/api/ask`     | `{question, mode?}` | **NDJSON stream** |
| POST | `/api/chat`    | `{messages:[{role,content}], mode?}` | **NDJSON stream** |
| POST | `/api/analyze` | `{file, baseline?, mode?}` | **NDJSON stream** |
| POST | `/api/learn`   | `{text, title?, setup?}` | `{ok, doc, setup}` |
| POST | `/api/sync`    | `{path, baseline?}` | `{ok, matched, skipped, base_map_ids}` |

- `mode` (optional): `"auto"` (default — classifier picks), `"fast"` (14B, snappy),
  `"deep"` (70B, slow but smarter). `analyze` defaults to `"deep"`.
- `analyze`/`sync` take **server-side paths** (the tower reads the `.tbw` files;
  it never writes them).

## Streaming format (NDJSON)

`/api/ask`, `/api/chat`, `/api/analyze` stream **one JSON object per line**:

```
{"type":"model","model":"hermes3:70b","tier":"deep"}   ← first: which model answered
{"type":"token","t":"The "}                            ← many of these
{"type":"token","t":"rear "}
{"type":"done"}                                        ← end (or {"type":"error","error":"..."})
```

Show a ⚡ (fast) or 🧠 (deep) badge from the first `model` line, append each
`token.t`, stop on `done`.

## Copy-paste: streaming ask (works with POST + fetch)

```js
const API = "http://192.168.1.201:8181";      // or 127.0.0.1 on the tower

async function ask(question, { mode = "auto", onModel, onToken, onDone } = {}) {
  const res = await fetch(`${API}/api/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, mode }),
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === "model") onModel?.(msg);       // {model, tier}
      else if (msg.type === "token") onToken?.(msg.t);
      else if (msg.type === "done") onDone?.();
      else if (msg.type === "error") throw new Error(msg.error);
    }
  }
}

// usage
ask("why do I get decel pops above 4k rpm?", {
  onModel: m => setBadge(m.tier),           // "fast" | "deep"
  onToken: t => appendToChat(t),
  onDone:  () => finishMessage(),
});
```

Non-streaming calls are plain JSON:

```js
await fetch(`${API}/api/learn`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text: "Cruise 2600 rpm best at 13.9 AFR", title: "cruise AFR" }),
}).then(r => r.json());   // -> {ok:true, doc:"...", setup:"m8-131-..."}
```

## Service control

```bash
sudo systemctl status tmax-api      # health
sudo systemctl restart tmax-api     # after editing src/
journalctl -u tmax-api -f           # logs
```
