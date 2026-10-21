// Хосты и сбор журналов по SSH: список хостов, подготовка, форма нового сбора
import { api } from '../api.js';
import { $, $$, RT, fmtBytes, h, mount, platIcon, raw, toast } from '../util.js';

const OS_NAME = { linux: 'Linux', macos: 'macOS', windows: 'Windows' };
const PRESETS = [['24 часа', 24], ['3 дня', 72], ['7 дней', 168], ['30 дней', 720]];

const tzLabel = (m) => {
  const s = m >= 0 ? '+' : '−';
  const a = Math.abs(m);
  return `UTC${s}${String(Math.floor(a / 60)).padStart(2, '0')}:${String(a % 60).padStart(2, '0')}`;
};
// эпоха → «ГГГГ-ММ-ДДTЧЧ:ММ» в поясе tz (минуты) — значение для <input type=datetime-local>
const toLocalInput = (epoch, tz) => new Date((epoch + tz * 60) * 1000).toISOString().slice(0, 16);
// и обратно
function fromLocalInput(v, tz) {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)$/.exec(v || '');
  if (!m) return null;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 1000 - tz * 60;
}
const fmtIn = (epoch, tz) => {
  if (!epoch) return '—';
  const s = toLocalInput(epoch, tz);
  return `${s.slice(8, 10)}.${s.slice(5, 7)}.${s.slice(0, 4)} ${s.slice(11, 16)}`;
};
const ago = (t) => {
  const d = Date.now() / 1000 - t;
  if (d < 90) return 'только что';
  if (d < 3600) return `${Math.round(d / 60)} мин назад`;
  if (d < 86400) return `${Math.round(d / 3600)} ч назад`;
  return `${Math.round(d / 86400)} дн назад`;
};

function copyBtn(text) {
  return h`<button class="btn small copy" type="button" data-copy="${text}">Копировать</button>`;
}
function bindCopy(root) {
  root.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-copy]');
    if (!b) return;
    try {
      await navigator.clipboard.writeText(b.dataset.copy);
    } catch (err) {
      const ta = document.createElement('textarea');
      ta.value = b.dataset.copy;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    b.textContent = 'Скопировано';
    setTimeout(() => { b.textContent = 'Копировать'; }, 1500);
  });
}

