// All network I/O for the app. Fetch helpers + the resumable chat stream.

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { window.dispatchEvent(new Event('tmax-auth-needed')); throw new Error('auth required'); }
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).error || msg; } catch { /* keep status */ }
    throw new Error(msg);
  }
  return res.json();
}

export const get = (p) => api(p);
export const post = (p, body) => api(p, { method: 'POST', body });
export const patch = (p, body) => api(p, { method: 'PATCH', body });

// --- resumable chat stream ---------------------------------------------------
// The vetted iOS-survival protocol: a NEW EventSource never sends Last-Event-ID,
// so on any wake signal (visibilitychange, pageshow, focus) we unconditionally
// close the possibly-zombie source and open a fresh one; the server replays the
// whole buffer from seq 0 and the consumer renders idempotently keyed by seq.
// Native auto-reconnect (which DOES send Last-Event-ID) still trims replays on
// transient drops. `done`/`error` are terminal: stop, never retry.

export function openChatStream(messageId, handlers) {
  let es = null;
  let closed = false;

  const wake = debounce(() => { if (!closed) connect(); }, 400);

  function connect() {
    if (es) { try { es.close(); } catch { /* already dead */ } }
    es = new EventSource(`/api/chat/stream/${messageId}`);
    for (const ev of ['status', 'citations', 'token', 'done', 'error']) {
      es.addEventListener(ev, (e) => {
        let data = {};
        try { data = JSON.parse(e.data); } catch { /* ignore */ }
        const seq = e.lastEventId !== '' ? Number(e.lastEventId) : null;
        if (ev === 'done' || ev === 'error') close();
        handlers[ev]?.(data, seq);
      });
    }
    es.onerror = () => { /* native reconnect handles transient drops */ };
  }

  function onVisible() { if (document.visibilityState === 'visible') wake(); }

  function close() {
    if (closed) return;
    closed = true;
    try { es?.close(); } catch { /* noop */ }
    document.removeEventListener('visibilitychange', onVisible);
    window.removeEventListener('pageshow', wake);
    window.removeEventListener('focus', wake);
  }

  document.addEventListener('visibilitychange', onVisible);
  window.addEventListener('pageshow', wake);
  window.addEventListener('focus', wake);
  connect();
  return { close };
}

// Track the in-flight message across tab-kill / PWA relaunch.
const INFLIGHT_KEY = 'tmax-inflight';
export function rememberInflight(sessionId, messageId) {
  localStorage.setItem(INFLIGHT_KEY, JSON.stringify({ sessionId, messageId }));
}
export function clearInflight() { localStorage.removeItem(INFLIGHT_KEY); }
export function getInflight() {
  try { return JSON.parse(localStorage.getItem(INFLIGHT_KEY)); } catch { return null; }
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
