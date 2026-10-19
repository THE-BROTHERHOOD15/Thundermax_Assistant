// Proposals — the vetting pipeline for tuning changes.
//
// Nothing here touches the ECM or writes .tbw. A proposal is a reviewed
// recommendation that Joshua then types into TMax Tuner BY HAND, so the whole
// screen is built around one job: make the safety state impossible to misread.
//
// Two rules drive the design:
//   1. Only deterministic code (src/guardrails.py) can hard-block. The
//      adversarial LLM reviewer can shout OBJECT, and that is rendered loudly,
//      but it is explicitly NOT a block and never gates a button.
//   2. Per-cell .tbw scaling is unconfirmed, so the app knows the DELTA but not
//      the resulting ABSOLUTE cell value. Every check that needed the absolute
//      value counts into checks_unverifiable, and approval of such a proposal
//      requires an explicit "I will confirm in TMax Tuner" acknowledgment.
//
// Security: model/user text never goes through innerHTML. Nodes are built with
// el()/textContent; the adversarial reviewer's prose goes through renderMarkdown().
import { get, post, debounce } from '/js/api.js';
import { el, toast, badge, renderMarkdown } from '/js/ui.js';

// --- state machine -----------------------------------------------------------

const STATES = ['draft', 'vetted', 'approved', 'applied_on_bike', 'validated_by_ride'];
const STATE_LABEL = {
  draft: 'draft',
  vetted: 'vetted',
  approved: 'approved',
  applied_on_bike: 'applied on bike',
  validated_by_ride: 'validated by ride',
  rejected: 'rejected',
};
const STATE_KIND = {
  draft: '', vetted: 'ok', approved: 'ok', applied_on_bike: 'warn',
  validated_by_ride: 'ok', rejected: 'bad',
};
const STATE_ORDER = [...STATES, 'rejected'];

const VET_STAGES = [
  ['guardrails', 'Guardrails', 'deterministic code — the only hard-block authority'],
  ['citations', 'Citations', 'does the corpus actually support the claim?'],
  ['adversarial', 'Adversarial review', 'a bigger model tries to refute it — advisory only'],
];

// Tier pinning: a proposal is refuted by a model at least one tier ABOVE it.
// The 14b is never allowed to be the refuter.
const REFUTER = {
  fast: 'the deep 70b',
  smart: 'the deep 70b',
  deep: 'the 32b',
};

const FALLBACK_TABLES = [
  'afr_target', 've_front', 've_rear', 'fuel_flow_front', 'fuel_flow_rear',
  'spark_advance_front', 'spark_advance_rear', 'rear_timing_offset',
  'decel_fuel_cut', 'autotune_zones', 'idle_rpm',
];
const SPARK_TABLES = ['spark_advance_front', 'spark_advance_rear', 'rear_timing_offset'];
const VE_TABLES = ['ve_front', 've_rear', 'fuel_flow_front', 'fuel_flow_rear'];
const UNITS = [
  ['deg', 'degrees advance'],
  ['ve_pct', '% VE / fuel'],
  ['afr', 'AFR points'],
];

let stream = null;      // live vet SSE
let pollTimer = null;   // belt-and-braces fallback while vetting

// --- entry point -------------------------------------------------------------

export async function mount(root, rest) {
  ensureStyles();
  if (rest[0] === 'new') return mountForm(root);
  if (rest[0]) return mountDetail(root, rest[0]);
  return mountList(root);
}

export function unmount() {
  closeStream();
}

function closeStream() {
  try { stream?.close(); } catch { /* already dead */ }
  stream = null;
  clearInterval(pollTimer);
  pollTimer = null;
}

// The app shell owns index.html; this only adds the stylesheet if it is not
// already linked there, so the view is never unstyled. Guarded — never doubles.
function ensureStyles() {
  if (document.querySelector('link[href="/css/proposals.css"]')) return;
  document.head.append(el('link', { rel: 'stylesheet', href: '/css/proposals.css' }));
}

// --- shared readers ----------------------------------------------------------
// The vet report shape is read defensively: blocks/warns may arrive as arrays
// (API contract) or as counts (guardrails.check_proposal returns ints). Either
// way the gate below counts correctly — a mis-read here would be a safety bug.

function nBlocks(r) {
  if (!r) return 0;
  return Array.isArray(r.blocks) ? r.blocks.length : Number(r.blocks) || 0;
}
function nWarns(r) {
  if (!r) return 0;
  return Array.isArray(r.warns) ? r.warns.length : Number(r.warns) || 0;
}
function blockList(r) {
  if (!r) return [];
  if (Array.isArray(r.blocks)) return r.blocks;
  return (r.findings || []).filter((f) => f.severity === 'block');
}
function warnList(r) {
  if (!r) return [];
  if (Array.isArray(r.warns)) return r.warns;
  return (r.findings || []).filter((f) => f.severity === 'warn');
}
function nUnverifiable(r) { return Number(r?.checks_unverifiable) || 0; }

// A list row carries {summary} rather than the full report; accept either.
function reportOf(p) {
  if (p.vet_report) return p.vet_report;
  if (p.summary && typeof p.summary === 'object') return p.summary;
  return null;
}
function isVetted(p) {
  const r = reportOf(p);
  return !!(r && (r.ts || r.findings || 'blocks' in r));
}