// ---------------------------------------------------------------------------
// Список хостов
export async function renderHosts(view) {
  mount(view, h`<div class="loading"><span class="spinner"></span></div>`);
  let hosts;
  let info;
  try {
    [hosts, info] = await Promise.all([api.get('/api/hosts'), api.get('/api/collect/info')]);
  } catch (e) {
    mount(view, h`<div class="note err">Не удалось загрузить список хостов: ${e.message}</div>`);
    return;
  }
  const scanCell = (x) => {
    const s = x.scan;
    if (!s) return h`<span class="muted">ещё не проверялся</span>`;
    if (!s.ok) return h`<span class="status error"><i></i>${s.error}</span><div class="muted small-t">${ago(s.at)}</div>`;
    return h`<span class="status ready"><i></i>доступен · ${s.sources} журн. · ${tzLabel(s.tz_offset_min || 0)}${s.tz_name ? ` ${s.tz_name}` : ''}</span>
      <div class="muted small-t">${s.host ? `имя на хосте: ${s.host} · ` : ''}проверен ${ago(s.at)}</div>`;
  };

  mount(view, h`
    ${!info.available && RT.runtime !== 'docker' && RT.runtime !== 'python' ? h`<div class="note err">Сбор с хостов недоступен. ${info.unavailable}</div>` : ''}
    ${!info.available && (RT.runtime === 'docker' || RT.runtime === 'python') ? h`<div class="note err">В контейнере нет клиента SSH или ключа панели — сбор с хостов недоступен. Пересоберите образ: <span class="mono">docker compose up -d --build</span>.</div>` : ''}
    <section class="panel">
      <div class="panel-h"><h2>Хосты</h2><div class="tools"><button class="btn primary small" type="button" id="h-add">+ Хост</button></div></div>
      <div id="h-form" class="hidden"></div>
      ${hosts.length ? h`<div class="tbl-wrap"><table class="tbl hosts-tbl"><thead><tr>
          <th>Хост</th><th>Система</th><th>Состояние</th><th class="r"></th></tr></thead><tbody>
        ${hosts.map((x) => h`<tr data-id="${x.id}">
          <td><b>${x.name}</b><div class="muted mono small-t">${x.user}@${x.address}${x.port !== 22 ? `:${x.port}` : ''}</div>${x.note ? h`<div class="muted small-t">${x.note}</div>` : ''}</td>
          <td class="nowrap"><span class="ink2">${raw(platIcon(x.os))}</span> ${OS_NAME[x.os]}</td>
          <td class="h-state">${scanCell(x)}</td>
          <td class="r nowrap">
            <a class="btn primary small" href="#/collect/${x.id}">Собрать…</a>
            <button class="btn small" type="button" data-act="scan">Проверить</button>
            <button class="btn small ghost" type="button" data-act="edit">Изменить</button>
            <button class="btn small ghost danger" type="button" data-act="del">Удалить</button>
          </td></tr>`)}
      </tbody></table></div>` : h`<div class="empty">Хостов пока нет. Подготовьте хост командой ниже и добавьте его кнопкой «+ Хост».</div>`}
    </section>

    <section class="panel" style="margin-top:18px">
      <div class="panel-h"><h2>Подготовка хоста</h2><span class="sub">один раз на каждый хост</span></div>
      <p class="ink2 setup-p">Скрипт кладёт на хост ключ этой панели. Войти с ключом можно только с адреса панели и только чтобы запустить сбор журналов: терминала и других команд нет. Скрипт сбора только читает журналы.</p>
      <div class="setup-grid">
        <div class="setup-os">
          <h3>${raw(platIcon('linux'))} Linux и ${raw(platIcon('macos'))} macOS</h3>
          <p class="muted small-t">В терминале хоста, под пользователем с правом sudo. Создаётся системный пользователь <span class="mono">${info.user}</span> без пароля:</p>
          ${info.local_only ? h`<p class="muted small-t">Панель открыта только на этом компьютере, поэтому хост не скачает скрипт по ссылке. Скачайте его: <a class="btn small" href="/setup/host.sh" download="logatlas-host-setup.sh">logatlas-host-setup.sh</a>, перенесите на хост (например, <span class="mono">scp logatlas-host-setup.sh пользователь@хост:</span>) и выполните там <span class="mono">sudo bash logatlas-host-setup.sh</span>. Либо откройте панель для сети: <span class="mono">"host": "0.0.0.0"</span> в settings.json в папке данных${RT.runtime.startsWith('windows') ? ' или служба Windows' : ''}.</p>`
    : h`<div class="cmdline"><code>${info.commands.unix}</code>${copyBtn(info.commands.unix)}</div>`}
          <p class="muted small-t">На macOS заранее включите «Удалённый вход»: Системные настройки → Основные → Общий доступ.</p>
        </div>
        <div class="setup-os">
          <h3>${raw(platIcon('windows'))} Windows 10/11</h3>
          <p class="muted small-t">1. Скачайте скрипт${info.local_only ? ' и перенесите его на хост' : ' на этот хост'}: <a class="btn small" href="${info.commands.windows_url}" download="logatlas-setup.ps1">logatlas-setup.ps1</a></p>
          <p class="muted small-t">2. В PowerShell от имени администратора перейдите в папку с файлом (<span class="mono">cd</span>) и выполните. Скрипт включит встроенный OpenSSH Server и добавит ключ панели к вашей учётной записи — новых пользователей не создаёт, ваш пароль и ваши ключи не трогает:</p>
          <div class="cmdline"><code>${info.commands.windows}</code>${copyBtn(info.commands.windows)}</div>
          <p class="muted small-t">В карточке хоста укажите имя этой учётной записи — скрипт напишет его в конце. Отдельный пользователь вместо вашей записи: добавьте к команде <span class="mono">-CreateUser</span>.</p>
        </div>
      </div>
      <details class="setup-more">
        <summary>Убрать с хоста, ключ панели</summary>
        <p class="muted small-t">Убрать пользователя и скрипты — Linux и macOS:</p>
        <div class="cmdline"><code>${info.commands.unix_remove}</code>${copyBtn(info.commands.unix_remove)}</div>
        <p class="muted small-t">Windows (PowerShell от имени администратора, в папке с logatlas-setup.ps1):</p>
        <div class="cmdline"><code>${info.commands.windows_remove}</code>${copyBtn(info.commands.windows_remove)}</div>
        <p class="muted small-t">Открытый ключ панели (лежит в <span class="mono">data/keys/</span>):</p>
        <div class="cmdline"><code>${info.pubkey || 'ещё не создан'}</code>${info.pubkey ? copyBtn(info.pubkey) : ''}</div>
        <p class="muted small-t">Хост разрешит вход с адресов: ${[info.panel_host].concat(info.panel_sources || []).filter(Boolean).join(', ') || '?'} (панель сама определяет, под каким адресом её видят хосты). Если подключение всё равно отклоняется, добавьте адрес панели в команду: <span class="mono">sudo PANEL_IP=192.168.x.x bash</span> — или задайте <span class="mono">LOGATLAS_PANEL_IPS</span> в настройках контейнера.</p>
      </details>
    </section>`);
  bindCopy(view);

  const form = $('#h-form');
  const openForm = (x) => {
    const v = x || { name: '', address: '', port: 22, os: 'linux', user: info.user, note: '' };
    form.classList.remove('hidden');
    mount(form, h`<div class="h-form">
      <label class="lbl">Название<input class="field" id="hf-name" value="${v.name}" placeholder="redos-pc"></label>
      <label class="lbl">Адрес (имя или IP)<input class="field" id="hf-addr" value="${v.address}" placeholder="192.168.10.20" required></label>
      <label class="lbl">Порт SSH<input class="field" id="hf-port" type="number" min="1" max="65535" value="${v.port}" style="width:90px"></label>
      <label class="lbl">Система<select class="field" id="hf-os">${Object.entries(OS_NAME).map(([k, n]) => h`<option value="${k}" ${v.os === k ? raw('selected') : ''}>${n}</option>`)}</select></label>
      <label class="lbl">Пользователь<input class="field" id="hf-user" value="${v.user}" placeholder="${v.os === 'windows' ? 'ваша учётная запись' : info.user}" style="width:170px"></label>
      <label class="lbl grow">Заметка<input class="field" id="hf-note" value="${v.note || ''}"></label>
      <div class="btn-row"><button class="btn primary" type="button" id="hf-save">${x ? 'Сохранить' : 'Добавить'}</button><button class="btn ghost" type="button" id="hf-cancel">Отмена</button></div>
    </div>`);
    $('#hf-addr').focus();
    $('#hf-os').addEventListener('change', (e) => {
      const u = $('#hf-user');
      const win = e.target.value === 'windows';
      if (win && u.value === info.user) u.value = '';
      if (!win && !u.value) u.value = info.user;
      u.placeholder = win ? 'ваша учётная запись' : info.user;
    });
    $('#hf-cancel').addEventListener('click', () => form.classList.add('hidden'));
    $('#hf-save').addEventListener('click', async () => {
      const body = {
        name: $('#hf-name').value.trim(), address: $('#hf-addr').value.trim(), port: +$('#hf-port').value || 22,
        os: $('#hf-os').value, user: $('#hf-user').value.trim(), note: $('#hf-note').value.trim(),
      };
      try {
        if (x) await api.put(`/api/hosts/${x.id}`, body); else await api.post('/api/hosts', body);
        renderHosts(view);
      } catch (err) { toast(err.message); }
    });
  };
  $('#h-add').addEventListener('click', () => openForm(null));

  view.querySelector('tbody')?.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const tr = b.closest('tr');
    const x = hosts.find((y) => y.id === tr.dataset.id);
    if (b.dataset.act === 'edit') { openForm(x); return; }
    if (b.dataset.act === 'del') {
      if (b.dataset.armed !== '1') {
        b.dataset.armed = '1';
        b.textContent = 'Удалить?';
        setTimeout(() => { b.dataset.armed = ''; b.textContent = 'Удалить'; }, 4000);
        return;
      }
      await api.del(`/api/hosts/${x.id}`);
      toast('Хост удалён из панели; на самом хосте ничего не изменилось');
      renderHosts(view);
      return;
    }
    if (b.dataset.act === 'scan') {
      const cell = tr.querySelector('.h-state');
      mount(cell, h`<span class="spinner"></span> <span class="muted">подключаюсь…</span>`);
      try { await api.post(`/api/hosts/${x.id}/scan`); } catch (err) { /* ошибка сохранится в карточке */ }
      renderHosts(view);
    }
  });
}

