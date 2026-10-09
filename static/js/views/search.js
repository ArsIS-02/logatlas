// Поиск по событиям: гистограмма совпадений, фасеты, таблица, выгрузка CSV
import { api } from '../api.js';
import { hist } from '../charts.js';
import { $, $$, CATS, catColor, esc, fmtBucket, fmtN, fmtTime, h, highlight, mount, raw } from '../util.js';

const FACETS = [['category', 'Категория', 'cat'], ['source', 'Источник', 'source'], ['process', 'Процесс', 'process'], ['level', 'Уровень', 'level']];
const LEVEL_RU = { debug: 'отладка', info: 'инфо', notice: 'notice', warning: 'предупреждение', error: 'ошибка', critical: 'критично' };

function q(v) { return /[\s"|]/.test(v) ? `"${String(v).replace(/"/g, '')}"` : v; }

export async function renderSearch(view, ctx) {
  const page = +(ctx.params.get('page') || 0);
  const order = ctx.params.get('order') || 'desc';
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  let d;
  try {
    d = await api.get(`/api/cases/${ctx.case.id}/search`, { q: ctx.q, from: ctx.from, to: ctx.to, page, order });
  } catch (e) {
    mount(view, h`<div class="note err">${e.message}</div>`);
    return;
  }
  const pages = Math.ceil(d.total / d.size);
  const exportHref = `/api/cases/${ctx.case.id}/export.csv?${new URLSearchParams(Object.entries({ q: ctx.q, from: ctx.from ?? '', to: ctx.to ?? '' }).filter(([, v]) => v !== ''))}`;
  const nav = (p, o = order) => ctx.go('search', { extra: { page: p || null, order: o === 'desc' ? null : o } });
  mount(view, h`
    <div class="res-h">
      <div><span class="tot num">${fmtN(d.total)}</span> <span class="ink2">${ctx.q ? 'совпадений' : 'событий — введите запрос в строке поиска'}</span> <span class="muted" style="font-size:12px">${d.took_ms} мс</span></div>
      <div class="btn-row">
        <button class="btn small ${order === 'desc' ? '' : 'ghost'}" type="button" data-order="desc">Сначала новые</button>
        <button class="btn small ${order === 'asc' ? '' : 'ghost'}" type="button" data-order="asc">Сначала старые</button>
        <a class="btn small" href="${exportHref}" download>Скачать CSV</a>
      </div>
    </div>
    ${d.total ? h`<section class="panel" style="padding-bottom:10px"><div class="chart" id="s-hist"></div>
      <div class="muted" style="font-size:12px;margin-top:4px">Столбец — ${fmtBucket(d.bucket)}. Выделите участок, чтобы сузить период.</div></section>` : ''}
    <div class="search-layout" style="margin-top:18px">
      <aside class="facets panel">
        ${d.total ? FACETS.map(([k, name, field]) => h`<h4>${name}</h4>${(d.facets[k] || []).map(([v, n]) => {
          const max = d.facets[k][0][1] || 1;
          const label = k === 'category' ? (CATS[v] || 'без категории') : k === 'level' ? (LEVEL_RU[v] || v || '—') : (v ?? '—');
          return h`<button type="button" class="facet" data-f="${field}" data-v="${v ?? ''}" title="Щелчок — добавить в запрос, Alt+щелчок — исключить">
            <span class="fb" style="width:${(100 * n) / max}%"></span>
            ${k === 'category' && v ? raw(`<i style="width:8px;height:8px;border-radius:2px;background:${catColor(v)};position:relative;z-index:1"></i>`) : ''}
            <span class="fn">${label}</span><span class="fc">${fmtN(n)}</span></button>`;
        })}`) : h`<div class="muted" style="font-size:13px">Нет совпадений. Попробуйте убрать часть условий или расширить период.</div>`}
      </aside>
      <section class="panel" style="padding:6px 0 12px">
        ${d.rows.length ? h`<div class="tbl-wrap"><table class="tbl" id="s-tbl"><thead><tr><th>Время</th><th>Источник</th><th>Процесс</th><th>Действие</th><th>Сообщение</th></tr></thead><tbody>
          ${d.rows.map((r) => h`<tr class="click" data-id="${r.id}">
            <td class="t">${fmtTime(r.t, 'ms')}</td>
            <td class="ink2" style="font-size:12.5px">${r.source || ''}</td>
            <td style="font-size:12.5px">${r.process || ''}${r.pid ? h`<span class="muted"> ${r.pid}</span>` : ''}</td>
            <td>${r.action ? h`<span class="cat-tag"><i style="background:${catColor(r.category)}"></i><span class="act">${r.action}</span></span>${r.subject ? h`<div class="subj">${r.subject}</div>` : ''}` : r.level && !['info', 'notice', 'debug'].includes(r.level) ? h`<span class="muted" style="font-size:12px">${LEVEL_RU[r.level] || r.level}</span>` : ''}</td>
            <td class="msg"><div class="clip">${raw(highlight(r.message, d.terms))}</div></td></tr>`)}
        </tbody></table></div>
        <div class="pager">
          <span>Стр. ${page + 1} из ${fmtN(Math.max(1, pages))}</span>
          <button class="btn small" type="button" id="pg-prev" ${page <= 0 ? 'disabled' : ''}>Назад</button>
          <button class="btn small" type="button" id="pg-next" ${page + 1 >= pages ? 'disabled' : ''}>Вперёд</button>
        </div>` : h`<div class="empty">Ничего не найдено</div>`}
      </section>
    </div>`);
  if (d.total) hist($('#s-hist'), { start: d.hist_start, bucket: d.bucket, hist: d.hist }, { height: 90, onSelect: (r) => ctx.setRange(r) });
  $$('[data-order]', view).forEach((b) => b.addEventListener('click', () => nav(0, b.dataset.order)));
  const prev = $('#pg-prev'), next = $('#pg-next');
  if (prev) prev.addEventListener('click', () => nav(page - 1));
  if (next) next.addEventListener('click', () => nav(page + 1));
  $$('.facet', view).forEach((b) => b.addEventListener('click', (e) => {
    const { f, v } = b.dataset;
    const term = v ? `${e.altKey ? '-' : ''}${f}:${q(v)}` : `${e.altKey ? '' : '-'}has:${f === 'cat' ? 'category' : f}`;
    ctx.search(`${ctx.q} ${term}`.trim());
  }));
  $$('#s-tbl tr[data-id]').forEach((tr) => tr.addEventListener('click', () => {
    $$('#s-tbl tr.focus').forEach((x) => x.classList.remove('focus'));
    tr.classList.add('focus');
    ctx.openEvent(+tr.dataset.id);
  }));
}
export { esc };
