// Сравнение сборов: что нового в этом сборе по сравнению с эталоном хоста или другим сбором,
// и панель «Эталон хоста» для вкладки «Обзор»
import { api } from '../api.js';
import { $, $$, fmtN, fmtTime, h, mount, toast } from '../util.js';

const SECTIONS = [
  ['new', 'Новое', 'есть в этом сборе, нет в сравниваемом'],
  ['more', 'Заметно чаще', 'в 10 и более раз чаще в сутки, чем в сравниваемом'],
  ['gone', 'Пропало', 'было в сравниваемом сборе, в этом нет'],
];

function sgn(n, s) { return n ? `${s}${fmtN(n)}` : '0'; }

function quote(v) { return `"${String(v).replace(/"/g, '')}"`; }

function queryFor(kind, name) {
  if (kind === 'processes') return `process:${quote(name)}`;
  if (kind === 'users') return `user:${quote(name)}`;
  return `subject:${quote(name)}`;
}

function caseLabel(c) {
  const d = c.t_min ? `${fmtTime(c.t_min, 'date')} — ${fmtTime(c.t_max, 'date')}` : '';
  return `${c.is_baseline ? '★ ' : ''}${c.name}${c.host ? ` · ${c.host}` : ''}${d ? ` · ${d}` : ''}`;
}

function newCount(diff) {
  return Object.values(diff || {}).reduce((a, k) => a + k.new + k.more, 0);
}

async function setBaseline(caseId, on) {
  if (on) await api.put(`/api/cases/${caseId}/baseline`, {});
  else await api.del(`/api/cases/${caseId}/baseline`);
}

// ---------------------------------------------------------------------------
// панель на вкладке «Обзор»
export async function baselinePanel(el, ctx) {
  if (!el) return;
  const c = ctx.case;
  let st;
  try {
    st = await api.get(`/api/cases/${c.id}/baseline`);
  } catch (e) {
    return;
  }
  if (!st.host) return;
  const cmpHref = (a, kind) => ctx.href('compare', { extra: { a }, sub: kind || undefined });
  let body;
  if (st.is_baseline) {
    body = h`<div class="panel-h"><h2>Эталон хоста ${st.host}</h2>
        <div class="tools"><button class="btn small ghost" type="button" data-bl="off">Снять отметку</button></div></div>
      <p class="muted bl-p">Этот сбор отмечен как «чистый». Следующие сборы хоста ${st.host} на вкладке «Обзор» сравниваются с ним: новые процессы, входы, службы, адреса и устройства.</p>`;
  } else if (st.baseline) {
    const b = st.baseline;
    const n = newCount(st.diff);
    const kinds = Object.entries(st.diff || {}).filter(([, v]) => v.new || v.more);
    body = h`<div class="panel-h"><h2>${n ? 'Отклонения от эталона' : 'По сравнению с эталоном нового нет'}</h2>
        <div class="tools"><a class="btn small" href="${cmpHref(b.id)}">Подробно</a>
          <button class="btn small ghost" type="button" data-bl="on" title="Сравнивать следующие сборы хоста с этим">Сделать эталоном этот сбор</button></div></div>
      <p class="muted bl-p">Эталон хоста ${st.host}: «${b.name}»${b.t_min ? `, ${fmtTime(b.t_min, 'date')} — ${fmtTime(b.t_max, 'date')}` : ''}.</p>
      ${kinds.length ? h`<div class="bl-kinds">${kinds.map(([k, v]) => h`<a class="bl-kind" href="${cmpHref(b.id, k)}">
        <span class="t">${v.title}</span><span class="v num">+${fmtN(v.new)}</span>${v.more ? h`<span class="m">чаще: ${fmtN(v.more)}</span>` : ''}</a>`)}</div>` : ''}`;
  } else {
    body = h`<div class="panel-h"><h2>Эталона для хоста ${st.host} нет</h2>
        <div class="tools"><button class="btn small" type="button" data-bl="on">Сделать этот сбор эталоном</button>
          <a class="btn small ghost" href="${ctx.href('compare')}">Сравнить с другим сбором</a></div></div>
      <p class="muted bl-p">Отметьте «чистый» сбор хоста как эталон — следующие сборы будут сравниваться с ним автоматически, и здесь появится всё новое: процессы, входы, службы, адреса, устройства.</p>`;
  }
  el.hidden = false;
  mount(el, body);
  $$('[data-bl]', el).forEach((btn) => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await setBaseline(c.id, btn.dataset.bl === 'on');
      toast(btn.dataset.bl === 'on' ? `Сбор стал эталоном хоста ${st.host}` : 'Отметка эталона снята');
      await baselinePanel(el, ctx);
    } catch (e) {
      btn.disabled = false;
      toast(e.message);
    }
  }));
}

