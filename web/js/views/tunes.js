// Tune library (cached NAS index) + visual region diff labeled by table band.
import { get, post } from '/js/api.js';
import { el, toast, badge } from '/js/ui.js';

let selected = []; // [sha1, sha1]

export async function mount(root, rest) {
  if (rest[0] === 'diff' && rest[1] && rest[2]) return mountDiff(root, rest[1], rest[2]);
  selected = [];
  root.append(el('h1', { class: 'page' }, 'Tune library'));
  const bar = el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px' });
  const listCard = el('div', { class: 'card' }, el('div', { class: 'muted' }, 'loading…'));
  root.append(bar, listCard);
  await renderList(bar, listCard);
}

async function renderList(bar, card) {
  const idx = await get('/api/tunes').catch(() => null);
  bar.textContent = '';
  card.textContent = '';
  const refresh = el('button', {
    onclick: async () => {
      try {
        await post('/api/tunes/refresh');
        toast('Refreshing from NAS in the background…');
        setTimeout(() => renderList(bar, card), 4000);
      } catch (e) { toast(`refresh: ${e.message}`, 'bad'); }
    },
  }, '⟳ Refresh from NAS');
  const syncBtn = el('button', {
    onclick: async () => {
      try {
        const r = await post('/api/tunes/sync');
        if (!r.ok) return toast(r.error, 'bad');
        toast(`synced: ${r.matched.length} matched my setup → KB`);
      } catch (e) { toast(`sync: ${e.message}`, 'bad'); }
    },
  }, 'Sync my tunes → KB');
  const diffBtn = el('button', { class: 'primary', disabled: '' }, 'Diff selected');
  diffBtn.onclick = () => { if (selected.length === 2) location.hash = `#/tunes/diff/${selected[0]}/${selected[1]}`; };
  bar.append(refresh, syncBtn, diffBtn);

  if (!idx) return card.append(el('div', { class: 'empty' }, 'Library unavailable.'));
  const stale = idx.nas_ok === false;
  bar.append(el('span', { class: 'muted', style: 'font-size:12px' },
    idx.refreshed_at ? `indexed ${idx.refreshed_at}` : 'never indexed'));
  if (stale) bar.append(badge(`NAS offline — cached index (${idx.error || ''})`, 'warn'));

  const tunes = idx.tunes || [];
  if (!tunes.length) {
    return card.append(el('div', { class: 'empty' },
      'No tunes indexed yet. The NAS tunes folder was not reachable at its documented path — ',
      'wake the NAS (or set TMAX_TUNES_DIR to the new location) and hit Refresh.'));
  }
  const wrap = el('div', { class: 'scroll-x' });
  const table = el('table', { class: 'data' },
    el('tr', {}, el('th', {}, ''), el('th', {}, 'file'), el('th', {}, 'base map'),
      el('th', {}, 'mine'), el('th', {}, 'valid'), el('th', {}, 'modified')));
  for (const t of tunes) {
    const cb = el('input', { type: 'checkbox' });
    cb.addEventListener('change', () => {
      if (cb.checked) selected.push(t.sha1);
      else selected = selected.filter((s) => s !== t.sha1);
      while (selected.length > 2) {
        const drop = selected.shift();
        const box = table.querySelector(`input[data-sha="${drop}"]`);
        if (box) box.checked = false;
      }
      diffBtn.disabled = selected.length !== 2;
    });
    cb.dataset.sha = t.sha1 || '';
    table.append(el('tr', {},
      el('td', {}, t.sha1 ? cb : ''),
      el('td', {}, t.name),
      el('td', { class: 'mono' }, t.base_map_id || t.error || '—'),
      el('td', {}, t.mine ? badge('my setup', 'ok') : ''),
      el('td', {}, t.valid ? '✓' : el('span', { class: 'badge bad' }, 'invalid')),
      el('td', { class: 'muted' }, t.mtime ? new Date(t.mtime * 1000).toLocaleDateString() : '—')));
  }
  wrap.append(table);
  card.append(wrap);
}

async function mountDiff(root, a, b) {
  root.append(el('h1', { class: 'page' }, 'Tune diff ',
    el('a', { href: '#/tunes', class: 'muted', style: 'font-size:13px' }, '← library')));
  const card = el('div', { class: 'card' }, el('div', { class: 'muted' }, 'diffing…'));
  root.append(card);
  let d;
  try { d = await get(`/api/tunes/diff?a=${a}&b=${b}`); }
  catch (e) { card.textContent = `diff failed: ${e.message}`; return; }
  card.textContent = '';
  if (d.error) return card.append(el('div', { class: 'empty' }, d.error));

  card.append(el('div', { class: 'muted', style: 'margin-bottom:10px' },
    `A ${d.a.base_map_id} → B ${d.b.base_map_id}`));
  if (d.identical) return card.append(el('div', { class: 'empty' }, 'The two tunes are byte-identical.'));

  const tiles = el('div', { class: 'tiles', style: 'margin-bottom:14px' });
  const order = Object.entries(d.summary).sort((x, y) => y[1].bytes - x[1].bytes);
  for (const [cat, v] of order) {
    tiles.append(el('div', { class: 'tile' },
      el('div', { class: `k cat cat-${cat}` }, cat),
      el('div', { class: 'v' }, `${v.bytes} B`),
      el('div', { class: 'muted', style: 'font-size:11px' },
        `${v.regions} region${v.regions === 1 ? '' : 's'} · ${v.confidences.filter((c) => c !== '-').join(', ') || 'unmapped'}`)));
  }
  card.append(tiles);

  const wrap = el('div', { class: 'scroll-x' });
  const table = el('table', { class: 'data' },
    el('tr', {}, el('th', {}, 'offset'), el('th', {}, 'table'), el('th', {}, 'category'),
      el('th', {}, 'conf'), el('th', {}, 'bytes'), el('th', {}, 'cells'),
      el('th', {}, 'Δ min'), el('th', {}, 'Δ max'), el('th', {}, 'Δ mode')));
  for (const r of d.rows) {
    const conf = r.confidence && r.confidence !== '-' ? badge(r.confidence, `conf-${r.confidence}`) : '';
    table.append(el('tr', {},
      el('td', { class: 'mono' }, r.offset_hex),
      el('td', {}, r.band || el('span', { class: 'muted' }, 'unmapped')),
      el('td', { class: `cat cat-${r.category}` }, r.category),
      el('td', {}, conf),
      el('td', { class: 'mono' }, String(r.changed_bytes)),
      el('td', { class: 'mono' }, r.cells != null ? String(r.cells) : '—'),
      el('td', { class: 'mono' }, r.delta_min != null ? String(r.delta_min) : '—'),
      el('td', { class: 'mono' }, r.delta_max != null ? String(r.delta_max) : '—'),
      el('td', { class: 'mono' }, r.delta_mode != null ? String(r.delta_mode) : '—')));
  }
  wrap.append(table);
  card.append(wrap,
    el('div', { class: 'muted', style: 'font-size:12px;margin-top:10px' },
      'Raw cell values are not yet in engineering units — read direction and location, not absolute numbers. ',
      'AUTOTUNE and METADATA churn on every ride/save and are usually not deliberate edits.'));
}

export function unmount() {}
