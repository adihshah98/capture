/** The cohort filter language, as the UI speaks it.

No DOM and no network: a slice is a plain object of five optional fields, and
these are the rules for building one, comparing one, and naming it.

The server owns the vocabulary in capture/analyze/slices.py. This file is the
create-dialog / live-count mirror: one value per field (the pickers are
single-select). `rowMatches` must agree with `row_matches_slice` for those
fields, and `sliceNameBits` must agree with `slice_label_parts`. When they
disagree the live n= and the suggested name silently drift from the cut.
*/

/** Same set as capture.attribution.META_SOURCES. */
const META_SOURCES = new Set(['facebook', 'meta', 'fb', 'instagram', 'ig']);

/** Same map as capture.analyze.slices.OUTCOME_LABELS, plus create-dialog "all". */
export const OUTCOME_LABELS = {
  purchased: 'purchased',
  checkout: 'reached checkout',
  rest: 'did not reach checkout',
  all: 'did not purchase',
};

/** Same order as capture.classify.OUTCOME_VALUES / DEVICE_VALUES. */
export const OUTCOME_VALUES = ['purchased', 'checkout', 'rest'];
export const DEVICE_VALUES = ['mobile', 'desktop'];

/** One group's filter. Every field is a single value; '' means "any". */
export const SLICE_FIELDS = ['outcome', 'device', 'page', 'utm_source', 'utm_campaign'];

export function emptySlice() {
  return { outcome: '', device: '', page: '', utm_source: '', utm_campaign: '' };
}

export function sliceIsEmpty(spec) {
  return SLICE_FIELDS.every((field) => !spec[field]);
}

function listedFacet(counts, field, order) {
  const items = counts[field];
  const keys = order || Object.keys(items).sort((a, b) => items[b] - items[a] || a.localeCompare(b));
  return keys
    .filter((key) => items[key])
    .map((key) => ({
      value: key,
      count: items[key],
      label: field === 'outcome' ? OUTCOME_LABELS[key] || key : key,
    }));
}

/** Distinct slice values on these rows — the Analysis picker is built from this. */
export function facetsFromRows(rows) {
  const counts = {
    outcome: {},
    device: {},
    page: {},
    utm_source: {},
    utm_campaign: {},
  };
  for (const row of rows || []) {
    const outcome = row.outcome || 'rest';
    counts.outcome[outcome] = (counts.outcome[outcome] || 0) + 1;
    if (row.device) counts.device[row.device] = (counts.device[row.device] || 0) + 1;
    if (row.page) counts.page[row.page] = (counts.page[row.page] || 0) + 1;
    const attrs = row.attribution || {};
    for (const key of ['utm_source', 'utm_campaign']) {
      const value = String(attrs[key] || '').trim().toLowerCase();
      if (value) counts[key][value] = (counts[key][value] || 0) + 1;
    }
  }
  return {
    n: (rows || []).length,
    outcome: listedFacet(counts, 'outcome', OUTCOME_VALUES),
    device: listedFacet(counts, 'device', DEVICE_VALUES),
    page: listedFacet(counts, 'page'),
    utm_source: listedFacet(counts, 'utm_source'),
    utm_campaign: listedFacet(counts, 'utm_campaign'),
  };
}

export function isMetaAttribution(attrs) {
  const a = attrs && typeof attrs === 'object' ? attrs : {};
  if (a.fbclid) return true;
  const source = String(a.utm_source || '').trim().toLowerCase();
  return META_SOURCES.has(source);
}

function normAttr(value) {
  return String(value || '').trim().toLowerCase();
}

/** Client-side mirror of row_matches_slice, for the live counts only. */
export function rowMatches(row, spec) {
  if (sliceIsEmpty(spec)) return true;
  if (spec.outcome && (row.outcome || 'rest') !== spec.outcome) return false;
  if (spec.device && (row.device || '') !== spec.device) return false;
  if (spec.page && (row.page || '') !== spec.page) return false;
  const attrs = row.attribution || {};
  if (spec.utm_source) {
    if (spec.utm_source === 'meta') {
      if (!isMetaAttribution(attrs)) return false;
    } else if (spec.utm_source === 'non_meta') {
      if (isMetaAttribution(attrs)) return false;
    } else if (normAttr(attrs.utm_source) !== spec.utm_source) {
      return false;
    }
  }
  if (spec.utm_campaign && normAttr(attrs.utm_campaign) !== spec.utm_campaign) {
    return false;
  }
  return true;
}

export function formatOutcome(value) {
  if (!value) return '';
  return OUTCOME_LABELS[value] || String(value);
}

export function formatSliceValue(field, value) {
  if (!value) return '';
  if (field === 'outcome') return formatOutcome(value);
  if (field === 'utm_source' && value === 'meta') return 'Meta';
  if (field === 'utm_source' && value === 'non_meta') return 'non-Meta';
  return String(value);
}

export function sliceNameBits(sliceState) {
  if (!sliceState) return [];
  const bits = [];
  const outcome = sliceState.outcome;
  if (outcome === 'all' || outcome === 'checkout,rest') {
    bits.push('did not purchase');
  } else if (outcome) {
    bits.push(formatOutcome(outcome));
  }
  for (const field of ['device', 'page']) {
    if (sliceState[field]) bits.push(String(sliceState[field]));
  }
  const source = sliceState.utm_source;
  if (source === 'meta') bits.push('Meta');
  else if (source === 'non_meta') bits.push('non-Meta');
  else if (source) bits.push(String(source));
  if (sliceState.utm_campaign) bits.push(`campaign=${sliceState.utm_campaign}`);
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
