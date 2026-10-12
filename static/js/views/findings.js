// Находки правил обнаружения и проверка индикаторов компрометации
import { api } from '../api.js';
import { $, $$, SEV, catTag, fmtN, fmtTime, h, mount, raw, sevIcon, sevLabel, store, toast } from '../util.js';

export async function renderFindings(view, ctx) {
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  const list = await api.get(`/api/cases/${ctx.case.id}/findings`, { from: ctx.from, to: ctx.to });
  const rules = ctx.meta.rules || [];
  const quiet = rules.filter((r) => !list.find((f) => f.id === r.id));
  mount(view, h`
    <div class="grid g-main">
      <div>
        ${list.length ? list.map((f) => h`<details class="finding" data-rule="${f.id}" ${ctx.sub === f.id ? 'open' : ''}>
          <summary>
            ${sevLabel(f.severity)}
            <div><div class="ft">${f.title}${f.mitre ? h`<span class="mitre" title="MITRE ATT&CK">${f.mitre}</span>` : ''}${f.sigma ? h`<span class="mitre sigma-tag" title="Импортированное правило Sigma">Sigma</span>` : ''}</div><div class="fd">${f.desc}</div></div>
            <div class="fm"><b class="num">${fmtN(f.n)}</b>${fmtTime(f.first, 'short')} — ${fmtTime(f.last, 'short')}</div>
          </summary>
          <div class="fbody"><div class="loading"><span class="spinner"></span></div></div>
        </details>`) : h`<div class="empty">Правила ничего не нашли${ctx.range ? ' в выбранном периоде' : ''}.</div>`}
        ${quiet.length ? h`<p class="muted" style="font-size:12.5px;margin-top:16px">Без срабатываний: ${quiet.map((r) => r.title).join(', ')}.</p>` : ''}
      </div>
      <aside>
        <section class="panel">
          <div class="panel-h"><h2>Проверка индикаторов</h2></div>
          <p class="ink2" style="font-size:13px;margin:0 0 10px">IP-адреса, домены, имена файлов, хэши, учётные записи — по одному в строке. Ищутся в тексте сообщений, объектах и именах процессов.</p>
          <textarea class="field" id="ioc" rows="7" placeholder="203.0.113.45&#10;evil-domain.example&#10;payload.sh" style="width:100%">${store('la-ioc') || ''}</textarea>
          <div class="btn-row" style="margin-top:10px"><button class="btn primary" type="button" id="ioc-go">Проверить</button><span class="muted" id="ioc-st" style="font-size:12.5px"></span></div>
          <div id="ioc-res" style="margin-top:12px"></div>
        </section>
      </aside>
    </div>`);

  const loaded = new Set();
  async function loadRule(det) {
    const rule = det.dataset.rule;
    if (loaded.has(rule)) return;
    loaded.add(rule);
    const body = det.querySelector('.fbody');
    const d = await api.get(`/api/cases/${ctx.case.id}/findings/${rule}`, { from: ctx.from, to: ctx.to });
    mount(body, h`
      ${d.groups.length > 1 ? h`<div class="groups">${d.groups.slice(0, 30).map((g) => h`<button type="button" class="chip" data-g="${g.name}" title="${g.name}">${g.name.length > 60 ? `${g.name.slice(0, 59)}…` : g.name} <span class="muted num">${fmtN(g.n)}</span></button>`)}</div>` : ''}
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Время</th><th>Действие</th><th>Объект</th><th>Источник</th></tr></thead><tbody>
        ${d.events.map((e) => h`<tr class="click" data-id="${e.id}"><td class="t">${fmtTime(e.t)}</td><td>${catTag(e.category)}<div class="act">${e.action || ''}</div></td><td class="subj">${e.subject || ''}${e.user ? h`<div class="muted">${e.user}</div>` : ''}</td><td class="ink2" style="font-size:12.5px">${e.source || ''}<div class="muted">${e.process || ''}</div></td></tr>`)}
      </tbody></table></div>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn small" type="button" data-search="rule:${rule}">Открыть в поиске</button>
        ${d.events.length >= 200 ? h`<span class="muted" style="font-size:12px">Показаны последние 200 событий</span>` : ''}
      </div>`);
    $$('tr[data-id]', body).forEach((tr) => tr.addEventListener('click', () => ctx.openEvent(+tr.dataset.id)));
    $$('[data-search]', body).forEach((b) => b.addEventListener('click', () => ctx.search(b.dataset.search)));
    $$('[data-g]', body).forEach((b) => b.addEventListener('click', () => ctx.search(`rule:${rule} "${b.dataset.g.replace(/"/g, '')}"`)));
  }
  $$('details.finding', view).forEach((det) => {
    det.addEventListener('toggle', () => { if (det.open) loadRule(det); });
    if (det.open) {
      loadRule(det);
      det.scrollIntoView({ block: 'start' });
    }
  });

  $('#ioc-go').addEventListener('click', async () => {
    const txt = $('#ioc').value;
    store('la-ioc', txt);
    const iocs = txt.split(/[\n,;]+/).map((s) => s.trim()).filter(Boolean);
    if (!iocs.length) { toast('Добавьте хотя бы один индикатор'); return; }
    $('#ioc-st').textContent = 'Проверяю…';
    const t = performance.now();
    try {
      const res = await api.post(`/api/cases/${ctx.case.id}/ioc`, { iocs, from: ctx.from, to: ctx.to });
      const hits = res.filter((r) => r.n);
      $('#ioc-st').textContent = `${hits.length} из ${res.length} найдено, ${Math.round(performance.now() - t)} мс`;
      mount($('#ioc-res'), h`<table class="tbl"><thead><tr><th>Индикатор</th><th class="r">Событий</th><th>Последнее</th></tr></thead><tbody>
        ${res.map((r) => h`<tr class="${r.n ? 'click' : ''}" data-ioc="${r.n ? r.ioc : ''}"><td class="mono" style="word-break:break-all">${r.n ? raw(sevIcon('high', 12)) : ''} ${r.ioc}</td><td class="r num">${fmtN(r.n)}</td><td class="t">${r.last ? fmtTime(r.last, 'short') : '—'}</td></tr>`)}
      </tbody></table>`);
      $$('#ioc-res tr[data-ioc]').forEach((tr) => tr.dataset.ioc && tr.addEventListener('click', () => ctx.search(`"${tr.dataset.ioc}"`)));
    } catch (e) {
      $('#ioc-st').textContent = '';
      toast(e.message);
    }
  });
}
export { SEV };
