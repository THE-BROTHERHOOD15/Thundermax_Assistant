// Co-pilot chat: streaming answers, inline [n] citations, tier switch, cancel.
// Rendering is idempotent: tokens are kept in a seq-keyed map and the answer is
// rebuilt from it, so a full buffer replay after an iOS resume never duplicates.
import { get, post, openChatStream, rememberInflight, clearInflight, getInflight } from '/js/api.js';
import { el, toast, renderMarkdown, showCitation, badge } from '/js/ui.js';

let stream = null;
let sessionId = null;

export async function mount(root, rest) {
  sessionId = rest[0] || null;
  const log = el('div', { class: 'chat-log' });
  const status = el('div', { class: 'chat-status' });
  const input = el('textarea', { placeholder: 'Ask about the tune… (Enter to send)', rows: 1 });
  const send = el('button', { class: 'primary' }, 'Send');
  const cancel = el('button', { class: 'danger hidden' }, 'Stop');
  const tierWrap = el('div', { class: 'seg' });
  const tiers = ['auto', 'fast', 'smart', 'deep'];
  let tier = localStorage.getItem('tmax-tier') || 'auto';
  for (const t of tiers) {
    const b = el('button', {
      class: t === tier ? 'active' : '',
      onclick: () => {
        tier = t; localStorage.setItem('tmax-tier', t);
        tierWrap.querySelectorAll('button').forEach((x) => x.classList.toggle('active', x.textContent === t));
        if (t === 'deep') toast('Deep = hermes3:70b — ~0.7 tok/s, answers take minutes');
      },
    }, t);
    tierWrap.append(b);
  }

  const compose = el('div', { class: 'chat-compose' }, input, send, cancel);
  root.append(el('div', { class: 'chat' },
    el('div', { style: 'display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding-bottom:6px' },
      el('h1', { class: 'page', style: 'margin:0' }, 'Co-pilot'), tierWrap),
    log, status, compose));

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  send.addEventListener('click', submit);

  if (sessionId) await loadSession(log, sessionId);

  // reattach to a generation that survived a tab-kill / PWA relaunch
  const inflight = getInflight();
  if (inflight && (!sessionId || inflight.sessionId === sessionId)) {
    sessionId = inflight.sessionId;
    if (!rest[0]) await loadSession(log, sessionId);
    const pane = paneFor(log, inflight.messageId);
    attach(inflight.messageId, pane, status, cancel);
  }

  async function submit() {
    const q = input.value.trim();
    if (!q || !send.disabled === false && stream) return;
    input.value = '';
    log.append(el('div', { class: 'msg user' },
      el('div', { class: 'meta' }, 'you'),
      el('div', { class: 'body' }, q)));
    log.scrollTo(0, 1e9);
    try {
      const r = await post('/api/chat/messages', { question: q, session_id: sessionId, tier });
      sessionId = r.session_id;
      history.replaceState(null, '', `#/chat/${sessionId}`);
      rememberInflight(sessionId, r.message_id);
      attach(r.message_id, paneFor(log, r.message_id), status, cancel);
    } catch (e) {
      toast(`send failed: ${e.message}`, 'bad');
    }
  }
}

function paneFor(log, messageId) {
  let pane = log.querySelector(`[data-mid="${messageId}"]`);
  if (!pane) {
    pane = el('div', { class: 'msg assistant', 'data-mid': messageId },
      el('div', { class: 'meta' }, el('span', { class: 'gen-dot' }), 'co-pilot'),
      el('div', { class: 'body' }));
    log.append(pane);
    log.scrollTo(0, 1e9);
  }
  return pane;
}

function attach(messageId, pane, status, cancelBtn) {
  stream?.close();
  const tokens = new Map(); // seq -> token: replays rebuild, never duplicate
  let cites = [];
  const body = pane.querySelector('.body');
  const meta = pane.querySelector('.meta');
  const log = pane.parentElement;
  let renderQueued = false;

  cancelBtn.classList.remove('hidden');
  cancelBtn.onclick = () => post(`/api/chat/messages/${messageId}/cancel`).catch(() => {});

  const render = () => {
    renderQueued = false;
    const text = [...tokens.entries()].sort((a, b) => a[0] - b[0]).map(([, t]) => t).join('');
    body.textContent = '';
    body.append(renderMarkdown(text, { onCite: (n) => { const c = cites.find((x) => x.n === n); if (c) showCitation(c); } }));
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 160;
    if (nearBottom) log.scrollTo(0, 1e9);
  };

  stream = openChatStream(messageId, {
    status: (d) => {
      if (d.phase === 'retrieving') status.textContent = 'searching the knowledge base…';
      else if (d.phase === 'degraded') { status.textContent = d.detail; toast(d.detail, 'bad'); }
      else if (d.phase === 'model') {
        status.textContent = `${d.model} (${d.tier}) — ${d.reason}`;
        meta.append(' ', badge(`${d.tier} · ${d.model}`));
      } else if (d.phase === 'queued') status.textContent = 'queued — another generation holds the slot…';
      else if (d.phase === 'generating') status.textContent = 'generating…';
    },
    citations: (d) => {
      cites = d.citations;
      if (cites.length) meta.append(' ', badge(`${cites.length} sources`));
    },
    token: (d, seq) => {
      tokens.set(seq ?? tokens.size, d.t);
      if (!renderQueued) { renderQueued = true; requestAnimationFrame(render); }
    },
    done: (d) => {
      clearInflight();
      status.textContent = d.elapsed_s ? `done in ${d.elapsed_s}s` : '';
      pane.querySelector('.gen-dot')?.remove();
      cancelBtn.classList.add('hidden');
      if (!tokens.size) backfill();  // resumed after the buffer expired
    },
    error: (d) => {
      clearInflight();
      pane.querySelector('.gen-dot')?.remove();
      cancelBtn.classList.add('hidden');
      if (d.code === 'expired') return backfill();
      status.textContent = '';
      body.append(el('div', { class: 'muted' }, `⚠ ${d.message || d.code}`));
    },
  });

  async function backfill() {
    try {
      const s = await get(`/api/chat/sessions/${sessionId || ''}`);
      const m = s.messages?.find((x) => x.message_id === messageId);
      if (m?.a) {
        cites = m.citations || [];
        body.textContent = '';
        body.append(renderMarkdown(m.a, { onCite: (n) => { const c = cites.find((x) => x.n === n); if (c) showCitation(c); } }));
        status.textContent = '';
      }
    } catch { /* leave whatever streamed */ }
  }
}

async function loadSession(log, sid) {
  try {
    const s = await get(`/api/chat/sessions/${sid}`);
    for (const m of s.messages || []) {
      log.append(el('div', { class: 'msg user' },
        el('div', { class: 'meta' }, 'you'),
        el('div', { class: 'body' }, m.q)));
      if (m.a) {
        const cites = m.citations || [];
        const pane = el('div', { class: 'msg assistant', 'data-mid': m.message_id },
          el('div', { class: 'meta' }, 'co-pilot', ' ', m.model ? badge(m.model) : ''),
          el('div', { class: 'body' }));
        pane.querySelector('.body').append(
          renderMarkdown(m.a, { onCite: (n) => { const c = cites.find((x) => x.n === n); if (c) showCitation(c); } }));
        log.append(pane);
      }
    }
    log.scrollTo(0, 1e9);
  } catch { /* fresh session */ }
}

export function unmount() {
  stream?.close();
  stream = null;
}