function safetyChip(p) {
  const r = reportOf(p);
  if (!isVetted(p)) return badge('unvetted', '');
  const b = nBlocks(r);
  if (b) return badge(`${b} block${b === 1 ? '' : 's'}`, 'bad');
  const w = nWarns(r);
  const u = nUnverifiable(r);
  if (w) return badge(`${w} warn${w === 1 ? '' : 's'}`, 'warn');
  if (u) return badge(`${u} unverifiable`, 'warn');
  return badge('clean', 'ok');
}

function normChange(c) {
  const band = (a, b) => (Array.isArray(a) ? a : [c[b[0]], c[b[1]]]);
  const rpm = band(c.rpm_band, ['rpm_min', 'rpm_max']);
  const tps = band(c.tps_band, ['tps_min', 'tps_max']);
  return {
    table: c.table || '—',
    rpmMin: rpm?.[0], rpmMax: rpm?.[1],
    tpsMin: tps?.[0], tpsMax: tps?.[1],
    direction: c.direction || '',
    magnitude: c.magnitude,
    unit: c.unit || '',
    target: c.target_value ?? null,
  };
}

function deltaText(n) {
  const mag = Number(n.magnitude);
  if (!Number.isFinite(mag)) return '—';
  const sign = n.direction === 'decrease' ? '−' : '+';
  const unit = n.unit === 've_pct' ? '%' : n.unit === 'deg' ? '°' : n.unit === 'afr' ? ' AFR' : ` ${n.unit}`;
  return `${sign}${Math.abs(mag)}${unit}`;
}

const rng = (a, b, suffix) => (a == null && b == null ? '—' : `${a ?? '?'}–${b ?? '?'}${suffix}`);
const tsText = (t) => (t ? String(t).replace('T', ' ').replace(/\.\d+/, '') : '—');

// --- list --------------------------------------------------------------------

async function mountList(root) {
  closeStream();
  root.append(el('div', { class: 'prop-head' },
    el('h1', { class: 'page', style: 'margin:0' }, 'Proposals'),
    el('a', { class: 'btn primary', href: '#/proposals/new' }, '+ New proposal')));

  const card = el('div', { class: 'card' }, el('div', { class: 'muted' }, 'loading…'));
  root.append(card);

  let d;
  try {
    d = await get('/api/proposals');
  } catch (e) {
    card.textContent = '';
    card.append(el('div', { class: 'notice bad' },
      `Proposals unavailable: ${e.message}. `,
      'The API server is not answering — nothing has been lost, this list is server-side.'));
    return;
  }
  card.textContent = '';

  const proposals = d.proposals || [];
  if (!proposals.length) {
    return card.append(el('div', { class: 'empty' },
      'No proposals yet. A proposal is a tuning change written down, vetted by the ',
      'guardrails and an adversarial reviewer, and only then applied by hand in TMax Tuner.'));
  }

  const groups = new Map();
  for (const p of proposals) {
    const s = STATE_ORDER.includes(p.state) ? p.state : 'draft';
    if (!groups.has(s)) groups.set(s, []);
    groups.get(s).push(p);
  }
  for (const s of STATE_ORDER) {
    const rows = groups.get(s);
    if (!rows) continue;
    rows.sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
    card.append(el('div', { class: 'prop-group' },
      el('span', { class: `prop-dot st-${s}` }),
      STATE_LABEL[s] || s,
      el('span', { class: 'muted' }, ` · ${rows.length}`)));
    for (const p of rows) card.append(proposalRow(p));
  }
}

function proposalRow(p) {
  const n = (p.changes || []).length;
  return el('a', { class: 'prow', href: `#/proposals/${p.id}` },
    el('span', { class: `prop-dot st-${p.state}` }),
    el('div', { class: 'prow-main' },
      el('div', { class: 'prow-claim' }, p.claim || '(no claim)'),
      el('div', { class: 'muted prow-meta' },
        `${STATE_LABEL[p.state] || p.state} · ${n} change${n === 1 ? '' : 's'} · ${tsText(p.ts)}`),
      typeof p.summary === 'string' && p.summary
        ? el('div', { class: 'muted prow-sum' }, p.summary) : null),
    el('div', { class: 'prow-side' }, safetyChip(p)));
}

// --- detail ------------------------------------------------------------------

async function mountDetail(root, id) {
  closeStream();
  root.append(el('h1', { class: 'page' }, 'Proposal ',
    el('a', { href: '#/proposals', class: 'muted', style: 'font-size:13px' }, '← all proposals')));
  const host = el('div');
  root.append(host, el('div', { class: 'muted' }, 'loading…'));

  let p;
  try {
    p = await get(`/api/proposals/${encodeURIComponent(id)}`);
  } catch (e) {
    root.lastChild.remove();
    host.append(el('div', { class: 'card' },
      el('div', { class: 'notice bad' }, `Could not load proposal ${id}: ${e.message}`),
      el('a', { class: 'btn', href: '#/proposals' }, 'Back to list')));
    return;
  }
  root.lastChild.remove();
  renderDetail(host, root, p);
}

function redraw(root, id) {
  closeStream();
  root.textContent = '';
  mountDetail(root, id);
}

