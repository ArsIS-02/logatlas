// Главная: загрузка журналов, форматы по платформам, список дел, импорт из папки inbox
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

export async function renderHome(view, { platform, cases, meta }) {
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
          <input id="folder" type="file" webkitdirectory multiple class="hidden">
          <label class="lbl">Название дела<input class="field" id="cname" placeholder="например, mbp-design-07, 9 октября" style="width:260px"></label>
          <label class="lbl">Часовой пояс хоста
            <select class="field" id="tzmode"><option value="auto">Определить по журналам</option><option value="fixed">Указать вручную</option></select></label>
          <label class="lbl hidden" id="tzwrap">Пояс<select class="field" id="tz">${raw(tzOptions())}</select></label>
        </div>
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

    <h2 class="section-h">${platform === 'all' ? 'Все дела' : `Дела: ${platName(platform)}`}</h2>
    <div class="case-rows">
      ${list.length ? list.map((c) => h`<a class="case-row" href="#/c/${c.id}/overview">
        <span class="ink2">${raw(platIcon(c.platform))}</span>
        <span><span class="nm">${c.name}</span><br><span class="sm">${c.host || platName(c.platform)}</span></span>
        <span class="sm hide-m num">${c.events ? `${fmtN(c.events)} событий` : ''}</span>
        <span class="status ${c.status}"><i></i>${{ ready: 'Готово', processing: 'Обработка', queued: 'В очереди', error: 'Ошибка' }[c.status] || c.status}</span>
        <span class="sm hide-m">${c.t_min ? `${fmtTime(c.t_min, 'date')} — ${fmtTime(c.t_max, 'date')}` : ''}</span>
        <span class="sm">${fmtTime(c.created * 1000 + (-new Date().getTimezoneOffset() * 60000), 'short')}</span>
      </a>`) : h`<div class="empty">Здесь появятся загруженные дела.</div>`}
    </div>

    <h2 class="section-h">Большие архивы</h2>
    <div class="panel" id="inbox"><div class="loading"><span class="spinner"></span></div></div>
  `);

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
