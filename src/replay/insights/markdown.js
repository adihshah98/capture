/** The minimal markdown the chat answers are allowed to use.

Deliberately tiny: paragraphs, `-` bullets and inline code, nothing else. It runs
on already-escaped text, so widening it means deciding what a model is allowed to
inject into the page.
*/

/** Minimal markdown: paragraphs, `-` bullets, inline code. */
export function chatMarkdown(html) {
  const blocks = html.split(/\n{2,}/);
  return blocks
    .map((block) => {
      const lines = block.split('\n').filter((l) => l.trim());
      if (!lines.length) return '';
      if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
        const items = lines
          .map((l) => `<li>${l.replace(/^\s*[-*]\s+/, '')}</li>`)
          .join('');
        return `<ul>${items}</ul>`;
      }
      return `<p>${lines.join('<br>')}</p>`;
    })
    .join('');
}

export function chatInline(html) {
  return html.replace(/`([^`]+)`/g, '<code>$1</code>');
}
