// Правила Sigma: загрузка наборов SigmaHQ, импорт .yml / .zip, список с фильтрами, включение и выключение, применение к сборам
import { api } from '../api.js';
import { $, $$, esc, fmtN, fmtTime, h, mount, raw, sevLabel, toast } from '../util.js';

const PRODUCTS = [['', 'Все системы'], ['windows', 'Windows'], ['linux', 'Linux'], ['macos', 'macOS'], ['_other', 'Без системы']];
const STATES = [['', 'Все'], ['on', 'Включены'], ['off', 'Выключены'], ['bad', 'Не применяются']];
const LEVELS = [['', 'Любой уровень'], ['critical', 'Критично'], ['high', 'Высокая'], ['medium', 'Средняя'], ['low', 'Низкая'], ['info', 'Инфо']];
const SHOW = 400;
const ruDate = (d) => (/^\d{4}-\d{2}-\d{2}$/.test(d || '') ? d.split('-').reverse().join('.') : d);

function uploadFiles(files, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    [...files].forEach((f) => fd.append('files', f, f.name));
    const x = new XMLHttpRequest();
    x.open('POST', '/api/sigma');
    x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let d = null;
      try { d = JSON.parse(x.responseText); } catch (e) { /* */ }
      if (x.status >= 200 && x.status < 300) resolve(d);
      else reject(new Error((d && d.error) || `Ошибка ${x.status}`));
    };
    x.onerror = () => reject(new Error('Сеть недоступна'));
    x.send(fd);
  });
}

