// App shell: hash router, nav, health polling, auth prompt, iOS keyboard fit.
import { get, post } from '/js/api.js';
import { el, toast, banner, hideBanner } from '/js/ui.js';
import * as dashboard from '/js/views/dashboard.js';
import * as chat from '/js/views/chat.js';
import * as tunes from '/js/views/tunes.js';
import * as journal from '/js/views/journal.js';
import * as proposals from '/js/views/proposals.js';
import * as dyno from '/js/views/dyno.js';
import * as lenovo from '/js/views/lenovo.js';

const VIEWS = {
  dashboard: { mod: dashboard, label: 'Dashboard', ico: '⌂' },
  chat: { mod: chat, label: 'Co-pilot', ico: '💬' },
  tunes: { mod: tunes, label: 'Tunes', ico: '⛭' },
  lenovo: { mod: lenovo, label: 'Lenovo', ico: '⇄' },
  journal: { mod: journal, label: 'Journal', ico: '✎' },
  proposals: { mod: proposals, label: 'Proposals', ico: '☑' },
  dyno: { mod: dyno, label: 'Dyno', ico: '◉' },
};
const MOBILE_TABS = ['dashboard', 'chat', 'tunes', 'lenovo', 'journal', 'proposals'];

export const state = { profile: null, health: null };

let current = null;

function route() {
  const [name, ...rest] = (location.hash.slice(2) || 'dashboard').split('/');
  const v = VIEWS[name] || VIEWS.dashboard;
  document.querySelectorAll('#nav a, #tabbar a').forEach((a) =>
    a.classList.toggle('active', a.dataset.view === name));
  const container = document.getElementById('view');
  current?.unmount?.();
  container.textContent = '';
  current = v.mod;
  v.mod.mount(container, rest, state);
}

function buildNav() {
  const nav = document.getElementById('nav');
  for (const [name, v] of Object.entries(VIEWS)) {
    nav.append(el('a', { href: `#/${name}`, 'data-view': name },
      el('span', { class: 'ico' }, v.ico), v.label));
  }
  const tab = document.getElementById('tabbar');
  for (const name of MOBILE_TABS) {
    const v = VIEWS[name];
    tab.append(el('a', { href: `#/${name}`, 'data-view': name },
      el('span', { class: 'ico' }, v.ico), v.label));
  }
}

async function pollHealth() {
  try {
    state.health = await get('/api/health');
    const h = state.health;
    const rail = document.getElementById('rail-health');
    rail.textContent = '';
    rail.append(
      el('div', {}, `ollama ${h.ollama?.ok ? '●' : '○'} es ${h.es?.ok ? '●' : '○'} nas ${h.nas?.ok ? '●' : '○'}`));
    if (!h.ollama?.ok) banner('Ollama is down — chat unavailable', '', { label: 'Reload', fn: () => location.reload() });
    else hideBanner();
  } catch (e) {
    if (e.message !== 'auth required') banner('Server unreachable', '', { label: 'Reload', fn: () => location.reload() });
  }
}

function authPrompt() {
  const container = document.getElementById('view');
  container.textContent = '';
  const input = el('input', { type: 'password', placeholder: 'access token' });
  container.append(el('div', { class: 'card', style: 'max-width:360px;margin:40px auto' },
    el('h2', {}, 'Unlock'),
    input,
    el('div', { style: 'margin-top:10px' },
      el('button', {
        class: 'primary',
        onclick: async () => {
          try { await post('/api/auth', { token: input.value }); location.reload(); }
          catch { toast('Bad token', 'bad'); }
        },
      }, 'Unlock')),
  ));
}

// iOS: the keyboard does not resize the layout viewport — fit the app to the
// visualViewport and hide the tab bar while typing so the composer stays visible.
function fitViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const app = document.getElementById('app');
  const fit = () => {
    const keyboard = vv.height < window.innerHeight - 60;
    document.body.classList.toggle('keyboard-open', keyboard);
    app.style.height = keyboard ? `${vv.height}px` : '';
    app.style.transform = keyboard ? `translateY(${vv.offsetTop}px)` : '';
    if (keyboard) document.querySelector('.chat-log')?.scrollTo(0, 1e9);
  };
  vv.addEventListener('resize', fit);
  vv.addEventListener('scroll', fit);
}

window.addEventListener('tmax-auth-needed', authPrompt);
window.addEventListener('hashchange', route);
window.addEventListener('error', () =>
  banner('Something broke', '', { label: 'Reload', fn: () => location.reload() }));

buildNav();
fitViewport();
route();
pollHealth();
setInterval(pollHealth, 20000);
