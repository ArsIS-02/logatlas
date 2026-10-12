// LogAtlas — маршрутизация, шапка, лента времени, боковая панель
import { api } from './api.js';
import { ribbon } from './charts.js';
import {
  $, $$, CAT_KEYS, PLATFORMS, RT, fmtN, fmtSpan, fmtTime, h, mount, platIcon, platName, plural, raw, toast,
} from './util.js';
import { renderHome } from './views/home.js';
import { renderOverview } from './views/overview.js';
import { renderTimeline } from './views/timeline.js';
import { renderSearch } from './views/search.js';
import { renderFindings } from './views/findings.js';
import { renderEntities } from './views/entities.js';
import { renderSources } from './views/sources.js';
import { renderCompare } from './views/compare.js';
import { renderReport } from './views/report.js';
import { renderProgress } from './views/progress.js';
import { renderHosts, renderCollect } from './views/hosts.js';
import { renderRules, RULES_ICON } from './views/rules.js';
import { openEvent, closeDrawer } from './views/event.js';

const TABS = [
  ['overview', 'Обзор', renderOverview],
  ['timeline', 'Хронология', renderTimeline],
  ['search', 'Поиск', renderSearch],
  ['findings', 'Находки', renderFindings],
  ['entities', 'Объекты', renderEntities],
  ['compare', 'Сравнение', renderCompare],
  ['sources', 'Источники', renderSources],
  ['report', 'Отчёт', renderReport],
];

const S = {
  meta: null,
  cases: [],
  route: { kind: 'home', platform: 'all' },
  case: null,
  full: null,         // обзор сбора без фильтра (для ленты времени)
  ribbonApi: null,
  poll: null,
  renderSeq: 0,
};

// ---------------------------------------------------------------------------
function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  const parts = (path || '').split('/').filter(Boolean);
  const params = new URLSearchParams(qs || '');
  if (parts[0] === 'c' && parts[1]) {
    const from = params.get('from'), to = params.get('to');
    return {
      kind: 'case', id: parts[1], tab: parts[2] || 'overview', sub: parts[3] || null,
      range: from && to ? [+from, +to] : null, q: params.get('q') || '', params,
    };
  }
  if (parts[0] === 'p' && parts[1]) return { kind: 'home', platform: parts[1] };
  if (parts[0] === 'hosts') return { kind: 'hosts' };
  if (parts[0] === 'rules') return { kind: 'rules' };
  if (parts[0] === 'collect' && parts[1]) return { kind: 'collect', hostId: parts[1] };
  return { kind: 'home', platform: 'all' };
}

export function caseHref(id, tab = 'overview', { range, q, sub, extra } = {}) {
  const p = new URLSearchParams();
  if (range) { p.set('from', range[0]); p.set('to', range[1]); }
  if (q) p.set('q', q);
  if (extra) Object.entries(extra).forEach(([k, v]) => v != null && p.set(k, v));
  const s = p.toString();
  return `#/c/${id}/${tab}${sub ? `/${sub}` : ''}${s ? `?${s}` : ''}`;
}

function go(tab, opts = {}) {
  const r = S.route;
  const range = 'range' in opts ? opts.range : r.range;
  const q = 'q' in opts ? opts.q : r.q;
  window.location.hash = caseHref(r.id, tab || r.tab, { range, q, sub: opts.sub, extra: opts.extra });
}

function ctx() {
  const r = S.route;
  return {
    case: S.case, meta: S.meta, full: S.full, range: r.range, q: r.q, sub: r.sub, params: r.params,
    from: r.range ? r.range[0] : undefined, to: r.range ? r.range[1] : undefined,
    go,
    setRange: (range) => go(null, { range }),
    search: (q, extra = {}) => go('search', { q, ...extra }),
    openEvent: (id) => openEvent(S.case.id, id, ctx()),
    refreshCase: () => loadCase(S.case.id, true),
    href: (tab, o) => caseHref(S.case.id, tab, o),
  };
}

// ---------------------------------------------------------------------------
async function loadCases() {
  try {
    S.cases = await api.get('/api/cases');
  } catch (e) {
    S.cases = [];
  }
  renderSide();
}

