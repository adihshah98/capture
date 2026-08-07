const MIN_MOVE_PX = 4;
const EMIT_INTERVAL_MS = 150;

export function startViewportCapture(emit) {
  const vv = window.visualViewport;
  if (!vv) return () => {};
  let last = null;
  let timer = 0;

  const send = () => {
    timer = 0;
    const s = {
      x: Math.round(vv.pageLeft),
      y: Math.round(vv.pageTop),
      w: Math.round(vv.width),
      h: Math.round(vv.height),
      scale: Math.round(vv.scale * 100) / 100,
    };
    const zoomed = s.scale > 1.01 || (last && last.scale > 1.01);
    const moved =
      !last ||
      s.scale !== last.scale ||
      Math.abs(s.x - last.x) > MIN_MOVE_PX ||
      Math.abs(s.y - last.y) > MIN_MOVE_PX;
    if (!zoomed || !moved) return;
    last = s;
    emit(s);
  };

  const onChange = () => {
    if (!timer) timer = setTimeout(send, EMIT_INTERVAL_MS);
  };

  vv.addEventListener('resize', onChange);
  vv.addEventListener('scroll', onChange);
  if (vv.scale > 1.01) send();
  return () => {
    clearTimeout(timer);
    vv.removeEventListener('resize', onChange);
    vv.removeEventListener('scroll', onChange);
  };
}
