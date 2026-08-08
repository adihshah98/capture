export function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function isLive(session) {
  return Date.now() - session.last_seen < 30000;
}

export function sessionDuration(session) {
  if (session.duration != null) return session.duration;
  const end = session.last_active
    ? Math.min(session.last_seen, session.last_active + 1500)
    : session.last_seen;
  return end - session.first_seen;
}

export function formatDuration(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function formatOffset(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export function pagePath(url) {
  try {
    const u = new URL(url);
    return u.pathname + u.search;
  } catch {
    return url || '';
  }
}

export function sourceLabel(session) {
  const a = session.attribution;
  if (!a) return 'direct';
  if (a.utm_source && a.utm_campaign) return `${a.utm_source}/${a.utm_campaign}`;
  return a.utm_source || a.utm_medium || (a.fbclid ? 'facebook' : 'direct');
}

export function orderAmount(order) {
  if (!order || typeof order.total !== 'number') return '';
  return `${order.total.toFixed(2)}${order.currency ? ' ' + order.currency : ''}`;
}

export function sessionOutcome(session) {
  const order = session.order || {};
  if (isLive(session)) return 'live';
  if (order.purchased_at) return 'purchased';
  if (session.interactions && session.interactions.rage) return 'rage';
  if (order.checkout_started_at) return 'checkout';
  return '';
}

export function sessionHeadline(session) {
  const order = session.order || {};
  const rage = (session.interactions && session.interactions.rage) || 0;
  const clicks = (session.interactions && session.interactions.clicks) || 0;
  const path = pagePath(session.url).split('?')[0] || '/';
  if (isLive(session)) return `Active now on ${path}`;
  if (order.purchased_at) {
    const amount = orderAmount(order);
    return amount ? `Purchased — ${amount}` : 'Purchased';
  }
  if (rage) return `Rage-clicked ${rage}× on ${path}`;
  if (order.checkout_started_at) return 'Reached checkout, did not buy';
  if (sessionDuration(session) < 10000) return `Bounced from ${path}`;
  if (session.interactions && !clicks) return `Scrolled ${path}, never clicked`;
  if (session.windows > 1) return `Browsed ${session.windows} tabs from ${path}`;
  return `Browsed ${path}`;
}

export function hostPath(url) {
  try {
    const u = new URL(url);
    return u.host + u.pathname;
  } catch {
    return url || '';
  }
}
