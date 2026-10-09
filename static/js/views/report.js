// Отчёт для печати / сохранения в PDF
import { api } from '../api.js';
import { stacked } from '../charts.js';
import { $, CATS, catTag, fmtN, fmtSpan, fmtTime, h, mount, platName, sevLabel } from '../util.js';

const KEY_CATS = ['system', 'access', 'persistence', 'software', 'device', 'security'];

export async function renderReport(view, ctx) {
  const c = ctx.case;
  mount(view, h`<div class="loading"><span class="spinner"></span> Готовлю отчёт…</div>`);
  const ov = ctx.range ? await api.get(`/api/cases/${c.id}/overview`, { from: ctx.from, to: ctx.to }) : ctx.full;
  const details = {};
  const top = ov.findings.filter((f) => f.severity !== 'info').slice(0, 10);
  await Promise.all(top.map(async (f) => {
    details[f.id] = await api.get(`/api/cases/${c.id}/findings/${f.id}`, { from: ctx.from, to: ctx.to });
  }));
  const tl = await api.get(`/api/cases/${c.id}/timeline`, { from: ctx.from ?? c.t_min, to: ctx.to ?? c.t_max, cats: KEY_CATS.join(',') });
  const key = (tl.points || []).filter((p) => !/Активность|Дисплей|IP-адрес|Шлюз/.test(p[3] || ''));
  mount(view, h`
    <div class="report">
      <div class="btn-row no-print" style="justify-content:flex-end;margin-bottom:12px">
        <button class="btn primary" type="button" id="print">Печать / сохранить в PDF</button>
      </div>
      <h1>Отчёт по журналам: ${c.name}</h1>
      <p class="ink2">${platName(c.platform)}${c.host ? `, хост ${c.host}` : ''}. Время хоста ${c.tz_label}. Период: ${fmtTime(ov.t_min)} — ${fmtTime(ov.t_max)} (${fmtSpan(ov.t_max - ov.t_min)}). Сформирован ${new Date().toLocaleString('ru-RU')}.</p>
      <section class="panel"><div class="panel-h"><h2>Объём данных</h2></div>
        <p style="margin:0 0 10px">${fmtN(ov.total)} событий из ${ov.sources.length} источников, ${fmtN(ov.categorized)} классифицированы. Процессов в журналах: ${fmtN(ov.n_processes)}.</p>
        <div class="chart" id="r-stk"></div>
      </section>
      <h2>Находки</h2>
      ${ov.findings.length ? h`<table class="tbl"><thead><tr><th>Важность</th><th>Правило</th><th class="r">Событий</th><th>Период</th></tr></thead><tbody>
        ${ov.findings.map((f) => h`<tr><td>${sevLabel(f.severity)}</td><td>${f.title}<div class="muted" style="font-size:12px">${f.desc}</div></td><td class="r num">${fmtN(f.n)}</td><td class="t">${fmtTime(f.first, 'short')} — ${fmtTime(f.last, 'short')}</td></tr>`)}
      </tbody></table>` : h`<p class="muted">Правила не сработали.</p>`}
      ${top.map((f) => h`<h2>${f.title}</h2>
        ${details[f.id].groups.length ? h`<table class="tbl"><thead><tr><th>Объект</th><th class="r">Событий</th><th>Первое</th><th>Последнее</th></tr></thead><tbody>
          ${details[f.id].groups.slice(0, 15).map((g) => h`<tr><td style="word-break:break-all">${g.name}</td><td class="r num">${fmtN(g.n)}</td><td class="t">${fmtTime(g.first)}</td><td class="t">${fmtTime(g.last)}</td></tr>`)}
        </tbody></table>` : ''}`)}
      <h2>Ключевые события хоста</h2>
      <p class="muted" style="font-size:12.5px">Категории: ${KEY_CATS.map((k) => CATS[k]).join(', ')}${tl.mode === 'buckets' ? '. Событий много — показаны только редкие действия.' : '.'}</p>
      <table class="tbl"><thead><tr><th>Время</th><th>Категория</th><th>Действие</th><th>Объект</th></tr></thead><tbody>
        ${key.slice(0, 400).map((p) => h`<tr><td class="t">${fmtTime(p[1])}</td><td>${catTag(p[2])}</td><td>${p[3] || ''}</td><td class="subj">${p[4] || ''}</td></tr>`)}
      </tbody></table>
    </div>`);
  stacked($('#r-stk'), { start: ov.hist_start, bucket: ov.bucket, series: ov.hist_cat, total: ov.hist_total }, { height: 180 });
  $('#print').addEventListener('click', () => window.print());
}
