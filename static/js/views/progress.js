// Ход сбора и обработки
import { ring } from '../charts.js';
import { fmtN, h, mount, raw } from '../util.js';

export function renderProgress(view, c) {
  if (c.status === 'error' && c.stage === 'Сбор не удался') {
    mount(view, h`<div class="progress-card">
      <div class="note err" style="text-align:left">Сбор с хоста не удался: ${c.message || 'неизвестная ошибка'}</div>
      <div class="btn-row" style="justify-content:center"><a class="btn primary" href="#/hosts">К хостам</a><a class="btn" href="#/">К списку сборов</a></div>
      <p class="muted" style="font-size:12.5px">Этот сбор можно удалить корзиной в боковой панели.</p></div>`);
    return;
  }
  if (c.status === 'error') {
    mount(view, h`<div class="progress-card">
      <div class="note err" style="text-align:left">Обработка не удалась: ${c.message || 'неизвестная ошибка'}</div>
      <p class="ink2">Проверьте, что архив не повреждён, и загрузите его снова. Подробности — в журнале контейнера: <span class="mono">docker compose logs logatlas</span>.</p>
      <a class="btn" href="#/">К списку сборов</a></div>`);
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
    <h2>${{ queued: 'Сбор в очереди на разбор', collecting: 'Собираю журналы с хоста' }[c.status] || 'Разбираю журналы'}</h2>
    <div class="stage">${c.stage || ''}</div>
    <div class="count">${c.events ? `${fmtN(c.events)} событий` : ''}</div>
    <p class="muted" style="font-size:13px;margin-top:18px">Страницу можно закрыть — обработка продолжится на сервере.</p>
  </div>`);
}
