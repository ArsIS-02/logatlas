// Карточка события в выдвижной панели
import { api } from '../api.js';
import { $, $$, catTag, fmtTime, h, mount, sevLabel } from '../util.js';

let current = null;

export function closeDrawer() {
  const d = $('#drawer');
  if (!d || !d.classList.contains('on')) return;
  d.classList.remove('on');
  d.setAttribute('aria-hidden', 'true');
  if (current?.returnFocus) current.returnFocus.focus?.();
  current = null;
}

function q(v) { return /[\s"|]/.test(String(v)) ? `"${String(v).replace(/"/g, '')}"` : v; }

export async function openEvent(caseId, id, ctx) {
  const d = $('#drawer');
  const body = $('#drawer-body');
  current = { id, returnFocus: document.activeElement };
  d.classList.add('on');
  d.setAttribute('aria-hidden', 'false');
  mount(body, h`<div class="loading"><span class="spinner"></span></div>`);
  d.querySelectorAll('[data-close]').forEach((x) => { x.onclick = closeDrawer; });
  let e;
  try {
    e = await api.get(`/api/cases/${caseId}/events/${id}`);
  } catch (err) {
    mount(body, h`<div class="note err">${err.message}</div>`);
    return;
  }
  if (current?.id !== id) return;
  const extra = e.extra && typeof e.extra === 'object' ? Object.entries(e.extra) : [];
  const btn = (label, query) => h`<button class="btn small" type="button" data-q="${query}">${label}</button>`;
  mount(body, h`
    <div class="dh">
      <div>
        <div class="muted num" style="font-size:12.5px;display:flex;gap:12px;flex-wrap:wrap"><span>${fmtTime(e.t, 'ms')}</span><span>${e.source || ''}</span></div>
        <h3 id="drawer-title">${e.action || e.process || 'Событие'}</h3>
        ${e.category ? h`<div style="margin-top:4px">${catTag(e.category)}</div>` : ''}
      </div>
      <button class="btn ghost small" type="button" data-close aria-label="Закрыть">Закрыть</button>
    </div>
    ${e.rules.length ? h`<div class="chips" style="margin-top:12px">${e.rules.map((r) => h`<button type="button" class="chip" data-rule="${r.id}">${sevLabel(r.severity)} ${r.title}</button>`)}</div>` : ''}
    <dl class="kv">
      ${e.subject ? h`<dt>Объект</dt><dd>${e.subject}</dd>` : ''}
      ${e.process ? h`<dt>Процесс</dt><dd><button class="link" type="button" data-q="process:${q(e.process)}">${e.process}</button>${e.pid ? h` <span class="muted">PID ${e.pid}</span>` : ''}</dd>` : ''}
      ${e.user ? h`<dt>Пользователь</dt><dd><button class="link" type="button" data-q="user:${q(e.user)}">${e.user}</button></dd>` : ''}
      ${e.host ? h`<dt>Хост</dt><dd>${e.host}</dd>` : ''}
      <dt>Уровень</dt><dd>${e.level || 'info'}</dd>
      ${e.code ? h`<dt>Код / подсистема</dt><dd class="mono">${e.code}</dd>` : ''}
      ${extra.map(([k, v]) => h`<dt>${{ path: 'Путь к программе', cat: 'Категория журнала', ch: 'Канал', no_ts: 'Без времени' }[k] || k}</dt><dd class="mono" style="word-break:break-all">${String(v)}</dd>`)}
      <dt>Файл</dt><dd class="mono" style="word-break:break-all">${e.file?.path || '—'}</dd>
    </dl>
    <pre class="raw">${e.message}</pre>
    <div class="btn-row" style="margin-top:14px">
      ${e.action ? btn('То же действие', `action:${q(e.action)}`) : ''}
      ${e.subject ? btn('Тот же объект', `subject:${q(e.subject)}`) : ''}
      ${e.process ? btn('Этот процесс', `process:${q(e.process)}`) : ''}
    </div>
    <div class="panel-h" style="margin-top:22px"><h2>Соседние события</h2>
      <div class="tools"><button class="btn small" type="button" id="ctx-all">Все источники</button><button class="btn ghost small" type="button" id="ctx-same">Только ${e.source}</button></div></div>
    <div id="ctx" class="ctx-box"></div>`);
  $$('[data-close]', body).forEach((x) => x.addEventListener('click', closeDrawer));
  $$('[data-q]', body).forEach((b) => b.addEventListener('click', () => { closeDrawer(); ctx.search(b.dataset.q); }));
  $$('[data-rule]', body).forEach((b) => b.addEventListener('click', () => { closeDrawer(); ctx.go('findings', { sub: b.dataset.rule }); }));
  const loadCtx = async (same) => {
    $('#ctx-all').className = `btn small ${same ? 'ghost' : ''}`;
    $('#ctx-same').className = `btn small ${same ? '' : 'ghost'}`;
    const rows = await api.get(`/api/cases/${caseId}/events/${id}/context`, { n: 25, same: same ? 1 : '' });
    mount($('#ctx'), h`<table class="tbl"><tbody>${rows.map((r) => h`<tr class="click ${r.id === id ? 'focus' : ''}" data-id="${r.id}">
      <td class="t">${fmtTime(r.t, 'time')}</td><td class="ink2" style="font-size:12px">${r.process || ''}</td>
      <td class="msg"><div class="clip">${r.action ? h`<b style="font-family:var(--font)">${r.action}</b> ` : ''}${r.message}</div></td></tr>`)}</tbody></table>`);
    $$('#ctx tr[data-id]').forEach((tr) => tr.addEventListener('click', () => openEvent(caseId, +tr.dataset.id, ctx)));
    const box = $('#ctx');
    const f = $('#ctx tr.focus');
    if (f && box) box.scrollTop = f.offsetTop - box.clientHeight / 2 + f.offsetHeight / 2;
  };
  $('#ctx-all').addEventListener('click', () => loadCtx(false));
  $('#ctx-same').addEventListener('click', () => loadCtx(true));
  loadCtx(false);
  body.closest('.drawer-panel').scrollTop = 0;
  body.scrollTop = 0;
  $('[data-close]', body)?.focus();
}
