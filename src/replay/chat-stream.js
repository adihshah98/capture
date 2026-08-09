// SSE parsing and safe incremental rendering for the insights chat.
//
// Kept out of insights.js so both halves can be tested without a DOM: the
// frame parser is a pure function over strings, and the "how much of this
// answer is safe to linkify yet" question is a pure function over text.

/**
 * Parse whole SSE frames out of a growing buffer.
 *
 * Returns [frames, rest] — `rest` is the trailing partial frame, which the
 * caller feeds back in with the next chunk. Frames are separated by a blank
 * line; anything without an `event:` line is ignored rather than guessed at.
 */
export function parseFrames(buffer) {
  const parts = String(buffer).split('\n\n');
  // The last piece is either a partial frame or '' when the buffer ended
  // cleanly on a separator; either way it is not ready to emit.
  const rest = parts.pop() ?? '';
  const frames = [];
  for (const part of parts) {
    let event = '';
    const dataLines = [];
    for (const line of part.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!event) continue;
    let data = {};
    if (dataLines.length) {
      try {
        data = JSON.parse(dataLines.join('\n'));
      } catch {
        // A frame we cannot read is worse than no frame — skip it rather
        // than surfacing half-parsed text as an answer.
        continue;
      }
    }
    frames.push({ event, data });
  }
  return [frames, rest];
}

// A citation is "S4" or "S4 [01:09]". Mid-stream, "S4" may be the start of
// "S4 [01:09]" — linkifying it early would produce a link to the session's
// start that then has to be undone, and "S4 [00:" would linkify as a bare
// ordinal with stray text after it. So we hold back a suffix that could still
// grow into a citation and render only the settled prefix.
//
// The longest thing we might be mid-way through is an ordinal plus a full
// stamp with a range: "S123 at [00:00:00.0-00:00:00.0]" — but rather than
// counting characters, find where the trailing candidate starts.
const TRAILING_PARTIAL = /S\d{0,3}(?:\s*(?:at\s*|@\s*)?[[(][\d:.\s–—-]*)?$/;

/**
 * Split streamed text into [settled, pending].
 *
 * `settled` is safe to escape and linkify — no citation can begin in it and
 * remain unfinished. `pending` is the tail that might still grow into one; it
 * is rendered as plain text until more arrives (or the stream ends, at which
 * point the caller renders everything as settled).
 */
export function splitSettled(text) {
  const s = String(text);
  const match = s.match(TRAILING_PARTIAL);
  if (!match || !match[0]) return [s, ''];
  return [s.slice(0, match.index), match[0]];
}
