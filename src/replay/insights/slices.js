/** The cohort filter language, as the UI speaks it.

No DOM and no network: a slice is a plain object of five optional fields, and
these are the rules for building one, comparing one, and naming it. The server
has the same vocabulary in capture/analyze/slices.py — the two must agree about
field names or a filter silently matches nothing.
*/

/** One group's filter. Every field is a single value; '' means "any". */
export const SLICE_FIELDS = ['outcome', 'device', 'page', 'utm_source', 'utm_campaign'];

export function emptySlice() {
  return { outcome: '', device: '', page: '', utm_source: '', utm_campaign: '' };
}

export function sliceIsEmpty(spec) {
  return SLICE_FIELDS.every((field) => !spec[field]);
}

/** Client-side mirror of row_matches_slice, for the live counts only. */
export function rowMatches(row, spec) {
  if (sliceIsEmpty(spec)) return true;
  if (spec.outcome && (row.outcome || 'rest') !== spec.outcome) return false;
  if (spec.device && (row.device || '') !== spec.device) return false;
  if (spec.page && (row.page || '') !== spec.page) return false;
  const attrs = row.attribution || {};
  const norm = (v) => String(v || '').trim().toLowerCase();
  if (spec.utm_source && norm(attrs.utm_source) !== spec.utm_source) return false;
  if (spec.utm_campaign && norm(attrs.utm_campaign) !== spec.utm_campaign) return false;
  return true;
}

export function formatCreateOutcome(value) {
  if (value === 'all') return 'non-purchase';
  if (value === 'purchased') return 'purchased';
  if (value === 'checkout') return 'checkout';
  return value || '';
}

export function sliceNameBits(sliceState) {
  if (!sliceState) return [];
  const bits = [];
  for (const field of SLICE_FIELDS) {
    const value = sliceState[field];
    if (!value) continue;
    bits.push(field === 'outcome' ? formatCreateOutcome(value) : String(value));
  }
  return bits;
}

export function suggestCohortName(opts) {
  const stamp = new Date().toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
  const parts = [stamp];
  if (opts.addB) {
    const aBits = sliceNameBits(opts.sliceA);
    const bBits = sliceNameBits(opts.sliceB);
    if (aBits.length || bBits.length) {
      parts.push(
        `A ${aBits.length ? aBits.join(' ') : 'any'} / B ${
          bBits.length ? bBits.join(' ') : 'any'
        }`
      );
    }
  } else {
    parts.push(...sliceNameBits(opts.sliceA));
  }
  return parts.join(' · ').slice(0, 120);
}
