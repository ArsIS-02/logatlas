// Обзор сбора: сводка, активность по категориям, тепловая карта, находки, потоки событий
import { api } from '../api.js';
import { baselinePanel } from './compare.js';
import { hbars, heatmap, sankey, stacked } from '../charts.js';
import {
  $, $$, CATS, CAT_KEYS, SEV, SEV_KEYS, catColor, fmtBucket, fmtN, fmtSpan, fmtTime, h, mount, plural, raw, sevIcon, sevLabel, store,
} from '../util.js';

const HEAT_OPTS = [['access', 'Доступ и сеансы'], ['process', 'Процессы'], ['classified', 'Все классифицированные'], ['all', 'Все события']];

function quote(v) { return /[\s"]/.test(v) ? `"${String(v).replace(/"/g, '')}"` : v; }

export async function renderOverview(view, ctx) {
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  const d = ctx.range ? await api.get(`/api/cases/${ctx.case.id}/overview`, { from: ctx.from, to: ctx.to }) : ctx.full;
  if (!d || !d.total) {
    mount(view, h`<div class="empty">В выбранном периоде событий нет. <a href="${ctx.href('overview')}">Показать весь период</a></div>`);
    return;
  }
  const sevCount = Object.fromEntries(SEV_KEYS.map((s) => [s, 0]));
  (d.findings || []).forEach((f) => { sevCount[f.severity] = (sevCount[f.severity] || 0) + f.n; });
  const span = d.t_max - d.t_min;
  const nSrc = d.sources.length;
  const nCat = Object.keys(d.categories).length;
  mount(view, h`
    <section class="summary">
      <div class="lede">
        <div class="big num">${fmtN(d.total)}<small>${plural(d.total, 'событие', 'события', 'событий')}</small></div>
        <p>За ${fmtSpan(span)} (${fmtTime(d.t_min)} — ${fmtTime(d.t_max)}) из ${nSrc} ${plural(nSrc, 'источника', 'источников', 'источников')}.
          ${fmtN(d.categorized)} ${plural(d.categorized, 'событие отнесено', 'события отнесены', 'событий отнесены')} к ${nCat} ${plural(nCat, 'категории', 'категориям', 'категориям')} — их и показывают графики ниже, остальное доступно в поиске.</p>
        <div class="facts">
          <div><b class="num">${fmtN(d.n_processes)}</b> <span>процессов в журналах</span></div>
          ${d.hosts.length ? h`<div><b>${d.hosts.join(', ')}</b> <span>${d.hosts.length > 1 ? 'хосты' : 'хост'}</span></div>` : ''}
          ${d.users.length ? h`<div><b>${d.users.slice(0, 4).join(', ')}</b> <span>пользователи в событиях доступа</span></div>` : ''}
        </div>
      </div>
      <div class="sevbar" role="list">
        ${SEV_KEYS.map((s) => h`<a role="listitem" class="sev-tile ${sevCount[s] ? '' : 'zero'}" href="${ctx.href('findings', { range: ctx.range })}">
          <span class="v num">${fmtN(sevCount[s])}</span>
          <span class="l">${raw(sevIcon(s))}${SEV[s].name}</span></a>`)}
      </div>
    </section>

    <section class="panel bl-panel" id="baseline-panel" hidden></section>

    <section class="panel">
      <div class="panel-h"><h2>Активность по категориям</h2><span class="sub">Столбец — ${fmtBucket(d.bucket)}. Выделите участок, чтобы приблизить.</span></div>
      <div class="chart" id="stk"></div>
      <div class="legend" id="stk-legend"></div>
    </section>

    <div class="grid g-main" style="margin-top:18px">
      <section class="panel">
        <div class="panel-h"><h2>Когда работали с хостом</h2>
          <div class="tools" id="heat-tools">${HEAT_OPTS.map(([k, n]) => h`<button type="button" class="chip" data-heat="${k}">${n}</button>`)}</div></div>
        <div class="chart" id="heat"></div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Время хоста. Полоса слева — ночные часы 00:00–06:00. Щелчок по ячейке выделяет этот час.</p>
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Находки</h2><a class="sub" href="${ctx.href('findings', { range: ctx.range })}">Все находки</a></div>
        ${d.findings.length ? h`<table class="tbl"><tbody>${d.findings.slice(0, 9).map((f) => h`
          <tr class="click" data-rule="${f.id}"><td style="width:28px">${raw(sevIcon(f.severity))}</td><td><div>${f.title}</div><div class="muted" style="font-size:12px">${fmtTime(f.last, 'short')}</div></td><td class="r num">${fmtN(f.n)}</td></tr>`)}
        </tbody></table>` : h`<div class="empty">Правила ничего не нашли</div>`}
      </section>
    </div>

    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>Откуда события и что в них</h2><span class="sub">источник → категория → действие; щелчок открывает поиск</span></div>
      <div class="chart" id="sankey"></div>
    </section>

    <div class="grid g-3" style="margin-top:18px">
      <section class="panel"><div class="panel-h"><h2>Частые действия</h2></div><div id="acts"></div></section>
      <section class="panel"><div class="panel-h"><h2>Процессы в журналах</h2></div><div id="procs"></div></section>
      <section class="panel"><div class="panel-h"><h2>Источники</h2></div><div id="srcs"></div></section>
    </div>
  `);

  baselinePanel($('#baseline-panel'), ctx);

  // столбцы по категориям
  const hidden = new Set(store('la-hidden-cats') || []);
  // если один всплеск задавливает остальное — по умолчанию сжатая шкала
  const sums = d.hist_total.map((_, i) => CAT_KEYS.reduce((a, k) => a + (d.hist_cat[k]?.[i] || 0), 0)).filter((v) => v > 0).sort((a, b) => a - b);
  const p90 = sums[Math.floor(sums.length * 0.9)] || 1;
  let scale = store('la-scale') || ((sums[sums.length - 1] || 0) > 12 * p90 ? 'sqrt' : 'linear');
  const drawStacked = () => {
    stacked($('#stk'), { start: d.hist_start, bucket: d.bucket, series: d.hist_cat, total: d.hist_total },
      { hidden, height: 230, scale, onSelect: (r) => ctx.setRange(r) });
    mount($('#stk-legend'), h`<button type="button" class="chip ${scale === 'sqrt' ? 'on' : ''}" id="scale-btn" title="Квадратичная шкала показывает и мелкие, и крупные всплески">Сжатая шкала</button>${CAT_KEYS.filter((k) => d.categories[k]).map((k) => h`<button type="button" class="chip ${hidden.has(k) ? 'off' : ''}" data-cat="${k}" aria-pressed="${!hidden.has(k)}"><span class="dot" style="background:${catColor(k)}"></span>${CATS[k]} <span class="muted num">${fmtN(d.categories[k])}</span></button>`)}`);
    $('#scale-btn').addEventListener('click', () => { scale = scale === 'sqrt' ? 'linear' : 'sqrt'; store('la-scale', scale); drawStacked(); });
    $$('#stk-legend [data-cat]').forEach((b) => b.addEventListener('click', () => {
      const k = b.dataset.cat;
      if (hidden.has(k)) hidden.delete(k); else hidden.add(k);
      store('la-hidden-cats', [...hidden]);
      drawStacked();
    }));
  };
  drawStacked();

  // тепловая карта
  let heatCat = store('la-heat') || 'access';
  if (heatCat === 'access' && !d.categories.access) heatCat = 'classified';
  const drawHeat = async () => {
    $$('#heat-tools [data-heat]').forEach((b) => b.classList.toggle('on', b.dataset.heat === heatCat));
    const hm = heatCat === 'access' ? d.heatmap : await api.get(`/api/cases/${ctx.case.id}/heatmap`, { cat: heatCat, from: ctx.from, to: ctx.to });
    if (!hm.cells.length) { mount($('#heat'), h`<div class="empty">Нет событий этой категории</div>`); return; }
    heatmap($('#heat'), hm, { onCell: (a, b) => ctx.setRange([a, b]) });
  };
  $$('#heat-tools [data-heat]').forEach((b) => b.addEventListener('click', () => { heatCat = b.dataset.heat; store('la-heat', heatCat); drawHeat(); }));
  drawHeat();

  $$('tr[data-rule]', view).forEach((tr) => tr.addEventListener('click', () => ctx.go('findings', { sub: tr.dataset.rule })));

  sankey($('#sankey'), d.sankey, {
    height: Math.max(300, Math.min(560, d.sankey.nodes.length * 17)),
    onNode: (id) => {
      const v = id.slice(2);
      if (id.startsWith('s:')) ctx.search(`source:${quote(v)} has:category`);
      else if (id.startsWith('c:')) ctx.search(`cat:${v}`);
      else if (!v.startsWith('Прочие')) ctx.search(`action:${quote(v)}`);
    },
  });
  hbars($('#acts'), d.top_actions.map((a) => ({ name: a.name, n: a.n, color: catColor(a.category), barColor: catColor(a.category) })),
    { onClick: (a) => ctx.search(`action:${quote(a.name)}`) });
  hbars($('#procs'), d.top_processes, { onClick: (a) => ctx.search(`process:${quote(a.name)}`) });
  hbars($('#srcs'), d.sources.map((s) => ({ name: s.source, n: s.n })), { onClick: (a) => ctx.search(`source:${quote(a.name)}`) });
}