function renderDetail(host, root, p) {
  const report = p.vet_report || null;
  const blocked = nBlocks(report) > 0;

  // header
  host.append(el('div', { class: 'card' },
    el('div', { class: 'prop-title' },
      el('h2', { class: 'prop-claim' }, p.claim || '(no claim)'),
      el('div', { class: 'prop-title-side' },
        badge(STATE_LABEL[p.state] || p.state, STATE_KIND[p.state] || ''),
        safetyChip(p))),
    el('div', { class: 'muted prop-sub' },
      el('span', { class: 'mono' }, String(p.id)), ` · created ${tsText(p.ts)}`,
      p.source_tier ? ` · proposed by the ${p.source_tier} tier` : ''),
    stepper(p.state),
    el('div', { class: 'prop-steps-note muted' },
      'Vetted is not a button. It is entered only by a vetting run that finds ',
      el('strong', {}, 'zero hard blocks'), '.')));

  if (blocked) {
    host.append(el('div', { class: 'card prop-blockcard' },
      el('div', { class: 'prop-blockhead' }, '⛔ BLOCKED'),
      el('div', {}, `${nBlocks(report)} hard block${nBlocks(report) === 1 ? '' : 's'} from the deterministic guardrails. `,
        'This proposal cannot be approved or applied. Clone it as a new draft with a smaller step.')));
  }

  host.append(vetCard(p, root));
  host.append(actionsCard(p, root));
  host.append(changesCard(p, root));
  host.append(dynoCard(p));
  host.append(historyCard(p));
}

function stepper(state) {
  const rejected = state === 'rejected';
  const idx = STATES.indexOf(state);
  const ol = el('ol', { class: 'prop-steps' });
  STATES.forEach((s, i) => {
    let cls = 'pending';
    if (!rejected && idx >= 0) {
      if (i < idx) cls = 'done';
      else if (i === idx) cls = 'current';
    }
    ol.append(el('li', { class: `prop-step ${cls}`, ...(cls === 'current' ? { 'aria-current': 'step' } : {}) },
      el('span', { class: 'prop-step-dot' }, cls === 'done' ? '✓' : String(i + 1)),
      el('span', { class: 'prop-step-label' }, STATE_LABEL[s])));
  });
  if (rejected) {
    ol.append(el('li', { class: 'prop-step rejected', 'aria-current': 'step' },
      el('span', { class: 'prop-step-dot' }, '✕'),
      el('span', { class: 'prop-step-label' }, 'rejected')));
  }
  return ol;
}

// --- vet report: the heart of the screen -------------------------------------

function vetCard(p, root) {
  const card = el('div', { class: 'card' }, el('h2', {}, 'Vet report'));
  const report = p.vet_report;

  const live = el('div');
  card.append(live);

  if (!report) {
    card.append(el('div', { class: 'empty' },
      'Not vetted yet. Vetting runs the deterministic guardrails, checks the claim ',
      'against the corpus, then asks a bigger model to refute it.'));
    card.append(vetControls(p, live, root));
    return card;
  }

  const blocks = blockList(report);
  const warns = warnList(report);
  const unver = nUnverifiable(report);

  // verdict strip
  const verdict = blocks.length ? 'bad' : (warns.length || unver) ? 'warn' : 'ok';
  card.append(el('div', { class: `prop-verdict ${verdict}` },
    el('div', { class: 'prop-verdict-main' },
      blocks.length ? `${blocks.length} HARD BLOCK${blocks.length === 1 ? '' : 'S'}`
        : warns.length || unver ? 'NO BLOCKS — READ THE WARNINGS'
          : 'CLEAR — no blocks, no warnings'),
    el('div', { class: 'prop-verdict-sub' },
      `guardrails ${blocks.length ? 'refused' : 'passed'} · ${warns.length} warn${warns.length === 1 ? '' : 's'} · ${unver} unverifiable · vetted ${tsText(report.ts)}`)));

  // blocks
  if (blocks.length) {
    const box = el('div', { class: 'prop-findings blocks' },
      el('div', { class: 'prop-findings-head bad' }, 'BLOCKS — deterministic code guardrails'),
      el('div', { class: 'prop-attrib' },
        'Issued by ', el('span', { class: 'mono' }, 'src/guardrails.py'),
        ' — pure code, no model, no network. This is the only authority that can stop a ',
        'proposal, and no reviewer, model or button can clear it.'));
    for (const f of blocks) box.append(finding(f, 'bad'));
    card.append(box);
  }

  // warns
  if (warns.length) {
    const box = el('div', { class: 'prop-findings warns' },
      el('div', { class: 'prop-findings-head warn' }, 'WARNINGS — advisory'),
      el('div', { class: 'prop-attrib' },
        'These do not stop anything. They are the house step limits telling you to ',
        'stage the change and ride between steps.'));
    for (const f of warns) box.append(finding(f, 'warn'));
    card.append(box);
  }

  // unverifiable — the .tbw scaling gap, in plain language
  if (unver) {
    card.append(el('div', { class: 'prop-unver' },
      el('div', { class: 'prop-unver-head' },
        el('span', { class: 'prop-unver-n' }, String(unver)),
        `check${unver === 1 ? '' : 's'} could not be verified`),
      el('div', {},
        'The per-cell scaling of the .tbw format is still unconfirmed, so this app knows ',
        el('strong', {}, 'how much a change moves a cell'),
        ' but not ', el('strong', {}, 'what the cell ends up reading'),
        '. Limits that need the absolute value — the 32° spark ceiling, the AFR windows — ',
        'could not be checked here. Read them off the screen in TMax Tuner before you commit.')));
  }

  if (!blocks.length && !warns.length && !unver) {
    card.append(el('div', { class: 'prop-attrib' },
      'Every deterministic guardrail check ran and passed with a known value.'));
  }

  // citation support
  const cites = report.citation_support || [];
  if (cites.length) {
    const box = el('div', { class: 'prop-cites' },
      el('div', { class: 'prop-findings-head' }, 'Citation support'));
    for (const c of cites) {
      const ok = c.supported === true || c.verdict === 'supported';
      const missing = c.supported === false || c.verdict === 'unsupported';
      box.append(el('div', { class: 'prop-cite' },
        badge(ok ? 'supported' : missing ? 'unsupported' : (c.verdict || 'n/a'),
          ok ? 'ok' : missing ? 'bad' : ''),
        el('div', {},
          el('div', { class: 'prop-cite-src mono' }, c.source || c.doc || c.path || '—'),
          c.quote || c.text ? el('div', { class: 'muted prop-cite-q' }, c.quote || c.text) : null)));
    }
    card.append(box);
  }

  // adversarial reviewer
  card.append(adversarialBox(report, p));

  card.append(vetControls(p, live, root));
  return card;
}

