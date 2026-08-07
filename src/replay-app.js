import { refreshAll } from './replay/session-list.js';
import { player } from './replay/session-player.js';
import { initInsights, refreshCohort } from './replay/insights.js';

addEventListener('keydown', (e) => {
  if (!player || e.target.closest('input, select, textarea, button')) return;
  if (e.code === 'Space') {
    e.preventDefault();
    player.toggle();
  } else if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') {
    e.preventDefault();
    player.seekBy(e.code === 'ArrowRight' ? 5000 : -5000);
  } else if (e.code === 'KeyF' && !e.metaKey && !e.ctrlKey && !e.altKey) {
    e.preventDefault();
    document.getElementById('pc-fs').click();
  }
});

document.getElementById('refresh').addEventListener('click', () => {
  refreshAll();
  if (document.body.classList.contains('mode-insights')) {
    refreshCohort().catch(() => {});
  }
});
initInsights();
refreshAll();
