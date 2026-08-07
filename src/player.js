import { Replayer, EventType, IncrementalSource } from 'rrweb';
import 'rrweb/dist/style.css';

export const ACTIVE_SOURCES = new Set([
  IncrementalSource.MouseMove,
  IncrementalSource.MouseInteraction,
  IncrementalSource.Scroll,
  IncrementalSource.ViewportResize,
  IncrementalSource.Input,
  IncrementalSource.TouchMove,
  IncrementalSource.MediaInteraction,
  IncrementalSource.Drag,
]);

const ACTIVITY_THRESHOLD_MS = 5000;
const MIN_SKIP_MS = 2000;
const END_EPSILON_MS = 50;

const REPLAY_STYLE_RULES = [
  '@media (prefers-reduced-motion: no-preference) { .scroll-trigger:not(.scroll-trigger--offscreen).animate--slide-in { animation: var(--animation-slide-in) } }',
  '*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important; animation-iteration-count: 1 !important; animation-fill-mode: forwards !important; transition-duration: 0s !important; transition-delay: 0s !important; }',
];

function isActiveEvent(e) {
  return (
    e.type === EventType.FullSnapshot ||
    e.type === EventType.Meta ||
    (e.type === EventType.IncrementalSnapshot && ACTIVE_SOURCES.has(e.data && e.data.source))
  );
}

function buildSegments(events) {
  const t0 = events[0].timestamp;
  const duration = Math.max(events[events.length - 1].timestamp - t0, 1);
  const active = [];
  let seg = null;
  for (const e of events) {
    if (!isActiveEvent(e)) continue;
    const t = e.timestamp - t0;
    if (seg && t - seg.end <= ACTIVITY_THRESHOLD_MS) {
      seg.end = t;
    } else {
      if (seg) active.push(seg);
      seg = { start: t, end: t };
    }
  }
  if (seg) active.push(seg);

  const segments = [];
  let cursor = 0;
  for (const a of active) {
    if (a.start > cursor) segments.push({ start: cursor, end: a.start, isActive: false });
    segments.push({ start: a.start, end: a.end, isActive: true });
    cursor = a.end;
  }
  if (cursor < duration) segments.push({ start: cursor, end: duration, isActive: false });
  return { segments, duration };
}

export class CapturePlayer {
  constructor({ events, mount, speed = 1, skipInactive = true, onTime, onState, onSkip, onAssetErrors }) {
    this.events = events;
    this.mount = mount;
    this.onTime = onTime || (() => {});
    this.onState = onState || (() => {});
    this.onSkip = onSkip || (() => {});
    this.onAssetErrors = onAssetErrors || (() => {});

    const { segments, duration } = buildSegments(events);
    this.segments = segments;
    this.duration = duration;
    this.startTs = events[0].timestamp;
    this.snapshotOffsets = events
      .filter((e) => e.type === EventType.FullSnapshot)
      .map((e) => e.timestamp - this.startTs);

    this.maxHeightByWidth = {};
    for (const e of events) {
      const d =
        e.type === EventType.Meta ||
        (e.type === EventType.IncrementalSnapshot && e.data.source === IncrementalSource.ViewportResize)
          ? e.data
          : null;
      if (d && d.width) {
        this.maxHeightByWidth[d.width] = Math.max(this.maxHeightByWidth[d.width] || 0, d.height);
      }
    }

    this.playing = false;
    this.ended = false;
    this.skipInactive = skipInactive;
    this.failedAssets = new Set();
    this.scrollTimers = [];
    this.recDims = null;

    this.replayer = new Replayer(events, {
      root: mount,
      speed,
      skipInactive: false,
      useVirtualDom: false,
      triggerFocus: false,
      insertStyleRules: REPLAY_STYLE_RULES,
      mouseTail: { strokeStyle: 'rgba(79, 70, 229, 0.5)', lineWidth: 2, duration: 700 },
    });

    this.muteRejections = (e) => e.preventDefault();
    this.trackAssetError = (e) => {
      const el = e.target;
      const url = el && (el.src || el.href);
      if (url && !this.failedAssets.has(url)) {
        this.failedAssets.add(url);
        this.onAssetErrors(this.failedAssets.size);
      }
    };

    this.replayer.on('resize', (dims) => {
      const height = this.maxHeightByWidth[dims.width] || dims.height;
      if (this.recDims && this.recDims.width === dims.width && this.recDims.height === height) return;
      this.recDims = { width: dims.width, height };
      this.rescale();
    });
    this.replayer.on('fullsnapshot-rebuilded', () => this.hookFrame());
    this.replayer.on('custom-event', (e) => {
      if (e.data && e.data.tag === 'capture-click') this.clickPulse(e.data.payload);
    });
    this.replayer.on('finish', () => {
      if (this.playing) this.finish();
    });
    this.hookFrame();

    this.resizeObserver = new ResizeObserver(() => this.rescale());
    this.resizeObserver.observe(mount);

    this.tick = this.tick.bind(this);
    this.play(0);
  }