function finding(f, kind) {
  return el('div', { class: `prop-finding ${kind}` },
    el('div', { class: 'prop-finding-top' },
      el('span', { class: 'prop-rule mono' }, f.rule || f.check || 'rule'),
      f.change_idx != null ? el('span', { class: 'muted prop-chg' }, `change #${Number(f.change_idx) + 1}`) : null),
    el('div', { class: 'prop-finding-msg' }, f.message || f.detail || ''));
}

function adversarialBox(report, p) {
  const a = report.adversarial;
  if (!a) {
    return el('div', { class: 'prop-adv' },
      el('div', { class: 'prop-findings-head' }, 'Adversarial review'),
      el('div', { class: 'muted' }, 'No adversarial pass recorded for this report.'));
  }
  const objects = a.verdict === 'OBJECT';
  const box = el('div', { class: `prop-adv ${objects ? 'object' : 'concur'}` });

  const model = a.model || 'unknown model';
  box.append(el('div', { class: 'prop-adv-head' },
    el('span', { class: 'prop-adv-verdict' }, objects ? '⚠ OBJECT' : '✓ CONCUR'),
    el('span', { class: 'prop-adv-model' }, 'refuted by ', el('span', { class: 'mono' }, model))));

  const pinned = REFUTER[p.source_tier] || 'a model above the proposing tier';
  box.append(el('div', { class: 'prop-attrib' },
    p.source_tier
      ? `Tier pinning: a ${p.source_tier}-tier proposal must be reviewed by ${pinned}. `
      : 'Tier pinning: a proposal is always reviewed by a model above the tier that wrote it. ',
    'The 14b is never the refuter.'));

  if (/14b/i.test(model)) {
    box.append(el('div', { class: 'notice bad' },
      'Tier pinning violated: this report was refuted by a 14b model. ',
      'Treat this review as worthless and re-vet.'));
  }

  box.append(el('div', { class: `prop-adv-note ${objects ? 'object' : ''}` },
    objects
      ? 'An OBJECT is a loud warning, NOT a block. A model cannot stop a proposal — only the guardrails can. Read the objection, then decide.'
      : 'A CONCUR is not a safety clearance. It only means the reviewer found nothing to refute.'));

  if (a.text) {
    // LLM prose — markdown-lite renderer, never innerHTML.
    box.append(el('div', { class: 'prop-adv-body' }, renderMarkdown(String(a.text))));
  }
  return box;
}

// --- live vetting (SSE) ------------------------------------------------------

function vetControls(p, live, root) {
  const bar = el('div', { class: 'prop-actions' });
  const terminal = p.state === 'validated_by_ride' || p.state === 'rejected';
  const runBtn = el('button', { class: p.vet_report ? '' : 'primary' },
    p.vet_report ? 'Re-run vetting' : 'Run vetting');
  const cancelBtn = el('button', { class: 'danger hidden' }, 'Cancel vetting');

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    try {
      await post(`/api/proposals/${encodeURIComponent(p.id)}/vet`);
    } catch (e) {
      runBtn.disabled = false;
      live.textContent = '';
      live.append(el('div', { class: 'notice bad' }, `Could not start vetting: ${e.message}`));
      return;
    }
    cancelBtn.classList.remove('hidden');
    startVetStream(p, live, root, () => {
      runBtn.disabled = false;
      cancelBtn.classList.add('hidden');
    });
  });

  cancelBtn.addEventListener('click', async () => {
    cancelBtn.disabled = true;
    try { await post(`/api/proposals/${encodeURIComponent(p.id)}/vet/cancel`); toast('cancel requested'); }
    catch (e) { toast(`cancel failed: ${e.message}`, 'bad'); cancelBtn.disabled = false; }
  });

  if (!terminal) bar.append(runBtn, cancelBtn);
  return bar;
}

