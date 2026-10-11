// Общие помощники интерфейса
export const CATS = {
  system: 'Система', access: 'Доступ и сеансы', process: 'Процессы и приложения', persistence: 'Закрепление',
  software: 'Установка ПО', device: 'Устройства', network: 'Сеть', security: 'Безопасность',
};
export const CAT_KEYS = Object.keys(CATS);

/** Как запущена панель (заполняет app.js из /api/meta): подсказки об ошибках зависят от этого */
export const RT = { runtime: 'docker', logHint: 'журнал контейнера: docker compose logs logatlas' };

export const SEV = {
  critical: { name: 'Критично', icon: 'octagon' },
  high: { name: 'Высокая', icon: 'triangle' },
  medium: { name: 'Средняя', icon: 'diamond' },
  low: { name: 'Низкая', icon: 'circle' },
  info: { name: 'Инфо', icon: 'info' },
};
export const SEV_KEYS = Object.keys(SEV);

export const PLATFORMS = {
  all: { name: 'Все сборы' },
  macos: { name: 'macOS' },
  windows: { name: 'Windows' },
  linux: { name: 'Linux' },
};

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
export const catColor = (c) => cssVar(`--c-${c}`) || cssVar('--muted');
export const sevColor = (s) => cssVar(`--s-${s}`) || cssVar('--muted');

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** h`...` — шаблон с экранированием подстановок; raw(x) — вставка без экранирования */
export class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(s);
export function h(strings, ...vals) {
  let out = strings[0];
  vals.forEach((v, i) => {
    if (Array.isArray(v)) out += v.map((x) => (x instanceof Raw ? x.s : esc(x))).join('');
    else if (v instanceof Raw) out += v.s;
    else if (v === null || v === undefined || v === false) out += '';
    else out += esc(v);
    out += strings[i + 1];
  });
  return raw(out);
}
export function mount(node, content) {
  if (!node) return node;
  node.innerHTML = content instanceof Raw ? content.s : String(content);
  return node;
}
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const nf = new Intl.NumberFormat('ru-RU');
export const fmtN = (n) => (n === null || n === undefined ? '—' : nf.format(n));
export function fmtCompact(n) {
  if (n === null || n === undefined) return '—';
  if (n < 10000) return nf.format(n);
  if (n < 1e6) return `${(n / 1000).toLocaleString('ru-RU', { maximumFractionDigits: n < 1e5 ? 1 : 0 })} тыс.`;
  return `${(n / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} млн`;
}
export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// Время хранится как «локальное время хоста», записанное в UTC-миллисекундах — показываем UTC-полями.
const p2 = (x) => String(x).padStart(2, '0');
export function fmtTime(ms, mode = 'full') {
  if (ms === null || ms === undefined) return '—';
  const d = new Date(ms);
  const date = `${p2(d.getUTCDate())}.${p2(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`;
  const time = `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
  if (mode === 'date') return date;
  if (mode === 'time') return time;
  if (mode === 'short') return `${p2(d.getUTCDate())}.${p2(d.getUTCMonth() + 1)} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
  if (mode === 'ms') return `${date} ${time}.${String(d.getUTCMilliseconds()).padStart(3, '0')}`;
  return `${date} ${time}`;
}
export function fmtSpan(ms) {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} с`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} мин`;
  const hh = Math.round(m / 6) / 10;
  if (hh < 48) return `${hh.toLocaleString('ru-RU')} ч`;
  const d = Math.round(hh / 2.4) / 10;
  return `${d.toLocaleString('ru-RU')} ${plural(Math.round(d), 'день', 'дня', 'дней')}`;
}
export function fmtBucket(ms) {
  const map = { 1000: '1 с', 5000: '5 с', 15000: '15 с', 30000: '30 с', 60000: '1 мин', 300000: '5 мин', 600000: '10 мин',
    900000: '15 мин', 1800000: '30 мин', 3600000: '1 ч', 10800000: '3 ч', 21600000: '6 ч', 43200000: '12 ч', 86400000: '1 сутки', 604800000: '7 суток' };
  return map[ms] || fmtSpan(ms);
}
export function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n.toLocaleString('ru-RU', { maximumFractionDigits: i ? 1 : 0 })} ${u[i]}`;
}

export function sevIcon(sev, size = 14) {
  const c = sevColor(sev);
  const s = size;
  const shape = {
    octagon: `<path d="M5 1h6l4 4v6l-4 4H5l-4-4V5z" fill="${c}"/><path d="M8 4.5v4.2M8 10.8v.6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
    triangle: `<path d="M8 1.5 15 14H1z" fill="${c}"/><path d="M8 6v3.6M8 11.4v.4" stroke="#1a1a1a" stroke-width="1.7" stroke-linecap="round"/>`,
    diamond: `<path d="M8 1 15 8 8 15 1 8z" fill="${c}"/>`,
    circle: `<circle cx="8" cy="8" r="6" fill="none" stroke="${c}" stroke-width="2.2"/>`,
    info: `<circle cx="8" cy="8" r="6.5" fill="none" stroke="${c}" stroke-width="1.5"/><path d="M8 7v4.2M8 4.6v.2" stroke="${c}" stroke-width="1.7" stroke-linecap="round"/>`,
  }[SEV[sev]?.icon || 'info'];
  return `<svg viewBox="0 0 16 16" width="${s}" height="${s}" aria-hidden="true">${shape}</svg>`;
}
export function sevLabel(sev) {
  return raw(`<span class="sev">${sevIcon(sev)}<span>${esc(SEV[sev]?.name || sev)}</span></span>`);
}
export function catTag(cat) {
  if (!cat) return raw('<span class="muted">—</span>');
  return raw(`<span class="cat-tag"><i style="background:${catColor(cat)}"></i>${esc(CATS[cat] || cat)}</span>`);
}