  clickPulse(payload) {
    const wrapper = this.replayer.wrapper;
    const mouse = wrapper.querySelector('.replayer-mouse');
    const pulse = document.createElement('div');
    pulse.className = payload && payload.rage ? 'click-pulse rage' : 'click-pulse';
    if (mouse && (mouse.style.left || mouse.style.top)) {
      pulse.style.left = mouse.style.left;
      pulse.style.top = mouse.style.top;
    } else if (payload && payload.x != null) {
      pulse.style.left = `${payload.x}px`;
      pulse.style.top = `${payload.y}px`;
    } else {
      return;
    }
    const live = wrapper.querySelectorAll('.click-pulse');
    if (live.length >= 4) live[0].remove();
    wrapper.appendChild(pulse);
    setTimeout(() => pulse.remove(), 1800);
  }

  hookFrame() {
    const win = this.replayer.iframe.contentWindow;
    if (!win) return;
    win.addEventListener('unhandledrejection', this.muteRejections);
    win.addEventListener('error', this.trackAssetError, true);
  }

  segmentAt(ms) {
    return this.segments.find((s) => ms >= s.start && ms < s.end) || null;
  }

  clampRenderable(ms) {
    const first = this.snapshotOffsets[0];
    return first !== undefined && ms <= first ? first + 1 : ms;
  }

  tick() {
    if (!this.playing) return;
    let t = this.replayer.getCurrentTime();
    if (t >= this.duration - END_EPSILON_MS) {
      this.finish();
      return;
    }
    const seg = this.segmentAt(t);
    if (this.skipInactive && seg && !seg.isActive && seg.end - seg.start >= MIN_SKIP_MS) {
      if (seg.end >= this.duration - END_EPSILON_MS) {
        this.finish();
        return;
      }
      this.onSkip(seg.end - t);
      this.replayer.play(seg.end);
      this.scheduleScrollFix();
      t = seg.end;
    }
    this.onTime(t);
    this.raf = requestAnimationFrame(this.tick);
  }

  play(fromMs) {
    const from =
      fromMs != null ? fromMs : this.ended ? 0 : this.replayer.getCurrentTime();
    this.ended = false;
    this.playing = true;
    this.replayer.play(this.clampRenderable(Math.max(0, Math.min(from, this.duration))));
    this.scheduleScrollFix();
    this.onState('playing');
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(this.tick);
  }

  pause(atMs) {
    cancelAnimationFrame(this.raf);
    this.playing = false;
    if (atMs != null) {
      const at = this.clampRenderable(Math.max(0, Math.min(atMs, this.duration)));
      this.replayer.pause(at);
      this.scheduleScrollFix();
      this.onTime(at);
    } else {
      this.replayer.pause();
    }
    this.onState(this.ended ? 'ended' : 'paused');
  }

  finish() {
    cancelAnimationFrame(this.raf);
    this.playing = false;
    this.ended = true;
    this.replayer.pause(this.duration);
    this.scheduleScrollFix();
    this.onTime(this.duration);
    this.onState('ended');
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play(this.ended ? 0 : undefined);
  }