// ---------------------------------------------------------------------------
// вкладка «Сравнение»
export async function renderCompare(view, ctx) {
  const c = ctx.case;
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  const [cands, st] = await Promise.all([
    api.get(`/api/cases/${c.id}/compare-candidates`),
    api.get(`/api/cases/${c.id}/baseline`, { diff: 0 }),
  ]);
  if (!cands.length) {
    mount(view, h`<div class="empty">Для сравнения нужен ещё хотя бы один разобранный сбор — например, «чистый» сбор этого же хоста за прошлую неделю.</div>`);
    return;
  }
  const want = ctx.params.get('a');
  let a = (want && cands.find((x) => x.id === want)) ? want
    : (st.baseline && !st.is_baseline ? st.baseline.id : (cands.find((x) => x.same_host) || cands[0]).id);
  const groups = [
    ['Эталон и сборы этого хоста', cands.filter((x) => x.is_baseline || x.same_host)],
    ['Другие сборы', cands.filter((x) => !x.is_baseline && !x.same_host)],
  ].filter(([, l]) => l.length);

  mount(view, h`
    <section class="panel cmp-head">
      <div class="cmp-pick">
        <label class="lbl">Сравнить этот сбор с
          <select class="field" id="cmp-a">${groups.map(([g, l]) => h`<optgroup label="${g}">${l.map((x) => h`<option value="${x.id}" ${x.id === a ? 'selected' : ''}>${caseLabel(x)}</option>`)}</optgroup>`)}</select>
        </label>
        <div class="btn-row" id="cmp-bl"></div>
      </div>
      <p class="muted cmp-note">Сравниваются процессы, запуски программ, пользователи, удалённые входы, установленное ПО, устройства, сетевые адреса и механизмы закрепления. Номера, GUID и регистр в именах не учитываются. Щелчок по строке открывает события в поиске.</p>
    </section>
    <div id="cmp-body"><div class="loading"><span class="spinner"></span></div></div>`);

  const blBox = $('#cmp-bl');
  const drawBl = () => {
    mount(blBox, st.host ? (st.is_baseline
      ? h`<span class="chip">★ эталон хоста ${st.host}</span><button class="btn small ghost" type="button" data-bl="off">Снять отметку</button>`
      : h`<button class="btn small ghost" type="button" data-bl="on">Сделать этот сбор эталоном хоста ${st.host}</button>`) : '');
    $$('[data-bl]', blBox).forEach((b) => b.addEventListener('click', async () => {
      try {
        await setBaseline(c.id, b.dataset.bl === 'on');
        Object.assign(st, await api.get(`/api/cases/${c.id}/baseline`, { diff: 0 }));
        drawBl();
        toast(st.is_baseline ? `Сбор стал эталоном хоста ${st.host}` : 'Отметка эталона снята');
      } catch (e) { toast(e.message); }
    }));
  };
  drawBl();

  async function load() {
    const body = $('#cmp-body');
    mount(body, h`<div class="loading"><span class="spinner"></span><p class="loading-t">Сравниваю сборы…</p></div>`);
    let d;
    try {
      d = await api.get('/api/compare', { a, b: c.id });
    } catch (e) {
      mount(body, h`<div class="note err">Не удалось сравнить: ${e.message}</div>`);
      return;
    }
    const A = d.a;
    const total = (key) => d.order.reduce((s, k) => s + d.kinds[k][`${key}_total`], 0);
    mount(body, h`
      <section class="panel">
        <div class="panel-h"><h2>Итог</h2><span class="sub">«${A.name}» (${fmtTime(A.t_min, 'date')} — ${fmtTime(A.t_max, 'date')}, ${d.days_a} сут.) → этот сбор (${d.days_b} сут.)</span></div>
        <div class="cmp-sum">
          <div><b class="num">${sgn(total('new'), '+')}</b><span>новых объектов</span></div>
          <div><b class="num">${fmtN(total('more'))}</b><span>заметно чаще</span></div>
          <div><b class="num">${sgn(total('gone'), '−')}</b><span>пропало</span></div>
        </div>
        <div class="cmp-kinds">${d.order.map((k) => {
          const v = d.kinds[k];
          return h`<a class="cmp-kind ${v.new_total + v.more_total ? 'hot' : ''}" href="#cmp-${k}" data-jump="${k}">
            <span class="t">${v.title}</span>
            <span class="row"><span class="n num">${sgn(v.new_total, '+')}</span>${v.more_total ? h`<span class="m num">↑${fmtN(v.more_total)}</span>` : ''}<span class="g num">${sgn(v.gone_total, '−')}</span></span>
            <span class="c muted">общих: ${fmtN(v.common)}</span></a>`;
        })}</div>
      </section>
      ${d.order.filter((k) => d.kinds[k].new_total + d.kinds[k].more_total + d.kinds[k].gone_total).map((k) => {
        const v = d.kinds[k];
        return h`<section class="panel cmp-sec" id="cmp-${k}">
          <div class="panel-h"><h2>${v.title}</h2><span class="sub">в этом сборе ${fmtN(v.n_b)}, в сравниваемом ${fmtN(v.n_a)}, общих ${fmtN(v.common)}</span></div>
          ${SECTIONS.filter(([s]) => v[s].length).map(([s, title, hint]) => h`
            <h3 class="cmp-h ${s}">${title} <span class="num">${fmtN(v[`${s}_total`])}</span> <span class="muted">— ${hint}${v[`${s}_total`] > v[s].length ? `, показаны первые ${v[s].length}` : ''}</span></h3>
            <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Объект</th><th>Действия</th><th class="r">${s === 'more' ? 'Было → стало' : 'Событий'}</th><th>Первое</th><th>Последнее</th></tr></thead><tbody>
            ${v[s].map((it) => h`<tr class="click" data-kind="${k}" data-sec="${s}" data-name="${it.name}">
              <td style="max-width:440px;word-break:break-all">${it.name}</td>
              <td><div class="acts">${Object.entries(it.actions).map(([act, n]) => h`<span>${act}${n > 1 ? ` ×${fmtN(n)}` : ''}</span>`)}</div></td>
              <td class="r num">${s === 'more' ? h`${fmtN(it.n_a)} → ${fmtN(it.n)} <span class="muted">×${it.ratio}</span>` : fmtN(it.n)}</td>
              <td class="t">${fmtTime(it.first, 'short')}</td><td class="t">${fmtTime(it.last, 'short')}</td></tr>`)}
            </tbody></table></div>`)}
        </section>`;
      })}
      ${total('new') + total('more') + total('gone') === 0 ? h`<div class="empty">Наборы объектов совпадают — нового, пропавшего и заметно участившегося нет.</div>` : ''}`);

    $$('tr[data-name]', body).forEach((tr) => tr.addEventListener('click', () => {
      const q = queryFor(tr.dataset.kind, tr.dataset.name);
      if (tr.dataset.sec === 'gone') window.location.hash = `#/c/${A.id}/search?q=${encodeURIComponent(q)}`;
      else ctx.search(q, { range: null });
    }));
    $$('[data-jump]', body).forEach((el) => el.addEventListener('click', (e) => {
      const sec = $(`#cmp-${el.dataset.jump}`);
      e.preventDefault();
      if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }));
    if (ctx.sub) {
      const sec = $(`#cmp-${ctx.sub}`);
      if (sec) setTimeout(() => sec.scrollIntoView({ block: 'start' }), 50);
    }
  }

  $('#cmp-a').addEventListener('change', (e) => {
    a = e.target.value;
    window.history.replaceState(null, '', ctx.href('compare', { extra: { a } }));
    load();
  });
  await load();
}