function renderSide() {
  const counts = { all: S.cases.length, macos: 0, windows: 0, linux: 0 };
  S.cases.forEach((c) => { if (counts[c.platform] !== undefined) counts[c.platform] += 1; });
  const kind = S.route.kind;
  const activePlat = kind === 'home' ? S.route.platform : kind === 'case' ? (S.case?.platform || 'all') : null;
  mount($('#platforms'), h`${Object.entries(PLATFORMS).map(([k, p]) => h`
    <a class="plat ${activePlat === k ? 'on' : ''}" href="#/p/${k}">${raw(platIcon(k))}<span>${p.name}</span><span class="cnt">${counts[k] || ''}</span></a>`)}
    <a class="plat plat-hosts ${kind === 'hosts' || kind === 'collect' ? 'on' : ''}" href="#/hosts">${raw(HOSTS_ICON)}<span>Хосты и сбор</span></a>
    <a class="plat ${kind === 'rules' ? 'on' : ''}" href="#/rules">${RULES_ICON}<span>Правила Sigma</span></a>`);
  const listPlat = activePlat || 'all';
  const list = S.cases.filter((c) => listPlat === 'all' || c.platform === listPlat || ['mixed', 'other', null].includes(c.platform));
  mount($('#side-cases'), h`
    <div class="side-h"><span>Сборы</span><a href="#/p/${listPlat}" title="Загрузить журналы">+ Загрузить</a></div>
    ${list.length ? list.map((c) => h`<div class="side-case"><a class="case-link ${S.route.id === c.id ? 'on' : ''}" href="#/c/${c.id}/overview" title="${c.name}">
      ${c.name}<small>${c.host || platName(c.platform)}${c.status !== 'ready' ? ` — ${statusName(c.status)}` : ''}</small></a><button class="case-del" type="button" data-id="${c.id}" title="Удалить сбор" aria-label="${`Удалить сбор «${c.name}»`}">${raw(TRASH)}<span></span></button></div>`)
    : h`<div class="muted" style="padding:6px 10px;font-size:12.5px">Пока пусто</div>`}`);
}

const HOSTS_ICON = '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3.5" width="14" height="5" rx="1.3"/><rect x="3" y="11.5" width="14" height="5" rx="1.3"/><path d="M6 6h.01M6 14h.01" stroke-width="2.2" stroke-linecap="round"/></svg>';
const TRASH = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11M6.5 4V2.5h3V4M4 4l.7 9.5h6.6L12 4M6.8 6.5v4.5M9.2 6.5v4.5"/></svg>';

function disarm(b) {
  clearTimeout(b._t);
  b.dataset.armed = '';
  b.classList.remove('armed');
  b.title = 'Удалить сбор';
  b.querySelector('span').textContent = '';
}

// Удаление сбора из боковой панели: первый щелчок — «Удалить?», второй в течение 4 с — удаление
async function onSideClick(e) {
  const b = e.target.closest('.case-del');
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  if (b.dataset.armed !== '1') {
    $$('#side-cases .case-del.armed').forEach(disarm);
    b.dataset.armed = '1';
    b.classList.add('armed');
    b.title = 'Нажмите ещё раз: сбор и его файлы удалятся безвозвратно';
    b.querySelector('span').textContent = 'Удалить?';
    b._t = setTimeout(() => disarm(b), 4000);
    return;
  }
  clearTimeout(b._t);
  b.disabled = true;
  const id = b.dataset.id;
  const wasOpen = S.route.kind === 'case' && S.route.id === id;
  try {
    await api.del(`/api/cases/${id}`);
    if (wasOpen) { S.case = null; S.full = null; }
    await loadCases();
    toast('Сбор удалён');
    if (wasOpen) window.location.hash = `#/p/${S.route.platform || 'all'}`;
    else if (S.route.kind === 'home') route();
  } catch (err) {
    toast(err.message);
    b.disabled = false;
    disarm(b);
  }
}

