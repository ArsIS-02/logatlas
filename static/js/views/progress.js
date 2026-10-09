// Ход обработки дела
import { ring } from '../charts.js';
import { fmtN, h, mount, raw } from '../util.js';

export function renderProgress(view, c) {
  if (c.status === 'error') {
    mount(view, h`<div class="progress-card">
      <div class="note err" style="text-align:left">Обработка не удалась: ${c.message || 'неизвестная ошибка'}</div>
      <p class="ink2">Проверьте, что архив не повреждён, и загрузите его снова. Подробности — в журнале контейнера: <span class="mono">docker compose logs logatlas</span>.</p>
      <a class="btn" href="#/">К списку дел</a></div>`);
    return;
  }
  const p = Math.max(0.02, c.progress || 0);
  const existing = view.querySelector('.progress-card[data-live]');
  if (existing) {
    const fg = existing.querySelector('.ring-fg');
    const circ = 2 * Math.PI * 60;
    fg.setAttribute('stroke-dashoffset', String(circ * (1 - p)));
    existing.querySelector('.pct').textContent = `${Math.round(p * 100)}%`;
    existing.querySelector('.stage').textContent = c.stage || '';
    existing.querySelector('.count').textContent = c.events ? `${fmtN(c.events)} событий` : '';
    return;
  }
  mount(view, h`<div class="progress-card" data-live>
    ${raw(ring(p))}
    <h2>${c.status === 'queued' ? 'Дело в очереди' : 'Разбираю журналы'}</h2>
    <div class="stage">${c.stage || ''}</div>
    <div class="count">${c.events ? `${fmtN(c.events)} событий` : ''}</div>
    <p class="muted" style="font-size:13px;margin-top:18px">Страницу можно закрыть — обработка продолжится на сервере.</p>
  </div>`);
}
