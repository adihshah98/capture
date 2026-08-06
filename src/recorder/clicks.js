const CLICKABLE =
  'a,button,select,summary,label,input[type=button],input[type=submit],input[type=reset],input[type=checkbox],input[type=radio],[role=button],[onclick]';
const MAX_LEVELS = 5;
const MAX_SELECTOR = 200;
const MAX_TEXT = 64;

const scrub = (raw) =>
  raw
    .replace(/\s+/g, ' ')
    .replace(/\d[\d\s()+-]{6,}\d/g, '…')
    .trim()
    .slice(0, MAX_TEXT)
    .trim();

export function selectorFor(el) {
  const parts = [];
  for (let node = el; node && node.tagName && parts.length < MAX_LEVELS; node = node.parentElement) {
    const tag = node.tagName.toLowerCase();
    if (tag === 'body' || tag === 'html') break;
    if (node.id) {
      parts.unshift(`${tag}#${node.id}`);
      break;
    }
    const classes = (typeof node.className === 'string' ? node.className : '')
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2);
    let part = tag + classes.map((c) => `.${c}`).join('');
    if (!classes.length) {
      let nth = 1;
      for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) {
        if (sib.tagName === node.tagName) nth += 1;
      }
      let laterTwin = false;
      for (let sib = node.nextElementSibling; sib && !laterTwin; sib = sib.nextElementSibling) {
        laterTwin = sib.tagName === node.tagName;
      }
      if (nth > 1 || laterTwin) part += `:nth-of-type(${nth})`;
    }
    parts.unshift(part);
  }
  return parts.join(' > ').slice(0, MAX_SELECTOR);
}

function collectText(el, depth, maskSelector) {
  let out = '';
  for (const node of el.childNodes || []) {
    if (out.length > MAX_TEXT * 2) break;
    if (node.nodeType === 3) {
      out += ' ' + (node.textContent || '');
    } else if (node.nodeType === 1 && depth > 1) {
      if (maskSelector && node.matches && node.matches(maskSelector)) continue;
      out += ' ' + collectText(node, depth - 1, maskSelector);
    }
  }
  return out;
}

export function textFor(el, maskSelector) {
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'input') {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'button' || type === 'submit' || type === 'reset') {
      return scrub(el.getAttribute('value') || '');
    }
    return '';
  }
  const text = scrub(collectText(el, 3, maskSelector));
  if (text) return text;
  for (const attr of ['aria-label', 'title', 'alt']) {
    const value = el.getAttribute && el.getAttribute(attr);
    if (value) return scrub(value);
  }
  return '';
}

export function createRageTracker({ count = 3, radiusPx = 30, windowMs = 1000 } = {}) {
  let recent = [];
  return (x, y, t) => {
    recent = recent.filter((c) => t - c.t < windowMs && Math.hypot(c.x - x, c.y - y) < radiusPx);
    recent.push({ x, y, t });
    if (recent.length >= count) {
      recent = [];
      return true;
    }
    return false;
  };
}

export function startClickCapture(emit, { blockSelector, maskTextSelector } = {}) {
  const blocked = ['.capture-block', blockSelector].filter(Boolean).join(',');
  const masked = ['.capture-mask', maskTextSelector].filter(Boolean).join(',');
  const isRageBurst = createRageTracker();

  const onClick = (event) => {
    if (!event.isTrusted) return;
    const target = event.target;
    if (!target || !target.closest || target.closest(blocked)) return;
    const rage = isRageBurst(event.clientX, event.clientY, Date.now());
    let el = target.closest(CLICKABLE);
    if (!el && (rage || getComputedStyle(target).cursor === 'pointer')) el = target;
    if (!el) return;
    emit({
      selector: selectorFor(el),
      text: el.closest(masked) ? '' : textFor(el, masked),
      x: Math.round(event.clientX),
      y: Math.round(event.clientY),
      ...(rage ? { rage: true } : {}),
    });
  };

  addEventListener('click', onClick, { capture: true, passive: true });
  return () => removeEventListener('click', onClick, true);
}
