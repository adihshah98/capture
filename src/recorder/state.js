export let status = 'disabled';
export let sessionId = null;
export let windowId = null;
export let visitorId = null;

export function setStatus(next) {
  status = next;
}

export function setSessionId(id) {
  sessionId = id;
}

export function setWindowId(id) {
  windowId = id;
}

export function setVisitorId(id) {
  visitorId = id;
}
