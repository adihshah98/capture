export function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
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

export function hostPath(url) {
  try {
    const u = new URL(url);
    return u.host + u.pathname;
  } catch {
    return url || '';
  }
}
