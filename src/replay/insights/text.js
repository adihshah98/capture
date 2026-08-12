/** Escaping and small formatters for the insights panels.

Pure string in, pure string out. escapeHtml is the only thing between a model
write-up and the DOM, so it is worth being able to test on its own.
*/

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, '&#39;');
}

export function formatViewClock(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n)) return '';
  try {
    return new Date(n).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return String(n);
  }
}

export function formatViewFacet(field, value) {
  if (!value) return '';
  if (field === 'outcome') {
    if (value === 'all') return 'did not purchase';
    if (value === 'purchased') return 'purchased';
    if (value === 'checkout') return 'reached checkout';
  }
  return String(value);
}

export function progressText(body) {
  const prog = body.progress || {};
  const batch = body.batch ? ' · Gemini batch queued' : '';
  const phase =
    body.status === 'idle'
      ? 'Sampled — not analyzed yet · '
      : body.status === 'paused'
        ? 'Paused · '
        : '';
  const sample = body.sample && typeof body.sample === 'object' ? body.sample : null;
  const requested = sample ? Number(sample.requested || 0) : 0;
  const matched = sample ? Number(sample.matched || 0) : 0;
  const shortfall =
    requested > 0 && matched < requested
      ? sample.capped
        ? ` · sampled ${matched} of ${requested} (scan hit candidate cap)`
        : ` · sampled ${matched} of ${requested} (pool had fewer matches)`
      : '';
  return (
    phase +
    `${prog.done || 0} / ${prog.total || 0} done · ${prog.running || 0} running · ` +
    `${prog.queued || 0} queued` +
    (prog.error ? ` · ${prog.error} failed` : '') +
    (prog.stale ? ` · ${prog.stale} missing a write-up` : '') +
    shortfall +
    batch
  );
}

/** The server refuses a cohort larger than this; see COHORT_MAX_SESSIONS in
capture/analyze. Kept here because clampGroupSize is what enforces it. */
export const COHORT_MAX_SESSIONS = 500;

export function clampGroupSize(raw, fallback = 100) {
  const n = Math.round(Number(raw) || 0);
  if (n < 1) return fallback;
  return Math.min(COHORT_MAX_SESSIONS, n);
}

export function localDateTimeMs(value) {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/** Sessions with a finished write-up. A row is only counted when it has text —
a status of done with nothing written is stale, not analyzed. */
export function analyzedCount(cohort) {
  if (!cohort) return 0;
  return (cohort.sessions || []).filter((s) => s.status === 'done' && s.text).length;
}
