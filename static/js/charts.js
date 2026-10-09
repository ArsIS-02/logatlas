/* global d3 */
// Графики на d3: лента времени, столбцы по категориям, тепловая карта, санкей, дорожки хронологии, покрытие источников
import { CATS, CAT_KEYS, catColor, cssVar, esc, fmtBucket, fmtCompact, fmtN, fmtTime, hideTip, moveTip, showTip } from './util.js';

const ru = d3.timeFormatLocale({
  dateTime: '%A, %e %B %Y г. %X', date: '%d.%m.%Y', time: '%H:%M:%S', periods: ['', ''],
  days: ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'],
  shortDays: ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'],
  months: ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'],
  shortMonths: ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'],
});
const fSec = ru.utcFormat(':%S');
const fMin = ru.utcFormat('%H:%M');
const fDay = ru.utcFormat('%e %b');
const fYear = ru.utcFormat('%Y');
export function tickFmt(date) {
  if (d3.utcMinute(date) < date) return fSec(date);
  if (d3.utcHour(date) < date) return fMin(date);
  if (d3.utcDay(date) < date) return fMin(date);
  if (d3.utcMonth(date) < date) return fDay(date);
  if (d3.utcYear(date) < date) return fDay(date);
  return fYear(date);
}

const intFmt = (v) => (Number.isInteger(v) ? d3.format('~s')(v) : '');
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const DUR = () => (reduced() ? 0 : 450);

/** Перерисовка при изменении ширины контейнера */
const observers = new WeakMap();
function responsive(el, draw) {
  let lastW = 0;
  const run = () => {
    const w = Math.floor(el.clientWidth);
    if (!w || w === lastW) return;
    lastW = w;
    draw(w);
  };
  const old = observers.get(el);
  if (old) old.disconnect();
  const ro = new ResizeObserver(() => run());
  ro.observe(el);
  observers.set(el, ro);
  run();
}

function svgIn(el, w, h) {
  el.innerHTML = '';
  return d3.select(el).append('svg').attr('viewBox', `0 0 ${w} ${h}`).attr('width', w).attr('height', h);
}

// ---------------------------------------------------------------------------
// Лента времени в шапке
export function ribbon(el, data, sel, onSelect) {
  const H = 46;
  const n = data.total.length;
  const t0 = data.start;
  const t1 = data.start + data.bucket * n;
  let api = { set() {} };
  responsive(el, (W) => {
    const svg = svgIn(el, W, H);
    const x = d3.scaleUtc().domain([t0, t1]).range([0, W]);
    const ymax = d3.max(data.total) || 1;
    const y = d3.scaleLog().domain([1, ymax + 1]).range([H - 15, 3]).clamp(true);
    const pts = (arr) => arr.map((v, i) => [t0 + i * data.bucket + data.bucket / 2, v + 1]);
    const area = d3.area().x((d) => x(d[0])).y0(H - 15).y1((d) => y(d[1])).curve(d3.curveStepAfter);
    svg.append('path').attr('class', 'r-area').attr('d', area(pts(data.total)));
    if (data.classified) svg.append('path').attr('class', 'r-area-c').attr('d', area(pts(data.classified)));
    svg.append('line').attr('class', 'r-base').attr('x1', 0).attr('x2', W).attr('y1', H - 15).attr('y2', H - 15);
    const ticks = x.ticks(Math.max(2, Math.floor(W / 120)));
    svg.selectAll('.r-tick').data(ticks).join('text').attr('class', 'r-tick')
      .attr('x', (d) => x(d)).attr('y', H - 2).attr('text-anchor', 'middle').text((d) => tickFmt(d));
    let silent = false;
    const brush = d3.brushX().extent([[0, 0], [W, H - 14]])
      .on('end', (ev) => {
        if (silent) return;
        if (!ev.selection) { onSelect(null); return; }
        const [a, b] = ev.selection.map((v) => Math.round(x.invert(v).getTime()));
        if (b - a < 1000) { onSelect(null); return; }
        onSelect([a, b]);
      });
    const g = svg.append('g').attr('class', 'brush').call(brush);
    g.selectAll('.handle').attr('rx', 2);
    api = {
      set(s) {
        silent = true;
        g.call(brush.move, s ? [x(Math.max(s[0], t0)), x(Math.min(s[1], t1))] : null);
        silent = false;
      },
    };
    api.set(sel);
  });
  return { set: (s) => api.set(s) };
}

