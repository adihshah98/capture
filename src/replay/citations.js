// Session citations in cut insights: "S4 [01:09]".
//
// The model is told to cite write-ups as S<n>, where n is the 1-based position
// in the prompt (see _cut_side_text in analyze.py). That is the same order the
// cut stores in `session_ids`, so n resolves positionally against it. Kept in
// its own module so it can be tested without a DOM.

// The prompt asks for "S4 [01:09]", but the model reliably drifts around that
// shape, and a citation that renders as dead text is worse than a loose match:
//   S8 at [00:08]      a linking word before the stamp
//   S24 (00:12)        parentheses instead of brackets
//   S3 [00:05-00:12]   a range, which a stall is naturally written as
//   S6 [00:17.5]       fractional seconds
// So: allow a short connector, either bracket style, and take the START of a
// range as the seek target. The ordinal is bounded by the group size, not here.
const TIME = String.raw`\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?`;
const CITATION_RE = new RegExp(
  String.raw`\bS(\d+)\s*(?:at\s+|@\s*)?[[(](` +
    TIME +
    String.raw`)(?:\s*[-–—]\s*` +
    TIME +
    String.raw`)?[\])]`,
  'g'
);

/**
 * "01:09" → 69000. Null when the stamp is not a time the player can seek to.
 *
 * Fractional seconds are floored: the player seeks in ms, but a write-up's
 * ".5" is not precise enough to be worth carrying.
 */
export function citationMs(stamp) {
  const parts = String(stamp).split(':');
  if (parts.length < 2 || parts.length > 3) return null;
  const nums = parts.map(Number);
  if (nums.some((n) => !Number.isFinite(n) || n < 0)) return null;
  const [h, m, s] = nums.length === 3 ? nums : [0, nums[0], nums[1]];
  // 90 seconds would have been written as 01:30; anything else is malformed.
  if (m > 59 || s >= 60) return null;
  return Math.floor(((h * 60 + m) * 60 + s) * 1000);
}

/**
 * Wrap each "S4 [01:09]" in a button linking to that session at that moment.
 *
 * `escaped` MUST already be HTML-escaped: this returns markup, so linkifying
 * raw text would let a model-written observation inject HTML. Citations that
 * do not resolve — ordinal past the end of `sessionIds`, unparseable stamp —
 * are left as plain text, since a link to the wrong session is worse than none.
 */
export function linkCitations(escaped, sessionIds, escapeAttr = (s) => s) {
  if (!Array.isArray(sessionIds) || !sessionIds.length) return escaped;
  return String(escaped).replace(CITATION_RE, (whole, n, stamp) => {
    const sid = sessionIds[Number(n) - 1];
    const ms = citationMs(stamp);
    if (!sid || ms == null) return whole;
    return (
      `<button type="button" class="cut-cite" data-cite-sid="${escapeAttr(sid)}"` +
      ` data-cite-t="${ms}" title="Open this session at ${escapeAttr(stamp)}">${whole}</button>`
    );
  });
}
