// Lenovo laptop portal — same pipe as the desktop icon (http://127.0.0.1:9191/).
import { el, toast } from '/js/ui.js';

const PORTAL = 'http://127.0.0.1:9191/';

export async function mount(root) {
  root.append(
    el('h1', { class: 'page' }, 'Lenovo portal'),
    el('p', { class: 'muted' },
      'Send and receive .tbw files with the Lenovo tuning laptop. ',
      'Same window as the desktop icon. Laptop can also open ',
      el('code', {}, 'http://192.168.1.201:9191/'),
      '.'),
  );
  let live = false;
  try {
    const r = await fetch(PORTAL + 'api/status', { signal: AbortSignal.timeout(2500) });
    live = r.ok;
  } catch {
    live = false;
  }
  if (!live) {
    root.append(el('div', { class: 'card' },
      el('p', {}, 'Portal is not up on :9191.'),
      el('button', {
        class: 'primary',
        onclick: () => { location.reload(); },
      }, 'Retry'),
      el('p', { class: 'muted' }, 'On the tower: systemctl --user start lenovo-file-drop.service  or click the Lenovo Portal desktop icon.')));
    toast('Lenovo portal offline', 'warn');
    return;
  }
  const frame = el('iframe', {
    src: PORTAL,
    title: 'Lenovo Portal',
    style: 'width:100%;height:calc(100dvh - 120px);border:1px solid var(--line);border-radius:10px;background:#111318',
  });
  root.append(frame);
}