// ---------------------------------------------------------------------------
// Столбцы по категориям с выделением периода
export function stacked(el, data, opts = {}) {
  const H = opts.height || 220;
  const keys = (opts.keys || CAT_KEYS).filter((k) => !(opts.hidden && opts.hidden.has(k)));
  const n = data.series[CAT_KEYS[0]]?.length || data.total?.length || 0;
  const rows = d3.range(n).map((i) => {
    const r = { i, t: data.start + i * data.bucket };
    keys.forEach((k) => { r[k] = data.series[k] ? data.series[k][i] : 0; });
    return r;
  });
  responsive(el, (W) => {
    const m = { t: 8, r: 6, b: 22, l: 44 };
    const svg = svgIn(el, W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleUtc().domain([data.start, data.start + data.bucket * n]).range([0, iw]);
    const stack = d3.stack().keys(keys)(rows);
    const ymax = d3.max(stack.length ? stack[stack.length - 1] : [], (d) => d[1]) || 1;
    const y = (opts.scale === 'sqrt' ? d3.scaleSqrt() : d3.scaleLinear()).domain([0, ymax]).nice(4).range([ih, 0]);
    g.append('g').attr('class', 'grid').selectAll('line').data(y.ticks(4)).join('line')
      .attr('x1', 0).attr('x2', iw).attr('y1', (d) => y(d)).attr('y2', (d) => y(d));
    g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(4).tickSize(0).tickPadding(8).tickFormat(intFmt))
      .call((a) => a.select('.domain').remove());
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(Math.max(2, Math.floor(iw / 110))).tickFormat(tickFmt).tickSizeOuter(0));
    const step = iw / Math.max(n, 1);
    const bw = Math.max(1, Math.min(24, step - (step > 4 ? 2 : 0.5)));
    const off = (step - bw) / 2;
    const layers = g.append('g').selectAll('g').data(stack).join('g').attr('fill', (d) => catColor(d.key));
    const surf = cssVar('--panel');
    layers.selectAll('rect').data((d) => d).join('rect')
      .attr('x', (d) => x(d.data.t) + off).attr('width', bw)
      .attr('y', ih).attr('height', 0)
      .attr('stroke', surf).attr('stroke-width', bw > 3 ? 1 : 0)
      .transition().duration(DUR()).delay((d, i) => (reduced() ? 0 : i * 2))
      .attr('y', (d) => y(d[1])).attr('height', (d) => Math.max(0, y(d[0]) - y(d[1])));
    const tip = (ev) => {
      const [px] = d3.pointer(ev, g.node());
      const i = Math.floor(px / step);
      const d = rows[i];
      if (!d) { hideTip(); return; }
      const lines = keys.filter((k) => d[k]).map((k) => `<div class="tt-row"><span><i style="background:${catColor(k)}"></i>${esc(CATS[k])}</span><b>${fmtN(d[k])}</b></div>`);
      const tot = data.total ? `<div class="tt-row"><span class="muted">Всего событий</span><b>${fmtN(data.total[d.i])}</b></div>` : '';
      showTip(`<div class="tt-t">${fmtTime(d.t, 'short')} · ${fmtBucket(data.bucket)}</div>${lines.join('') || '<span class="muted">нет классифицированных событий</span>'}${tot}`, ev);
    };
    const brush = d3.brushX().extent([[0, 0], [iw, ih]]).on('end', (ev) => {
      if (!ev.selection || !ev.sourceEvent) return;
      const [a, b] = ev.selection.map((v) => Math.round(x.invert(v).getTime()));
      g.select('.brush').call(brush.move, null);
      if (b - a >= 1000 && opts.onSelect) opts.onSelect([a, b]);
    });
    const bg = g.append('g').attr('class', 'brush').call(brush);
    if (!opts.onSelect) bg.style('pointer-events', 'all').select('.overlay').attr('cursor', 'default');
    bg.on('mousemove.tip', tip).on('mouseleave.tip', hideTip).on('mousedown.tip', hideTip);
  });
}

