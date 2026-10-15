// Переключение языка интерфейса LogAtlas.
//
// Ключ перевода — сама русская строка: t('Обзор') при выборе английского возвращает 'Overview'.
// Для строк с подстановками используем {n}: t('{n} событий', { n }) → '123 events'.
// Язык хранится в localStorage (logatlas.lang) и уходит на сервер заголовком X-LogAtlas-Lang.

const EN = {
  // навигация и каркас
  'Платформы': 'Platforms',
  'Хосты и сбор': 'Hosts & collection',
  'Правила Sigma': 'Sigma rules',
  'Покрытие': 'Coverage',
  'Сборы': 'Cases',
  'Загрузить журналы': 'Upload logs',
  'Загрузить': 'Upload',
  'Пока пусто': 'Nothing yet',
  'Сменить тему': 'Switch theme',
  'Тема': 'Theme',
  'Удалить сбор': 'Delete case',
  'Нажмите ещё раз: сбор и его файлы удалятся безвозвратно': 'Click again: the case and its files will be deleted permanently',
  'Удалить?': 'Delete?',
  'Сбор удалён': 'Case deleted',
  'Сбор удалён, разбор остановлен': 'Case deleted, parsing stopped',
  'готово': 'ready',
  'обработка': 'processing',
  'в очереди': 'queued',
  'ошибка': 'error',
  'идёт сбор': 'collecting',

  // вкладки сбора
  'Обзор': 'Overview',
  'Хронология': 'Timeline',
  'Поиск': 'Search',
  'Находки': 'Findings',
  'Объекты': 'Entities',
  'Сравнение': 'Compare',
  'Источники': 'Sources',
  'Отчёт': 'Report',

  // шапка
  'Анализ журналов': 'Log analysis',
  'Журналы ': 'Logs: ',
  'Хосты и сбор журналов': 'Hosts & log collection',
  'Новый сбор с хоста': 'New host collection',
  'импорт правил SigmaHQ и своих': 'import SigmaHQ and custom rules',
  'что размечено и что закрыто правилами': 'what is tagged and what rules cover',
  'по SSH, ключом панели': 'over SSH, with the panel key',
  'Хост ': 'Host ',
  'Время хоста ': 'Host time ',
  ' событий': ' events',
  'Синтаксис': 'Syntax',
  'Поиск по журналам': 'Search logs',
  'Поиск: sudo, process:sshd, /regex/…': 'Search: sudo, process:sshd, /regex/…',
  'Лента времени: выделите период': 'Timeline: select a period',
  'Период': 'Period',
  'Сбросить период': 'Reset period',
  'Весь период сбора. Выделите участок на ленте, чтобы сузить все вкладки.': 'Whole case period. Select a range on the ribbon to narrow every tab.',
  'все события ': 'all events ',
  'классифицированные': 'classified',

  // подсказки синтаксиса поиска
  'подстрока в сообщении, процессе или объекте': 'substring in message, process or object',
  'фраза целиком': 'exact phrase',
  'поле: process, source, user, host, level, code, pid': 'field: process, source, user, host, level, code, pid',
  'шаблон, * — любые символы': 'pattern, * — any characters',
  'категория: система, доступ, процессы, закрепление, по, устройства, сеть, безопасность': 'category: system, access, processes, persistence, software, devices, network, security',
  'подстрока в действии, объекте (subject:) или сообщении (msg:)': 'substring in action, subject (subject:) or message (msg:)',
  'уровень и выше': 'level and above',
  'исключить': 'exclude',
  'несколько значений': 'multiple values',
  'регулярное выражение по сообщению': 'regex over the message',
  'события из находки': 'events from a finding',
  'Условия объединяются через «И». Регистр латиницы не важен.': 'Terms are combined with AND. Latin case does not matter.',

  // состояние загрузки
  'Открываю сбор…': 'Opening case…',
  'Не удалось открыть сбор: ': 'Could not open the case: ',
  'К списку сборов': 'Back to the list',
  'Ошибка: ': 'Error: ',
  'Журналы обработаны': 'Logs processed',
  'Готовлю сводку по сбору. Для сбора из миллионов событий при первом открытии это может занять несколько минут — потом он будет открываться сразу. Страницу можно не закрывать.':
    'Preparing the case overview. For a case with millions of events the first open can take a few minutes — afterwards it opens instantly. You can leave this page open.',

  // главная — загрузка
  'Перетащите журналы или архив сюда': 'Drag logs or an archive here',
  'Подойдут отдельные файлы и архивы tar, tar.gz, zip, gz, bz2, xz — вложенные архивы тоже распакуются. Формат каждого файла определяется автоматически, файлы обрабатываются здесь, на этом сервере.':
    'Single files and tar, tar.gz, zip, gz, bz2, xz archives are accepted — nested archives are unpacked too. Each file format is detected automatically; files are processed here, on this server.',
  'Выбрать файлы': 'Choose files',
  'Выбрать папку': 'Choose folder',
  'Собрать с хоста…': 'Collect from a host…',
  'Этот Mac': 'This Mac',
  'Этот компьютер': 'This computer',
  'Название сбора': 'Case name',
  'Часовой пояс хоста': 'Host timezone',
  'Определить по журналам': 'Detect from logs',
  'Указать вручную': 'Set manually',
  'Пояс': 'Timezone',
  'Период разбора': 'Parsing period',
  'Весь архив': 'Whole archive',
  'Последние 24 часа': 'Last 24 hours',
  'Последние 3 дня': 'Last 3 days',
  'Последние 7 дней': 'Last 7 days',
  'Последние 30 дней': 'Last 30 days',
  'Свой период': 'Custom period',
  'С': 'From',
  'По': 'To',
  'Можно оставить «Весь архив» — разберутся все события, — или выбрать период: тогда в сбор попадут только события за него, остальные отбросит разбор.':
    'Leave "Whole archive" to parse everything, or pick a period: only events within it will make it into the case, the rest is dropped.',
  'Проверьте период: начало должно быть раньше конца': 'Check the period: start must be before end',
  'Все сборы': 'All cases',
  'Выбрать все': 'Select all',
  'Удалить выбранные': 'Delete selected',
  'выбрано: {n}': 'selected: {n}',
  'Удаление…': 'Deleting…',
  'Здесь появятся загруженные и собранные с хостов журналы.': 'Uploaded and host-collected logs will appear here.',
  'Большие архивы': 'Large archives',
  'последние сутки': 'last 24 hours',
  '3 дня': '3 days',
  'неделя': 'week',
  '2 недели': '2 weeks',
  '30 дней': '30 days',
  'Собрать и разобрать': 'Collect and parse',
  'Сборы: ': 'Cases: ',
  'событий': 'events',

  // хронология
  'Хронология хоста': 'Host timeline',
  'Сделать периодом сбора': 'Set as case period',
  'Исходный масштаб': 'Reset zoom',
  'Перетаскивайте, чтобы сдвинуть; Ctrl, ⌘ или Shift с колесом — масштаб, двойной щелчок — приблизить. Точки — отдельные события, полосы — плотность частых событий.':
    'Drag to pan; Ctrl, ⌘ or Shift with the wheel to zoom, double-click to zoom in. Dots are single events, bars are density of frequent events.',
  'События в окне': 'Events in view',
  '{n} событий; полосы по {bucket}, списком — редкие действия': '{n} events; bars every {bucket}, the list shows rare actions',
  '{n} событий': '{n} events',
    'Категория': 'Category',
  'Действие': 'Action',
    'Процесс': 'Process',
  'Показаны последние 2000. Приблизьте участок или откройте поиск.': 'Showing the last 2000. Zoom in or open search.',
  'В окне нет классифицированных событий': 'No classified events in this view',

  // статусы и состояния
  'В очереди': 'Queued', 'Готово': 'Ready', 'Обработка': 'Processing', 'Идёт сбор': 'Collecting',
  'Ошибка': 'Error', 'не удался': 'failed', 'пропущен': 'skipped', 'неизвестная ошибка': 'unknown error',
  'Сбор не удался': 'Collection failed', 'Сбор в очереди на разбор': 'Case queued for parsing',
  'Разбираю журналы': 'Parsing logs', 'Собираю журналы': 'Collecting logs', 'Проверяю…': 'Checking…',
  'Определяется…': 'Detecting…', 'Запускаю…': 'Starting…', 'Загрузка…': 'Loading…', 'Передача…': 'Uploading…',
  'Запуск сбора…': 'Starting collection…', 'разобран': 'parsed', 'идёт': 'running', 'только что': 'just now',
  'ещё не создан': 'not created yet', 'Сеть недоступна': 'Network unavailable',

  // категории и уровни
  'Безопасность': 'Security', 'Сеть': 'Network', 'Система': 'System', 'Процессы': 'Processes',
  'Доступ и сеансы': 'Access & sessions', 'Установка ПО': 'Software install', 'Устройства': 'Devices',
  'Закрепление': 'Persistence', 'Прочие': 'Other', 'Другое': 'Other', 'Смешанное': 'Mixed',
  'Критично': 'Critical', 'Высокая': 'High', 'Средняя': 'Medium', 'Низкая': 'Low', 'Инфо': 'Info',
  'критично': 'critical', 'инфо': 'info', 'предупреждение': 'warning', 'отладка': 'debug',
  'Без времени': 'No time', 'без категории': 'no category', 'Без системы': 'No system',
  'Любой уровень': 'Any level', 'Все классифицированные': 'All classified', 'Все события': 'All events',
  'Все системы': 'All systems', 'Все': 'All', 'текст со временем': 'text with time', 'JSON-журнал': 'JSON log',

  // таблицы и поля
  'Источник': 'Source', 'Канал': 'Channel', 'Объект': 'Object',
  'Событие': 'Event', 'Уровень': 'Level', 'Время': 'Time',
  'Категория журнала': 'Log category', 'Путь к программе': 'Program path',
  'Удалить': 'Delete', 'Сохранить': 'Save', 'Сохранено': 'Saved', 'Копировать': 'Copy',
  'Скопировано': 'Copied', 'Добавить': 'Add', 'Удалить безвозвратно': 'Delete permanently',
  'Обновить справочник': 'Update reference', 'Проверить обновления': 'Check updates',
  'нет справочника': 'no reference', 'Справочник не загружен.': 'Reference not loaded.',
  'встроенное': 'built-in', 'встроенный снимок': 'built-in snapshot', 'включено': 'on', 'выключено': 'off',
  'Включено': 'On', 'Выключено': 'Off', 'Включены': 'Enabled', 'Не применяются': 'Not applied',

  // сравнение и эталон
  'Отклонения от эталона': 'Deviations from baseline', 'Было → стало': 'Was → now', 'Новое': 'New',
  'Пропало': 'Gone', 'Заметно чаще': 'Noticeably more frequent', 'То же действие': 'Same action',
  'Тот же объект': 'Same object', 'Этот процесс': 'This process', 'Другие сборы': 'Other cases',
  'Эталон и сборы этого хоста': 'Baseline and cases of this host',
  'По сравнению с эталоном нового нет': 'Nothing new compared to baseline',
  'Отметка эталона снята': 'Baseline cleared', 'было в сравниваемом сборе, в этом нет': 'in the compared case, absent here',
  'есть в этом сборе, нет в сравниваемом': 'present here, absent in the compared case',
  'в 10 и более раз чаще в сутки, чем в сравниваемом': '10× or more often per day than in the compared case',
  'Начало периода должно быть раньше конца.': 'Period start must be before end.',
  'Выберите хотя бы один журнал': 'Select at least one log', 'Добавьте хотя бы один индикатор': 'Add at least one indicator',
  'Отметьте файлы для импорта': 'Select files to import',
  'Запрошены права администратора. Подтвердите в появившемся окне. Архив разберётся автоматически.':
    'Administrator rights requested. Confirm in the dialog that appears. The archive will be parsed automatically.',
  'macOS спросит пароль администратора — LogAtlas его не видит.': 'macOS will ask for the administrator password — LogAtlas never sees it.',
  'ваша учётная запись': 'your account', 'журнал контейнера: docker compose logs logatlas': 'container log: docker compose logs logatlas',
  'Хост удалён из панели; на самом хосте ничего не изменилось': 'Host removed from the panel; nothing changed on the host itself',
  'событий — введите запрос в строке поиска': 'events — type a search query',
  'Все события получили действие.': 'Every event got an action.', 'Событий много — показаны только редкие действия.': 'Many events — only rare actions are shown.',
  'Всё время': 'All time', '24 часа': '24 hours', '7 дней': '7 days',
  '1 сутки': '1 day', '7 суток': '7 days', 'ночь': 'night',

  // месяцы и дни недели
  'янв': 'Jan', 'фев': 'Feb', 'мар': 'Mar', 'апр': 'Apr', 'мая': 'May', 'июн': 'Jun', 'июл': 'Jul',
  'авг': 'Aug', 'сен': 'Sep', 'окт': 'Oct', 'ноя': 'Nov', 'дек': 'Dec',
  'января': 'January', 'февраля': 'February', 'марта': 'March', 'апреля': 'April', 'июня': 'June',
  'июля': 'July', 'августа': 'August', 'сентября': 'September', 'октября': 'October', 'ноября': 'November',
  'декабря': 'December',
  'пн': 'Mon', 'вт': 'Tue', 'ср': 'Wed', 'чт': 'Thu', 'пт': 'Fri', 'сб': 'Sat', 'вс': 'Sun',
  'понедельник': 'Monday', 'вторник': 'Tuesday', 'среда': 'Wednesday', 'четверг': 'Thursday',
  'пятница': 'Friday', 'суббота': 'Saturday', 'воскресенье': 'Sunday',

  // единицы и склонения
  'Б': 'B', 'КБ': 'KB', 'МБ': 'MB', 'ГБ': 'GB', 'ТБ': 'TB',
  '1 с': '1 s', '5 с': '5 s', '15 с': '15 s', '30 с': '30 s',
  '1 мин': '1 min', '5 мин': '5 min', '10 мин': '10 min', '15 мин': '15 min', '30 мин': '30 min',
  '1 ч': '1 h', '3 ч': '3 h', '6 ч': '6 h', '12 ч': '12 h',
  'хост': 'host', 'хосты': 'hosts', 'источника': 'source', 'источников': 'sources', 'файл': 'file',
  'файлов': 'files', 'совпадений': 'matches', 'событие': 'event', 'события': 'events', 'сбор': 'case',
  'сбора': 'cases', 'сборов': 'cases', 'процессов': 'processes', 'объектов': 'objects',
  'категории': 'categories', 'категориям': 'categories', 'события отнесены': 'events matched',
  'событие отнесено': 'event matched', 'событий отнесены': 'events matched', 'Событий': 'Events',
};

