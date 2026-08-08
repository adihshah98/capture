import { formatBytes, formatDuration, isLive, orderAmount, sessionDuration, sourceLabel } from './format.js';

const stripEl = document.getElementById('session-strip');
const urlParamsEl = document.getElementById('url-params');

const ICON = {
  desktop: '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M9 20h6M12 17v3"/>',
  mobile: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.2 2"/>',
  pointer: '<path d="M6 3.5 18.5 11l-5.4 1.4L11 18z"/>',
  bag: '<path d="M5 8h14l-1 12H6z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/>',
  tag: '<path d="M11 3H4v7l10 10 7-7z"/><circle cx="7.8" cy="7.8" r="1.2"/>',
  disc: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.6"/>',
};

const svg = (paths) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ` +
  `stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;

function item(icon, html, cls = '') {
  const el = document.createElement('span');
  el.className = `item ${cls}`.trim();
  el.innerHTML = (icon ? svg(icon) : '') + `<span class="num">${html}</span>`;
  return el;
}

export function clearSessionDetails() {
  stripEl.querySelectorAll(':scope > :not(#url-params)').forEach((el) => el.remove());
  closeParams();
}

function closeParams() {
  urlParamsEl.hidden = true;
  const toggle = document.getElementById('params-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
}

export function renderSessionDetails(session) {
  clearSessionDetails();
  if (!session) return;

  const parts = [];
  const device = session.device;
  if (device) {
    const mobile = device.width < 768;
    parts.push(
      item(mobile ? ICON.mobile : ICON.desktop, `${mobile ? 'mobile' : 'desktop'} ${device.width}×${device.height}`)
    );
  }
  parts.push(
    isLive(session)
      ? item(ICON.clock, 'live now', 'live')
      : item(
          ICON.clock,
          session.windows > 1
            ? `${formatDuration(sessionDuration(session))} · ${session.windows} tabs`
            : formatDuration(sessionDuration(session))
        )
  );

  const inter = session.interactions;
  if (inter && inter.clicks) {
    parts.push(item(ICON.pointer, `${inter.clicks} click${inter.clicks > 1 ? 's' : ''}`));
  }
  if (inter && inter.rage) {
    parts.push(item(null, `${inter.rage} rage click${inter.rage > 1 ? 's' : ''}`, 'bad'));
  }

  const order = session.order;
  if (order && order.purchased_at) {
    const amount = orderAmount(order);
    parts.push(item(ICON.bag, amount ? `purchased ${amount}` : 'purchased', 'good'));
  } else if (order && order.checkout_started_at) {
    parts.push(item(ICON.bag, 'reached checkout', 'warnish'));
  }

  const toggle = document.createElement('button');
  toggle.id = 'params-toggle';
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.title = 'Show the URLs this session visited and their query params';
  toggle.innerHTML =
    svg(ICON.tag) +
    `<span>${sourceLabel(session)}</span>` +
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ` +
    `stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`;
  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = urlParamsEl.hidden;
    urlParamsEl.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  });
  parts.push(toggle);

  parts.push(item(ICON.disc, `${session.chunk_count} chunks · ${formatBytes(session.bytes)}`));

  const frag = document.createDocumentFragment();
  parts.forEach((el, i) => {
    if (i) {
      const sep = document.createElement('span');
      sep.className = 'sep';
      sep.textContent = '·';
      frag.appendChild(sep);
    }
    frag.appendChild(el);
  });
  stripEl.insertBefore(frag, urlParamsEl);
}

addEventListener('click', (e) => {
  if (!urlParamsEl.hidden && !e.target.closest('#url-params, #params-toggle')) closeParams();
});
addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeParams();
});
