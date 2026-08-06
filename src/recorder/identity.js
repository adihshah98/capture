import { config, REMOTE_CONFIG_KEY } from './config.js';

const SESSION_STORAGE_KEY = 'capture_session';
const VISITOR_STORAGE_KEY = 'capture_visitor';
const WINDOW_STORAGE_KEY = 'capture_window_id';
const OPT_OUT_KEY = 'capture_opt_out';

function tryStorage(fn, fallback = null) {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

export const isOptedOut = () => tryStorage(() => localStorage.getItem(OPT_OUT_KEY) === '1', false);

export const setOptedOut = (value) =>
  tryStorage(() =>
    value ? localStorage.setItem(OPT_OUT_KEY, '1') : localStorage.removeItem(OPT_OUT_KEY)
  );

export const forgetVisitor = () =>
  tryStorage(() => {
    localStorage.removeItem(SESSION_STORAGE_KEY);
    localStorage.removeItem(VISITOR_STORAGE_KEY);
    localStorage.removeItem(REMOTE_CONFIG_KEY);
    sessionStorage.removeItem(WINDOW_STORAGE_KEY);
  });

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export const readSession = () =>
  tryStorage(() => JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY))) || null;

export function writeSession(session) {
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
}

export function getSession() {
  const now = Date.now();
  let session = readSession();
  const expired =
    !session ||
    now - session.lastActivity > config.sessionIdleTimeoutMs ||
    now - session.started > config.sessionMaxLengthMs;
  if (expired) {
    session = {
      id: uuid(),
      started: now,
      lastActivity: now,
      sampled: Math.random() < config.sampleRate,
    };
    writeSession(session);
    return { ...session, rotated: true };
  }
  session.lastActivity = now;
  if (typeof session.sampled !== 'boolean') session.sampled = true;
  writeSession(session);
  return { ...session, rotated: false };
}

export function getWindowId() {
  let id = sessionStorage.getItem(WINDOW_STORAGE_KEY);
  if (!id) {
    id = uuid();
    sessionStorage.setItem(WINDOW_STORAGE_KEY, id);
  }
  return id;
}

export const getVisitorId = () =>
  tryStorage(() => {
    let id = localStorage.getItem(VISITOR_STORAGE_KEY);
    if (!id) {
      id = uuid();
      localStorage.setItem(VISITOR_STORAGE_KEY, id);
    }
    return id;
  });
