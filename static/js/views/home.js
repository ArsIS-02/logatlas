// Главная: загрузка журналов, форматы по платформам, список сборов, импорт из папки inbox
import { api } from '../api.js';
import { $, $$, fmtBytes, fmtN, fmtTime, h, mount, platIcon, platName, raw, toast } from '../util.js';

const HOWTO = {
  macos: {
    title: 'macOS',
    cmd: `log show --last 3d --style ndjson > unified.ndjson
pmset -g log > pmset.log
sudo tar -czf maclogs.tar.gz -C /var/log system.log* install.log \\
  wifi.log* com.apple.xpc.launchd`,
  },
  windows: {
    title: 'Windows',
    cmd: `wevtutil epl Security C:\\logs\\Security.evtx
wevtutil epl System C:\\logs\\System.evtx
wevtutil epl "Microsoft-Windows-Sysmon/Operational" C:\\logs\\Sysmon.evtx
Compress-Archive C:\\logs\\*.evtx winlogs.zip`,
  },
  linux: {
    title: 'Linux (auditd)',
    cmd: `sudo tar -czf linuxlogs.tar.gz /var/log/audit /var/log/secure* \\
  /var/log/messages*
journalctl --since "3 days ago" -o json > journal.json`,
  },
};

function tzOptions() {
  const off = -new Date().getTimezoneOffset();
  const opts = [];
  for (let m = -12 * 60; m <= 14 * 60; m += 30) {
    const s = m >= 0 ? '+' : '-';
    const a = Math.abs(m);
    opts.push(`<option value="${m}" ${m === off ? 'selected' : ''}>UTC${s}${String(Math.floor(a / 60)).padStart(2, '0')}:${String(a % 60).padStart(2, '0')}</option>`);
  }
  return opts.join('');
}

