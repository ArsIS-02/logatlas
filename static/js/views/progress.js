// Ход сбора и обработки: кольцо прогресса и журнал шагов
import { ring } from '../charts.js';
import { RT, fmtN, h, mount, raw } from '../util.js';

const two = (n) => String(n).padStart(2, '0');
const clock = (t) => { const d = new Date(t * 1000); return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`; };
const dateTime = (t) => { const d = new Date(t * 1000); return `${two(d.getDate())}.${two(d.getMonth() + 1)}.${d.getFullYear()} ${two(d.getHours())}:${two(d.getMinutes())}`; };

/** Список шагов: время, значок, текст */
export function stepsHtml(log) {
  if (!log || !log.length) return h`<p class="muted small-t">Шагов пока нет.</p>`;
  return h`<ol class="steps">${log.map((e) => h`<li class="st-${e.l}"><time>${clock(e.t)}</time><i aria-hidden="true"></i><span>${e.m}</span></li>`)}</ol>`;
}

/** Итог по журналу шагов: когда, успешно ли, сколько длилось */
export function stepsSummary(c) {
  const log = c.log || [];
  if (!log.length) return '';
  const start = log[0].t;
  const end = log[log.length - 1].t;
  const sec = Math.max(0, Math.round(end - start));
  const dur = sec >= 60 ? `${Math.floor(sec / 60)} мин ${sec % 60} с` : `${sec} с`;
  const errs = log.filter((e) => e.l === 'err').length;
  const warns = log.filter((e) => e.l === 'warn').length;
  const state = c.status === 'error' || errs ? 'не удался' : c.status === 'ready' ? (warns ? `успешно, предупреждений: ${warns}` : 'успешно') : 'идёт';
  return `${dateTime(start)} · ${state} · ${dur} · шагов: ${log.length}`;
}

/** Длительность человеческим языком: 12 с, 3 мин 05 с, 1 ч 20 мин */
function dur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return `${sec} с`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} мин ${two(sec % 60)} с`;
  return `${Math.floor(m / 60)} ч ${two(m % 60)} мин`;
}

/** Когда начался сбор или разбор (мс): время создания дела, иначе — первый шаг журнала */
function startedAt(c) {
  const t = c.created || (c.log && c.log.length ? c.log[0].t : 0);
  return t ? t * 1000 : 0;
}

/** Строка «идёт 3 мин · осталось ≈ 7 мин · 12 345 соб/с» — пересчитывается каждую секунду */
function liveText(c) {
  const t0 = startedAt(c);
  if (!t0) return '';
  const el = Math.max(0, (Date.now() - t0) / 1000);
  const p = Math.max(0.02, Math.min(1, c.progress || 0));
  const parts = [`идёт ${dur(el)}`];
  if (p > 0.05 && p < 0.995 && el > 3) parts.push(`осталось ≈ ${dur((el * (1 - p)) / p)}`);
  const ev = c.events || 0;
  if (ev > 50 && el > 2) parts.push(`${fmtN(Math.round(ev / el))} соб/с`);
  return parts.join(' · ');
}

let liveTimer = null;
let liveCase = null;

function stopLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}

function startLive() {
  stopLive();
  liveTimer = setInterval(() => {
    const el = document.querySelector('.progress-card[data-live] .timer');
    if (!el || !liveCase) { stopLive(); return; }
    el.textContent = liveText(liveCase);
  }, 1000);
}

function stepsPanel(c, open = true) {
  return h`<details class="steps-box" ${open ? raw('open') : ''}>
    <summary>Ход сбора <span class="muted">${stepsSummary(c)}</span></summary>
    <div class="steps-scroll">${stepsHtml(c.log)}</div></details>`;
}

function scrollDown(view) {
  const sc = view.querySelector('.steps-scroll');
  if (sc) sc.scrollTop = sc.scrollHeight;
}

export function renderProgress(view, c) {
  if (c.status === 'error' && c.stage === 'Сбор не удался') {
    stopLive();
    mount(view, h`<div class="progress-card">
      <div class="note err" style="text-align:left">Сбор не удался: ${c.message || 'неизвестная ошибка'}</div>
      ${stepsPanel(c)}
      <div class="btn-row" style="justify-content:center;margin-top:16px"><a class="btn primary" href="#/hosts">К хостам</a><a class="btn" href="#/">К списку сборов</a></div>
      <p class="muted" style="font-size:12.5px">Этот сбор можно удалить корзиной в боковой панели.</p></div>`);
    scrollDown(view);
    return;
  }
  if (c.status === 'error') {
    stopLive();
    mount(view, h`<div class="progress-card">
      <div class="note err" style="text-align:left">Обработка не удалась: ${c.message || 'неизвестная ошибка'}</div>
      ${stepsPanel(c)}
      <p class="ink2">Проверьте, что архив не повреждён, и загрузите его снова. Подробности — ${RT.logHint}.</p>
      <a class="btn" href="#/">К списку сборов</a></div>`);
    scrollDown(view);
    return;
  }
  const p = Math.max(0.02, c.progress || 0);
  liveCase = c;
  const existing = view.querySelector('.progress-card[data-live]');
  if (existing) {
    const timer = existing.querySelector('.timer');
    if (timer) timer.textContent = liveText(c);
    const fg = existing.querySelector('.ring-fg');
    const circ = 2 * Math.PI * 60;
    fg.setAttribute('stroke-dashoffset', String(circ * (1 - p)));
    existing.querySelector('.pct').textContent = `${Math.round(p * 100)}%`;
    existing.querySelector('h2').textContent = title(c);
    existing.querySelector('.stage').textContent = c.stage || '';
    existing.querySelector('.count').textContent = c.events ? `${fmtN(c.events)} событий` : '';
    const box = existing.querySelector('.steps-box');
    const n = (c.log || []).length;
    if (box && +box.dataset.n !== n) {
      const sc = box.querySelector('.steps-scroll');
      const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 40;
      mount(sc, stepsHtml(c.log));
      box.querySelector('summary .muted').textContent = stepsSummary(c);
      box.dataset.n = n;
      if (atBottom) sc.scrollTop = sc.scrollHeight;
    }
    return;
  }
  mount(view, h`<div class="progress-card" data-live>
    ${raw(ring(p))}
    <h2>${title(c)}</h2>
    <div class="stage">${c.stage || ''}</div>
    <div class="timer">${liveText(c)}</div>
    <div class="count">${c.events ? `${fmtN(c.events)} событий` : ''}</div>
    ${stepsPanel(c)}
    <p class="muted" style="font-size:13px;margin-top:18px">Страницу можно закрыть — сбор и обработка продолжатся на сервере.</p>
  </div>`);
  const box = view.querySelector('.steps-box');
  if (box) box.dataset.n = (c.log || []).length;
  startLive();
  scrollDown(view);
}

function title(c) {
  return { queued: 'Сбор в очереди на разбор', collecting: 'Собираю журналы' }[c.status] || 'Разбираю журналы';
}