export function highlight(text, terms) {
  let s = esc(text ?? '');
  if (!terms || !terms.length) return s;
  const parts = terms.filter((t) => t && t.length > 1).map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!parts.length) return s;
  try {
    const rx = new RegExp(`(${parts.join('|')})`, 'gi');
    s = s.replace(rx, '<mark>$1</mark>');
  } catch (e) { /* ignore */ }
  return s;
}

// всплывающая подсказка
const tt = () => document.getElementById('tooltip');
export function showTip(html, ev) {
  const el = tt();
  el.innerHTML = html;
  el.classList.add('on');
  moveTip(ev);
}
export function moveTip(ev) {
  const el = tt();
  const pad = 14;
  const r = el.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = ev.clientY - r.height - pad;
  el.style.left = `${Math.max(8, x)}px`;
  el.style.top = `${Math.max(8, y)}px`;
}
export function hideTip() { tt().classList.remove('on'); }

let toastTimer;
export function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), 2600);
}

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(val));
  } catch (e) { /* хранилище недоступно */ }
  return undefined;
}

export const PLATFORM_ICON = {
  macos: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><rect x="2.5" y="3.5" width="15" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M7 17h6M10 13.5V17" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  windows: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M3 4.5 9 3.6v6H3zm7-1 7-1v7.1h-7zM3 10.5h6v6L3 15.6zm7 0h7V17.5l-7-1z" fill="currentColor" opacity=".9"/></svg>',
  linux: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M4 5.5 8 9l-4 3.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 14.5h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  all: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><rect x="3" y="3" width="6" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="11" y="3" width="6" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="3" y="11" width="6" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="11" y="11" width="6" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
  mixed: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><circle cx="7" cy="10" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="13" cy="10" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
  other: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M5 3h7l3 3v11H5z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
};
export const platIcon = (p) => PLATFORM_ICON[p] || PLATFORM_ICON.other;
export const platName = (p) => ({ macos: 'macOS', windows: 'Windows', linux: 'Linux', mixed: 'Смешанное', other: 'Другое' }[p] || 'Определяется…');