function startVetStream(p, live, root, onEnd) {
  closeStream();
  live.textContent = '';

  const rows = new Map();
  const panel = el('div', { class: 'prop-vetlive' },
    el('div', { class: 'prop-vetlive-head' },
      el('span', { class: 'gen-dot' }), 'vetting…'),
    el('div', { class: 'muted prop-vetlive-note' },
      'The adversarial pass runs on the 70b at roughly 0.7 tokens/second — it can take ',
      'several minutes. You can cancel, leave this screen, or lock the phone; the run ',
      'continues on the server and the report is here when it finishes.'));
  for (const [key, label, note] of VET_STAGES) {
    const status = el('span', { class: 'prop-stage-status' }, 'waiting');
    const detail = el('div', { class: 'muted prop-stage-detail' }, note);
    const row = el('div', { class: 'prop-stage waiting' },
      el('div', { class: 'prop-stage-top' },
        el('span', { class: 'prop-stage-name' }, label), status),
      detail);
    rows.set(key, { row, status, detail });
    panel.append(row);
  }
  live.append(panel);

  const startedTs = p.vet_report?.ts || null;
  const finish = (msg, kind) => {
    closeStream();
    onEnd?.();
    panel.querySelector('.gen-dot')?.remove();
    panel.querySelector('.prop-vetlive-head').textContent = msg;
    if (kind) panel.classList.add(kind);
  };

  const apply = (d) => {
    const r = rows.get(d.stage);
    if (!r) return;
    const st = d.status || 'running';
    r.row.className = `prop-stage ${st}`;
    r.status.textContent = st;
    if (d.detail) r.detail.textContent = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail);
  };

  const done = () => { finish('vetting complete — loading report…'); redraw(root, p.id); };

  stream = openVetStream(p.id, {
    stage: (d) => {
      apply(d);
      if (d.stage === 'adversarial' && d.status === 'done') done();
    },
    done: () => done(),
    error: (d) => finish(`vetting failed: ${d.message || d.code || 'unknown error'}`, 'failed'),
  });

  // Fallback for a dead/blocked SSE: if the report lands, pick it up anyway.
  pollTimer = setInterval(async () => {
    try {
      const fresh = await get(`/api/proposals/${encodeURIComponent(p.id)}`);
      const ts = fresh.vet_report?.ts || null;
      if (fresh.vet_report && ts !== startedTs) done();
    } catch { /* server busy generating — keep waiting */ }
  }, 6000);
}