  seekTo(ms, opts = {}) {
    let want = Math.max(0, Math.min(ms, this.duration));
    if (this.skipInactive) {
      const seg = this.segmentAt(want);
      if (
        seg &&
        !seg.isActive &&
        seg.end - seg.start >= MIN_SKIP_MS &&
        seg.end < this.duration - END_EPSILON_MS
      ) {
        this.onSkip(seg.end - want);
        want = seg.end;
      }
    }
    let target = this.clampRenderable(want);
    if (this.snapshotOffsets.includes(target)) target += 1;
    const resume = opts.play != null ? opts.play : this.playing;
    if (target >= this.duration - END_EPSILON_MS && !resume) {
      this.finish();
      return;
    }
    this.ended = false;
    if (resume) this.play(target);
    else this.pause(target);
  }

  seekBy(deltaMs) {
    const current = this.ended ? this.duration : this.replayer.getCurrentTime();
    let target = current + deltaMs;
    if (deltaMs < 0 && this.skipInactive) {
      const seg = this.segmentAt(target);
      if (seg && !seg.isActive && seg.end - seg.start >= MIN_SKIP_MS) {
        const remaining = Math.max(-deltaMs - Math.max(current - seg.end, 0), 0);
        target = seg.start - remaining;
      }
    }
    this.seekTo(target);
  }

  setSpeed(speed) {
    if (this.playing) {
      const t = this.replayer.getCurrentTime();
      this.replayer.pause();
      this.replayer.setConfig({ speed });
      this.replayer.play(t);
    } else {
      this.replayer.setConfig({ speed });
    }
  }

  setSkipInactive(on) {
    this.skipInactive = on;
  }

  applyRecordedScroll() {
    const doc = this.replayer.iframe.contentDocument;
    const win = this.replayer.iframe.contentWindow;
    if (!doc || !win) return;
    const mirror = this.replayer.getMirror();
    const docId = mirror.getId(doc);
    const t = this.startTs + this.replayer.getCurrentTime();
    const byTarget = new Map();
    for (const e of this.events) {
      if (e.timestamp > t) break;
      if (e.type === EventType.FullSnapshot && e.data.initialOffset) {
        byTarget.clear();
        byTarget.set(docId, { x: e.data.initialOffset.left, y: e.data.initialOffset.top });
      } else if (
        e.type === EventType.IncrementalSnapshot &&
        e.data.source === IncrementalSource.Scroll
      ) {
        byTarget.set(e.data.id, { x: e.data.x, y: e.data.y });
      }
    }
    if (!byTarget.has(docId)) byTarget.set(docId, { x: 0, y: 0 });
    for (const [id, pos] of byTarget) {
      if (id === docId) {
        win.scrollTo({ left: pos.x, top: pos.y, behavior: 'auto' });
        continue;
      }
      const el = mirror.getNode(id);
      if (el && el.scrollTo) el.scrollTo({ left: pos.x, top: pos.y, behavior: 'auto' });
    }
  }

  scheduleScrollFix() {
    this.scrollTimers.forEach(clearTimeout);
    this.scrollTimers = [];
    let lastHeight = -1;
    let stable = 0;
    const deadline = Date.now() + 3500;
    const step = () => {
      this.applyRecordedScroll();
      const doc = this.replayer.iframe.contentDocument;
      const height = doc && doc.body ? doc.body.scrollHeight : -1;
      stable = height === lastHeight ? stable + 1 : 0;
      lastHeight = height;
      if (stable < 3 && Date.now() < deadline) {
        this.scrollTimers.push(setTimeout(step, 150));
      }
    };
    this.scrollTimers.push(setTimeout(step, 90));
  }

  rescale() {
    if (!this.recDims) return;
    const w = this.mount.clientWidth;
    const h = this.mount.clientHeight;
    let scale = Math.min(w / this.recDims.width, h / this.recDims.height);
    if (!isFinite(scale) || scale <= 0) return;
    if (Math.abs(scale - 1) < 0.001) scale = 0.999;
    this.replayer.wrapper.style.transform = `scale(${scale}) translate(-50%, -50%)`;
    this.replayer.wrapper.style.setProperty('--rescale', scale);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.scrollTimers.forEach(clearTimeout);
    this.resizeObserver.disconnect();
    this.playing = false;
    try {
      this.replayer.destroy();
    } catch {
    }
  }
}