let LANG = 'ru';
try { LANG = localStorage.getItem('logatlas.lang') || 'ru'; } catch (e) { /* */ }

export function getLang() { return LANG; }

export function setLang(lang) {
  LANG = lang === 'en' ? 'en' : 'ru';
  try { localStorage.setItem('logatlas.lang', LANG); } catch (e) { /* */ }
  document.documentElement.lang = LANG;
}

// t('Обзор') → 'Overview' (en) / 'Обзор' (ru); t('{n} событий', { n }) → параметрическая подстановка.
export function t(s, ...params) {
  const base = LANG === 'en' ? (Object.prototype.hasOwnProperty.call(EN, s) ? EN[s] : s) : s;
  if (!params.length) return base;
  const p = params.length === 1 && params[0] && typeof params[0] === 'object' ? params[0] : params;
  let i = 0;
  return String(base).replace(/\{([a-zA-Z0-9_]*)\}/g, (m, k) => {
    if (k === '') { const v = Array.isArray(p) ? p[i++] : undefined; return v == null ? '' : v; }
    const v = Array.isArray(p) ? undefined : p[k];
    return v == null ? m : v;
  });
}

// Перевод уже отрендеренного DOM по точному словарю: покрывает строки, не обёрнутые в t().
// Не трогает input.value и не переводит текст, содержащий параметры (числа/имена) — их переводят
// явные вызовы t(...) в шаблонах.
export function translateDOM(root) {
  if (LANG !== 'en' || !root || !root.querySelectorAll) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((n) => {
    const v = n.nodeValue;
    if (!v) return;
    const k = v.trim();
    if (k && Object.prototype.hasOwnProperty.call(EN, k)) n.nodeValue = v.replace(k, EN[k]);
  });
  root.querySelectorAll('[placeholder],[title],[aria-label]').forEach((el) => {
    ['placeholder', 'title', 'aria-label'].forEach((a) => {
      const v = el.getAttribute(a);
      if (!v) return;
      const k = v.trim();
      if (k && Object.prototype.hasOwnProperty.call(EN, k)) el.setAttribute(a, EN[k]);
    });
  });
}

export function applyLang(lang) {
  setLang(lang);
  // api.js читает язык через getLang() на каждый запрос, поэтому достаточно перерисовать текущий экран.
  if (typeof window !== 'undefined' && window.__laRerender) window.__laRerender();
}

export default { getLang, setLang, t, applyLang, translateDOM };
