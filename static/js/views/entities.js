// Объекты: приложения, пользователи, удалённый доступ, ПО, устройства, сеть, закрепление
import { api } from '../api.js';
import { $, $$, fmtN, fmtTime, h, mount } from '../util.js';

const ORDER = ['apps', 'users', 'remote', 'software', 'devices', 'network', 'persistence', 'security', 'system', 'processes'];

function quote(v) { return `"${String(v).replace(/"/g, '')}"`; }

export async function renderEntities(view, ctx) {
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  const counts = await api.get(`/api/cases/${ctx.case.id}/entity-counts`, { from: ctx.from, to: ctx.to });
  counts.processes = { title: 'Процессы в журналах', n: null };
  let kind = ctx.sub && counts[ctx.sub] ? ctx.sub : ORDER.find((k) => counts[k]?.n) || 'apps';
  mount(view, h`
    <nav class="subtabs" id="ent-tabs">${ORDER.map((k) => h`<button type="button" class="chip ${k === kind ? 'on' : ''}" data-k="${k}" ${counts[k]?.n === 0 ? 'style="opacity:.55"' : ''}>${counts[k].title}${counts[k].n != null ? h` <span class="muted num">${fmtN(counts[k].n)}</span>` : ''}</button>`)}</nav>
    <section class="panel"><div id="ent-body"><div class="loading"><span class="spinner"></span></div></div></section>`);
  const c = ctx.case;
  const t0 = ctx.range ? ctx.range[0] : c.t_min;
  const t1 = ctx.range ? ctx.range[1] : c.t_max;
  const span = Math.max(1, t1 - t0);

  async function load() {
    $$('#ent-tabs [data-k]').forEach((b) => b.classList.toggle('on', b.dataset.k === kind));
    const body = $('#ent-body');
    mount(body, h`<div class="loading"><span class="spinner"></span></div>`);
    const d = await api.get(`/api/cases/${c.id}/entities`, { kind, from: ctx.from, to: ctx.to });
    if (!d.items.length) { mount(body, h`<div class="empty">Нет объектов этого типа${ctx.range ? ' в выбранном периоде' : ''}</div>`); return; }
    const isProc = kind === 'processes';
    mount(body, h`<div class="panel-h"><h2>${d.title}</h2><span class="sub">${fmtN(d.items.length)} ${isProc ? 'процессов' : 'объектов'}${d.total ? `, ${fmtN(d.total)} событий` : ''}</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>${isProc ? 'Процесс' : 'Объект'}</th>${isProc ? h`<th class="r">Источников</th>` : h`<th>Действия</th>`}<th class="r">Событий</th><th>Первое</th><th>Последнее</th><th style="min-width:140px">Активность в периоде</th></tr></thead><tbody>
      ${d.items.map((it) => h`<tr class="click" data-name="${it.name}">
        <td style="max-width:380px;word-break:break-all">${it.name}</td>
        ${isProc ? h`<td class="r num">${it.sources}</td>` : h`<td><div class="acts">${Object.entries(it.actions).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([a, n]) => h`<span>${a}${n > 1 ? ` ×${fmtN(n)}` : ''}</span>`)}</div></td>`}
        <td class="r num">${fmtN(it.n)}</td>
        <td class="t">${fmtTime(it.first, 'short')}</td><td class="t">${fmtTime(it.last, 'short')}</td>
        <td><div class="span-bar"><i style="left:${Math.max(0, Math.min(100, ((it.first - t0) / span) * 100))}%;width:${Math.max(0.8, ((it.last - it.first) / span) * 100)}%"></i></div></td>
      </tr>`)}</tbody></table></div>`);
    $$('tr[data-name]', body).forEach((tr) => tr.addEventListener('click', () => {
      const n = tr.dataset.name;
      if (isProc) ctx.search(`process:${quote(n)}`);
      else if (kind === 'users') ctx.search(`cat:access user:${quote(n)}`);
      else ctx.search(`subject:${quote(n)}`);
    }));
  }
  $$('#ent-tabs [data-k]').forEach((b) => b.addEventListener('click', () => {
    kind = b.dataset.k;
    window.history.replaceState(null, '', ctx.href('entities', { range: ctx.range, sub: kind }));
    load();
  }));
  await load();
}