// Same iOS-survival shape as openChatStream in api.js: any wake signal closes
// the possibly-zombie source and opens a fresh one, and stage rows are set (not
// appended) so a full replay is idempotent.
function openVetStream(id, handlers) {
  let es = null;
  let closed = false;
  const wake = debounce(() => { if (!closed) connect(); }, 400);

  function handle(name, e) {
    let data = {};
    try { data = JSON.parse(e.data); } catch { /* non-JSON keepalive */ }
    if (name === 'error' || data.code === 'error') { close(); return handlers.error?.(data); }
    if (data.stage) handlers.stage?.(data);
    if (name === 'done' || data.done === true || data.stage === 'done') {
      close();
      handlers.done?.(data);
    }
  }

  function connect() {
    if (es) { try { es.close(); } catch { /* already dead */ } }
    es = new EventSource(`/api/proposals/${encodeURIComponent(id)}/vet/stream`);
    for (const ev of ['stage', 'vet', 'progress', 'status', 'message', 'done', 'error']) {
      es.addEventListener(ev, (e) => handle(ev, e));
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

// --- transitions -------------------------------------------------------------

function actionsCard(p, root) {
  const card = el('div', { class: 'card' }, el('h2', {}, 'Next step'));
  const err = el('div', { class: 'prop-err hidden' });
  const report = p.vet_report;
  const blocks = nBlocks(report);
  const unver = nUnverifiable(report);

  const go = async (to, args, btn) => {
    err.classList.add('hidden');
    if (btn) btn.disabled = true;
    try {
      const r = await post(`/api/proposals/${encodeURIComponent(p.id)}/transition`, { to, ...args });
      if (r && r.error) throw new Error(r.error);
      toast(`→ ${STATE_LABEL[r?.state || to] || to}`);
      redraw(root, p.id);
    } catch (e) {
      // 409 refusals carry the reason — keep it on screen, not in a 4s toast.
      err.textContent = `Refused: ${e.message}`;
      err.classList.remove('hidden');
      if (btn) btn.disabled = false;
    }
  };

  if (p.state === 'draft') {
    card.append(el('div', { class: 'notice' },
      blocks
        ? 'This draft is blocked by the guardrails. It cannot advance — clone it as a new draft with a smaller step.'
        : report
          ? 'Vetting has run. If the report shows zero blocks the proposal is already vetted; otherwise fix the draft by cloning it.'
          : 'Run vetting above. There is no button that marks a proposal vetted — only the vet run itself can, and only on a zero-block result.'));
  }

  if (p.state === 'vetted') card.append(approveBlock(p, blocks, unver, go));

  if (p.state === 'approved') {
    const note = el('textarea', { rows: 3, placeholder: 'e.g. entered +2% VE at 0–2% TPS / 3840–4608 rpm in TMax Tuner, saved as lrst_131_ve_decel_v4, flashed 14:20' });
    const btn = el('button', { class: 'primary', disabled: '' }, 'Mark applied on bike');
    note.addEventListener('input', () => { btn.disabled = !note.value.trim(); });
    btn.addEventListener('click', () => go('applied_on_bike', { note: note.value.trim() }, btn));
    card.append(el('div', { class: 'notice warn' },
      'This app never writes to the ECM. Type the change into TMax Tuner yourself, ',
      'then record exactly what you entered — that note is the only record of what is on the bike.'),
      el('label', { class: 'f block' }, el('span', {}, 'What did you enter in TMax Tuner? (required)'), note),
      el('div', { class: 'prop-actions' }, btn));
  }

  if (p.state === 'applied_on_bike') card.append(validateBlock(p, go));

  if (p.state === 'validated_by_ride') {
    card.append(el('div', { class: 'notice ok' },
      'Validated by ride. The linked journal entry is now vetted knowledge — it is cited ',
      'as authority and carries the setup retrieval boost.'));
  }
  if (p.state === 'rejected') {
    card.append(el('div', { class: 'notice bad' }, 'Rejected. Clone it if you want to try a different step.'));
  }

  // always available
  const bar = el('div', { class: 'prop-actions' });
  bar.append(el('button', {
    onclick: () => cloneDraft(p),
  }, 'Clone as new draft'));
  if (p.state !== 'rejected' && p.state !== 'validated_by_ride') {
    bar.append(el('button', {
      class: 'danger',
      onclick: (ev) => {
        const reason = prompt('Why is this rejected? (recorded in the history)');
        if (reason === null) return;
        go('rejected', { reason: reason.trim() }, ev.currentTarget);
      },
    }, 'Reject'));
  }
  card.append(bar, err);
  card.append(el('div', { class: 'muted prop-immutable' },
    'Changes cannot be edited after creation — a vetted report must always describe the ',
    'exact numbers that were vetted. Clone instead.'));
  return card;
}

function approveBlock(p, blocks, unver, go) {
  const wrap = el('div');
  const btn = el('button', { class: 'primary', disabled: '' }, 'Approve');

  if (blocks) {
    wrap.append(el('div', { class: 'notice bad' },
      `Approval is unavailable: ${blocks} hard block${blocks === 1 ? '' : 's'} from the guardrails.`));
    wrap.append(el('div', { class: 'prop-actions' }, btn));
    return wrap;
  }
  if (!p.vet_report) {
    wrap.append(el('div', { class: 'notice bad' },
      'Approval is unavailable: no vet report exists. Run vetting first.'));
    wrap.append(el('div', { class: 'prop-actions' }, btn));
    return wrap;
  }

  if (unver) {
    const cb = el('input', { type: 'checkbox' });
    wrap.append(el('div', { class: 'prop-ack' },
      el('label', { class: 'prop-ack-label' }, cb,
        el('span', {}, 'I will confirm the absolute values in TMax Tuner')),
      el('div', { class: 'muted prop-ack-why' },
        `${unver} check${unver === 1 ? '' : 's'} needed a value this app cannot compute, because `,
        'per-cell .tbw scaling is unconfirmed. Approving means you accept that the ceiling ',
        'and window checks happen on the TMax Tuner screen, by eye, before you flash.')));
    cb.addEventListener('change', () => { btn.disabled = !cb.checked; });
    btn.addEventListener('click', () => go('approved', { ack_unverifiable: true }, btn));
  } else {
    btn.disabled = false;
    wrap.append(el('div', { class: 'notice ok' },
      'Zero blocks and every check verifiable. Approval only means you intend to enter ',
      'this in TMax Tuner — it changes nothing on the bike.'));
    btn.addEventListener('click', () => go('approved', {}, btn));
  }
  wrap.append(el('div', { class: 'prop-actions' }, btn));
  return wrap;
}

function validateBlock(p, go) {
  const wrap = el('div');
  wrap.append(el('div', { class: 'notice warn' },
    'Linking a ride promotes that journal entry to ', el('strong', {}, 'vetted knowledge'),
    ': it stops being "something I wrote down" and starts being cited as authority by the ',
    'co-pilot, with the setup retrieval boost. Only link a ride that actually tested this change.'));

  const sel = el('select', {});
  sel.append(el('option', { value: '' }, 'loading rides…'));
  const btn = el('button', { class: 'primary', disabled: '' }, 'Mark validated by ride');
  sel.addEventListener('change', () => { btn.disabled = !sel.value; });
  btn.addEventListener('click', () => go('validated_by_ride', { journal_id: sel.value }, btn));

  wrap.append(el('label', { class: 'f block' }, el('span', {}, 'Journal entry that validated this'), sel),
    el('div', { class: 'prop-actions' }, btn,
      el('a', { class: 'btn', href: '#/journal/new' }, 'Log a ride first')));

  get('/api/journal').then((d) => {
    const entries = (d.entries || []).filter((e) => !e.retracted);
    sel.textContent = '';
    if (!entries.length) {
      sel.append(el('option', { value: '' }, 'no journal entries yet'));
      return;
    }
    sel.append(el('option', { value: '' }, 'select a ride…'));
    for (const e of entries) {
      const mine = e.proposal_id === p.id ? ' ✓ linked to this proposal' : '';
      sel.append(el('option', { value: e.id }, `${tsText(e.ts).slice(0, 16)} — ${e.title}${mine}`));
    }
  }).catch((e) => {
    sel.textContent = '';
    sel.append(el('option', { value: '' }, `journal unavailable (${e.message})`));
  });
  return wrap;
}

async function cloneDraft(p) {
  try {
    const body = {
      claim: p.claim,
      changes: (p.changes || []).map((c) => ({ ...c })),
      source_tier: p.source_tier || 'smart',
      cloned_from: p.id,
    };
    const created = await post('/api/proposals', body);
    if (created?.error) return toast(created.error, 'bad');
    toast('cloned as a new draft');
    location.hash = `#/proposals/${created.id}`;
  } catch (e) {
    toast(`clone failed: ${e.message}`, 'bad');
  }
}

// --- changes / dyno / history ------------------------------------------------

function changesCard(p) {
  const card = el('div', { class: 'card' }, el('h2', {}, 'Changes (immutable)'));
  const changes = p.changes || [];
  if (!changes.length) {
    card.append(el('div', { class: 'empty' }, 'No changes recorded.'));
    return card;
  }
  const wrap = el('div', { class: 'scroll-x' });
  const table = el('table', { class: 'data' },
    el('tr', {}, el('th', {}, '#'), el('th', {}, 'table'), el('th', {}, 'rpm band'),
      el('th', {}, 'tps band'), el('th', {}, 'change'), el('th', {}, 'resulting value')));
  changes.forEach((c, i) => {
    const n = normChange(c);
    const cat = SPARK_TABLES.includes(n.table) ? 'TIMING'
      : VE_TABLES.includes(n.table) ? 'FUEL'
        : n.table === 'afr_target' ? 'AFR' : 'SHARED';
    table.append(el('tr', {},
      el('td', { class: 'mono' }, String(i + 1)),
      el('td', { class: `cat cat-${cat}` }, n.table),
      el('td', { class: 'mono' }, rng(n.rpmMin, n.rpmMax, ' rpm')),
      el('td', { class: 'mono' }, rng(n.tpsMin, n.tpsMax, '%')),
      el('td', { class: `mono prop-delta ${n.direction === 'decrease' ? 'down' : 'up'}` }, deltaText(n)),
      el('td', { class: 'mono' }, n.target != null ? String(n.target)
        : el('span', { class: 'muted' }, 'unknown'))));
  });
  wrap.append(table);
  card.append(wrap, el('div', { class: 'muted prop-immutable' },
    'Deltas, not absolutes. "Resulting value" is only known when it was entered by hand — ',
    'otherwise the app knows how far a cell moves but not where it lands.'));
  return card;
}

function dynoCard(p) {
  const card = el('div', { class: 'card' }, el('h2', {}, 'Attached dyno runs'));
  const runs = p.dyno_runs || [];
  if (!runs.length) {
    card.append(el('div', { class: 'empty' }, 'No dyno runs attached.'));
    return card;
  }
  for (const r of runs) {
    const tiles = el('div', { class: 'tiles' });
    for (const [k, v] of Object.entries(r)) {
      if (v === null || v === undefined || typeof v === 'object') continue;
      if (k === 'id' || k === 'label' || k === 'name') continue;
      tiles.append(el('div', { class: 'tile' },
        el('div', { class: 'k' }, k.replace(/_/g, ' ')),
        el('div', { class: 'v' }, String(v))));
    }
    card.append(el('div', { class: 'prop-run' },
      el('div', { class: 'prop-run-head' },
        el('span', {}, r.label || r.name || r.id || 'run'),
        r.id ? el('a', { class: 'muted', href: `#/dyno/${r.id}` }, 'open in dyno →') : null),
      tiles));
  }
  card.append(el('div', { class: 'muted prop-immutable' },
    'Virtual dyno output is a model of the engine, not a measurement of it.'));
  return card;
}

function historyCard(p) {
  const card = el('div', { class: 'card' }, el('h2', {}, 'History'));
  const hist = p.history || [];
  if (!hist.length) {
    card.append(el('div', { class: 'empty' }, 'No history recorded.'));
    return card;
  }
  const list = el('div', { class: 'prop-hist' });
  for (const h of [...hist].reverse()) {
    const to = h.to || h.state || h.event || 'entry';
    const note = h.note || h.reason || h.detail || h.message || '';
    list.append(el('div', { class: 'prop-hist-row' },
      el('span', { class: `prop-dot st-${to}` }),
      el('div', {},
        el('div', { class: 'prop-hist-top' },
          el('span', {}, h.from ? `${STATE_LABEL[h.from] || h.from} → ${STATE_LABEL[to] || to}` : (STATE_LABEL[to] || to)),
          el('span', { class: 'muted' }, tsText(h.ts))),
        h.actor ? el('div', { class: 'muted prop-hist-actor' }, String(h.actor)) : null,
        note ? el('div', { class: 'prop-hist-note' }, String(note)) : null)));
  }
  card.append(list);
  return card;
}

// --- new proposal form -------------------------------------------------------

async function mountForm(root) {
  closeStream();
  root.append(el('h1', { class: 'page' }, 'New proposal ',
    el('a', { href: '#/proposals', class: 'muted', style: 'font-size:13px' }, '← all proposals')));
  const card = el('div', { class: 'card' });
  root.append(card);

  card.append(el('div', { class: 'notice' },
    'A proposal is a written-down tuning change. Creating one changes nothing: it starts ',
    'as a ', el('strong', {}, 'draft'), ' and has to survive vetting before it can be approved, ',
    'and you still enter it by hand in TMax Tuner afterwards.'));

  const claim = el('textarea', { rows: 3, placeholder: 'e.g. decel pops above 4k are lean overrun — +2% VE at 0–2% TPS, 3840–4608 rpm should quiet them' });
  card.append(el('label', { class: 'f block' }, el('span', {}, 'Claim — what and why'), claim));

  let tier = 'smart';
  const seg = el('div', { class: 'seg' });
  const tierNote = el('div', { class: 'muted prop-tier-note' });
  const setTier = (t) => {
    tier = t;
    seg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.tier === t));
    tierNote.textContent = `Will be reviewed by ${REFUTER[t] || 'a higher tier'} — never the 14b.`;
  };
  for (const t of ['fast', 'smart', 'deep']) {
    seg.append(el('button', { 'data-tier': t, onclick: () => setTier(t) }, t));
  }
  setTier(tier);
  card.append(el('label', { class: 'f block' }, el('span', {}, 'Source tier — who is proposing this'), seg), tierNote);

  // table list from the server so the form can never offer a table guardrails rejects
  let tables = FALLBACK_TABLES;
  try {
    const prof = await get('/api/profile');
    if (prof?.guardrails?.tables?.length) tables = prof.guardrails.tables;
  } catch { /* offline: fall back to the known TMax Tuner pages */ }

  const rowsWrap = el('div', { class: 'prop-rows' });
  const rows = [];
  const addRow = () => {
    const r = changeRow(tables, () => {
      if (rows.length <= 1) return toast('a proposal needs at least one change', 'bad');
      const i = rows.indexOf(r);
      rows.splice(i, 1);
      r.node.remove();
      renumber();
    });
    rows.push(r);
    rowsWrap.append(r.node);
    renumber();
  };
  const renumber = () => rows.forEach((r, i) => { r.num.textContent = `Change ${i + 1}`; });

  card.append(el('h3', { class: 'prop-h3' }, 'Changes'), rowsWrap);
  card.append(el('div', { class: 'prop-actions' },
    el('button', { onclick: addRow }, '+ Add change')));
  addRow();

  const err = el('div', { class: 'prop-err hidden' });
  const save = el('button', { class: 'primary' }, 'Create draft');
  card.append(el('div', { class: 'prop-actions' }, save,
    el('a', { class: 'btn', href: '#/proposals' }, 'Cancel')), err);

  save.addEventListener('click', async () => {
    err.classList.add('hidden');
    const fail = (m) => { err.textContent = m; err.classList.remove('hidden'); return null; };
    if (!claim.value.trim()) return fail('Write the claim — what changes and why.');

    const changes = [];
    for (let i = 0; i < rows.length; i++) {
      const v = rows[i].value();
      if (v.error) return fail(`Change ${i + 1}: ${v.error}`);
      changes.push(v.change);
    }
    if (!changes.length) return fail('Add at least one change.');

    save.disabled = true;
    try {
      const created = await post('/api/proposals', {
        claim: claim.value.trim(), changes, source_tier: tier,
      });
      if (created?.error) throw new Error(created.error);
      toast('draft created — run vetting next');
      location.hash = `#/proposals/${created.id}`;
    } catch (e) {
      save.disabled = false;
      fail(`Could not create the proposal: ${e.message}`);
    }
  });
}

function changeRow(tables, onRemove) {
  const num = el('div', { class: 'prop-row-num' }, 'Change');
  const table = el('select', {});
  table.append(el('option', { value: '' }, 'select a TMax Tuner page…'));
  for (const t of tables) table.append(el('option', { value: t }, t));

  const unit = el('select', {});
  for (const [v, label] of UNITS) unit.append(el('option', { value: v }, label));

  // Pick the unit that matches the table so the guardrails read what he meant.
  table.addEventListener('change', () => {
    if (SPARK_TABLES.includes(table.value)) unit.value = 'deg';
    else if (VE_TABLES.includes(table.value)) unit.value = 've_pct';
    else if (table.value === 'afr_target') unit.value = 'afr';
  });

  const dir = el('select', {});
  dir.append(el('option', { value: 'increase' }, 'increase'), el('option', { value: 'decrease' }, 'decrease'));

  const rpmMin = el('input', { type: 'number', inputmode: 'numeric', step: '1', min: '0', placeholder: '3840' });
  const rpmMax = el('input', { type: 'number', inputmode: 'numeric', step: '1', min: '0', placeholder: '4608' });
  const tpsMin = el('input', { type: 'number', inputmode: 'numeric', step: '1', min: '0', max: '100', placeholder: '0' });
  const tpsMax = el('input', { type: 'number', inputmode: 'numeric', step: '1', min: '0', max: '100', placeholder: '2' });
  const mag = el('input', { type: 'number', inputmode: 'decimal', step: '0.1', min: '0', placeholder: '2' });
  const target = el('input', { type: 'number', inputmode: 'decimal', step: '0.1', placeholder: 'leave blank if unknown' });

  const f = (label, input) => el('label', { class: 'f' }, el('span', {}, label), input);
  const node = el('div', { class: 'prop-row' },
    el('div', { class: 'prop-row-head' }, num,
      el('button', { class: 'danger prop-row-x', onclick: onRemove }, 'Remove')),
    el('div', { class: 'fgrid' },
      f('Table', table),
      f('Direction', dir),
      f('Magnitude', mag),
      f('Unit', unit),
      f('RPM from', rpmMin),
      f('RPM to', rpmMax),
      f('TPS from %', tpsMin),
      f('TPS to %', tpsMax),
      f('Resulting value (optional)', target)),
    el('div', { class: 'muted prop-row-hint' },
      'Fill "resulting value" only if you read it off TMax Tuner — it lets the guardrails ',
      'check the hard ceiling and AFR windows instead of counting the check as unverifiable.'));

  const value = () => {
    const num_ = (i) => (i.value === '' ? null : Number(i.value));
    if (!table.value) return { error: 'pick a table.' };
    const m = num_(mag);
    if (m === null || !(m > 0)) return { error: 'magnitude must be greater than zero.' };
    const r0 = num_(rpmMin), r1 = num_(rpmMax), t0 = num_(tpsMin), t1 = num_(tpsMax);
    if (r0 === null || r1 === null) return { error: 'give both ends of the rpm band.' };
    if (r0 > r1) return { error: 'rpm from is above rpm to.' };
    if (t0 === null || t1 === null) return { error: 'give both ends of the tps band.' };
    if (t0 > t1) return { error: 'tps from is above tps to.' };
    if (t0 < 0 || t1 > 100) return { error: 'tps must be between 0 and 100%.' };
    const change = {
      table: table.value,
      rpm_min: r0, rpm_max: r1, tps_min: t0, tps_max: t1,
      direction: dir.value, magnitude: m, unit: unit.value,
    };
    const tv = num_(target);
    if (tv !== null) change.target_value = tv;
    return { change };
  };

  return { node, num, value };
}
