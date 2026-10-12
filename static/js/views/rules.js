// Правила Sigma: импорт .yml / .zip, список с фильтрами, включение и выключение, применение к сборам
import { api } from '../api.js';
import { $, $$, esc, fmtN, fmtTime, h, mount, raw, sevLabel, toast } from '../util.js';

const PRODUCTS = [['', 'Все системы'], ['windows', 'Windows'], ['linux', 'Linux'], ['macos', 'macOS'], ['_other', 'Без системы']];
const STATES = [['', 'Все'], ['on', 'Включены'], ['off', 'Выключены'], ['bad', 'Не применяются']];
const LEVELS = [['', 'Любой уровень'], ['critical', 'Критично'], ['high', 'Высокая'], ['medium', 'Средняя'], ['low', 'Низкая'], ['info', 'Инфо']];
const SHOW = 400;

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
        <div class="panel-h"><h2>Импорт правил</h2></div>
        <form class="drop small-drop" id="sg-drop">
          <div>
            <p class="ink2" style="margin:0 0 10px">Файлы правил <span class="mono">.yml</span> или архив <span class="mono">.zip</span> с ними — например, весь репозиторий
              <a href="https://github.com/SigmaHQ/sigma" target="_blank" rel="noopener">SigmaHQ</a> (Code → Download ZIP) или файл <span class="mono">sigma_core.zip</span> из его релизов. Перетащите сюда или выберите:</p>
            <div class="btn-row"><label class="btn primary" for="sg-files">Выбрать файлы</label><input id="sg-files" type="file" multiple accept=".yml,.yaml,.zip" class="hidden">
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
      mount(box, h`<div class="empty">Правил Sigma пока нет — импортируйте файлы .yml или архив .zip.</div>`);
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

  // импорт
  async function doImport(files) {
    if (!files || !files.length) return;
    const st = $('#sg-st');
    st.textContent = 'Передача…';
    try {
      const rep = await uploadFiles(files, (p) => { st.textContent = p < 1 ? `Передача ${Math.round(p * 100)}%` : 'Разбор правил…'; });
      st.textContent = '';
      mount($('#sg-report'), h`<div class="note sg-rep">
        Добавлено: <b>${fmtN(rep.added)}</b>, обновлено: <b>${fmtN(rep.updated)}</b>, не применяются: <b>${fmtN(rep.unsupported)}</b>${rep.skipped ? `, пропущено не-правил: ${fmtN(rep.skipped)}` : ''}${rep.errors.length ? `, ошибок: ${rep.errors.length}` : ''}.
        ${rep.errors.length || rep.problems.length ? h`<details><summary>Подробности</summary>
          ${rep.errors.length ? h`<ul>${rep.errors.slice(0, 50).map((x) => h`<li>${x}</li>`)}</ul>` : ''}
          ${rep.problems.length ? h`<ul>${rep.problems.slice(0, 100).map((p) => h`<li>${p.title} — ${p.reason}</li>`)}</ul>` : ''}</details>` : ''}
        <div style="margin-top:6px">Чтобы проверить уже разобранные сборы, нажмите «Применить ко всем сборам».</div></div>`);
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

  drawCounts();
  drawList();
}

export const RULES_ICON = raw('<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.5 16 5v4.5c0 3.8-2.5 6.6-6 8-3.5-1.4-6-4.2-6-8V5z"/><path d="m7.2 10 2 2 3.8-4"/></svg>');
