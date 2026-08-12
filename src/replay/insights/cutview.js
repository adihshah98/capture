/** Turning a cut payload into HTML.

Pure: a cut object in, a string out. The DOM half is one assignment in index.js,
which is what makes the interesting part — how a model's write-up becomes markup —
reachable from a test.

Everything the model produced goes through escapeHtml before it reaches the page.
Its output is untrusted text, so widening what survives that is a security
decision rather than a formatting one.
*/

import { linkCitations } from '../citations.js';
import { escapeAttr, escapeHtml } from './text.js';

export const CUT_SECTIONS = [
  { key: 'journey', title: 'How they moved through the site', empty: 'The write-ups do not describe a common route.' },
  { key: 'attention', title: 'What held their attention', empty: 'Nothing held attention consistently across these sessions.' },
  { key: 'intent', title: 'Signals of intent', empty: 'No consistent buying signals stood out.' },
  { key: 'blockers', title: 'What stopped them', cls: 'friction', empty: 'No blockers were reported in these write-ups.' },
  { key: 'caveats', title: 'Worth checking', cls: 'caveats' },
];

/** Escaped observation text with each "S4 [01:09]" turned into a seek link. */
export function withCitations(text, sessionIds, citations) {
  return linkCitations(escapeHtml(text || ''), sessionIds, escapeAttr, citations);
}

/** An insight item: observation, its share of the group, and what to do. */
export function insightItem(item, id, sessionIds, citations) {
  if (typeof item === 'string') {
    return `<li>${withCitations(item, sessionIds, citations)}</li>`;
  }
  const share = item.share
    ? `<span class="cut-share">${escapeHtml(item.share)}</span>`
    : '';
  const action = item.action
    ? `<p class="cut-action">${escapeHtml(item.action)}</p>`
    : '';
  const idAttr = id ? ` id="${escapeAttr(id)}"` : '';
  const observation = withCitations(item.observation, sessionIds, citations);
  return `<li${idAttr} class="cut-insight">${share}${observation}${action}</li>`;
}

/** action text -> the id of the insight that proposed it. Caveats are skipped:
they are context, not something to act on. */
export function actionTargets(analysis) {
  const targets = new Map();
  for (const { key } of CUT_SECTIONS) {
    if (key === 'caveats') continue;
    const items = analysis[key] || [];
    items.forEach((item, i) => {
      if (!item || typeof item !== 'object') return;
      const action = typeof item.action === 'string' ? item.action.trim().toLowerCase() : '';
      if (!action || targets.has(action)) return;
      targets.set(action, `cut-insight-${key}-${i}`);
    });
  }
  return targets;
}

/** The insight a recommendation refers back to, matched loosely because the
model rarely repeats an action verbatim. Longest overlap wins. */
export function targetForRecommendation(line, targets) {
  const key = String(line || '').trim().toLowerCase();
  if (!key) return null;
  if (targets.has(key)) return targets.get(key);
  let best = null;
  let bestLen = 0;
  for (const [action, id] of targets) {
    if (action.length <= bestLen) continue;
    if (key.includes(action) || action.includes(key)) {
      best = id;
      bestLen = action.length;
    }
  }
  return best;
}

export function recommendationItem(line, targetId) {
  const text = escapeHtml(line);
  if (!targetId) return `<li>${text}</li>`;
  return `<li>
    <button type="button" class="cut-rec-link" data-scroll-to="${escapeAttr(targetId)}">${text}</button>
  </li>`;
}

/** The cut body, exactly as index.js assigns it to innerHTML. */
export function cutHtml(body) {
  const analysis = body.analysis || {};
  const n = body.n || 0;

  // Small groups are the norm here; say so rather than letting counts imply rigour.
  const thin = n > 0 && n < 3 ? '<span class="cut-thin">thin evidence</span>' : '';

  const headline = analysis.headline
    ? `<div class="cut-headline">${escapeHtml(analysis.headline)}</div>`
    : '';

  const targets = actionTargets(analysis);
  // The stored map is what the model was shown; session_ids is the positional
  // fallback for cuts written before the map was recorded.
  const sessionIds = body.session_ids || [];
  const citations = body.citations || null;

  const sections = CUT_SECTIONS.map(({ key, title, cls, empty }) => {
    const items = analysis[key] || [];
    // Caveats have no empty copy: an absent caveat list is not worth a block.
    if (!items.length && !empty) return '';
    const count = key === 'journey' ? `<span class="n">(n=${n})</span>${thin}` : '';
    return `<div class="cut-block ${cls || ''}" id="cut-section-${escapeAttr(key)}">
      <h4>${escapeHtml(title)}${count}</h4>
      ${
        items.length
          ? `<ul>${items
              .map((item, i) =>
                insightItem(
                  item,
                  key === 'caveats' ? null : `cut-insight-${key}-${i}`,
                  sessionIds,
                  citations
                )
              )
              .join('')}</ul>`
          : `<p class="cut-empty">${escapeHtml(empty)}</p>`
      }
    </div>`;
  }).join('');

  // Punch list after the evidence so recommendations close the cut. Absent on
  // thin/empty cuts — no empty block.
  const recs = analysis.recommendations || [];
  const recommendations = recs.length
    ? `<div class="cut-block recommendations">
        <h4>Recommendations</h4>
        <ul>${recs
          .map((line) => recommendationItem(line, targetForRecommendation(line, targets)))
          .join('')}</ul>
      </div>`
    : '';

  const meta = `<p class="cut-meta">
    Read from ${n} analyzed session write-up(s) in this cohort.
    Written by ${escapeHtml(body.model || 'the model')}.
    Cohort membership is fixed, so this cut is cached and served without another
    model call — use Run analysis again to ask once more.
  </p>`;

  return headline + sections + recommendations + meta;
}
