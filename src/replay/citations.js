// Session citations in cut insights: "S4 [01:09]", or a bare "S4".
//
// The model is told to cite write-ups as S<n>, where n is the 1-based position
// in the prompt (see _cut_side_text in analyze.py). That is the same order the
// cut stores in `session_ids`, so n resolves positionally against it. Kept in
// its own module so it can be tested without a DOM.
//
// The ordinal is the anchor and the timestamp is optional, because the model
// only has a moment to point at when the claim is about a moment. Counting
// claims cite bare ordinals, and both forms routinely appear in one sentence:
//
//   "4 of 7 purchased the default (S1, S2, S4, S7), while 3 switched to
//    '30-Night' (S3 [01:10], S5 [00:10], S6 [00:36])"
//
// Requiring a stamp left the first group dead while the second linked. A bare
// citation opens its session at the start; that is still the right session.

// MM:SS, HH:MM:SS, optional fractional seconds.
const TIME = String.raw`\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?`;

// S<n>, then optionally: a connector ("at", "@"), a bracket or paren, a time
// or time range, close. Everything after the ordinal is optional, so a bare
// "S4" matches and a decorated "S4 at [00:05-00:12]" matches the whole span.
//
// A bare S<n> is a loose pattern, so it is fenced on both sides.
//
// Leading (?<![\w$£€#]) keeps "CSS3", "AS1" and "$S5" out. The trailing
// lookahead requires the ordinal to end a clause or a citation list — a comma,
// a closing bracket, "and", or end of string — so "the S3 model" and "size S4
// shirt" stay prose. A following word means it is a noun phrase, not a cite.
//
// A stamped citation skips the fence entirely: the bracket disambiguates it.
const STAMP =
  String.raw`\s*(?:at\s+|@\s*)?[[(](` +
  TIME +
  String.raw`)(?:\s*[-–—]\s*` +
  TIME +
  String.raw`)?[\])]`;
const CITATION_RE = new RegExp(
  String.raw`(?<![\w$£€#])S(\d{1,3})(?:` +
    STAMP +
    String.raw`|(?=\s*(?:[,;:)\].]|and\b|&|$)))`,
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
 * Wrap each "S4" / "S4 [01:09]" in a button linking to that session.
 *
 * `escaped` MUST already be HTML-escaped: this returns markup, so linkifying
 * raw text would let a model-written observation inject HTML.
 *
 * Left as plain text when the citation cannot be trusted to point somewhere:
 * an ordinal past the end of `sessionIds`, or a stamp that is present but
 * unparseable — a link to the wrong session is worse than no link. A stamp
 * that is merely absent is fine; the session opens at its start.
 */
export function linkCitations(escaped, sessionIds, escapeAttr = (s) => s) {
  if (!Array.isArray(sessionIds) || !sessionIds.length) return escaped;
  return String(escaped).replace(CITATION_RE, (whole, n, stamp) => {
    const sid = sessionIds[Number(n) - 1];
    if (!sid) return whole;
    // A written-but-malformed stamp is a signal the match is not what it
    // looks like, so decline it rather than silently seeking to 0.
    const ms = stamp === undefined ? 0 : citationMs(stamp);
    if (ms == null) return whole;
    const title = stamp
      ? `Open this session at ${escapeAttr(stamp)}`
      : 'Open this session';
    return (
      `<button type="button" class="cut-cite" data-cite-sid="${escapeAttr(sid)}"` +
      ` data-cite-t="${ms}" title="${title}">${whole}</button>`
    );
  });
}
