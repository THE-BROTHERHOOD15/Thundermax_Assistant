// Phase 5 — proposals + deterministic guardrail vetting. LLM never hard-blocks.
import { get, post } from '/js/api.js';
import { el, toast, badge } from '/js/ui.js';

const TABLES = [
  'afr_target', 've_front', 've_rear', 'fuel_flow_front', 'fuel_flow_rear',
  'spark_advance_front', 'spark_advance_rear', 'rear_timing_offset',
  'decel_fuel_cut', 'autotune_zones', 'idle_rpm',
];

export async function mount(root, rest) {
  if (rest[0] === 'new') return mountNew(root);
  root.append(el('h1', { class: 'page' }, 'Proposals'));
  root.append(el('div', { class: 'muted', style: 'margin-bottom:10px' },
    'You apply changes in TMax Tuner. This app never writes .tbw files. Only guardrail code can hard-block.'));
  root.append(el('a', { class: 'btn primary', href: '#/proposals/new', style: 'margin-bottom:12px' }, 'New proposal'));
  const data = await get('/api/proposals').catch((e) => {
    root.append(el('div', { class: 'empty' }, String(e.message)));
    return null;
  });
  if (!data) return;
  const list = data.proposals || [];
  if (!list.length) {
    root.append(el('div', { class: 'empty' }, 'No proposals yet.'));
    return;
  }
  for (const p of list) root.append(renderProposal(p));
}

function renderProposal(p) {
  const vet = p.vet || {};
  const tone = !vet.passed ? 'bad' : (vet.warns ? 'warn' : 'ok');
  const card = el('div', { class: 'card' });
  card.append(
    el('div', { class: 'row', style: 'display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap' },
      el('strong', {}, p.title),
      badge(p.status || 'open', tone)),
    el('div', { class: 'muted' }, p.claim || ''),
    el('div', { class: 'muted' }, `${(p.changes || []).length} change(s) · blocks ${vet.blocks || 0} · warns ${vet.warns || 0}`),
  );
  (vet.findings || []).forEach((f) => {
    card.append(el('div', { class: `muted` }, `${f.severity}: ${f.message}`));
  });
  const row = el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-top:10px' });
  const applied = el('button', {}, 'Mark applied in TMax');
  applied.onclick = async () => {
    try {
      const r = await post(`/api/proposals/${p.id}/status`, { status: 'applied-in-tmax' });
      if (r.error) return toast(r.error, 'bad');
      toast('marked applied (you flashed it — not this app)');
      location.reload();
    } catch (e) { toast(e.message, 'bad'); }
  };
  const reject = el('button', { class: 'danger' }, 'Reject');
  reject.onclick = async () => {
    try {
      await post(`/api/proposals/${p.id}/status`, { status: 'rejected' });
      location.reload();
    } catch (e) { toast(e.message, 'bad'); }
  };
  row.append(applied, reject);
  card.append(row);
  return card;
}

function mountNew(root) {
  root.append(el('h1', { class: 'page' }, 'New proposal'));
  const title = el('input', { placeholder: 'e.g. rear timing −1° above 70% TPS' });
  const claim = el('textarea', { rows: '3', placeholder: 'Why. Cite a ride note or log. You will apply this in TMax Tuner.' });
  const table = el('select', {}, ...TABLES.map((t) => el('option', { value: t }, t)));
  const dir = el('select', {},
    el('option', { value: 'decrease' }, 'decrease'),
    el('option', { value: 'increase' }, 'increase'));
  const mag = el('input', { type: 'number', step: '0.1', value: '1', style: 'width:6rem' });
  const unit = el('select', {},
    el('option', { value: 'deg' }, 'deg'),
    el('option', { value: 've_pct' }, 've_pct'),
    el('option', { value: 'afr' }, 'afr'));
  const rpmLo = el('input', { type: 'number', value: '3840', style: 'width:6rem' });
  const rpmHi = el('input', { type: 'number', value: '4608', style: 'width:6rem' });
  const tpsLo = el('input', { type: 'number', value: '0', style: 'width:5rem' });
  const tpsHi = el('input', { type: 'number', value: '2', style: 'width:5rem' });
  const btn = el('button', { class: 'primary' }, 'Vet with guardrails');
  btn.onclick = async () => {
    const change = {
      table: table.value,
      cylinder: 'both',
      rpm_band: [Number(rpmLo.value), Number(rpmHi.value)],
      tps_band: [Number(tpsLo.value), Number(tpsHi.value)],
      direction: dir.value,
      magnitude: Number(mag.value),
      unit: unit.value,
      claim: claim.value,
    };
    try {
      const r = await post('/api/proposals', {
        title: title.value, claim: claim.value, changes: [change],
      });
      if (r.vet && !r.vet.passed) toast('blocked by guardrails — not applied', 'bad');
      else toast(r.vet && r.vet.warns ? 'passed with warnings' : 'passed');
      location.hash = '#/proposals';
    } catch (e) { toast(e.message, 'bad'); }
  };
  root.append(el('div', { class: 'card' },
    el('label', {}, 'Title'), title,
    el('label', {}, 'Claim'), claim,
    el('label', {}, 'Table (TMax page)'), table,
    el('div', { class: 'row', style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:end' },
      el('div', {}, el('label', {}, 'Dir'), dir),
      el('div', {}, el('label', {}, 'Magnitude'), mag),
      el('div', {}, el('label', {}, 'Unit'), unit)),
    el('div', { class: 'row', style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:end' },
      el('div', {}, el('label', {}, 'RPM lo'), rpmLo),
      el('div', {}, el('label', {}, 'RPM hi'), rpmHi),
      el('div', {}, el('label', {}, 'TPS lo'), tpsLo),
      el('div', {}, el('label', {}, 'TPS hi'), tpsHi)),
    el('div', { style: 'margin-top:12px' }, btn),
    el('div', { class: 'muted', style: 'margin-top:8px' },
      'Never writes .tbw. Spark >±2°/step and VE >±5% hard-block. Rear timing cannot advance.')));
}

export function unmount() {}
