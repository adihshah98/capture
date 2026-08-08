const UNFOCUS_SETTLE_MS = 120;

const readState = () => {
  if (document.visibilityState === 'hidden') return 'hidden';
  return document.hasFocus() ? 'focused' : 'unfocused';
};

export function startVisibilityCapture(emit) {
  let state = readState();
  let timer = 0;

  const settle = (at) => {
    timer = 0;
    const next = readState();
    if (next === state) return;
    state = next;
    emit(next, at);
  };

  const onChange = () => {
    const at = Date.now();
    clearTimeout(timer);
    timer = 0;
    if (readState() === 'unfocused') timer = setTimeout(() => settle(at), UNFOCUS_SETTLE_MS);
    else settle(at);
  };

  document.addEventListener('visibilitychange', onChange);
  addEventListener('focus', onChange);
  addEventListener('blur', onChange);
  emit(state, Date.now());

  return () => {
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', onChange);
    removeEventListener('focus', onChange);
    removeEventListener('blur', onChange);
  };
}
