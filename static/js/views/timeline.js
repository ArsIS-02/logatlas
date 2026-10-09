// Хронология: дорожки по категориям с масштабированием и список событий в окне
import { api } from '../api.js';
import { swimlane } from '../charts.js';
import { $, $$, CATS, CAT_KEYS, catColor, catTag, fmtBucket, fmtN, fmtSpan, fmtTime, h, mount } from '../util.js';

export async function renderTimeline(view, ctx) {
  const c = ctx.case;
  const base = ctx.range || [c.t_min, c.t_max];
  let win = base.slice();
  const cats = new Set();
  mount(view, h`
    <section class="panel">
      <div class="panel-h">
        <h2>Хронология хоста</h2>
        <div class="tools">
          <span class="sub" id="tl-span"></span>
          <button class="btn small" type="button" id="tl-apply" disabled>Сделать периодом дела</button>
          <button class="btn ghost small" type="button" id="tl-reset" disabled>Исходный масштаб</button>
        </div>
      </div>
      <div class="legend" id="tl-cats" style="margin:0 0 12px">${CAT_KEYS.map((k) => h`<button type="button" class="chip" data-cat="${k}"><span class="dot" style="background:${catColor(k)}"></span>${CATS[k]}</button>`)}</div>
      <div class="chart" id="tl"></div>
      <p class="muted" style="font-size:12px;margin:10px 0 0">Перетаскивайте, чтобы сдвинуть; Ctrl, ⌘ или Shift с колесом — масштаб, двойной щелчок — приблизить. Точки — отдельные события, полосы — плотность частых событий.</p>
    </section>
    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>События в окне</h2><span class="sub" id="tl-count"></span></div>
      <div id="tl-list" class="tl-list"></div>
    </section>`);

  let seq = 0;
  async function load() {
    const my = ++seq;
    $('#tl-span').textContent = `${fmtTime(win[0], 'short')} — ${fmtTime(win[1], 'short')}, ${fmtSpan(win[1] - win[0])}`;
    const d = await api.get(`/api/cases/${c.id}/timeline`, { from: Math.round(win[0]), to: Math.round(win[1]), cats: [...cats].join(',') });
    if (my !== seq) return;
    const zoomed = win[0] !== base[0] || win[1] !== base[1];
    $('#tl-apply').disabled = !zoomed;
    $('#tl-reset').disabled = !zoomed;
    swimlane($('#tl'), d, {
      domain: win,
      onPoint: (id) => ctx.openEvent(id),
      onZoom: (dm) => { win = dm; load(); },
      onRange: (r) => { win = r; load(); },
    });
    const pts = (d.points || []).slice().reverse();
    $('#tl-count').textContent = d.mode === 'buckets'
      ? `${fmtN(d.count)} событий; полосы по ${fmtBucket(d.bucket)}, списком — редкие действия`
      : `${fmtN(d.count)} событий`;
    mount($('#tl-list'), pts.length ? h`<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Время</th><th>Категория</th><th>Действие</th><th>Объект</th><th>Процесс</th></tr></thead><tbody>
      ${pts.slice(0, 2000).map((p) => h`<tr class="click" data-id="${p[0]}"><td class="t">${fmtTime(p[1])}</td><td>${catTag(p[2])}</td><td class="act">${p[3] || ''}</td><td class="subj">${p[4] || ''}</td><td class="ink2">${p[5] || ''}</td></tr>`)}
      </tbody></table></div>${pts.length > 2000 ? h`<p class="muted" style="font-size:12px">Показаны последние 2000. Приблизьте участок или откройте поиск.</p>` : ''}`
      : h`<div class="empty">В окне нет классифицированных событий</div>`);
    $$('#tl-list tr[data-id]').forEach((tr) => tr.addEventListener('click', () => ctx.openEvent(+tr.dataset.id)));
  }
  $$('#tl-cats [data-cat]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.cat;
    if (cats.has(k)) cats.delete(k); else cats.add(k);
    $$('#tl-cats [data-cat]').forEach((x) => x.classList.toggle('on', cats.has(x.dataset.cat)));
    load();
  }));
  $('#tl-apply').addEventListener('click', () => ctx.setRange([Math.round(win[0]), Math.round(win[1])]));
  $('#tl-reset').addEventListener('click', () => { win = base.slice(); load(); });
  await load();
}