export async function renderHome(view, { platform, cases, meta, refresh }) {
  const list = cases.filter((c) => platform === 'all' || c.platform === platform);
  const plats = platform === 'all' ? ['macos', 'windows', 'linux'] : [platform];
  const sup = meta?.supported || {};
  mount(view, h`
    <form class="drop" id="drop">
      <div class="glyph"><svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true"><path d="M16 21V7m0 0-5.5 5.5M16 7l5.5 5.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/><path d="M6 20v4.5A1.5 1.5 0 0 0 7.5 26h17a1.5 1.5 0 0 0 1.5-1.5V20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></div>
      <div>
        <h2>Перетащите журналы или архив сюда</h2>
        <p>Подойдут отдельные файлы и архивы tar, tar.gz, zip, gz, bz2, xz — вложенные архивы тоже распакуются. Формат каждого файла определяется автоматически, файлы обрабатываются здесь, на этом сервере.</p>
        <div class="up-opts">
          <label class="btn primary" for="files">Выбрать файлы</label>
          <input id="files" type="file" multiple class="hidden">
          <label class="btn" for="folder">Выбрать папку</label>
          <a class="btn" href="#/hosts">Собрать с хоста…</a>
          ${meta?.local_collect ? h`<button class="btn" type="button" id="local-collect" title="Выгрузить журналы этого компьютера и сразу разобрать их (нужны права администратора)">${meta.local_collect === 'macos' ? 'Этот Mac' : 'Этот компьютер'}${meta.host ? ` (${meta.host})` : ''}…</button>` : ''}
          <input id="folder" type="file" webkitdirectory multiple class="hidden">
          <label class="lbl">Название сбора<input class="field" id="cname" placeholder="например, mbp-design-07, 9 октября" style="width:260px"></label>
          <label class="lbl">Часовой пояс хоста
            <select class="field" id="tzmode"><option value="auto">Определить по журналам</option><option value="fixed">Указать вручную</option></select></label>
          <label class="lbl hidden" id="tzwrap">Пояс<select class="field" id="tz">${raw(tzOptions())}</select></label>
        </div>
        <div id="local-box" class="local-box hidden"></div>
        <div id="picked" class="hidden" style="margin-top:14px"></div>
      </div>
    </form>

    <div class="formats">
      ${plats.map((p) => h`<div class="panel">
        <h3>${raw(platIcon(p))}${HOWTO[p].title}</h3>
        <ul>${(sup[p] || []).map((x) => h`<li>${x}</li>`)}</ul>
        <div class="cmd">${HOWTO[p].cmd}</div>
      </div>`)}
    </div>

    <h2 class="section-h">${platform === 'all' ? 'Все сборы' : `Сборы: ${platName(platform)}`}</h2>
    ${list.length ? h`<div class="case-tools">
      <label class="pick-all"><input type="checkbox" id="pick-all"> Выбрать все</label>
      <span class="muted" id="pick-n"></span>
      <button class="btn danger" id="pick-del" type="button" disabled>Удалить выбранные</button>
    </div>` : ''}
    <div class="case-rows" id="case-rows">
      ${list.length ? list.map((c) => h`<a class="case-row" href="#/c/${c.id}/overview" data-id="${c.id}">
        <input type="checkbox" class="pick" data-id="${c.id}" aria-label="${`Выбрать сбор «${c.name}»`}">
        <span class="ink2">${raw(platIcon(c.platform))}</span>
        <span title="${c.name}"><span class="nm">${c.name}</span><span class="sm">${c.host || platName(c.platform)}</span></span>
        <span class="sm hide-m num">${c.events ? `${fmtN(c.events)} событий` : ''}</span>
        <span class="status ${c.status}"><i></i>${{ ready: 'Готово', processing: 'Обработка', queued: 'В очереди', error: 'Ошибка', collecting: 'Идёт сбор' }[c.status] || c.status}</span>
        <span class="sm hide-m">${c.t_min ? `${fmtTime(c.t_min, 'date')} — ${fmtTime(c.t_max, 'date')}` : ''}</span>
        <span class="sm">${fmtTime(c.created * 1000 + (-new Date().getTimezoneOffset() * 60000), 'short')}</span>
      </a>`) : h`<div class="empty">Здесь появятся загруженные и собранные с хостов журналы.</div>`}
    </div>

    <h2 class="section-h">Большие архивы</h2>
    <div class="panel" id="inbox"><div class="loading"><span class="spinner"></span></div></div>
  `);

  // Выбор и удаление сборов из списка
  if (list.length) {
    const sel = new Set();
    const del = $('#pick-del');
    const disarm = () => { clearTimeout(del._t); del.dataset.armed = ''; del.textContent = 'Удалить выбранные'; };
    const update = () => {
      const n = sel.size;
      $('#pick-n').textContent = n ? `выбрано: ${n}` : '';
      del.disabled = !n;
      disarm();
      const all = $('#pick-all');
      all.checked = n > 0 && n === list.length;
      all.indeterminate = n > 0 && n < list.length;
      $$('#case-rows .case-row').forEach((r) => r.classList.toggle('picked', sel.has(r.dataset.id)));
    };
    $('#case-rows').addEventListener('click', (e) => {
      const cb = e.target.closest('input.pick');
      if (!cb) return;
      e.stopPropagation();
      if (cb.checked) sel.add(cb.dataset.id); else sel.delete(cb.dataset.id);
      update();
    });
    $('#pick-all').addEventListener('change', (e) => {
      sel.clear();
      $$('#case-rows input.pick').forEach((cb) => { cb.checked = e.target.checked; if (cb.checked) sel.add(cb.dataset.id); });
      update();
    });
    del.addEventListener('click', async () => {
      if (del.dataset.armed !== '1') {
        del.dataset.armed = '1';
        del.textContent = `Удалить безвозвратно: ${sel.size}`;
        del._t = setTimeout(disarm, 4000);
        return;
      }
      clearTimeout(del._t);
      del.disabled = true;
      del.textContent = 'Удаление…';
      let ok = 0;
      const errs = [];
      for (const id of sel) {
        try { await api.del(`/api/cases/${id}`); ok += 1; } catch (err) { errs.push(err.message); }
      }
      toast(`Удалено сборов: ${ok}${errs.length ? `; не удалось: ${errs.length} — ${errs[0]}` : ''}`);
      await refresh();
    });
  }

  let picked = [];
  const pickedEl = $('#picked');
  const drop = $('#drop');
  const showPicked = () => {
    if (!picked.length) { pickedEl.classList.add('hidden'); return; }
    const total = picked.reduce((a, f) => a + f.size, 0);
    pickedEl.classList.remove('hidden');
    mount(pickedEl, h`<div class="btn-row">
        <span><b>${picked.length}</b> ${picked.length === 1 ? 'файл' : 'файлов'}, ${fmtBytes(total)}</span>
        <span class="muted" style="font-size:12px">${picked.slice(0, 4).map((f) => f.webkitRelativePath || f.name).join(', ')}${picked.length > 4 ? '…' : ''}</span>
      </div>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn primary" id="go" type="button">Загрузить и разобрать</button>
        <button class="btn ghost" id="clear" type="button">Очистить</button>
        <span id="upst" class="muted" style="font-size:12.5px"></span>
      </div>`);
    $('#clear').addEventListener('click', () => { picked = []; showPicked(); });
    $('#go').addEventListener('click', upload);
  };
  const addFiles = (fl) => {
    picked = picked.concat([...fl].filter((f) => !/(^|\/)\.(DS_Store|_)/.test(f.webkitRelativePath || f.name)));
    showPicked();
  };
  $('#files').addEventListener('change', (e) => addFiles(e.target.files));
  $('#folder').addEventListener('change', (e) => addFiles(e.target.files));
  $('#tzmode').addEventListener('change', (e) => $('#tzwrap').classList.toggle('hidden', e.target.value === 'auto'));
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files); });

  async function upload() {
    const fd = new FormData();
    picked.forEach((f) => fd.append('files', f, (f.webkitRelativePath || f.name).replace(/\//g, '__')));
    fd.append('name', $('#cname').value.trim());
    fd.append('tz_mode', $('#tzmode').value);
    fd.append('tz', $('#tz').value);
    if (platform !== 'all') fd.append('platform', platform);
    const btn = $('#go');
    btn.disabled = true;
    const st = $('#upst');
    try {
      const res = await api.upload(fd, (p) => { st.textContent = `Передача ${Math.round(p * 100)}%`; });
      window.location.hash = `#/c/${res.id}/overview`;
    } catch (e) {
      st.textContent = '';
      btn.disabled = false;
      toast(e.message);
    }
  }

  // Журналы этого компьютера (LogAtlas для Windows: окно сборщика; для macOS: период и пароль администратора)
  const lc = $('#local-collect');
  const box = $('#local-box');
  // сбор сразу появляется в списке сборов: открываем его страницу — там ход сбора, и он не теряется
  const startLocal = async (body) => {
    lc.disabled = true;
    let job;
    try {
      job = await api.post('/api/local/collect', body);
    } catch (e) {
      lc.disabled = false;
      toast(e.message);
      return;
    }
    if (meta.local_collect !== 'macos') toast('Открыто окно сборщика: выберите журналы и период и нажмите «Собрать». Архив разберётся сам.');
    window.dispatchEvent(new Event('la:cases-changed'));
    if (job.case) window.location.hash = `#/c/${job.case}/overview`;
  };
  if (lc) {
    lc.addEventListener('click', () => {
      if (meta.local_collect !== 'macos') { startLocal({}); return; }
      box.classList.toggle('hidden');
      mount(box, h`<label class="lbl">Период<select class="field" id="local-days">
          <option value="1">последние сутки</option><option value="3" selected>3 дня</option><option value="7">неделя</option>
          <option value="14">2 недели</option><option value="30">30 дней</option></select></label>
        <button class="btn primary" type="button" id="local-go">Собрать и разобрать</button>
        <span class="muted local-hint">Unified log без самых шумных подсистем, питание и установки. macOS спросит пароль администратора — LogAtlas его не видит.</span>`);
      $('#local-go').addEventListener('click', () => startLocal({ days: +$('#local-days').value }));
    });
  }

  // inbox
  try {
    const ib = await api.get('/api/inbox');
    const el = $('#inbox');
    mount(el, h`<div class="panel-h"><h2>Папка inbox на сервере</h2><span class="sub">${ib.dir}</span></div>
      <p class="ink2" style="margin:0 0 12px;font-size:13px">Архивы в несколько гигабайт удобнее скопировать на сервер (например, <span class="mono">scp</span> в папку <span class="mono">data/inbox</span> рядом с docker-compose.yml) и импортировать отсюда — без передачи через браузер.</p>
      ${ib.items.length ? h`<div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th>Файл</th><th class="r">Размер</th></tr></thead><tbody>
        ${ib.items.map((it, i) => h`<tr><td style="width:28px"><input type="checkbox" data-i="${i}" aria-label="Выбрать ${it.path}"></td><td class="mono">${it.path}</td><td class="r num">${fmtBytes(it.size)}</td></tr>`)}
      </tbody></table></div>
      <div class="btn-row" style="margin-top:12px"><button class="btn primary" id="ib-go" type="button">Импортировать выбранное</button></div>`
      : h`<div class="muted" style="font-size:13px">Папка пуста.</div>`}`);
    const go = $('#ib-go');
    if (go) {
      go.addEventListener('click', async () => {
        const files = $$('#inbox input[type=checkbox]:checked').map((c) => ib.items[+c.dataset.i].path);
        if (!files.length) { toast('Отметьте файлы для импорта'); return; }
        try {
          const res = await api.post('/api/inbox/import', {
            files, name: $('#cname').value.trim(), tz_mode: $('#tzmode').value, tz: +$('#tz').value,
          });
          window.location.hash = `#/c/${res.id}/overview`;
        } catch (e) { toast(e.message); }
      });
    }
  } catch (e) {
    mount($('#inbox'), h`<div class="muted">Папка inbox недоступна</div>`);
  }
}