export function statusName(s) {
  return { ready: 'готово', processing: 'обработка', queued: 'в очереди', error: 'ошибка', collecting: 'сбор с хоста' }[s] || s;
}

// ---------------------------------------------------------------------------
async function loadCase(id, force = false) {
  if (!force && S.case && S.case.id === id && S.case.status === 'ready' && S.full) return;
  const c = await api.get(`/api/cases/${id}`);
  S.case = c;
  S.full = null;
  if (c.status === 'ready' && c.events) {
    S.full = await api.get(`/api/cases/${id}/overview`);
  }
}

function renderTop() {
  const top = $('#top');
  const r = S.route;
  if (r.kind === 'rules') {
    mount(top, h`<div class="top-row"><div class="top-title"><h1>Правила Sigma</h1>
      <div class="chips"><span class="chip">импорт правил SigmaHQ и своих</span></div></div></div>`);
    S.ribbonApi = null;
    return;
  }
  if (r.kind === 'hosts' || r.kind === 'collect') {
    mount(top, h`<div class="top-row"><div class="top-title"><h1>${r.kind === 'hosts' ? 'Хосты и сбор журналов' : 'Новый сбор с хоста'}</h1>
      <div class="chips"><span class="chip">по SSH, ключом панели</span></div></div></div>`);
    S.ribbonApi = null;
    return;
  }
  if (r.kind !== 'case' || !S.case) {
    mount(top, h`<div class="top-row"><div class="top-title"><h1>${r.platform && r.platform !== 'all' ? `Журналы ${PLATFORMS[r.platform]?.name || ''}` : 'Анализ журналов'}</h1>
      <div class="chips"><span class="chip">${S.cases.length} ${plural(S.cases.length, 'сбор', 'сбора', 'сборов')}</span></div></div></div>`);
    S.ribbonApi = null;
    return;
  }
  const c = S.case;
  const f = S.full;
  const nf = f?.findings ? f.findings.filter((x) => ['critical', 'high'].includes(x.severity)).reduce((a, x) => a + x.n, 0) : 0;
  const ready = c.status === 'ready';
  mount(top, h`
    <div class="top-row">
      <div class="top-title">
        <h1 title="${c.name}">${c.name}</h1>
        <div class="chips">
          <span class="chip">${raw(platIcon(c.platform))}${platName(c.platform)}</span>
          ${c.host ? h`<span class="chip">Хост ${c.host}</span>` : ''}
          <span class="chip">Время хоста ${c.tz_label}</span>
          ${ready ? h`<span class="chip num">${fmtN(c.events)} событий</span>` : ''}
          ${ready && c.t_min ? h`<span class="chip">${fmtTime(c.t_min, 'date')} — ${fmtTime(c.t_max, 'date')}</span>` : ''}
        </div>
      </div>
      ${ready ? h`<form class="qbox" id="qform" role="search">
        <svg class="qicon" viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="m13 13 4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
        <input id="q" name="q" autocomplete="off" spellcheck="false" placeholder="Поиск: sudo, process:sshd, /regex/…" value="${r.q}" aria-label="Поиск по журналам">
        <button class="btn ghost small qhelp" type="button" id="qhelp" aria-expanded="false">Синтаксис</button>
        <div class="qpop hidden" id="qpop"></div>
      </form>` : ''}
    </div>
    ${ready && f ? h`<div class="ribbon" id="ribbon" aria-label="Лента времени: выделите период"></div>
    <div class="ribbon-info" id="ribbon-info"></div>` : ''}
    ${ready ? h`<nav class="tabs" role="tablist">${TABS.map(([k, name]) => h`<a class="tab ${r.tab === k ? 'on' : ''}" role="tab" aria-selected="${r.tab === k}" href="${caseHref(c.id, k, { range: r.range, q: k === 'search' ? r.q : '' })}">${name}${k === 'findings' && nf ? h`<span class="badge hot">${fmtN(nf)}</span>` : ''}</a>`)}</nav>` : ''}`);
  if (ready && f) {
    const classified = f.hist_total.map((_, i) => CAT_KEYS.reduce((a, k) => a + (f.hist_cat[k]?.[i] || 0), 0));
    S.ribbonApi = ribbon($('#ribbon'), { start: f.hist_start, bucket: f.bucket, total: f.hist_total, classified }, r.range,
      (sel) => ctx().setRange(sel));
    renderRibbonInfo();
    bindSearch();
  }
}