// ---------------------------------------------------------------------------
// Простая гистограмма (поиск) с выделением периода
export function hist(el, data, opts = {}) {
  const H = opts.height || 96;
  const n = data.hist.length;
  responsive(el, (W) => {
    const m = { t: 6, r: 4, b: 20, l: 40 };
    const svg = svgIn(el, W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    const x = d3.scaleUtc().domain([data.start, data.start + data.bucket * n]).range([0, iw]);
    const y = d3.scaleLinear().domain([0, d3.max(data.hist) || 1]).nice(3).range([ih, 0]);
    g.append('g').attr('class', 'grid').selectAll('line').data(y.ticks(3)).join('line')
      .attr('x1', 0).attr('x2', iw).attr('y1', (d) => y(d)).attr('y2', (d) => y(d));
    g.append('g').attr('class', 'axis').call(d3.axisLeft(y).ticks(3).tickSize(0).tickPadding(6).tickFormat(intFmt)).call((a) => a.select('.domain').remove());
    g.append('g').attr('class', 'axis').attr('transform', `translate(0,${ih})`).call(d3.axisBottom(x).ticks(Math.max(2, Math.floor(iw / 110))).tickFormat(tickFmt).tickSizeOuter(0));
    const step = iw / Math.max(n, 1);
    const bw = Math.max(1, Math.min(24, step - (step > 4 ? 2 : 0.5)));
    const color = opts.color || cssVar('--accent');
    g.append('g').selectAll('rect').data(data.hist).join('rect')
      .attr('x', (d, i) => x(data.start + i * data.bucket) + (step - bw) / 2).attr('width', bw)
      .attr('y', (d) => y(d)).attr('height', (d) => ih - y(d)).attr('fill', color).attr('rx', bw > 6 ? 2 : 0);
    const tip = (ev) => {
      const [px] = d3.pointer(ev, g.node());
      const i = Math.floor(px / step);
      if (i < 0 || i >= n) { hideTip(); return; }
      showTip(`<div class="tt-t">${fmtTime(data.start + i * data.bucket, 'short')} · ${fmtBucket(data.bucket)}</div><b>${fmtN(data.hist[i])}</b> ${opts.unit || 'совпадений'}`, ev);
    };
    const brush = d3.brushX().extent([[0, 0], [iw, ih]]).on('end', (ev) => {
      if (!ev.selection || !ev.sourceEvent) return;
      const [a, b] = ev.selection.map((v) => Math.round(x.invert(v).getTime()));
      g.select('.brush').call(brush.move, null);
      if (b - a >= 1000 && opts.onSelect) opts.onSelect([a, b]);
    });
    g.append('g').attr('class', 'brush').call(brush)
      .on('mousemove.tip', tip).on('mouseleave.tip', hideTip).on('mousedown.tip', hideTip);
  });
}

// ---------------------------------------------------------------------------
// Тепловая карта «день × час»
export function heatmap(el, data, opts = {}) {
  // непрерывный ряд дней (дни без событий — пустые строки)
  const DAYMS = 86400000;
  const days = [];
  if (data.days.length) for (let t = data.days[0]; t <= data.days[data.days.length - 1]; t += DAYMS) days.push(t);
  const idx = new Map(days.map((d, i) => [d, i]));
  data = { ...data, cells: data.cells.map(([di, hh, v]) => [idx.get(data.days[di]), hh, v]) };
  const max = d3.max(data.cells, (c) => c[2]) || 1;
  const stops = ['--heat-0', '--heat-1', '--heat-2', '--heat-3', '--heat-4'].map(cssVar);
  const interp = d3.interpolateRgbBasis(stops);
  const color = (v) => (v ? interp(0.15 + 0.85 * Math.sqrt(v / max)) : stops[0]);
  const fmtD = ru.utcFormat('%a %e %b');
  responsive(el, (W) => {
    const labelW = 74;
    const cw = Math.max(6, Math.floor((W - labelW - 4) / 24));
    const ch = days.length > 40 ? 9 : days.length > 20 ? 14 : Math.min(cw * 0.9, 30);
    const H = 22 + days.length * ch + 4;
    const svg = svgIn(el, W, H);
    const g = svg.append('g').attr('transform', `translate(${labelW},18)`);
    // ночная полоса
    g.append('rect').attr('x', -2).attr('y', -16).attr('width', cw * 6 + 2).attr('height', days.length * ch + 18)
      .attr('fill', cssVar('--brass-soft')).attr('rx', 6);
    g.append('text').attr('class', 'lbl').attr('x', 4).attr('y', -5).text('ночь');
    [0, 6, 12, 18, 23].forEach((hh) => g.append('text').attr('class', 'lbl').attr('x', hh * cw + cw / 2)
      .attr('y', -5).attr('text-anchor', 'middle').text(hh === 0 ? '' : `${hh}:00`));
    const map = new Map(data.cells.map((c) => [`${c[0]}:${c[1]}`, c[2]]));
    const cells = [];
    days.forEach((d, di) => { for (let hh = 0; hh < 24; hh += 1) cells.push({ di, hh, v: map.get(`${di}:${hh}`) || 0, d }); });
    g.selectAll('rect.c').data(cells).join('rect').attr('class', 'c')
      .attr('x', (c) => c.hh * cw + 1).attr('y', (c) => c.di * ch + 1)
      .attr('width', cw - 2).attr('height', Math.max(2, ch - 2)).attr('rx', Math.min(3, ch / 4))
      .attr('fill', (c) => color(c.v)).style('cursor', (c) => (c.v && opts.onCell ? 'pointer' : 'default'))
      .on('mousemove', (ev, c) => showTip(`<div class="tt-t">${fmtTime(c.d + c.hh * 3600000, 'date')}, ${c.hh}:00–${c.hh + 1}:00</div><b>${fmtN(c.v)}</b> ${opts.unit || 'событий'}`, ev))
      .on('mouseleave', hideTip)
      .on('click', (ev, c) => { if (c.v && opts.onCell) opts.onCell(c.d + c.hh * 3600000, c.d + (c.hh + 1) * 3600000 - 1); });
    const every = Math.ceil(12 / ch);
    svg.append('g').attr('transform', 'translate(0,18)').selectAll('text').data(days).join('text')
      .attr('class', 'lbl').attr('x', labelW - 8).attr('text-anchor', 'end')
      .attr('y', (d, i) => i * ch + ch / 2 + 4).text((d, i) => (i % every === 0 ? fmtD(new Date(d)) : ''));
  });
}

// ---------------------------------------------------------------------------
// Горизонтальные полосы (HTML)
export function hbars(el, items, opts = {}) {
  if (!items.length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const max = d3.max(items, (d) => d.n) || 1;
  el.innerHTML = `<div class="hb">${items.map((d, i) => `
    <button class="hb-row" data-i="${i}" type="button" title="${esc(d.name)}">
      <span class="hb-name">${d.color ? `<i style="background:${d.color}"></i>` : ''}${esc(d.name ?? '—')}</span>
      <span class="hb-bar"><b style="width:${Math.max(1.5, (100 * d.n) / max)}%;background:${d.barColor || cssVar('--accent')}"></b></span>
      <span class="hb-v num">${fmtN(d.n)}</span>
    </button>`).join('')}</div>`;
  if (opts.onClick) el.querySelectorAll('.hb-row').forEach((b) => b.addEventListener('click', () => opts.onClick(items[+b.dataset.i])));
}

// ---------------------------------------------------------------------------
// Санкей: источник → категория → действие
export function sankey(el, data, opts = {}) {
  if (!data.links.length) { el.innerHTML = '<div class="empty">Нет классифицированных событий</div>'; return; }
  const H = opts.height || 380;
  responsive(el, (W) => {
    const svg = svgIn(el, W, H);
    const left = Math.min(150, W * 0.2), right = Math.min(240, W * 0.32);
    const nodes = data.nodes.map((id) => ({ id }));
    const links = data.links.map((l) => ({ ...l }));
    const sk = d3.sankey().nodeId((d) => d.id).nodeWidth(10).nodePadding(9)
      .nodeSort((a, b) => b.value - a.value)
      .extent([[left, 6], [W - right, H - 6]]);
    let graph;
    try { graph = sk({ nodes, links }); } catch (e) { el.innerHTML = '<div class="empty">Не удалось построить диаграмму</div>'; return; }
    const catOf = (n) => {
      if (n.id.startsWith('c:')) return n.id.slice(2);
      if (n.id === 'a:Прочие действия') return null;
      if (n.id.startsWith('a:')) return n.targetLinks[0]?.source.id.slice(2);
      return null;
    };
    const nameOf = (n) => {
      const s = n.id.slice(2);
      return n.id.startsWith('c:') ? CATS[s] || s : s;
    };
    const ink = cssVar('--ink-2');
    const linkG = svg.append('g').attr('fill', 'none');
    const lk = linkG.selectAll('path').data(graph.links).join('path')
      .attr('d', d3.sankeyLinkHorizontal())
      .attr('stroke', (d) => catColor(d.source.id.startsWith('c:') ? catOf(d.source) : catOf(d.target)))
      .attr('stroke-opacity', 0.28)
      .attr('stroke-width', (d) => Math.max(1, d.width))
      .on('mousemove', (ev, d) => showTip(`${esc(nameOf(d.source))} → ${esc(nameOf(d.target))}<br><b>${fmtN(d.value)}</b> событий`, ev))
      .on('mouseleave', hideTip);
    const node = svg.append('g').selectAll('g').data(graph.nodes).join('g');
    node.append('rect').attr('x', (d) => d.x0).attr('y', (d) => d.y0).attr('height', (d) => Math.max(1, d.y1 - d.y0))
      .attr('width', (d) => d.x1 - d.x0).attr('rx', 2)
      .attr('fill', (d) => (d.id.startsWith('s:') ? ink : catColor(catOf(d))));
    node.append('text').attr('class', 'lbl')
      .attr('x', (d) => (d.id.startsWith('s:') ? d.x0 - 8 : d.x1 + 8))
      .attr('y', (d) => (d.y0 + d.y1) / 2).attr('dy', '0.35em')
      .attr('text-anchor', (d) => (d.id.startsWith('s:') ? 'end' : 'start'))
      .text((d) => {
        const t = nameOf(d);
        const max = d.id.startsWith('a:') ? Math.floor(right / 6.6) : d.id.startsWith('s:') ? Math.floor(left / 6.6) : 26;
        return t.length > max ? `${t.slice(0, max - 1)}…` : t;
      })
      .filter((d) => d.y1 - d.y0 < 5 && !d.id.startsWith('c:')).attr('opacity', 0);
    node.style('cursor', opts.onNode ? 'pointer' : 'default')
      .on('mouseenter', (ev, d) => {
        lk.attr('stroke-opacity', (l) => (l.source === d || l.target === d ? 0.6 : 0.08));
      })
      .on('mousemove', (ev, d) => showTip(`<b>${esc(nameOf(d))}</b><br>${fmtN(d.value)} событий`, ev))
      .on('mouseleave', () => { lk.attr('stroke-opacity', 0.28); hideTip(); })
      .on('click', (ev, d) => opts.onNode && opts.onNode(d.id));
  });
}

// ---------------------------------------------------------------------------
// Дорожки хронологии с масштабированием колесом/жестом
export function swimlane(el, data, opts = {}) {
  const lanes = CAT_KEYS;
  const laneH = 34;
  const m = { t: 26, r: 12, b: 8, l: 168 };
  const H = m.t + lanes.length * laneH + m.b;
  const dom = opts.domain;
  const hashY = (s) => {
    let x = 0;
    for (let i = 0; i < (s || '').length; i += 1) x = (x * 31 + s.charCodeAt(i)) % 997;
    return (x / 997 - 0.5) * (laneH - 14);
  };
  let api = { reset() {} };
  responsive(el, (W) => {
    const svg = svgIn(el, W, H);
    const iw = W - m.l - m.r;
    const x0 = d3.scaleUtc().domain(dom).range([0, iw]);
    let x = x0.copy();
    const surf = cssVar('--panel');
    // подписи дорожек
    const counts = {};
    (data.points || []).forEach((p) => { counts[p[2]] = (counts[p[2]] || 0) + 1; });
    (data.buckets || []).forEach((b) => { counts[b[1]] = (counts[b[1]] || 0) + b[2] - 0; });
    const lab = svg.append('g').attr('transform', `translate(0,${m.t})`);
    lanes.forEach((c, i) => {
      lab.append('rect').attr('x', 8).attr('y', i * laneH + laneH / 2 - 4).attr('width', 8).attr('height', 8).attr('rx', 2).attr('fill', catColor(c))
        .attr('opacity', counts[c] ? 1 : 0.3);
      lab.append('text').attr('class', 'lbl').attr('x', 24).attr('y', i * laneH + laneH / 2 + 4).text(CATS[c])
        .attr('opacity', counts[c] ? 1 : 0.45);
    });
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    g.append('defs').append('clipPath').attr('id', 'swl-clip').append('rect').attr('x', -6).attr('y', -4).attr('width', iw + 12).attr('height', lanes.length * laneH + 8);
    g.append('g').selectAll('rect').data(lanes).join('rect').attr('x', 0).attr('width', iw)
      .attr('y', (d, i) => i * laneH + 2).attr('height', laneH - 4).attr('rx', 6)
      .attr('fill', (d, i) => (i % 2 ? 'transparent' : cssVar('--panel-2')));
    const axisG = g.append('g').attr('class', 'axis').attr('transform', 'translate(0,-4)');
    const body = g.append('g').attr('clip-path', 'url(#swl-clip)');
    const laneIdx = Object.fromEntries(lanes.map((c, i) => [c, i]));
    let bucketSel = null;
    if (data.mode === 'buckets' && data.buckets) {
      const bmax = d3.max(data.buckets, (b) => b[2]) || 1;
      const op = d3.scaleSqrt().domain([0, bmax]).range([0.18, 0.95]);
      bucketSel = body.append('g').selectAll('rect').data(data.buckets).join('rect')
        .attr('y', (b) => laneIdx[b[1]] * laneH + 7).attr('height', laneH - 14).attr('rx', 2)
        .attr('fill', (b) => catColor(b[1])).attr('opacity', (b) => op(b[2]))
        .on('mousemove', (ev, b) => showTip(`<div class="tt-t">${fmtTime(b[0], 'short')} · ${fmtBucket(data.bucket)}</div><b>${fmtN(b[2])}</b> событий — ${esc(CATS[b[1]])}`, ev))
        .on('mouseleave', hideTip)
        .on('click', (ev, b) => opts.onRange && opts.onRange([b[0], b[0] + data.bucket - 1]));
    }
    const pts = body.append('g').selectAll('circle').data(data.points || []).join('circle')
      .attr('r', 4.5).attr('cy', (p) => laneIdx[p[2]] * laneH + laneH / 2 + hashY(p[3]))
      .attr('fill', (p) => catColor(p[2])).attr('stroke', surf).attr('stroke-width', 2).style('cursor', 'pointer')
      .on('mousemove', (ev, p) => showTip(`<div class="tt-t">${fmtTime(p[1])}</div><b>${esc(p[3] || '')}</b>${p[4] ? `<br>${esc(p[4])}` : ''}${p[5] ? `<br><span class="muted">${esc(p[5])}</span>` : ''}`, ev))
      .on('mouseleave', hideTip)
      .on('click', (ev, p) => opts.onPoint && opts.onPoint(p[0]));
    function place() {
      axisG.call(d3.axisTop(x).ticks(Math.max(2, Math.floor(iw / 120))).tickFormat(tickFmt).tickSizeOuter(0))
        .call((a) => a.select('.domain').remove());
      pts.attr('cx', (p) => x(p[1]));
      if (bucketSel) bucketSel.attr('x', (b) => x(b[0])).attr('width', (b) => Math.max(1.5, x(b[0] + data.bucket) - x(b[0]) - 1));
    }
    place();
    const zoom = d3.zoom().scaleExtent([1, 5000]).extent([[0, 0], [iw, H]]).translateExtent([[0, 0], [iw, H]])
      .filter((ev) => (ev.type === 'wheel' ? ev.ctrlKey || ev.metaKey || ev.shiftKey || opts.wheel : !ev.button))
      .on('zoom', (ev) => { x = ev.transform.rescaleX(x0); place(); })
      .on('end', (ev) => {
        if (ev.transform.k === 1 && ev.transform.x === 0) return;
        const d = x.domain().map((v) => v.getTime());
        if (opts.onZoom) opts.onZoom(d);
      });
    const zr = g.insert('rect', ':first-child').attr('width', iw).attr('height', lanes.length * laneH).attr('fill', 'transparent');
    svg.call(zoom);
    zr.lower();
    api = { reset: () => svg.transition().duration(DUR()).call(zoom.transform, d3.zoomIdentity) };
  });
  return { reset: () => api.reset() };
}

// ---------------------------------------------------------------------------
// Покрытие источников во времени
export function gantt(el, rows, opts = {}) {
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const rowH = 24;
  const m = { t: 22, r: 70, b: 6, l: Math.min(190, 40 + d3.max(rows, (r) => r.label.length) * 6.6) };
  const H = m.t + rows.length * rowH + m.b;
  const t0 = d3.min(rows, (r) => r.t_min);
  const t1 = d3.max(rows, (r) => r.t_max);
  responsive(el, (W) => {
    const svg = svgIn(el, W, H);
    const iw = W - m.l - m.r;
    const x = d3.scaleUtc().domain([t0, Math.max(t1, t0 + 60000)]).range([0, iw]);
    const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
    g.append('g').attr('class', 'axis').attr('transform', 'translate(0,-4)')
      .call(d3.axisTop(x).ticks(Math.max(2, Math.floor(iw / 110))).tickFormat(tickFmt).tickSizeOuter(0)).call((a) => a.select('.domain').remove());
    g.append('g').attr('class', 'grid').selectAll('line').data(x.ticks(Math.max(2, Math.floor(iw / 110)))).join('line')
      .attr('x1', (d) => x(d)).attr('x2', (d) => x(d)).attr('y1', 0).attr('y2', rows.length * rowH);
    const row = g.selectAll('g.row').data(rows).join('g').attr('class', 'row').attr('transform', (d, i) => `translate(0,${i * rowH})`);
    row.append('rect').attr('x', (d) => x(d.t_min)).attr('y', 6).attr('height', rowH - 12).attr('rx', 3)
      .attr('width', (d) => Math.max(3, x(d.t_max) - x(d.t_min))).attr('fill', (d) => d.color || cssVar('--accent')).attr('opacity', 0.85)
      .on('mousemove', (ev, d) => showTip(`<b>${esc(d.label)}</b><br>${fmtTime(d.t_min)} — ${fmtTime(d.t_max)}<br>${fmtN(d.n)} событий`, ev))
      .on('mouseleave', hideTip)
      .style('cursor', opts.onClick ? 'pointer' : 'default')
      .on('click', (ev, d) => opts.onClick && opts.onClick(d));
    row.append('text').attr('class', 'lbl').attr('x', -10).attr('y', rowH / 2 + 4).attr('text-anchor', 'end').text((d) => d.label);
    row.append('text').attr('class', 'val').attr('x', iw + 8).attr('y', rowH / 2 + 4).text((d) => fmtCompact(d.n));
  });
}

// ---------------------------------------------------------------------------
// Кольцо прогресса
export function ring(pct) {
  const r = 60, c = 2 * Math.PI * r;
  return `<div class="ring-wrap"><svg viewBox="0 0 140 140"><circle cx="70" cy="70" r="${r}" fill="none" class="ring-bg" stroke-width="10"/>
    <circle cx="70" cy="70" r="${r}" fill="none" class="ring-fg" stroke-width="10" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - pct)}"/></svg>
    <div class="pct">${Math.round(pct * 100)}%</div></div>`;
}
