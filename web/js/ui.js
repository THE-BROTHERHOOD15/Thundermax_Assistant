// Shared widgets: DOM helpers, toasts, banner, markdown-lite, citation chips.
// Security rule: model/user text NEVER goes through innerHTML — the renderer
// escapes first, builds risky nodes (links, chips, code) via createElement.

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    n.append(c.nodeType ? c : document.createTextNode(c));
  }
  return n;
}

export function toast(msg, kind = '') {
  const t = el('div', { class: `toast ${kind}` }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), 4200);
}

export function banner(msg, kind = '', action = null) {
  const b = document.getElementById('banner');
  b.textContent = '';
  b.className = `banner ${kind}`;
  b.append(msg);
  if (action) b.append(el('button', { onclick: action.fn }, action.label));
  b.classList.remove('hidden');
}
export function hideBanner() { document.getElementById('banner').classList.add('hidden'); }

export function badge(text, kind = '') { return el('span', { class: `badge ${kind}` }, text); }

// --- markdown-lite -----------------------------------------------------------
// Enough for LLM output: headings, bold/italic, lists, tables, code. Escape-
// first; code blocks extracted to placeholders BEFORE inline transforms so
// markdown inside code never renders; hrefs allowlisted (https?:, #, /);
// [n] becomes a citation chip button built with createElement.

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const esc = (s) => s.replace(/[&<>"]/g, (c) => ESC[c]);

export function renderMarkdown(text, { onCite } = {}) {
  const root = el('div');
  const codeBlocks = [];
  let src = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, body) => {
    codeBlocks.push(body);
    return `${codeBlocks.length - 1}`;
  });
  const inlineCode = [];
  src = src.replace(/`([^`\n]+)`/g, (_, body) => {
    inlineCode.push(body);
    return `${inlineCode.length - 1}`;
  });

  const lines = src.split('\n');
  let list = null, table = null, para = [];

  const flushPara = () => {
    if (!para.length) return;
    root.append(el('p', {}, ...inline(para.join(' '))));
    para = [];
  };
  const flushList = () => { if (list) { root.append(list); list = null; } };
  const flushTable = () => { if (table) { root.append(table); table = null; } };

  function inline(s) {
    // bold/italic via escaped-HTML string, then split out chips/links/code marks
    let h = esc(s)
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, '$1<i>$2</i>');
    const frag = [];
    // tokenize: citation [n], links [t](url), inline-code placeholders, rest
    const rx = /\[(\d{1,2})\]|\[([^\]]+)\]\(([^)\s]+)\)|(\d+)/g;
    let last = 0, m;
    const pushHtml = (html) => {
      if (!html) return;
      const span = el('span');
      span.innerHTML = html; // safe: content was escaped, only our <b>/<i> tags
      frag.push(...span.childNodes);
    };
    while ((m = rx.exec(h)) !== null) {
      pushHtml(h.slice(last, m.index));
      last = rx.lastIndex;
      if (m[1] !== undefined) {
        const n = Number(m[1]);
        frag.push(el('button', { class: 'cite-chip', onclick: () => onCite?.(n) }, String(n)));
      } else if (m[2] !== undefined) {
        const url = m[3];
        if (/^(https?:|\/|#)/i.test(url)) {
          frag.push(el('a', { href: url, target: '_blank', rel: 'noopener' }, m[2]));
        } else {
          frag.push(document.createTextNode(`${m[2]} (${url})`));
        }
      } else if (m[4] !== undefined) {
        frag.push(el('code', {}, inlineCode[Number(m[4])]));
      }
    }
    pushHtml(h.slice(last));
    return frag;
  }

  for (const raw of lines) {
    const codeMatch = raw.match(/^(\d+)$/);
    if (codeMatch) {
      flushPara(); flushList(); flushTable();
      root.append(el('pre', {}, el('code', {}, codeBlocks[Number(codeMatch[1])])));
      continue;
    }
    const line = raw.trimEnd();
    if (!line.trim()) { flushPara(); flushList(); flushTable(); continue; }
    const hm = line.match(/^(#{1,4})\s+(.*)/);
    if (hm) {
      flushPara(); flushList(); flushTable();
      root.append(el(`h${Math.min(hm[1].length + 2, 5)}`, {}, ...inline(hm[2])));
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flushPara(); flushTable();
      if (!list) list = el('ul');
      list.append(el('li', {}, ...inline(line.replace(/^\s*[-*]\s+/, ''))));
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      flushPara(); flushList();
      if (/^\s*\|[\s:|-]+\|\s*$/.test(line)) continue; // separator row
      if (!table) table = el('table');
      const cells = line.trim().replace(/^\||\|$/g, '').split('|');
      table.append(el('tr', {}, ...cells.map((c) => el(table.children.length ? 'td' : 'th', {}, ...inline(c.trim())))));
      continue;
    }
    flushList(); flushTable();
    para.push(line);
  }
  flushPara(); flushList(); flushTable();
  return root;
}

// citation slide-over
export function showCitation(cite) {
  document.querySelector('.slideover')?.remove();
  const panel = el('div', { class: 'slideover' },
    el('button', { class: 'close', onclick: () => panel.remove() }, '✕'),
    el('h3', {}, `[${cite.n}] ${cite.source}`),
    el('div', { class: 'src' }, `${cite.retriever} match${cite.path ? ' · ' + cite.path : ''}`),
    el('pre', { class: 'passage' }, cite.text),
  );
  document.body.append(panel);
}

export function fmtAgo(s) {
  if (s == null) return '—';
  if (s < 90) return `${s | 0}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
