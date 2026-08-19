// Dashboard: safety card, bike profile, health tiles, recent sessions, quick actions.
import { get } from '/js/api.js';
import { el, badge, fmtAgo } from '/js/ui.js';

export async function mount(root) {
  root.append(el('h1', { class: 'page' }, 'Dashboard'));
  const grid = el('div', { class: 'grid cols-2' });
  root.append(grid);

  const healthCard = el('div', { class: 'card' }, el('h2', {}, 'Systems'), el('div', { class: 'muted' }, 'checking…'));
  const bikeCard = el('div', { class: 'card' }, el('h2', {}, 'Bike'), el('div', { class: 'muted' }, 'loading…'));
  const safetyCard = el('div', { class: 'card' }, el('h2', {}, 'Safety card — house limits'), el('div', { class: 'muted' }, 'loading…'));
  const recentCard = el('div', { class: 'card' }, el('h2', {}, 'Recent sessions'), el('div', { class: 'muted' }, 'loading…'));
  grid.append(bikeCard, healthCard, safetyCard, recentCard);

  get('/api/profile').then((p) => {
    renderBike(bikeCard, p);
    renderSafety(safetyCard, p.guardrails);
  }).catch(() => { bikeCard.lastChild.textContent = 'profile unavailable'; });

  get('/api/health').then((h) => renderHealth(healthCard, h))
    .catch(() => { healthCard.lastChild.textContent = 'health unavailable'; });

  get('/api/chat/sessions').then(({ sessions }) => {
    recentCard.lastChild.remove();
    if (!sessions.length) return recentCard.append(el('div', { class: 'empty' }, 'No sessions yet — ask the co-pilot something.'));
    const ul = el('div');
    for (const s of sessions.slice(0, 6)) {
      ul.append(el('div', { class: 'safety' },
        el('div', { class: 'row' },
          el('a', { href: `#/chat/${s.id}` }, s.title || s.id),
          el('span', { class: 'muted' }, `${s.messages} msg`))));
    }
    recentCard.append(ul);
  }).catch(() => { recentCard.lastChild.textContent = 'sessions unavailable'; });

  root.append(el('div', { style: 'margin-top:14px;display:flex;gap:10px;flex-wrap:wrap' },
    el('a', { class: 'btn primary', href: '#/chat' }, 'Ask the co-pilot'),
    el('a', { class: 'btn', href: '#/journal/new' }, 'New ride note'),
    el('a', { class: 'btn', href: '#/dyno' }, 'Virtual dyno'),
  ));
}

function renderHealth(card, h) {
  card.lastChild.remove();
  const tiles = el('div', { class: 'tiles' });
  const tile = (k, v, ok) => el('div', { class: `tile ${ok === true ? 'ok' : ok === false ? 'bad' : ''}` },
    el('div', { class: 'k' }, k), el('div', { class: 'v' }, v));
  tiles.append(
    tile('Ollama', h.ollama?.ok ? (h.ollama.version || 'up') : 'down', h.ollama?.ok),
    tile('Elasticsearch', h.es?.ok ? `${h.es.chunks} chunks` : 'down', h.es?.ok),
    tile('NAS', h.nas?.ok ? `${h.nas.tunes_visible} tunes` : 'offline', h.nas?.ok),
    tile('Tune index', h.tune_index?.count ? `${h.tune_index.count} · ${fmtAgo(h.tune_index.age_s)}` : 'empty', h.tune_index?.count > 0),
    tile('Corpus', `${h.corpus_docs} docs`, h.keyword_leg),
    tile('Disk free', `${h.disk_free_gb} GB`, h.disk_free_gb > 5),
  );
  card.append(tiles);
  if (h.ollama?.models) {
    const missing = Object.entries(h.ollama.models).filter(([, ok]) => !ok).map(([t]) => t);
    if (missing.length) card.append(el('div', { class: 'muted', style: 'margin-top:8px' }, `missing models: ${missing.join(', ')}`));
  }
}

function renderBike(card, p) {
  card.lastChild.remove();
  const prof = p.profile;
  const rows = el('div', { class: 'safety' });
  const row = (k, v) => rows.append(el('div', { class: 'row' }, el('span', {}, k), el('span', { class: 'lim' }, v)));
  row('Bike', prof.model || prof.label || '—');
  row('Engine', prof.engine || '—');
  row('ECM', prof.ecm || '—');
  row('Base maps on record', String((prof.base_map_ids || []).length));
  if (prof.injectors) {
    const inj = typeof prof.injectors === 'string' ? prof.injectors
      : `${prof.injectors.flow_g_s || ''} g/s`;
    rows.append(el('div', { class: 'row' }, el('span', {}, 'Injectors'),
      el('span', { class: 'lim' }, inj)));
  }
  if (prof.current_flash_file) {
    row('Flash-first', prof.current_flash_file);
  }
  card.append(rows);
}

function renderSafety(card, g) {
  card.lastChild.remove();
  const rows = el('div', { class: 'safety' });
  const row = (k, v) => rows.append(el('div', { class: 'row' }, el('span', {}, k), el('span', { class: 'lim' }, v)));
  row('WOT AFR', `${g.afr.wot[0]}–${g.afr.wot[1]} (hard ${g.afr.wot_hard[0]}–${g.afr.wot_hard[1]})`);
  row('Cruise / idle AFR', `${g.afr.cruise[0]}–${g.afr.cruise[1]} / ${g.afr.idle[0]}–${g.afr.idle[1]}`);
  row('Rear cylinder', `≈${g.afr.rear_richer} richer · timing ≤ front`);
  row('Spark cruise / WOT', `${g.spark.cruise[0]}–${g.spark.cruise[1]}° / ${g.spark.wot[0]}–${g.spark.wot[1]}°`);
  row('Spark ceiling / step', `${g.spark.ceiling}° / ±${g.spark.max_step}°`);
  row('VE step', `warn > ±${g.ve_step.warn_pct}% · block > ±${g.ve_step.block_pct}%`);
  row('AutoTune gates', `${g.temps_f.autotune_enable}–${g.temps_f.autotune_disable}°F`);
  row('Heat retard knee', `${g.temps_f.heat_retard}°F`);
  row('Injector duty', `amber ${g.injector_duty.amber_pct}% · red ${g.injector_duty.red_pct}%`);
  rows.append(el('div', { class: 'row' }, el('span', {}, '.tbw files'),
    el('span', { class: 'lim' }, 'NEVER written by this app')));
  card.append(rows);
}

export function unmount() {}
