// Phase 4 — ride journal + learned KB.
import { get, post } from '/js/api.js';
import { el, toast } from '/js/ui.js';

export async function mount(root, rest) {
  if (rest[0] === 'new') return mountNew(root);
  if (rest[0] === 'kb' && rest[1]) return mountKb(root, rest[1]);
  if (rest[0]) return mountNote(root, rest[0]);
  root.append(el('h1', { class: 'page' }, 'Journal'));
  root.append(el('div', { style: 'margin-bottom:12px' },
    el('a', { class: 'btn primary', href: '#/journal/new' }, 'New ride note')));
  const data = await get('/api/journal').catch((e) => {
    root.append(el('div', { class: 'empty' }, String(e.message)));
    return null;
  });
  if (!data) return;
  const notes = el('div', { class: 'card' }, el('h2', {}, 'Ride notes'));
  if (!data.notes.length) notes.append(el('div', { class: 'empty' }, 'No ride notes yet.'));
  else data.notes.forEach((n) => {
    notes.append(el('div', { class: 'safety' },
      el('div', { class: 'row' },
        el('a', { href: `#/journal/${n.id}` }, n.title || n.id),
        el('span', { class: 'muted' }, n.ts || n.kind || ''))));
  });
  const kb = el('div', { class: 'card' }, el('h2', {}, 'Learned KB (this setup)'));
  if (!data.learned.length) kb.append(el('div', { class: 'empty' }, 'No learned docs yet — sync a tune or add a note.'));
  else data.learned.forEach((n) => {
    kb.append(el('div', { class: 'safety' },
      el('div', { class: 'row' },
        el('a', { href: `#/journal/kb/${n.id}` }, n.title),
        el('span', { class: 'badge' }, 'kb'))));
  });
  const sess = el('div', { class: 'card' }, el('h2', {}, 'Co-pilot sessions'));
  (data.sessions || []).slice(0, 8).forEach((s) => {
    sess.append(el('div', { class: 'safety' },
      el('div', { class: 'row' },
        el('a', { href: `#/chat/${s.id}` }, s.title || s.id),
        el('span', { class: 'muted' }, `${s.messages} msg`))));
  });
  root.append(notes, kb, sess);
}

function mountNew(root) {
  root.append(el('h1', { class: 'page' }, 'New ride note'));
  const title = el('input', { placeholder: 'e.g. 19 Aug heat-soak ride' });
  const body = el('textarea', { rows: '8', placeholder: 'What happened: CHT, AFR, pops, what you changed in TMax (you flash — this app never writes .tbw).' });
  const kind = el('select', {},
    el('option', { value: 'ride' }, 'Ride'),
    el('option', { value: 'flash' }, 'Flash / WRITE'),
    el('option', { value: 'note' }, 'Shop note'));
  const btn = el('button', { class: 'primary' }, 'Save to journal + KB');
  btn.onclick = async () => {
    try {
      const r = await post('/api/journal', { title: title.value, body: body.value, kind: kind.value });
      if (r.error) return toast(r.error, 'bad');
      toast('saved');
      location.hash = `#/journal/${r.id}`;
    } catch (e) { toast(e.message, 'bad'); }
  };
  root.append(el('div', { class: 'card' },
    el('label', {}, 'Kind'), kind,
    el('label', {}, 'Title'), title,
    el('label', {}, 'Note'), body,
    el('div', { style: 'margin-top:12px' }, btn)));
}

async function mountNote(root, id) {
  const n = await get(`/api/journal/${id}`).catch(() => null);
  if (!n || n.error) return root.append(el('div', { class: 'empty' }, 'Note not found'));
  root.append(el('h1', { class: 'page' }, n.title || n.id),
    el('div', { class: 'muted' }, `${n.kind || ''} · ${n.ts || ''}`),
    el('div', { class: 'card' }, el('div', { style: 'white-space:pre-wrap' }, n.body || '')));
}

async function mountKb(root, stem) {
  const n = await get(`/api/kb/${stem}`).catch(() => null);
  if (!n || n.error) return root.append(el('div', { class: 'empty' }, 'KB doc not found'));
  root.append(el('h1', { class: 'page' }, n.title),
    el('div', { class: 'card' }, el('pre', { class: 'passage', style: 'white-space:pre-wrap;font:inherit' }, n.body || '')));
}

export function unmount() {}