export async function renderRules(view) {
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  let data = await api.get('/api/sigma');
  const f = { q: '', product: '', state: '', level: '' };
  const sel = new Set();
  let applyTimer = null;

  mount(view, h`
    <div class="grid g-collect">
      <section class="panel">
        <div class="panel-h"><h2>Правила SigmaHQ</h2><span class="sub" id="sg-feed-sub"></span></div>
        ${data.feed_enabled ? h`<p class="ink2" style="font-size:13px;margin:0 0 12px">Готовые наборы из открытого репозитория
          <a href="${data.upstream}" target="_blank" rel="noopener">SigmaHQ</a> на GitHub. Повторная загрузка обновляет набор: новые правила добавятся, изменённые обновятся, убранные из SigmaHQ удалятся. Выключенные правила останутся выключенными, импортированные вами — не тронутся.</p>
        <div class="sg-feed">
          <select class="field" id="sg-pkg" aria-label="Набор правил">${data.packages.map((p) => h`<option value="${p.id}">${p.title}</option>`)}</select>
          <button class="btn primary" type="button" id="sg-upd">Загрузить</button>
        </div>
        <p class="muted" id="sg-pkg-desc" style="font-size:12.5px;margin:6px 0 8px"></p>
        <label class="sg-chk"><input type="checkbox" id="sg-upd-apply" checked> Сразу проверить разобранные сборы новыми правилами</label>
        <div id="sg-upd-st"></div>` : h`<p class="ink2" style="font-size:13px;margin:0 0 12px">Загрузка наборов из SigmaHQ отключена администратором — импортируйте правила файлами ниже.</p>`}
        <form class="drop small-drop" id="sg-drop" style="margin-top:14px">
          <div>
            <p class="ink2" style="margin:0 0 10px">Свои правила <span class="mono">.yml</span> или архив <span class="mono">.zip</span> — например, пакет SigmaHQ, скачанный на другом компьютере, если здесь нет интернета. Перетащите сюда или выберите:</p>
            <div class="btn-row"><label class="btn" for="sg-files">Выбрать файлы</label><input id="sg-files" type="file" multiple accept=".yml,.yaml,.zip" class="hidden">
              <span class="muted" id="sg-st" style="font-size:12.5px"></span></div>
          </div>
        </form>
        <div id="sg-report"></div>
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Проверка сборов</h2></div>
        <p class="ink2" style="font-size:13px;margin:0 0 12px">Новые сборы проверяются включёнными правилами Sigma сразу после разбора. Уже разобранные сборы — кнопкой ниже: находки появятся на вкладке «Находки» с меткой Sigma.</p>
        <div class="btn-row"><button class="btn" type="button" id="sg-apply">Применить ко всем сборам</button><span class="muted" id="sg-apply-st" style="font-size:12.5px"></span></div>
        <p class="muted" style="font-size:12px;margin:14px 0 0">Поля Sigma берутся из событий: EventData Windows (Sysmon, Security, PowerShell…), записи auditd, командные строки и пользователи. Агрегации (<span class="mono">| count()</span>), корреляции и журналы облаков и сетевых устройств не применяются — такие правила помечены с причиной.</p>
      </section>
    </div>
    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>Правила</h2><span class="sub" id="sg-counts"></span></div>
      <div class="sg-tools">
        <input class="field" id="sg-q" placeholder="Название, MITRE, категория журнала…" style="flex:1 1 260px">
        <select class="field" id="sg-prod">${PRODUCTS.map(([v, n]) => h`<option value="${v}">${n}</option>`)}</select>
        <select class="field" id="sg-level">${LEVELS.map(([v, n]) => h`<option value="${v}">${n}</option>`)}</select>
        <select class="field" id="sg-state">${STATES.map(([v, n]) => h`<option value="${v}">${n}</option>`)}</select>
      </div>
      <div class="sg-bulk btn-row">
        <label class="pick-all"><input type="checkbox" id="sg-all"> Выбрать показанные</label>
        <span class="muted" id="sg-n"></span>
        <button class="btn small" type="button" data-bulk="on" disabled>Включить</button>
        <button class="btn small" type="button" data-bulk="off" disabled>Выключить</button>
        <button class="btn small danger" type="button" data-bulk="del" disabled>Удалить</button>
      </div>
      <div id="sg-list"></div>
    </section>`);

  const filtered = () => {
    const q = f.q.trim().toLowerCase();
    return data.rules.filter((r) => {
      if (f.product === '_other' ? ['windows', 'linux', 'macos'].includes(r.product) : f.product && r.product !== f.product) return false;
      if (f.level && r.level !== f.level) return false;
      if (f.state === 'on' && !(r.enabled && !r.error)) return false;
      if (f.state === 'off' && (r.enabled || r.error)) return false;
      if (f.state === 'bad' && !r.error) return false;
      if (q && !`${r.title} ${r.mitre || ''} ${r.category} ${r.service} ${r.product} ${r.id}`.toLowerCase().includes(q)) return false;
      return true;
    });
  };

  const drawCounts = () => {
    const c = data.counts;
    mount($('#sg-counts'), c.total ? `всего ${fmtN(c.total)} · применяются ${fmtN(c.enabled)} · не применяются ${fmtN(c.unsupported)}` : '');
  };

  const drawBulk = () => {
    $('#sg-n').textContent = sel.size ? `выбрано: ${fmtN(sel.size)}` : '';
    $$('[data-bulk]').forEach((b) => { b.disabled = !sel.size; });
  };

  function drawList() {
    const list = filtered();
    const shown = list.slice(0, SHOW);
    const box = $('#sg-list');
    if (!data.rules.length) {
      mount(box, h`<div class="empty">Правил Sigma пока нет — загрузите набор SigmaHQ или импортируйте файлы .yml / .zip.</div>`);
      return;
    }
    if (!list.length) { mount(box, h`<div class="empty">Под фильтр ничего не подходит</div>`); return; }
    mount(box, h`<div class="tbl-wrap"><table class="tbl sg-tbl"><thead><tr><th style="width:28px"></th><th>Правило</th><th>Журнал</th><th>Уровень</th><th>Состояние</th></tr></thead><tbody>
      ${shown.map((r) => h`<tr class="click" data-id="${r.id}">
        <td><input type="checkbox" class="sg-pick" data-id="${r.id}" ${sel.has(r.id) ? 'checked' : ''} aria-label="${`Выбрать «${r.title}»`}"></td>
        <td><div>${r.title}${r.mitre ? h`<span class="mitre">${r.mitre}</span>` : ''}</div><div class="muted sg-src">${r.source || ''}</div></td>
        <td class="t">${[r.product, r.category || r.service].filter(Boolean).join(' / ') || '—'}</td>
        <td>${sevLabel(r.level)}</td>
        <td>${r.error ? h`<span class="sg-bad" title="${r.error}">не применяется: ${r.error}</span>`
          : h`<label class="sg-sw"><input type="checkbox" class="sg-en" data-id="${r.id}" ${r.enabled ? 'checked' : ''}> ${r.enabled ? 'включено' : 'выключено'}</label>`}</td>
      </tr>`)}</tbody></table></div>
      ${list.length > SHOW ? h`<p class="muted" style="font-size:12.5px;margin:10px 0 0">Показаны первые ${SHOW} из ${fmtN(list.length)} — уточните фильтр.</p>` : ''}`);
    $('#sg-all').checked = shown.length > 0 && shown.every((r) => sel.has(r.id));
  }

  async function reload() {
    data = await api.get('/api/sigma');
    const ids = new Set(data.rules.map((r) => r.id));
    [...sel].forEach((id) => { if (!ids.has(id)) sel.delete(id); });
    drawCounts();
    drawBulk();
    drawList();
  }

  // список: выбор, включение, YAML по щелчку
  $('#sg-list').addEventListener('click', async (e) => {
    const pick = e.target.closest('.sg-pick');
    if (pick) {
      e.stopPropagation();
      if (pick.checked) sel.add(pick.dataset.id); else sel.delete(pick.dataset.id);
      drawBulk();
      return;
    }
    const en = e.target.closest('.sg-en');
    if (en) {
      e.stopPropagation();
      try { await api.patch('/api/sigma', { ids: [en.dataset.id], enabled: en.checked }); await reload(); } catch (err) { toast(err.message); }
      return;
    }
    if (e.target.closest('label, a, input')) return;
    const tr = e.target.closest('tr[data-id]');
    if (!tr) return;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('sg-yaml')) { next.remove(); return; }
    const r = await api.get(`/api/sigma/rules/${encodeURIComponent(tr.dataset.id)}`);
    tr.insertAdjacentHTML('afterend', `<tr class="sg-yaml"><td></td><td colspan="4">${r.description ? `<p class="ink2" style="margin:0 0 8px;font-size:13px">${esc(r.description)}</p>` : ''}<pre>${esc(r.yaml)}</pre></td></tr>`);
  });

  $('#sg-all').addEventListener('change', (e) => {
    filtered().slice(0, SHOW).forEach((r) => { if (e.target.checked) sel.add(r.id); else sel.delete(r.id); });
    drawBulk();
    drawList();
  });
  $$('[data-bulk]').forEach((b) => b.addEventListener('click', async () => {
    const ids = [...sel];
    try {
      if (b.dataset.bulk === 'del') {
        if (b.dataset.armed !== '1') {
          b.dataset.armed = '1';
          b.textContent = `Удалить ${fmtN(ids.length)}?`;
          setTimeout(() => { b.dataset.armed = ''; b.textContent = 'Удалить'; }, 4000);
          return;
        }
        b.dataset.armed = '';
        b.textContent = 'Удалить';
        await api.del('/api/sigma', { ids });
        sel.clear();
        toast(`Удалено правил: ${fmtN(ids.length)}`);
      } else {
        await api.patch('/api/sigma', { ids, enabled: b.dataset.bulk === 'on' });
        toast(`${b.dataset.bulk === 'on' ? 'Включено' : 'Выключено'} правил: ${fmtN(ids.length)}`);
      }
      await reload();
    } catch (err) { toast(err.message); }
  }));

  // фильтры
  const bindF = (id, key, ev = 'change') => $(id).addEventListener(ev, (e) => { f[key] = e.target.value; drawList(); });
  bindF('#sg-q', 'q', 'input');
  bindF('#sg-prod', 'product');
  bindF('#sg-level', 'level');
  bindF('#sg-state', 'state');

  const reportHtml = (rep, extra) => h`<div class="note sg-rep">
        ${extra || ''}Добавлено: <b>${fmtN(rep.added)}</b>, обновлено: <b>${fmtN(rep.updated)}</b>${rep.removed ? h`, удалено устаревших: <b>${fmtN(rep.removed)}</b>` : ''}, не применяются: <b>${fmtN(rep.unsupported)}</b>${rep.skipped ? `, пропущено не-правил: ${fmtN(rep.skipped)}` : ''}${rep.errors.length ? `, ошибок: ${rep.errors.length}` : ''}.
        ${rep.errors.length || rep.problems.length ? h`<details><summary>Подробности</summary>
          ${rep.errors.length ? h`<ul>${rep.errors.slice(0, 50).map((x) => h`<li>${x}</li>`)}</ul>` : ''}
          ${rep.problems.length ? h`<ul>${rep.problems.slice(0, 100).map((p) => h`<li>${p.title} — ${p.reason}</li>`)}</ul>` : ''}</details>` : ''}
        ${rep.applying ? h`<div style="margin-top:6px">Разобранные сборы проверяются новыми правилами — ход в блоке «Проверка сборов».</div>`
          : h`<div style="margin-top:6px">Чтобы проверить уже разобранные сборы, нажмите «Применить ко всем сборам».</div>`}</div>`;

  // импорт
  async function doImport(files) {
    if (!files || !files.length) return;
    const st = $('#sg-st');
    st.textContent = 'Передача…';
    try {
      const rep = await uploadFiles(files, (p) => { st.textContent = p < 1 ? `Передача ${Math.round(p * 100)}%` : 'Разбор правил…'; });
      st.textContent = '';
      mount($('#sg-report'), reportHtml(rep));
      await reload();
    } catch (err) {
      st.textContent = '';
      toast(err.message);
    }
  }
  $('#sg-files').addEventListener('change', (e) => doImport(e.target.files));
  const drop = $('#sg-drop');
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => doImport(e.dataTransfer.files));

  // применение к сборам
  const applyBtn = $('#sg-apply');
  const drawApply = (a) => {
    const st = $('#sg-apply-st');
    if (!st) return false;
    if (a.state === 'running') {
      applyBtn.disabled = true;
      mount(st, h`<span class="spinner"></span> ${fmtN(a.done)} из ${fmtN(a.total)}${a.current ? ` — «${a.current}»` : ''}, находок: ${fmtN(a.findings)}`);
      return true;
    }
    applyBtn.disabled = false;
    st.textContent = a.finished ? `Готово: проверено сборов ${fmtN(a.total)}, находок Sigma ${fmtN(a.findings)} (${fmtTime((a.finished + 0) * 1000 + (-new Date().getTimezoneOffset() * 60000), 'short')})` : '';
    return false;
  };
  const pollApply = () => {
    clearInterval(applyTimer);
    applyTimer = setInterval(async () => {
      if (!document.body.contains(applyBtn)) { clearInterval(applyTimer); return; }
      try {
        const a = await api.get('/api/sigma/apply');
        if (!drawApply(a)) { clearInterval(applyTimer); window.dispatchEvent(new Event('la:cases-changed')); }
      } catch (e) { /* повторим */ }
    }, 1000);
  };
  applyBtn.addEventListener('click', async () => {
    try { drawApply(await api.post('/api/sigma/apply', {})); pollApply(); } catch (err) { toast(err.message); }
  });
  if (drawApply(data.apply)) pollApply();
  if (!data.yaml) mount($('#sg-report'), h`<div class="note err">В этой сборке нет пакета PyYAML — импорт правил Sigma недоступен.</div>`);

  // загрузка набора из SigmaHQ
  let updTimer = null;
  const MB = (n) => `${(n / 1048576).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} МБ`;
  const drawFeed = (feed) => {
    const sub = $('#sg-feed-sub');
    if (!sub) return;
    sub.textContent = feed ? `${feed.title}${feed.released ? ` от ${ruDate(feed.released)}` : ''} · ${fmtN(feed.count)} правил` : 'ещё не загружались';
    const btn = $('#sg-upd');
    if (btn && !btn.disabled) btn.textContent = feed && feed.package === $('#sg-pkg').value ? 'Проверить обновления' : 'Загрузить';
  };
  const drawPkg = () => {
    const p = data.packages.find((x) => x.id === $('#sg-pkg').value);
    $('#sg-pkg-desc').textContent = p ? `${p.title}: ${p.desc}.` : '';
    drawFeed(data.feed);
  };
  const drawUpdate = (u, feed) => {
    const st = $('#sg-upd-st');
    const btn = $('#sg-upd');
    if (!st) return false;
    if (u.state === 'running') {
      btn.disabled = true;
      btn.textContent = 'Загрузка…';
      const got = u.received ? ` — ${MB(u.received)}${u.size ? ` из ${MB(u.size)}` : ''}` : '';
      mount(st, h`<div class="sg-upd-run"><span class="spinner"></span> ${u.stage || 'Загрузка…'}${got}</div>`);
      return true;
    }
    btn.disabled = false;
    data.feed = feed !== undefined ? feed : data.feed;
    drawFeed(data.feed);
    if (u.error) mount(st, h`<div class="note err">${u.error}</div>`);
    else if (u.report && u.report.same) mount(st, h`<div class="note sg-rep">У вас уже последняя версия набора «${u.report.title}»${u.report.released ? ` (релиз от ${ruDate(u.report.released)})` : ''} — изменений нет.</div>`);
    else if (u.report) mount(st, reportHtml(u.report, h`Набор «${u.report.title}»${u.report.released ? ` от ${ruDate(u.report.released)}` : ''}${u.report.version ? h` <span class="mono muted">${u.report.version}</span>` : ''}. `));
    else st.textContent = '';
    return false;
  };
  const pollUpdate = () => {
    clearInterval(updTimer);
    updTimer = setInterval(async () => {
      if (!document.body.contains($('#sg-upd'))) { clearInterval(updTimer); return; }
      try {
        const u = await api.get('/api/sigma/update');
        if (!drawUpdate(u, u.feed)) {
          clearInterval(updTimer);
          if (u.report && !u.report.same) {
            await reload();
            if (u.report.applying) { drawApply(await api.get('/api/sigma/apply')); pollApply(); }
          }
        }
      } catch (e) { /* повторим */ }
    }, 800);
  };
  if (data.feed_enabled && $('#sg-pkg')) {
    $('#sg-pkg').value = (data.update.state === 'running' && data.update.package) || (data.feed && data.feed.package) || data.default_package;
    $('#sg-pkg').addEventListener('change', drawPkg);
    drawPkg();
    $('#sg-upd').addEventListener('click', async () => {
      try {
        drawUpdate(await api.post('/api/sigma/update', { package: $('#sg-pkg').value, apply: $('#sg-upd-apply').checked }));
        pollUpdate();
      } catch (err) { toast(err.message); }
    });
    if (drawUpdate(data.update)) pollUpdate();
    if (!data.yaml) $('#sg-upd').disabled = true;
  }

  drawCounts();
  drawList();
}

export const RULES_ICON = raw('<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.5 16 5v4.5c0 3.8-2.5 6.6-6 8-3.5-1.4-6-4.2-6-8V5z"/><path d="m7.2 10 2 2 3.8-4"/></svg>');
