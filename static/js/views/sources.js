// Источники: покрытие по времени, список файлов, дозагрузка, настройки сбора
import { api } from '../api.js';
import { gantt } from '../charts.js';
import { $, cssVar, fmtBytes, fmtN, fmtTime, h, mount, toast } from '../util.js';
import { app } from '../app.js';
import { stepsHtml, stepsSummary } from './progress.js';

const PARSER_RU = {
  macos_unified: 'macOS unified log', macos_launchd: 'macOS launchd', macos_wifi: 'macOS wifi.log', macos_pmset: 'macOS pmset',
  syslog: 'syslog', linux_auditd: 'Linux auditd', linux_journald: 'Linux journald', win_evtx: 'Windows EVTX',
  win_xml: 'Windows XML', win_json: 'Windows JSON', win_csv: 'Windows CSV', jsonl: 'JSON-журнал', text: 'текст со временем',
};

export async function renderSources(view, ctx) {
  const c = ctx.case;
  const files = c.files || [];
  const by = {};
  files.filter((f) => f.events && f.t_min).forEach((f) => {
    const k = f.label || f.path;
    const r = by[k] || (by[k] = { label: k, t_min: f.t_min, t_max: f.t_max, n: 0, files: 0 });
    r.t_min = Math.min(r.t_min, f.t_min);
    r.t_max = Math.max(r.t_max, f.t_max);
    r.n += f.events;
    r.files += 1;
  });
  const rows = Object.values(by).sort((a, b) => b.n - a.n);
  const skipped = files.filter((f) => f.status !== 'ok');
  const notes = c.summary?.notes || [];
  mount(view, h`
    ${notes.length ? h`<div class="note">${notes.join('; ')}</div>` : ''}
    ${c.log && c.log.length ? h`<details class="steps-box panel-steps">
      <summary>Журнал сбора <span class="muted">${stepsSummary(c)}</span></summary>
      <div class="steps-scroll">${stepsHtml(c.log)}</div></details>` : ''}
    <section class="panel">
      <div class="panel-h"><h2>Покрытие по времени</h2><span class="sub">какой период охватывает каждый источник</span></div>
      <div class="chart" id="cov"></div>
    </section>
    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>Файлы</h2><span class="sub">${files.length} ${files.length === 1 ? 'файл' : 'файлов'}${c.summary?.took ? `, разбор занял ${c.summary.took} с` : ''}</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Формат</th><th class="r">Событий</th><th class="r" title="Служебные строки, которые не сохраняются">Шум</th><th>Период</th><th>Состояние</th></tr></thead><tbody>
        ${files.map((f) => h`<tr>
          <td class="mono" style="word-break:break-all">${f.path}${f.upload && f.upload !== f.path ? h`<div class="muted" style="font-family:var(--font)">из ${f.upload.replace(/^\d+_/, '')}</div>` : ''}</td>
          <td class="ink2">${PARSER_RU[f.parser] || f.parser || '—'}</td>
          <td class="r num">${fmtN(f.events)}</td>
          <td class="r num muted">${f.noise ? fmtN(f.noise) : ''}</td>
          <td class="t">${f.t_min ? `${fmtTime(f.t_min, 'short')} — ${fmtTime(f.t_max, 'short')}` : '—'}</td>
          <td>${f.status === 'ok' ? h`<span class="status ready"><i></i>разобран</span>` : h`<span class="status ${f.status === 'error' ? 'error' : ''}"><i></i>${f.status === 'skipped' ? 'пропущен' : f.status}</span>${f.error ? h`<div class="muted" style="font-size:12px">${f.error}</div>` : ''}`}</td>
        </tr>`)}
      </tbody></table></div>
      ${skipped.length ? h`<p class="muted" style="font-size:12.5px">Пропущенные файлы — двоичные или неизвестного формата. Если это журнал, выгрузите его в текст (например, <span class="mono">log show --style ndjson</span> для .logarchive) и добавьте в сбор.</p>` : ''}
    </section>
    <div class="grid g-2" style="margin-top:18px">
      <section class="panel">
        <div class="panel-h"><h2>Добавить файлы в сбор</h2></div>
        <p class="ink2" style="font-size:13px;margin:0 0 12px">Новые события встанут в ту же хронологию, правила пересчитаются.</p>
        <div class="btn-row"><label class="btn" for="add-files">Выбрать файлы</label><input id="add-files" type="file" multiple class="hidden"><span class="muted" id="add-st" style="font-size:12.5px"></span></div>
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Сбор</h2></div>
        <label class="lbl">Название<input class="field" id="c-name" value="${c.name}"></label>
        <label class="lbl" style="margin-top:10px">Заметки<textarea class="field" id="c-notes" rows="3" style="font-family:var(--font);font-size:13px">${c.notes || ''}</textarea></label>
        <div class="btn-row" style="margin-top:12px">
          <button class="btn" type="button" id="c-save">Сохранить</button>
          <button class="btn danger" type="button" id="c-del">Удалить сбор</button>
        </div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Часовой пояс хоста: ${c.tz_label}${c.tz_mode === 'auto' ? ', определён по журналам' : ''}.</p>
      </section>
    </div>`);
  gantt($('#cov'), rows.map((r) => ({ ...r, color: cssVar('--accent') })), {
    onClick: (r) => ctx.search(`source:"${r.label}"`),
  });
  $('#add-files').addEventListener('change', async (e) => {
    const fl = [...e.target.files];
    if (!fl.length) return;
    const fd = new FormData();
    fl.forEach((f) => fd.append('files', f, f.name));
    fd.append('case_id', c.id);
    try {
      await api.upload(fd, (p) => { $('#add-st').textContent = `Передача ${Math.round(p * 100)}%`; });
      await app.loadCases();
      app.reload();
    } catch (err) { toast(err.message); }
  });
  $('#c-save').addEventListener('click', async () => {
    await api.patch(`/api/cases/${c.id}`, { name: $('#c-name').value.trim() || c.name, notes: $('#c-notes').value });
    await app.loadCases();
    toast('Сохранено');
    app.reload();
  });
  $('#c-del').addEventListener('click', async () => {
    const b = $('#c-del');
    if (b.dataset.armed !== '1') {
      b.dataset.armed = '1';
      b.textContent = 'Удалить безвозвратно';
      setTimeout(() => { b.dataset.armed = ''; b.textContent = 'Удалить сбор'; }, 4000);
      return;
    }
    try {
      await api.del(`/api/cases/${c.id}`);
      await app.loadCases();
      window.location.hash = '#/';
      toast('Сбор удалён');
    } catch (err) { toast(err.message); }
  });
}
