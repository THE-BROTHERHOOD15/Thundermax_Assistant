// Phase 6 — virtual dyno gauges from house limits. Not live ECU.
import { get } from '/js/api.js';
import { el } from '/js/ui.js';

export async function mount(root) {
  root.append(el('h1', { class: 'page' }, 'Virtual dyno'));
  const hint = el('div', { class: 'muted', style: 'margin-bottom:10px' },
    'House-limit model — not live telemetry. Apply in TMax Tuner. This app never writes .tbw.');
  const controls = el('div', { class: 'card', style: 'display:flex;gap:16px;flex-wrap:wrap;align-items:end' });
  const rpm = el('input', { type: 'range', min: '900', max: '5600', value: '2500' });
  const tps = el('input', { type: 'range', min: '0', max: '100', value: '20' });
  const cht = el('input', { type: 'range', min: '160', max: '360', value: '240' });
  const rpmL = el('span', { class: 'mono' }, '2500');
  const tpsL = el('span', { class: 'mono' }, '20');
  const chtL = el('span', { class: 'mono' }, '240');
  controls.append(
    el('div', {}, el('label', {}, 'RPM ', rpmL), rpm),
    el('div', {}, el('label', {}, 'TPS % ', tpsL), tps),
    el('div', {}, el('label', {}, 'CHT °F ', chtL), cht),
  );
  const gauges = el('div', { class: 'gauges' });
  const notes = el('div', { class: 'card' });
  root.append(hint, controls, gauges, notes);

  async function refresh() {
    rpmL.textContent = rpm.value;
    tpsL.textContent = tps.value;
    chtL.textContent = cht.value;
    const d = await get(`/api/dyno?rpm=${rpm.value}&tps=${tps.value}&cht=${cht.value}`);
    gauges.textContent = '';
    (d.gauges || []).forEach((g) => gauges.append(gaugeEl(g)));
    notes.textContent = '';
    notes.append(
      el('h2', {}, 'Shop facts'),
      el('div', {}, d.mode),
      el('div', { class: 'safety' },
        el('div', { class: 'row' }, el('span', {}, 'AutoTune window'),
          el('span', { class: 'lim' }, d.autotune_window ? '200–280°F OK' : 'OUTSIDE — do not lock trims')),
        el('div', { class: 'row' }, el('span', {}, 'Belly derate'),
          el('span', { class: 'lim' }, d.belly ? 'on (knock-prone midrange)' : 'off')),
        el('div', { class: 'row' }, el('span', {}, 'Flash-first'),
          el('span', { class: 'lim' }, d.flash_first || '')),
        el('div', { class: 'row' }, el('span', {}, 'Do not flash'),
          el('span', { class: 'lim' }, d.no_flash || '')),
      ),
    );
  }
  rpm.oninput = tps.oninput = cht.oninput = refresh;
  await refresh();
}

function gaugeEl(g) {
  const pct = Math.max(0, Math.min(100, ((g.value - g.min) / (g.max - g.min)) * 100));
  const tone = g.value >= (g.red || 1e9) ? 'bad' : (g.value >= (g.warn || 1e9) ? 'warn' : 'ok');
  return el('div', { class: `card gauge ${tone}` },
    el('div', { class: 'k' }, g.name),
    el('div', { class: 'v' }, `${g.value}${g.unit || ''}`),
    el('div', { class: 'bar' }, el('div', { class: 'fill', style: `width:${pct}%` })),
    el('div', { class: 'muted' }, g.note || ''));
}

export function unmount() {}
