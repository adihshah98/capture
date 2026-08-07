import { ACTIVE_SOURCES } from '../player.js';
import { formatDuration, formatOffset } from './format.js';
import { player } from './session-player.js';

const playerHostEl = document.getElementById('player-host');
const skipNoteEl = document.getElementById('skip-note');
const assetErrorsEl = document.getElementById('asset-errors');
const pcToggleEl = document.getElementById('pc-toggle');
const pcCurEl = document.getElementById('pc-cur');
const pcDurEl = document.getElementById('pc-dur');
const pcBarEl = document.getElementById('pc-bar');
const pcGapsEl = document.getElementById('pc-gaps');
const pcHistEl = document.getElementById('pc-hist');
const pcFillEl = document.getElementById('pc-fill');
const pcDotEl = document.getElementById('pc-dot');
const pcSpeedsEl = document.getElementById('pc-speeds');
const pcSkipEl = document.querySelector('#pc-skip input');
const pcFsEl = document.getElementById('pc-fs');

export const currentSpeed = () => Number(pcSpeedsEl.querySelector('.active').dataset.speed);
export const skipEnabled = () => pcSkipEl.checked;

function drawHistogram(events, duration) {
  const BUCKETS = 120;
  const counts = new Array(BUCKETS).fill(0);
  const start = events[0].timestamp;
  for (const e of events) {
    if (e.type === 3 && ACTIVE_SOURCES.has(e.data && e.data.source)) {
      const i = Math.min(BUCKETS - 1, Math.floor(((e.timestamp - start) / duration) * BUCKETS));
      counts[i] += 1;
    }
  }
  const smooth = counts.map(
    (_, i) => (counts[i - 1] || 0) * 0.25 + counts[i] * 0.5 + (counts[i + 1] || 0) * 0.25
  );
  const max = Math.max(...smooth, 1);

  const width = Math.max(pcBarEl.clientWidth, 200);
  const height = 14;
  pcHistEl.width = width * devicePixelRatio;
  pcHistEl.height = height * devicePixelRatio;
  const ctx = pcHistEl.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = 'rgba(79, 70, 229, 0.4)';
  const bw = width / BUCKETS;
  for (let i = 0; i < BUCKETS; i++) {
    if (!smooth[i]) continue;
    const h = Math.max(2, (smooth[i] / max) * height);
    ctx.fillRect(i * bw, height - h, Math.max(1, bw - 0.5), h);
  }
}

export function renderScrubberChrome(p) {
  pcDurEl.textContent = formatOffset(p.duration);
  pcGapsEl.innerHTML = '';
  for (const seg of p.segments) {
    if (seg.isActive || seg.end - seg.start < 2000) continue;
    const band = document.createElement('div');
    band.style.left = `${(seg.start / p.duration) * 100}%`;
    band.style.width = `${((seg.end - seg.start) / p.duration) * 100}%`;
    pcGapsEl.appendChild(band);
  }
  drawHistogram(p.events, p.duration);
}

export function updateScrubber(ms) {
  if (!player || scrubbing) return;
  const pct = Math.min(100, (ms / player.duration) * 100);
  pcFillEl.style.width = `${pct}%`;
  pcDotEl.style.left = `${pct}%`;
  pcCurEl.textContent = formatOffset(ms);
}

export function updatePlayState(state) {
  pcToggleEl.textContent = state === 'playing' ? '⏸' : '▶';
  if (state === 'ended') skipNoteEl.hidden = true;
}

let skipNoteTimer;
export function showSkipNote(skippedMs) {
  skipNoteEl.textContent = `skipped ${formatDuration(skippedMs)} idle`;
  skipNoteEl.hidden = false;
  clearTimeout(skipNoteTimer);
  skipNoteTimer = setTimeout(() => {
    skipNoteEl.hidden = true;
  }, 900);
}

export function showAssetErrors(count) {
  assetErrorsEl.textContent = `${count} asset${count > 1 ? 's' : ''} failed to load`;
  assetErrorsEl.hidden = false;
}

let scrubbing = false;

function barPct(e) {
  const rect = pcBarEl.getBoundingClientRect();
  return Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
}

function previewAt(pct) {
  pcFillEl.style.width = `${pct * 100}%`;
  pcDotEl.style.left = `${pct * 100}%`;
  pcCurEl.textContent = formatOffset(pct * player.duration);
}

pcBarEl.addEventListener('pointerdown', (e) => {
  if (!player) return;
  pcBarEl.setPointerCapture(e.pointerId);
  scrubbing = true;
  previewAt(barPct(e));
});
pcBarEl.addEventListener('pointermove', (e) => {
  if (scrubbing && player && e.buttons) previewAt(barPct(e));
});
pcBarEl.addEventListener('pointerup', (e) => {
  if (!scrubbing) return;
  scrubbing = false;
  if (player) player.seekTo(barPct(e) * player.duration);
});
pcBarEl.addEventListener('pointercancel', () => {
  scrubbing = false;
});

pcToggleEl.addEventListener('click', () => player && player.toggle());
pcSpeedsEl.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-speed]');
  if (!btn || !player) return;
  player.setSpeed(Number(btn.dataset.speed));
  for (const b of pcSpeedsEl.querySelectorAll('button')) b.classList.toggle('active', b === btn);
});
pcSkipEl.addEventListener('change', () => player && player.setSkipInactive(pcSkipEl.checked));
pcFsEl.addEventListener('click', () => {
  (document.fullscreenElement
    ? document.exitFullscreen()
    : playerHostEl.requestFullscreen()
  ).catch(() => {});
});
