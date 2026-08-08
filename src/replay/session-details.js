import { formatBytes, formatDuration, formatTime, isLive, sessionDuration } from './format.js';

const detailsEl = document.getElementById('session-details');

export function clearSessionDetails() {
  detailsEl.innerHTML = '<div class="empty">Device, duration and visitor details appear here.</div>';
}

export function renderSessionDetails(session) {
  if (!session) {
    clearSessionDetails();
    return;
  }
  const rows = [];
  const device = session.device;
  if (device) {
    rows.push(['device', `${device.width < 768 ? 'mobile' : 'desktop'} · ${device.width} × ${device.height}`]);
  }
  rows.push(['started', formatTime(session.first_seen)]);
  rows.push([
    'duration',
    isLive(session)
      ? 'live'
      : session.windows > 1
        ? 'multiple tabs'
        : formatDuration(sessionDuration(session)),
  ]);
  if (session.visitor_id) rows.push(['visitor', String(session.visitor_id).slice(0, 8)]);
  const inter = session.interactions;
  if (inter && inter.clicks) rows.push(['clicks', String(inter.clicks)]);
  if (inter && inter.rage) rows.push(['rage clicks', String(inter.rage)]);
  const order = session.order;
  if (order && (order.purchased_at || order.checkout_started_at)) {
    const amount =
      typeof order.total === 'number'
        ? ` · ${order.total.toFixed(2)}${order.currency ? ' ' + order.currency : ''}`
        : '';
    rows.push(['order', (order.purchased_at ? 'purchased' : 'reached checkout') + amount]);
  }
  rows.push(['recording', `${session.chunk_count} chunks · ${formatBytes(session.bytes)}`]);

  detailsEl.innerHTML = '';
  const table = document.createElement('table');
  for (const [key, value] of rows) {
    const row = document.createElement('tr');
    const k = document.createElement('td');
    k.className = 'k';
    k.textContent = key;
    const v = document.createElement('td');
    v.textContent = value;
    row.append(k, v);
    table.appendChild(row);
  }
  detailsEl.appendChild(table);
}