// ---------------------------------------------------------------------------
// Новый сбор с хоста
export async function renderCollect(view, { id }) {
  let host;
  try {
    host = (await api.get('/api/hosts')).find((x) => x.id === id);
  } catch (e) { host = null; }
  if (!host) {
    mount(view, h`<div class="note err">Хост не найден.</div><a class="btn" href="#/hosts">К хостам</a>`);
    return;
  }
  const head = h`<div class="collect-head">
    <span class="ink2">${raw(platIcon(host.os))}</span>
    <div><h2>${host.name}</h2><div class="muted mono small-t">${host.user}@${host.address}${host.port !== 22 ? `:${host.port}` : ''} · ${OS_NAME[host.os]}</div></div>
    <a class="btn ghost small" href="#/hosts" style="margin-left:auto">К хостам</a></div>`;
  mount(view, h`${head}<div class="loading"><span class="spinner"></span><p>Подключаюсь к хосту и ищу журналы…</p></div>`);

  let scan;
  try {
    scan = await api.post(`/api/hosts/${host.id}/scan`);
  } catch (e) {
    const keyChanged = /Отпечаток/.test(e.message);
    mount(view, h`${head}<div class="note err">${e.message}</div>
      <div class="btn-row"><button class="btn primary" type="button" id="c-retry">Повторить</button>
      ${keyChanged ? h`<button class="btn danger" type="button" id="c-reset">Сбросить отпечаток и повторить</button>` : ''}
      <a class="btn ghost" href="#/hosts">Как подготовить хост</a></div>`);
    $('#c-retry').addEventListener('click', () => renderCollect(view, { id }));
    $('#c-reset')?.addEventListener('click', async () => {
      await api.del(`/api/hosts/${host.id}/hostkey`);
      renderCollect(view, { id });
    });
    return;
  }

  const hostTz = scan.tz_offset_min || 0;
  const browserTz = -new Date().getTimezoneOffset();
  const tzOpts = [];
  for (let m = -12 * 60; m <= 14 * 60; m += 30) tzOpts.push(m);
  const srcs = scan.sources || [];
  const st = { tz: hostTz, preset: 72, nameEdited: false };

  mount(view, h`${head}
    ${scan.outdated ? h`<div class="note">На хосте старая версия скрипта сбора (${scan.version || '1.0'}): собрать можно, но новая собирает в несколько раз быстрее — журналы выгружаются параллельно на всех ядрах хоста и сразу сжимаются${scan.version === '1.1' ? '' : ', а в «Ходе сбора» виден результат по каждому журналу'}. Повторите подготовку хоста (раздел «Хосты и сбор»), чтобы обновить скрипт.</div>` : ''}
    ${scan.os_mismatch ? h`<div class="note">На хосте обнаружена система «${OS_NAME[scan.os] || scan.os}», а в карточке указана «${OS_NAME[host.os]}». Исправьте карточку хоста.</div>` : ''}
    <p class="ink2 collect-meta">Имя на хосте: <b>${scan.host}</b> · часовой пояс хоста: <b>${tzLabel(hostTz)}</b>${scan.tz_name ? ` (${scan.tz_name})` : ''} · время на хосте: ${fmtIn(scan.now, hostTz)}</p>
    <div class="grid g-collect">
      <section class="panel">
        <div class="panel-h"><h2>Журналы</h2><span class="sub" id="c-sum"></span></div>
        ${srcs.length ? h`<div class="tbl-wrap"><table class="tbl src-tbl"><thead><tr><th></th><th>Журнал</th><th class="r">Размер</th><th>Записи в журнале</th></tr></thead><tbody>
          ${srcs.map((s) => h`<tr><td><input type="checkbox" class="src" value="${s.id}" ${s.default ? raw('checked') : ''} id="src-${s.id}"></td>
            <td><label for="src-${s.id}"><b>${s.label}</b><div class="muted small-t">${s.desc}</div></label></td>
            <td class="r nowrap">${s.size ? fmtBytes(s.size) : '—'}</td>
            <td class="t src-range" data-first="${s.first || ''}" data-last="${s.last || ''}"></td></tr>`)}
        </tbody></table></div>` : h`<div class="empty">На хосте не найдено известных журналов.</div>`}
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Период</h2></div>
        <div class="btn-row presets">${PRESETS.map(([n, hrs]) => h`<button class="chip" type="button" data-h="${hrs}">${n}</button>`)}<button class="chip" type="button" data-h="0">Свой</button></div>
        <div class="period-grid">
          <label class="lbl">С<input class="field" type="datetime-local" id="c-from"></label>
          <label class="lbl">По<input class="field" type="datetime-local" id="c-to"></label>
          <label class="lbl span2">Часовой пояс периода и анализа
            <select class="field" id="c-tz">
              <option value="${hostTz}">Пояс хоста — ${tzLabel(hostTz)}${scan.tz_name ? ` ${scan.tz_name}` : ''}</option>
              ${browserTz !== hostTz ? h`<option value="${browserTz}">Пояс этого компьютера — ${tzLabel(browserTz)}</option>` : ''}
              <optgroup label="Другой">${tzOpts.map((m) => h`<option value="${m}" data-other="1">${tzLabel(m)}</option>`)}</optgroup>
            </select></label>
          <label class="lbl span2">Название сбора<input class="field" id="c-name"></label>
        </div>
        <p class="muted small-t" id="c-hint"></p>
        <div class="btn-row" style="margin-top:12px"><button class="btn primary" type="button" id="c-go">Собрать</button><span class="muted small-t" id="c-st"></span></div>
      </section>
    </div>`);

  const inFrom = $('#c-from');
  const inTo = $('#c-to');
  const inName = $('#c-name');
  const fmtShort = (e) => fmtIn(e, st.tz);
  const autoName = () => {
    const f = fromLocalInput(inFrom.value, st.tz);
    const t = fromLocalInput(inTo.value, st.tz);
    return f && t ? `${host.name} · ${fmtShort(f)} – ${fmtShort(t)} (${tzLabel(st.tz)})` : host.name;
  };
  const refresh = () => {
    $$('.presets .chip').forEach((c) => c.classList.toggle('on', +c.dataset.h === st.preset));
    $$('.src-range').forEach((td) => {
      const f = +td.dataset.first;
      const l = +td.dataset.last;
      td.textContent = f && l ? `${fmtShort(f)} — ${fmtShort(l)}` : '—';
    });
    const sel = $$('input.src:checked');
    const bytes = sel.reduce((a, cb) => a + (srcs.find((s) => s.id === cb.value)?.size || 0), 0);
    $('#c-sum').textContent = `выбрано ${sel.length} из ${srcs.length}${bytes ? ` · журналы целиком — ${fmtBytes(bytes)}, выгрузится только период` : ''}`;
    const f = fromLocalInput(inFrom.value, st.tz);
    const t = fromLocalInput(inTo.value, st.tz);
    const bad = !f || !t || f >= t;
    $('#c-hint').textContent = bad ? 'Начало периода должно быть раньше конца.' : `Время в полях — ${tzLabel(st.tz)}; в этом же поясе будет показан сбор.`;
    $('#c-hint').classList.toggle('err-t', bad);
    $('#c-go').disabled = bad || !sel.length;
    if (!st.nameEdited) inName.value = autoName();
  };
  const applyPreset = (hrs) => {
    st.preset = hrs;
    if (hrs) {
      const now = Math.floor(Date.now() / 1000);
      inTo.value = toLocalInput(now, st.tz);
      inFrom.value = toLocalInput(now - hrs * 3600, st.tz);
    }
    refresh();
  };
  $('.presets').addEventListener('click', (e) => {
    const c = e.target.closest('[data-h]');
    if (c) applyPreset(+c.dataset.h);
  });
  [inFrom, inTo].forEach((i) => i.addEventListener('input', () => { st.preset = 0; refresh(); }));
  $('#c-tz').addEventListener('change', (e) => {
    // сохраняем те же моменты времени, переписывая их в новом поясе
    const f = fromLocalInput(inFrom.value, st.tz);
    const t = fromLocalInput(inTo.value, st.tz);
    st.tz = +e.target.value;
    if (f) inFrom.value = toLocalInput(f, st.tz);
    if (t) inTo.value = toLocalInput(t, st.tz);
    refresh();
  });
  inName.addEventListener('input', () => { st.nameEdited = inName.value.trim() !== ''; });
  view.addEventListener('change', (e) => { if (e.target.matches('input.src')) refresh(); });
  applyPreset(72);

  $('#c-go').addEventListener('click', async () => {
    const b = $('#c-go');
    b.disabled = true;
    $('#c-st').textContent = 'Запускаю…';
    try {
      const r = await api.post('/api/collect', {
        host_id: host.id,
        since: fromLocalInput(inFrom.value, st.tz),
        until: fromLocalInput(inTo.value, st.tz) + 59,
        sources: $$('input.src:checked').map((cb) => cb.value),
        tz: st.tz,
        name: inName.value.trim() || autoName(),
      });
      window.dispatchEvent(new CustomEvent('la:cases-changed'));
      window.location.hash = `#/c/${r.id}/overview`;
    } catch (err) {
      toast(err.message);
      $('#c-st').textContent = '';
      b.disabled = false;
    }
  });
}