function renderRibbonInfo() {
  const el = $('#ribbon-info');
  if (!el) return;
  const r = S.route.range;
  mount(el, r ? h`<span>Период</span><span class="rng">${fmtTime(r[0])} — ${fmtTime(r[1])}</span><span class="muted">${fmtSpan(r[1] - r[0])}</span>
      <button class="btn ghost small" id="rng-reset" type="button">Сбросить период</button>`
    : h`<span class="muted">Весь период сбора. Выделите участок на ленте, чтобы сузить все вкладки.</span>
      <span class="legend-inline"><i class="li-a"></i>все события <i class="li-c"></i>классифицированные</span>`);
  const b = $('#rng-reset');
  if (b) b.addEventListener('click', () => ctx().setRange(null));
}

const SYNTAX = [
  ['sudo', 'подстрока в сообщении, процессе или объекте'],
  ['"screen sharing"', 'фраза целиком'],
  ['process:sshd', 'поле: process, source, user, host, level, code, pid'],
  ['process:kav*', 'шаблон, * — любые символы'],
  ['cat:доступ', 'категория: система, доступ, процессы, закрепление, по, устройства, сеть, безопасность'],
  ['action:вход', 'подстрока в действии, объекте (subject:) или сообщении (msg:)'],
  ['level:warning+', 'уровень и выше'],
  ['-process:mdworker', 'исключить'],
  ['process:sudo|sshd', 'несколько значений'],
  ['/fail(ed|ure)/', 'регулярное выражение по сообщению'],
  ['rule:remote_access', 'события из находки'],
];

function bindSearch() {
  const form = $('#qform');
  if (!form) return;
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('#q').value.trim();
    ctx().search(q);
  });
  const pop = $('#qpop');
  mount(pop, h`<table>${SYNTAX.map(([a, b]) => h`<tr><td><code data-ex="${a}">${a}</code></td><td>${b}</td></tr>`)}</table>
    <p class="muted" style="margin:10px 0 0;font-size:12px">Условия объединяются через «И». Регистр латиницы не важен.</p>`);
  $('#qhelp').addEventListener('click', () => {
    pop.classList.toggle('hidden');
    $('#qhelp').setAttribute('aria-expanded', String(!pop.classList.contains('hidden')));
  });
  pop.addEventListener('click', (e) => {
    const ex = e.target.closest('[data-ex]');
    if (!ex) return;
    const inp = $('#q');
    inp.value = `${inp.value.trim()} ${ex.dataset.ex}`.trim();
    inp.focus();
    pop.classList.add('hidden');
  });
  document.addEventListener('click', (e) => {
    if (!form.contains(e.target)) pop.classList.add('hidden');
  }, { once: true });
}

