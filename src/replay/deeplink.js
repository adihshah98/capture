export function parseHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  return { p: h.get('p') || '', s: h.get('s') || '', t: Math.max(0, Number(h.get('t')) || 0) };
}

export function writeHash(next) {
  const h = new URLSearchParams();
  if (next.p) h.set('p', next.p);
  if (next.s) h.set('s', next.s);
  if (next.t) h.set('t', String(next.t));
  const hash = h.toString();
  try {
    history.replaceState(null, '', hash ? `#${hash}` : location.pathname + location.search);
  } catch {
  }
}
