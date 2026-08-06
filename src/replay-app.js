import { refreshAll } from './replay/session-list.js';
import { player } from './replay/session-player.js';

addEventListener('keydown', (e) => {
  if (!player || e.target.closest('input, select, textarea, button')) return;
  if (e.code === 'Space') {
    e.preventDefault();
    player.toggle();
  } else if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') {
    e.preventDefault();
    player.seekBy(e.code === 'ArrowRight' ? 5000 : -5000);
  }
});

document.getElementById('refresh').addEventListener('click', refreshAll);
refreshAll();