// ---------------------------------------------------------------------------
async function route() {
  const seq = ++S.renderSeq;
  const prev = S.route;
  S.route = parseHash();
  closeDrawer();
  clearInterval(S.poll);
  const view = $('#view');
  const r = S.route;
  if (r.kind === 'home') {
    S.case = null;
    S.full = null;
    renderSide();
    renderTop();
    await renderHome(view, { platform: r.platform, cases: S.cases, meta: S.meta, reload: loadCases, refresh: async () => { await loadCases(); route(); } });
    return;
  }
  if (r.kind === 'rules') {
    S.case = null;
    S.full = null;
    renderSide();
    renderTop();
    await renderRules(view);
    return;
  }
  if (r.kind === 'hosts' || r.kind === 'collect') {
    S.case = null;
    S.full = null;
    renderSide();
    renderTop();
    if (r.kind === 'hosts') await renderHosts(view);
    else await renderCollect(view, { id: r.hostId });
    return;
  }
  const caseChanged = !prev || prev.id !== r.id;
  if (caseChanged) {
    mount(view, h`<div class="loading"><span class="spinner"></span><p class="loading-t">Открываю сбор…</p></div>`);
    setTimeout(() => {
      const t = view.querySelector('.loading-t');
      if (t && seq === S.renderSeq) {
        t.textContent = 'Готовлю сводку по сбору. Для сбора из миллионов событий при первом открытии это может занять '
          + 'несколько минут — потом он будет открываться сразу. Страницу можно не закрывать.';
      }
    }, 3000);
  }
  try {
    await loadCase(r.id, caseChanged);
  } catch (e) {
    mount(view, h`<div class="note err">Не удалось открыть сбор: ${e.message}</div><a href="#/">К списку сборов</a>`);
    return;
  }
  if (seq !== S.renderSeq) return;
  renderSide();
  const c = S.case;
  if (c.status !== 'ready') {
    renderTop();
    renderProgress(view, c);
    S.poll = setInterval(async () => {
      try {
        const nc = await api.get(`/api/cases/${c.id}`);
        if (nc.status === 'ready' || nc.status === 'error') {
          clearInterval(S.poll);
          await loadCases();
          await loadCase(c.id, true);
          route();
          if (nc.status === 'ready') toast('Журналы обработаны');
          return;
        }
        S.case = nc;
        renderProgress(view, nc);
      } catch (e) { /* повторим */ }
    }, 1000);
    return;
  }
  const sameTop = !caseChanged && prev && prev.kind === 'case' && $('#ribbon');
  if (sameTop) {
    // обновляем только вкладки, период и строку поиска — лента остаётся на месте
    $$('.tab').forEach((t) => t.classList.toggle('on', t.getAttribute('href').includes(`/${r.tab}`)));
    $$('.tab').forEach((t) => {
      const k = TABS.find(([key]) => t.getAttribute('href').split('?')[0].endsWith(`/${key}`))?.[0];
      if (k) t.setAttribute('href', caseHref(c.id, k, { range: r.range, q: k === 'search' ? r.q : '' }));
    });
    const q = $('#q');
    if (q && document.activeElement !== q) q.value = r.q;
    if (S.ribbonApi) S.ribbonApi.set(r.range);
    renderRibbonInfo();
  } else {
    renderTop();
  }
  const tab = TABS.find(([k]) => k === r.tab) || TABS[0];
  view.dataset.tab = tab[0];
  try {
    await tab[2](view, ctx());
  } catch (e) {
    console.error(e);
    mount(view, h`<div class="note err">Ошибка: ${e.message}</div>`);
  }
}

// ---------------------------------------------------------------------------
function theme() {
  const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = cur;
  try { localStorage.setItem('la-theme', cur); } catch (e) { /* */ }
  route();
}

async function boot() {
  $('#theme-btn').addEventListener('click', theme);
  $('#side-cases').addEventListener('click', onSideClick);
  window.addEventListener('la:cases-changed', () => loadCases());
  // команды из меню окна LogAtlas (LogAtlas.exe, LogAtlas.app)
  window.laCommand = (cmd) => {
    if (cmd !== 'collect-here') return;
    const onHome = S.route.kind === 'home';
    if (!onHome) window.location.hash = '#/';
    setTimeout(() => { const b = document.getElementById('local-collect'); if (b) b.click(); }, onHome ? 0 : 700);
  };
  try {
    S.meta = await api.get('/api/meta');
    $('#ver').textContent = `v${S.meta.version}`;
    if (S.meta.runtime) RT.runtime = S.meta.runtime;
    if (S.meta.log_hint) RT.logHint = S.meta.log_hint;
  } catch (e) { S.meta = { categories: {}, supported: {}, rules: [] }; }
  await loadCases();
  window.addEventListener('hashchange', route);
  window.addEventListener('keydown', (e) => {
    if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      const q = $('#q');
      if (q) { e.preventDefault(); q.focus(); q.select(); }
    }
    if (e.key === 'Escape') closeDrawer();
  });
  route();
}

export const app = { loadCases, route, reload: () => { S.route = { kind: 'none' }; S.case = null; S.full = null; route(); } };
boot();
