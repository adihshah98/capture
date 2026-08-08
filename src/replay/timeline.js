import { formatOffset, pagePath } from './format.js';
import { gapEntries } from './gaps.js';
import { player } from './session-player.js';
import { clearUrlTable, renderUrlTable } from './url-table.js';

const timelineListEl = document.getElementById('timeline-list');
const timelineCountEl = document.getElementById('timeline-count');

let timelineRows = [];
export let syncStart = 0;

export function setSyncStart(t) {
  syncStart = t;
}

export function clearTimeline() {
  timelineListEl.innerHTML = '';
  timelineRows = [];
  clearUrlTable();
}

export function syncTimeline(currentMs) {
  const rows = timelineRows;
  let currentIdx = -1;
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].t - syncStart <= currentMs) currentIdx = i;
    else break;
  }
  if (rows.lastIdx === currentIdx) return;
  rows.lastIdx = currentIdx;
  rows.forEach((row, i) => {
    row.el.classList.toggle('current', i === currentIdx);
    row.el.classList.toggle('future', i > currentIdx);
  });
  if (currentIdx >= 0 && !rows[currentIdx].el.parentElement.hidden) {
    rows[currentIdx].el.scrollIntoView({ block: 'nearest' });
  }
}

export function renderTimeline(events, navigations, session, showOrder = true) {
  const markers = events.filter((e) => e.type === 5 && e.data && e.data.tag);
  const end = events.length ? events[events.length - 1].timestamp : 0;
  const entries = gapEntries(markers, end);

  for (const event of markers) {
    const tag = event.data.tag;
    if (tag === 'capture-trigger') {
      const reason = (event.data.payload && event.data.payload.reason) || 'unknown';
      entries.push({ t: event.timestamp, kind: 'trigger', css: 'kind-trigger', detail: `kept because: ${reason}` });
    } else if (tag === 'capture-click') {
      const payload = event.data.payload || {};
      const label = payload.text ? `“${payload.text}”` : payload.selector || '';
      entries.push({
        t: event.timestamp,
        kind: payload.rage ? 'rage' : 'click',
        css: payload.rage ? 'kind-rage' : 'kind-click',
        detail: payload.rage ? `rage click — ${label}` : label,
        title: payload.selector,
      });
    }
  }
  for (const nav of navigations) {
    entries.push({ t: nav.t, kind: 'page', css: 'kind-page', detail: pagePath(nav.url).split('?')[0], title: nav.url });
  }
  entries.sort((a, b) => a.t - b.t);

  const order = showOrder && session && session.order;
  if (events.length && order && (order.purchased_at || order.checkout_started_at)) {
    const amount =
      typeof order.total === 'number'
        ? ` — purchased ${order.total.toFixed(2)}${order.currency ? ' ' + order.currency : ''}`
        : '';
    const first = events[0].timestamp;
    const last = events[events.length - 1].timestamp;
    const at = order.checkout_started_at;
    entries.push({
      t: at ? Math.min(Math.max(at, first), last) : last,
      kind: order.purchased_at ? 'purchased' : 'checkout',
      css: 'kind-order',
      detail: `continued to checkout${order.purchased_at ? amount : ''}`,
    });
  }

  const start = events.length ? events[0].timestamp : entries.length ? entries[0].t : 0;
  timelineCountEl.textContent = entries.length ? `(${entries.length})` : '';
  clearTimeline();
  renderUrlTable(navigations);
  if (entries.length === 0) {
    timelineListEl.innerHTML = '<li class="empty">No clicks, triggers, idle gaps or navigations in this recording.</li>';
    return;
  }
  for (const entry of entries) {
    const li = document.createElement('li');
    li.className = entry.css;
    li.innerHTML = `<span class="ts"></span><span class="kind"></span><span class="detail"></span>`;
    const ts = li.querySelector('.ts');
    ts.textContent = formatOffset(entry.t - start);
    ts.title = new Date(entry.t).toLocaleTimeString();
    li.querySelector('.kind').textContent = entry.kind;
    li.querySelector('.detail').textContent = entry.detail;
    if (entry.title) li.title = entry.title;
    li.addEventListener('click', () => player && player.seekTo(entry.t - syncStart));
    timelineRows.push({ el: li, t: entry.t });
    timelineListEl.appendChild(li);
  }
}
