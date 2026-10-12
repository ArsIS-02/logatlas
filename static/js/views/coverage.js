// Покрытие: насколько полно размечены события и насколько закрыта матрица MITRE ATT&CK
import { api } from '../api.js';
import { $, $$, esc, fmtN, fmtTime, h, mount, raw, sevLabel, toast } from '../util.js';

const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
const ruDate = (d) => (/^\d{4}-\d{2}-\d{2}$/.test(d || '') ? d.split('-').reverse().join('.') : d || '');

export async function renderCoverage(view) {
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  let data = await api.get('/api/coverage');
  let updTimer = null;
  let openTactic = null;

  mount(view, h`
    <div class="grid g-collect">
      <section class="panel">
        <div class="panel-h"><h2>Разметка событий</h2><span class="sub" id="cv-ev-sub"></span></div>
        <div id="cv-ev"></div>
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Справочник ATT&CK</h2><span class="sub" id="cv-mi-sub"></span></div>
        <div id="cv-mi"></div>
      </section>
    </div>
    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>Матрица MITRE ATT&CK</h2><span class="sub" id="cv-mx-sub"></span></div>
      <div id="cv-mx"></div>
    </section>
    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>Правила без срабатываний</h2><span class="sub" id="cv-si-sub"></span></div>
      <div id="cv-si"></div>
    </section>`);

  // --- разметка событий
  function drawEvents() {
    const e = data.events;
    const labelled = e.total - e.unlabelled;
    mount($('#cv-ev-sub'), e.total ? `${fmtN(e.total)} событий в ${fmtN(data.cases.length)} сборах` : '');
    if (!e.total) {
      mount($('#cv-ev'), h`<div class="empty">Нет разобранных сборов — загрузите журналы, и здесь будет видно, что LogAtlas не распознал.</div>`);
      return;
    }
    mount($('#cv-ev'), h`
      <div class="cv-bar" title="${`Размечено ${fmtN(labelled)}, без разметки ${fmtN(e.unlabelled)}`}">
        <span class="cv-bar-on" style="width:${pct(labelled, e.total)}%"></span>
      </div>
      <p class="ink2" style="font-size:13px;margin:8px 0 12px">Распознано <b>${pct(labelled, e.total)}%</b> событий (${fmtN(labelled)} из ${fmtN(e.total)}).
        ${e.unlabelled ? h`Остальные <b>${fmtN(e.unlabelled)}</b> попали в поиск, но не получили действия и категории — их не видят встроенные правила.` : 'Все события получили действие.'}
        ${e.noise ? h`<br><span class="muted">Ещё ${fmtN(e.noise)} распознано, но намеренно оставлено без категории как заведомый шум (служебные входы, учётные записи компьютеров).</span>` : ''}</p>
      ${e.groups.length ? h`<div class="tbl-wrap"><table class="tbl cv-tbl"><thead><tr><th>Журнал</th><th>Процесс</th><th class="num">Событий</th><th>Пример</th></tr></thead><tbody>
        ${e.groups.map((g) => h`<tr><td class="t">${g.source || '—'}</td><td class="t mono">${g.process || '—'}</td>
          <td class="num">${fmtN(g.n)}</td><td class="muted cv-sample" title="${g.sample}">${g.sample || ''}</td></tr>`)}
      </tbody></table></div>
      <p class="muted" style="font-size:12px;margin:10px 0 0">Это и есть список на доработку: частый процесс отсюда стоит добавить в разбор или закрыть своим правилом Sigma.</p>` : ''}`);
  }

  // --- справочник ATT&CK и его обновление
  function drawMitre(u) {
    const m = data.mitre;
    const meta = m.meta;
    mount($('#cv-mi-sub'), meta ? `версия ${meta.version}${meta.released ? ` от ${ruDate(meta.released)}` : ''}` : 'нет справочника');
    const running = u && u.state === 'running';
    mount($('#cv-mi'), h`
      <p class="ink2" style="font-size:13px;margin:0 0 12px">${meta
        ? h`Тактик и техник: <b>${fmtN(meta.count)}</b>. Источник: ${meta.source === 'встроенный снимок'
          ? h`встроенный снимок — он всегда под рукой, но обновляется только с новой версией LogAtlas.`
          : h`официальный репозиторий MITRE, загружен ${meta.updated ? fmtTime(meta.updated * 1000, 'short') : ''}.`}`
        : 'Справочник не загружен.'}</p>
      ${meta && meta.can_update ? h`<div class="btn-row">
        <button class="btn ${meta.source === 'встроенный снимок' ? 'primary' : ''}" type="button" id="cv-upd" ${running ? 'disabled' : ''}>
          ${running ? 'Загрузка…' : 'Обновить справочник'}</button>
        <span class="muted" id="cv-upd-st" style="font-size:12.5px"></span></div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Берётся из <a href="https://github.com/mitre-attack/attack-stix-data" target="_blank" rel="noopener">mitre-attack/attack-stix-data</a> на GitHub: по сети около 6 МБ.</p>`
        : h`<p class="muted" style="font-size:12px;margin:0">Обновление из сети отключено (LOGATLAS_MITRE_UPDATE=0).</p>`}
      <div id="cv-upd-res"></div>`);
    const st = $('#cv-upd-st');
    if (running && st) {
      const mb = (n) => `${(n / 1048576).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} МБ`;
      mount(st, h`<span class="spinner"></span> ${u.stage || ''}${u.received ? ` — ${mb(u.received)}` : ''}`);
    } else if (u && u.error) {
      mount($('#cv-upd-res'), h`<div class="note err">${u.error}</div>`);
    } else if (u && u.report) {
      const r = u.report;
      mount($('#cv-upd-res'), h`<div class="note">${r.same
        ? h`Справочник уже последней версии — ATT&CK ${r.version}${r.released ? ` от ${ruDate(r.released)}` : ''}.`
        : h`Загружен ATT&CK <b>${r.version}</b>${r.released ? ` от ${ruDate(r.released)}` : ''}${r.was ? h` (было ${r.was})` : ''}: тактик ${fmtN(r.tactics)}, техник ${fmtN(r.techniques)}.`}</div>`);
    }
    if ($('#cv-upd')) {
      $('#cv-upd').addEventListener('click', async () => {
        try { drawMitre(await api.post('/api/mitre/update', {})); pollUpdate(); } catch (err) { toast(err.message); }
      });
    }
  }

  const pollUpdate = () => {
    clearInterval(updTimer);
    updTimer = setInterval(async () => {
      if (!document.body.contains($('#cv-mi'))) { clearInterval(updTimer); return; }
      try {
        const u = await api.get('/api/mitre/update');
        if (u.state !== 'running') {
          clearInterval(updTimer);
          if (u.report && !u.report.same) { data = await api.get('/api/coverage'); drawAll(u); return; }
        }
        drawMitre(u);
      } catch (e) { /* повторим */ }
    }, 800);
  };

  // --- матрица
  function drawMatrix() {
    const m = data.mitre;
    mount($('#cv-mx-sub'), `${fmtN(m.parents_covered)} из ${fmtN(m.parents_total)} техник, с подтехниками ${fmtN(m.techniques_covered)} из ${fmtN(m.techniques_total)}`);
    const r = data.rules;
    mount($('#cv-mx'), h`
      <p class="ink2" style="font-size:13px;margin:0 0 14px">Считается по включённым правилам: встроенных ${fmtN(r.builtin)}, Sigma ${fmtN(r.sigma)}.
        Тёмная часть столбца — техники, по которым уже были находки, светлая — закрытые правилами, но ни разу не сработавшие.</p>
      ${m.stale.length ? h`<div class="note warn cv-stale">
        <b>Правила ссылаются на отозванные техники ATT&CK.</b> Такое бывает, когда MITRE переделывает тактику: код остаётся в правиле, а в матрице его уже нет. Покрытие посчитано по заменам, но коды стоит поправить.
        <ul>${m.stale.map((s) => h`<li><span class="mono">${s.code}</span> ${s.name} → <span class="mono">${s.replaced_by || '—'}</span> ${s.replaced_name}
          <span class="muted">(${s.rules.map((x) => x.title).join(', ')})</span></li>`)}</ul></div>` : ''}
      ${m.unknown_codes.length ? h`<div class="note warn">Коды, которых нет в справочнике: ${m.unknown_codes.map((c) => h`<span class="mono">${c}</span> `)}</div>` : ''}
      <div class="cv-mx">${m.tactics.map((t) => h`
        <button class="cv-tac ${t.covered ? '' : 'none'} ${openTactic === t.id ? 'on' : ''}" type="button" data-tac="${t.id}"
                title="${`${t.en}: закрыто ${t.covered} из ${t.total}`}">
          <span class="cv-tac-n">${t.name}</span>
          <span class="cv-col"><span class="cv-col-cov" style="height:${Math.min(100, pct(t.covered, t.total) * 3)}%"></span>
            <span class="cv-col-fire" style="height:${Math.min(100, pct(t.fired, t.total) * 3)}%"></span></span>
          <span class="cv-tac-v">${t.covered}<small>/${t.total}</small></span>
        </button>`)}</div>
      <div id="cv-tac-det"></div>`);
    drawTactic();
  }

  function drawTactic() {
    const box = $('#cv-tac-det');
    if (!box) return;
    const t = data.mitre.tactics.find((x) => x.id === openTactic);
    if (!t) { mount(box, h`<p class="muted" style="font-size:12.5px;margin:12px 0 0">Щёлкните по тактике, чтобы увидеть закрытые техники.</p>`); return; }
    mount(box, h`<div class="cv-det">
      <h3>${t.name} <span class="muted">${t.en} · закрыто ${t.covered} из ${t.total}</span></h3>
      ${t.techniques.length ? h`<div class="tbl-wrap"><table class="tbl cv-tbl"><thead><tr><th>Техника</th><th>Правила</th><th class="num">Находок</th></tr></thead><tbody>
        ${t.techniques.map((x) => h`<tr><td><a href="${x.url}" target="_blank" rel="noopener"><span class="mono">${x.id}</span></a> ${x.name}</td>
          <td class="muted">${x.titles.join('; ')}${x.rules > x.titles.length ? ` и ещё ${x.rules - x.titles.length}` : ''}</td>
          <td class="num">${x.fired ? fmtN(x.fired) : '—'}</td></tr>`)}
      </tbody></table></div>` : h`<div class="empty">По этой тактике нет ни одного включённого правила. Загрузите набор SigmaHQ пошире или напишите своё правило.</div>`}</div>`);
  }

  // --- молчащие правила
  function drawSilent() {
    const r = data.rules;
    mount($('#cv-si-sub'), `${fmtN(r.silent_total)} из ${fmtN(r.total)}`);
    if (!r.silent_total) { mount($('#cv-si'), h`<div class="empty">Все включённые правила хотя бы раз срабатывали.</div>`); return; }
    mount($('#cv-si'), h`
      <p class="ink2" style="font-size:13px;margin:0 0 12px">Эти правила включены, но не дали ни одной находки ни на одном сборе. Само по себе это не плохо — возможно, таких событий просто не было. Но если правило молчит на всех сборах подряд, стоит проверить, попадают ли в журналы нужные события.</p>
      <div class="tbl-wrap"><table class="tbl cv-tbl"><thead><tr><th>Правило</th><th>Откуда</th><th>Уровень</th><th>ATT&CK</th></tr></thead><tbody>
        ${r.silent.map((x) => h`<tr><td>${x.title}</td><td class="t">${x.kind === 'sigma' ? 'Sigma' : 'встроенное'}</td>
          <td>${sevLabel(x.severity)}</td><td class="mono muted">${x.mitre || '—'}</td></tr>`)}
      </tbody></table></div>
      ${r.silent_total > r.silent.length ? h`<p class="muted" style="font-size:12.5px;margin:10px 0 0">Показаны первые ${fmtN(r.silent.length)} из ${fmtN(r.silent_total)}.</p>` : ''}`);
  }

  function drawAll(u) {
    drawEvents();
    drawMitre(u || data.mitre.update);
    drawMatrix();
    drawSilent();
  }

  view.addEventListener('click', (e) => {
    const b = e.target.closest('[data-tac]');
    if (!b) return;
    openTactic = openTactic === b.dataset.tac ? null : b.dataset.tac;
    $$('.cv-tac').forEach((x) => x.classList.toggle('on', x.dataset.tac === openTactic));
    drawTactic();
  });

  drawAll();
  if (data.mitre.update && data.mitre.update.state === 'running') pollUpdate();
}

export const COVERAGE_ICON = raw('<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 16.5V9m4.3 7.5V4.5M11.7 16.5v-5m4.3 5V7"/><path d="M2.5 18.5h15"/></svg>');
