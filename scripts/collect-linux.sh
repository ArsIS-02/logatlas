#!/bin/bash
# Сбор журналов Linux (РЕД ОС, RHEL, Debian, Ubuntu) для LogAtlas.
#
#   sudo bash collect-linux.sh            диалог: какие журналы, за какой период (по часам этого хоста), отправка в LogAtlas
#   sudo bash collect-linux.sh 3          без вопросов: журналы по умолчанию за 3 дня, архив в текущей папке
#
# Параметры (для запуска без диалога или из collect-linux-remote):
#   --since ВРЕМЯ  --until ВРЕМЯ   период: "2026-10-07 14:00", "07.10.2026", "3d", "12h", "today", "yesterday", "now"
#   --sources a,b,c                журналы (идентификаторы — в первой колонке списка), all — все найденные
#   --upload URL                   адрес LogAtlas, например http://192.168.1.10:8484
#   --outdir ПАПКА                 куда положить архив
#   --yes                          не задавать вопросов
#   --client                       режим для collect-linux-remote: архив в ~/logatlas-collect пользователя sudo,
#                                  коды выхода 0 — отправлено, 10 — архив готов, но не отправлен, 11 — отменено, 12 — нет записей
#   --scan-json                    для веб-панели LogAtlas: найденные журналы в JSON (stdout)
#   --stream                       для веб-панели LogAtlas: архив tar.gz в stdout, ход сбора — строки LOGATLAS-PROGRESS в stderr
set -uo pipefail
shopt -s nullglob

# ------------------------------------------------------------------ оформление
if [ -t 1 ]; then
  B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else
  B=''; D=''; G=''; Y=''; R=''; C=''; N=''
fi
# ширина колонок считается в символах — нужна UTF-8 локаль для LC_CTYPE
unset LC_ALL
for l in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8 ru_RU.UTF-8 ru_RU.utf8; do
  if locale -a 2>/dev/null | grep -qx "$l"; then export LC_CTYPE="$l"; break; fi
done
pad()  { local s="$1" w="$2"; printf '%s%*s' "$s" $(( w > ${#s} ? w - ${#s} : 0 )) ''; }
say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "${Y}$*${N}"; }
die()  { printf '%s\n' "${R}$*${N}" >&2; exit "${2:-1}"; }
ask()  { local __v; read -r -p "$1" __v </dev/tty || __v=''; printf '%s' "$__v"; }

# ------------------------------------------------------------------ параметры
SINCE_IN=''; UNTIL_IN=''; SOURCES_IN=''; UPLOAD="${LOGATLAS_URL:-}"; OUTDIR=''; YES=0; CLIENT=0; LEGACY_DAYS=''; MODE=''
while [ $# -gt 0 ]; do
  case "$1" in
    --scan-json) MODE=scan; YES=1 ;;
    --stream)  MODE=stream; YES=1; UPLOAD='' ;;
    --since)   SINCE_IN="${2:-}"; shift ;;
    --until)   UNTIL_IN="${2:-}"; shift ;;
    --sources) SOURCES_IN="${2:-}"; shift ;;
    --upload)  UPLOAD="${2:-}"; shift ;;
    --outdir)  OUTDIR="${2:-}"; shift ;;
    --yes|-y)  YES=1 ;;
    --client)  CLIENT=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    [0-9]*)    LEGACY_DAYS="$1"; YES=1 ;;
    *) die "Неизвестный параметр: $1 (см. --help)" ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || die "Нужны права root: sudo bash $0"
[ -t 0 ] || [ -e /dev/tty ] || YES=1
# для веб-панели: данные идут в fd 3 (исходный stdout), всё остальное — в stderr, без цветов
if [ -n "$MODE" ]; then
  exec 3>&1 1>&2
  B=''; D=''; G=''; Y=''; R=''; C=''; N=''
fi
progress() { [ -n "$MODE" ] && printf 'LOGATLAS-PROGRESS %s\n' "$*" >&2; return 0; }

PY=''
for p in python3 /usr/libexec/platform-python; do
  if command -v "$p" >/dev/null 2>&1; then PY="$(command -v "$p")"; break; fi
done

HOST="$(hostname -s 2>/dev/null || hostname)"
TZNAME="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
[ -n "$TZNAME" ] || TZNAME="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')"
OFF="$(date +%z)"                                   # +0500
OFFMIN=$(( (10#${OFF:1:2} * 60 + 10#${OFF:3:2}) * (${OFF:0:1}1) ))
OFFTXT="UTC${OFF:0:3}:${OFF:3:2}"

fmt()  { date -d "@$1" '+%Y-%m-%d %H:%M'; }
fmtd() { date -d "@$1" '+%d.%m.%Y %H:%M'; }

# ------------------------------------------------------------------ вспомогательный разборщик (python)
HELPER="$(mktemp /tmp/logatlas-helper.XXXXXX)"
WORK=''
cleanup() { rm -f "$HELPER"; [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup EXIT
trap 'echo; warn "Прервано."; exit 11' INT TERM

cat > "$HELPER" <<'PYEOF'
import sys, os, re, gzip, time, calendar, json

MON = {m: i for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}
AUD = re.compile(rb'msg=audit\((\d{9,11})\.\d+:')
CLS = re.compile(rb'^([A-Z][a-z]{2}) {1,2}(\d{1,2}) (\d\d):(\d\d):(\d\d)')
ISO = re.compile(rb'(\d{4})-(\d\d)-(\d\d)(?:T| +)(\d\d):(\d\d):(\d\d)(?:[.,]\d+)?(Z|[+-]\d\d:?\d\d)?')
ACC = re.compile(rb'\[(\d\d)/([A-Z][a-z]{2})/(\d{4}):(\d\d):(\d\d):(\d\d) ([+-]\d\d)(\d\d)\]')
CTM = re.compile(rb'(?:[A-Z][a-z]{2}) ([A-Z][a-z]{2}) {1,2}(\d{1,2}) (\d\d):(\d\d):(\d\d)(?:\.\d+)? (\d{4})')


def local(y, mo, d, h, mi, s):
    try:
        return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    except (OverflowError, ValueError):
        return None


class Clock:
    """Время строки в секундах эпохи; для syslog без года год берётся по дате изменения файла."""

    def __init__(self, mtime):
        r = time.localtime(mtime)
        self.y, self.mo, self.d = r.tm_year, r.tm_mon, r.tm_mday

    def year(self, mo, d):
        return self.y - 1 if (mo, d) > (self.mo, self.d + 1) else self.y

    def ts(self, line):
        if b'msg=audit(' in line:
            m = AUD.search(line)
            if m:
                return float(m.group(1))
        m = CLS.match(line)
        if m and m.group(1).decode() in MON:
            mo = MON[m.group(1).decode()]
            d = int(m.group(2))
            return local(self.year(mo, d), mo, d, int(m.group(3)), int(m.group(4)), int(m.group(5)))
        head = line[:300]
        m = ISO.search(head)
        if m:
            y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
            z = m.group(7)
            if not z:
                return local(y, mo, d, h, mi, s)
            try:
                t = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
            except (OverflowError, ValueError):
                return None
            if z == b'Z':
                return t
            z = z.replace(b':', b'')
            off = (int(z[1:3]) * 60 + int(z[3:5])) * 60
            return t - off if z[:1] == b'+' else t + off
        m = ACC.search(head)
        if m and m.group(2).decode() in MON:
            try:
                t = calendar.timegm((int(m.group(3)), MON[m.group(2).decode()], int(m.group(1)),
                                     int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, 0))
            except (OverflowError, ValueError):
                return None
            off = (abs(int(m.group(7))) * 60 + int(m.group(8))) * 60
            return t - off if m.group(7)[:1] == b'+' else t + off
        m = CTM.search(head)
        if m and m.group(1).decode() in MON:
            return local(int(m.group(6)), MON[m.group(1).decode()], int(m.group(2)),
                         int(m.group(3)), int(m.group(4)), int(m.group(5)))
        return None


def opener(p):
    return gzip.open(p, 'rb') if p.endswith('.gz') else open(p, 'rb')


def stem(p):
    s = re.sub(r'\.gz$', '', os.path.basename(p))
    return re.sub(r'(-\d{8}|\.\d+)$', '', s)


def out_name(p):
    d = os.path.dirname(p)
    s = stem(p)
    if d.startswith('/var/ossec'):
        return 'wazuh_' + s
    rel = os.path.relpath(d, '/var/log')
    if rel in ('.', 'audit') or rel.startswith('..'):
        return s
    return rel.replace('/', '_') + '_' + s


def groups(paths):
    g = {}
    for p in paths:
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        g.setdefault(out_name(p), []).append((mt, p))
    for k in g:
        g[k].sort()
    return g


def first_ts(p, mt):
    c = Clock(mt)
    try:
        with opener(p) as f:
            for i, line in enumerate(f):
                t = c.ts(line)
                if t:
                    return t
                if i > 300:
                    break
    except (OSError, EOFError):
        pass
    return None


def last_ts(p, mt):
    c = Clock(mt)
    try:
        if p.endswith('.gz'):
            last = None
            with opener(p) as f:
                for line in f:
                    t = c.ts(line)
                    if t:
                        last = t
            return last
        with open(p, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            data = f.read()
        for line in reversed(data.splitlines()):
            t = c.ts(line)
            if t:
                return t
    except (OSError, EOFError):
        pass
    return None


def read_input():
    by = {}
    for line in sys.stdin:
        line = line.rstrip('\n')
        if '\t' in line:
            sid, p = line.split('\t', 1)
            by.setdefault(sid, []).append(p)
    return by


def scan():
    for sid, paths in read_input().items():
        lo = hi = None
        for name, files in groups(paths).items():
            mt0, p0 = files[0]
            mt1, p1 = files[-1]
            a = first_ts(p0, mt0)
            b = last_ts(p1, mt1)
            if a and (lo is None or a < lo):
                lo = a
            if b and (hi is None or b > hi):
                hi = b
        print('%s\t%s\t%s' % (sid, int(lo) if lo else '', int(hi) if hi else ''))


def filt(since, until, outdir):
    total_lines = total_bytes = 0
    for sid, paths in read_input().items():
        for name, files in groups(paths).items():
            dst = os.path.join(outdir, name)
            n = 0
            newest = files[-1][0]
            with open(dst, 'wb') as out:
                for mt, p in files:
                    if mt < since - 120:
                        continue                      # файл закончился раньше начала периода
                    c = Clock(mt)
                    keep = False
                    try:
                        with opener(p) as f:
                            for line in f:
                                t = c.ts(line)
                                if t is not None:
                                    keep = since <= t < until
                                if keep:
                                    out.write(line)
                                    n += 1
                    except (OSError, EOFError) as e:
                        sys.stderr.write('%s: %s\n' % (p, e))
            if n == 0:
                os.remove(dst)
                continue
            os.utime(dst, (newest, newest))
            total_lines += n
            total_bytes += os.path.getsize(dst)
    print(json.dumps({'lines': total_lines, 'bytes': total_bytes}))


if __name__ == '__main__':
    if sys.argv[1] == 'scan':
        scan()
    else:
        filt(float(sys.argv[2]), float(sys.argv[3]), sys.argv[4])
PYEOF

# ------------------------------------------------------------------ какие журналы бывают
IDS=(); declare -A LABEL DESC DEF GLOB AVAIL FILES SIZE FIRST LAST SEL
src() { IDS+=("$1"); LABEL[$1]="$2"; DEF[$1]="$3"; DESC[$1]="$4"; GLOB[$1]="${5:-}"; }
src journal  'journald'          1 'системный журнал systemd: службы, sshd, sudo, ядро'
src audit    'auditd'            1 'аудит: входы, команды, изменения файлов и учёток' '/var/log/audit/audit.log*'
src secure   'Аутентификация'    1 'secure / auth.log: ssh, sudo, su, PAM'              '/var/log/secure* /var/log/auth.log*'
src messages 'Системный syslog'  0 'messages / syslog (обычно дублирует journald)'      '/var/log/messages* /var/log/syslog*'
src cron     'Cron'              0 'запуск заданий по расписанию'                       '/var/log/cron*'
src pkg      'Пакеты'            1 'установка и удаление пакетов (dnf, yum, apt)'        '/var/log/dnf.log* /var/log/dnf.rpm.log* /var/log/yum.log* /var/log/apt/history.log* /var/log/dpkg.log*'
src kern     'Ядро'              0 'kern.log'                                           '/var/log/kern.log*'
src mail     'Почта'             0 'maillog / mail.log'                                 '/var/log/maillog* /var/log/mail.log*'
src fail2ban 'fail2ban'          1 'блокировки адресов'                                 '/var/log/fail2ban.log*'
src firewall 'firewalld'         0 'сообщения firewalld'                                '/var/log/firewalld*'
src web      'Веб-серверы'       0 'журналы nginx и Apache'                             '/var/log/nginx/*.log* /var/log/httpd/*log* /var/log/apache2/*.log*'
src wtmp     'Входы (last)'      1 'успешные и неудачные входы из wtmp/btmp'
src wazuh    'Wazuh alerts'      0 'alerts.json сервера Wazuh (может быть большим)'     '/var/ossec/logs/alerts/alerts.json /var/ossec/logs/alerts/*/*/*/ossec-alerts-*.json*'

human() {  # байты → 12,3 МБ
  awk -v b="${1:-0}" 'BEGIN{ split("Б КБ МБ ГБ ТБ",u," "); i=1; while (b>=1024 && i<5) { b/=1024; i++ }
    s = (i==1) ? sprintf("%d %s", b, u[i]) : sprintf("%.1f %s", b, u[i]); gsub(/\./, ",", s); print s }'
}

discover() {
  local id f files list size
  list="$(mktemp /tmp/logatlas-list.XXXXXX)"
  for id in "${IDS[@]}"; do
    AVAIL[$id]=0; SIZE[$id]=0; FIRST[$id]=''; LAST[$id]=''; FILES[$id]=''
    case "$id" in
      journal)
        if command -v journalctl >/dev/null 2>&1 && journalctl -q -n1 --no-pager 2>/dev/null | grep -q .; then
          AVAIL[$id]=1
          SIZE[$id]=$(du -scb /var/log/journal /run/log/journal 2>/dev/null | tail -1 | cut -f1)
          FIRST[$id]=$(journalctl -q --no-pager -o short-unix 2>/dev/null | head -n1 | cut -d. -f1)
          LAST[$id]=$(journalctl -q --no-pager -o short-unix -n1 -r 2>/dev/null | head -n1 | cut -d. -f1)
        fi ;;
      wtmp)
        if command -v last >/dev/null 2>&1 && { [ -s /var/log/wtmp ] || [ -s /var/log/btmp ]; }; then
          AVAIL[$id]=1
          SIZE[$id]=$(du -cb /var/log/wtmp /var/log/btmp 2>/dev/null | tail -1 | cut -f1)
        fi ;;
      *)
        files=()
        for f in ${GLOB[$id]}; do [ -f "$f" ] && [ -s "$f" ] && files+=("$f"); done
        if [ ${#files[@]} -gt 0 ]; then
          AVAIL[$id]=1
          FILES[$id]="$(printf '%s\n' "${files[@]}")"
          SIZE[$id]=$(du -cb "${files[@]}" 2>/dev/null | tail -1 | cut -f1)
          for f in "${files[@]}"; do printf '%s\t%s\n' "$id" "$f"; done >> "$list"
        fi ;;
    esac
  done
  if [ -n "$PY" ] && [ -s "$list" ]; then
    while IFS=$'\t' read -r id a b; do FIRST[$id]="$a"; LAST[$id]="$b"; done < <("$PY" "$HELPER" scan < "$list" 2>/dev/null)
  fi
  rm -f "$list"
}

AVAIL_IDS=()
show_menu() {
  local i=0 id mark period
  say ""
  say "${B}Журналы на ${HOST}${N}  ${D}(время хоста: $(date '+%Y-%m-%d %H:%M') ${OFFTXT}${TZNAME:+, $TZNAME})${N}"
  say "${D}   №      $(pad 'Журнал' 18) $(pad 'Размер' 10)  $(pad 'Записи в журнале' 35) Что внутри${N}"
  for id in "${AVAIL_IDS[@]}"; do
    i=$((i + 1))
    if [ "${SEL[$id]}" = 1 ]; then mark="${G}[x]${N}"; else mark='[ ]'; fi
    if [ -n "${FIRST[$id]}" ] && [ -n "${LAST[$id]}" ]; then period="$(fmt "${FIRST[$id]}") — $(fmt "${LAST[$id]}")"; else period='—'; fi
    printf "  %2d  %s %s %10s  %s ${D}%s${N}\n" "$i" "$mark" "$(pad "${LABEL[$id]}" 18)" "$(human "${SIZE[$id]}")" "$(pad "$period" 35)" "${DESC[$id]}"
  done
}

choose_sources() {
  local a n id
  while true; do
    show_menu
    say ""
    a="$(ask "Номера через пробел — включить/выключить, a — все, 0 — ни одного, Enter — продолжить: ")"
    [ -z "$a" ] && break
    for n in $a; do
      case "$n" in
        a|A|все) for id in "${AVAIL_IDS[@]}"; do SEL[$id]=1; done ;;
        0)       for id in "${AVAIL_IDS[@]}"; do SEL[$id]=0; done ;;
        *) if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le ${#AVAIL_IDS[@]} ]; then
             id="${AVAIL_IDS[$((n - 1))]}"; if [ "${SEL[$id]}" = 1 ]; then SEL[$id]=0; else SEL[$id]=1; fi
           else warn "  не понял: $n"; fi ;;
      esac
    done
  done
}

# Время в секунды эпохи по часовому поясу хоста
parse_time() {
  local s="$1" t
  s="$(printf '%s' "$s" | sed 's/^ *//; s/ *$//')"
  if [[ "$s" =~ ^@[0-9]{9,11}$ ]]; then printf '%s\n' "${s#@}"; return; fi
  case "$s" in
    now|сейчас)        date +%s; return ;;
    today|сегодня)     date -d 'today 00:00' +%s; return ;;
    yesterday|вчера)   date -d 'yesterday 00:00' +%s; return ;;
  esac
  if [[ "$s" =~ ^([0-9]+)(d|д|h|ч|m|м)$ ]]; then
    case "${BASH_REMATCH[2]}" in d|д) t=days ;; h|ч) t=hours ;; *) t=minutes ;; esac
    date -d "${BASH_REMATCH[1]} $t ago" +%s; return
  fi
  if [[ "$s" =~ ^([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})(.*)$ ]]; then
    s="${BASH_REMATCH[3]}-${BASH_REMATCH[2]}-${BASH_REMATCH[1]}${BASH_REMATCH[4]}"
  fi
  [[ "$s" =~ ^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}([ T][0-9]{1,2}:[0-9]{2}(:[0-9]{2})?)?$ ]] || return 1
  date -d "${s/T/ }" +%s 2>/dev/null
}

dur() {
  local s=$(( $1 )) d h m
  d=$((s / 86400)); h=$((s % 86400 / 3600)); m=$((s % 3600 / 60))
  if [ $d -gt 0 ]; then printf '%d дн %d ч' $d $h; elif [ $h -gt 0 ]; then printf '%d ч %d мин' $h $m; else printf '%d мин' $m; fi
}

choose_period() {
  local a def_s
  def_s=$(date -d '3 days ago' +%s)
  say ""
  say "${B}Период сбора${N} — по часам хоста ${HOST} (${OFFTXT}${TZNAME:+, $TZNAME}); сейчас там $(date '+%Y-%m-%d %H:%M')."
  say "${D}  Форматы: 2026-10-07 14:00 · 07.10.2026 14:00 · 2026-10-07 · 3d (3 дня назад) · 12h · today · yesterday · now${N}"
  while true; do
    a="$(ask "  С какого времени [$(fmt "$def_s")]: ")"
    if [ -z "$a" ]; then SINCE=$def_s; break; fi
    SINCE=$(parse_time "$a") && [ -n "$SINCE" ] && break
    warn "  не понял время: $a"
  done
  while true; do
    a="$(ask "  По какое время    [сейчас]: ")"
    if [ -z "$a" ]; then UNTIL=$(date +%s); break; fi
    UNTIL=$(parse_time "$a") || { warn "  не понял время: $a"; continue; }
    # дата без времени в поле «по» — до конца этого дня
    [[ "$a" =~ ^[0-9.:-]+$ && ! "$a" =~ : ]] && UNTIL=$((UNTIL + 86399))
    [ "$UNTIL" -gt "$SINCE" ] && break
    warn "  конец периода должен быть позже начала ($(fmt "$SINCE"))"
  done
}

# ------------------------------------------------------------------ 1. что есть на хосте
say "${B}LogAtlas — сбор журналов Linux${N}"
say "${D}Поиск журналов…${N}"
discover
for id in "${IDS[@]}"; do [ "${AVAIL[$id]}" = 1 ] && AVAIL_IDS+=("$id") && SEL[$id]="${DEF[$id]}"; done
# messages включаем по умолчанию, только если нет journald
[ "${AVAIL[journal]}" = 1 ] || { [ "${AVAIL[messages]}" = 1 ] && SEL[messages]=1; }

if [ "$MODE" = scan ]; then
  jstr() { local v="${1//\\/\\\\}"; v="${v//\"/\\\"}"; printf '"%s"' "$v"; }
  {
    printf '{"os":"linux","host":%s,"tz_offset_min":%d,"tz_name":%s,"now":%d,"python":%s,"sources":[' \
      "$(jstr "$HOST")" "$OFFMIN" "$(jstr "$TZNAME")" "$(date +%s)" "$([ -n "$PY" ] && echo true || echo false)"
    first=1
    for id in "${AVAIL_IDS[@]}"; do
      [ $first = 1 ] || printf ','; first=0
      printf '{"id":%s,"label":%s,"desc":%s,"size":%d,"first":%s,"last":%s,"default":%s}' \
        "$(jstr "$id")" "$(jstr "${LABEL[$id]}")" "$(jstr "${DESC[$id]}")" "${SIZE[$id]:-0}" \
        "${FIRST[$id]:-null}" "${LAST[$id]:-null}" "$([ "${SEL[$id]}" = 1 ] && echo true || echo false)"
    done
    printf ']}\n'
  } >&3
  exit 0
fi
[ ${#AVAIL_IDS[@]} -gt 0 ] || die "На хосте не найдено ни одного известного журнала." 12

if [ -n "$SOURCES_IN" ]; then
  for id in "${AVAIL_IDS[@]}"; do SEL[$id]=0; done
  for id in ${SOURCES_IN//,/ }; do
    if [ "$id" = all ]; then for x in "${AVAIL_IDS[@]}"; do SEL[$x]=1; done
    elif [ "${AVAIL[$id]:-0}" = 1 ]; then SEL[$id]=1
    else warn "Журнал «${id}» на хосте не найден — пропускаю"; fi
  done
fi

# ------------------------------------------------------------------ 2. выбор и период
if [ -n "$LEGACY_DAYS" ]; then
  SINCE=$(date -d "$LEGACY_DAYS days ago" +%s); UNTIL=$(date +%s)
elif [ $YES = 1 ]; then
  SINCE=$(parse_time "${SINCE_IN:-3d}") || die "Не понял --since: $SINCE_IN"
  UNTIL=$(parse_time "${UNTIL_IN:-now}") || die "Не понял --until: $UNTIL_IN"
  [ "$UNTIL" -gt "$SINCE" ] || die "--until должен быть позже --since"
else
  choose_sources
  if [ -n "$SINCE_IN" ]; then SINCE=$(parse_time "$SINCE_IN") || die "Не понял --since"; UNTIL=$(parse_time "${UNTIL_IN:-now}") || die "Не понял --until"
  else choose_period; fi
fi

CHOSEN=(); for id in "${AVAIL_IDS[@]}"; do [ "${SEL[$id]}" = 1 ] && CHOSEN+=("$id"); done
if [ ${#CHOSEN[@]} -eq 0 ]; then warn "Не выбрано ни одного журнала."; exit 11; fi

# куда класть архив
if [ "$MODE" = stream ]; then
  OUTDIR=/var/tmp
elif [ -z "$OUTDIR" ]; then
  h="${HOME:-/root}"
  [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && h="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  if [ $CLIENT = 1 ] && [ -n "$h" ] && [ -d "$h" ]; then
    OUTDIR="$h/logatlas-collect"
  elif [ $CLIENT = 1 ]; then
    OUTDIR=/var/tmp/logatlas-collect
  else
    OUTDIR="$PWD"
  fi
fi
[ "$MODE" = stream ] || mkdir -p "$OUTDIR" || die "Не удалось создать папку $OUTDIR"
ARCHIVE="$OUTDIR/${HOST}-logs-$(date -d "@$SINCE" +%Y%m%d-%H%M)_$(date -d "@$UNTIL" +%Y%m%d-%H%M).tar.gz"
CASE="${HOST} · $(fmtd "$SINCE") – $(fmtd "$UNTIL") (${OFFTXT})"

est=0; for id in "${CHOSEN[@]}"; do est=$((est + ${SIZE[$id]:-0})); done
free=$(df -Pk /var/tmp | awk 'NR==2{print $4 * 1024}')

say ""
say "${B}Итого${N}"
say "  Хост:     ${HOST}"
say "  Период:   $(fmt "$SINCE") — $(fmt "$UNTIL") ${OFFTXT} ($(dur $((UNTIL - SINCE))))"
say "  Журналы:  $(for id in "${CHOSEN[@]}"; do printf '%s, ' "${LABEL[$id]}"; done | sed 's/, $//')"
say "  Архив:    ${ARCHIVE}"
if [ -n "$UPLOAD" ]; then say "  Отправка: ${UPLOAD%/} (LogAtlas)"; else say "  Отправка: нет — архив останется на хосте"; fi
[ -n "$free" ] && [ "$est" -gt "$free" ] && warn "  Внимание: в /var/tmp свободно $(human "$free"), а журналы занимают до $(human "$est")."
if [ $YES = 0 ]; then
  a="$(ask "Начать сбор? [Y/n]: ")"
  case "$a" in n|N|no|нет|н) say "Отменено."; exit 11 ;; esac
fi

# ------------------------------------------------------------------ 3. сбор
WORK="$(mktemp -d /var/tmp/logatlas.XXXXXX)"
say ""
say "${B}Сбор${N}  ${D}(временная папка ${WORK})${N}"
i=0; got=0
for id in "${CHOSEN[@]}"; do
  i=$((i + 1))
  progress "$i" "${#CHOSEN[@]}" "${LABEL[$id]}"
  printf "  [%d/%d] %s " "$i" "${#CHOSEN[@]}" "$(pad "${LABEL[$id]}" 18)"
  dir="$WORK/$id"; mkdir -p "$dir"
  case "$id" in
    journal)
      journalctl --since "$(date -d "@$SINCE" '+%Y-%m-%d %H:%M:%S')" --until "$(date -d "@$UNTIL" '+%Y-%m-%d %H:%M:%S')" -o json --no-pager > "$dir/journal.json" 2>/dev/null ;;
    wtmp)
      s="$(date -d "@$SINCE" '+%Y-%m-%d %H:%M:%S')"; u="$(date -d "@$UNTIL" '+%Y-%m-%d %H:%M:%S')"
      for kind in last lastb; do
        f=/var/log/wtmp; [ $kind = lastb ] && f=/var/log/btmp
        [ -s "$f" ] || continue
        { last -F -w --time-format iso -s "$s" -t "$u" -f "$f" 2>/dev/null || last -F -w -s "$s" -t "$u" -f "$f" 2>/dev/null; } \
          | grep -Ev '^(wtmp|btmp) begins|^$' > "$dir/$kind.txt" || true
      done ;;
    *)
      if [ -n "$PY" ]; then
        printf '%s\n' "${FILES[$id]}" | sed "s/^/$id\t/" | "$PY" "$HELPER" filter "$SINCE" "$UNTIL" "$dir" >/dev/null 2>"$dir/.err" || true
        rm -f "$dir/.err"
      else
        # без python: целиком файлы, изменявшиеся за период (лишние строки LogAtlas покажет вне диапазона)
        while IFS= read -r f; do
          [ -n "$f" ] && [ "$(stat -c %Y "$f")" -ge "$SINCE" ] && cp -p "$f" "$dir/" 2>/dev/null
        done <<< "${FILES[$id]}"
      fi ;;
  esac
  find "$dir" -type f -empty -delete 2>/dev/null
  bytes=$(du -sb "$dir" 2>/dev/null | cut -f1); lines=$(find "$dir" -type f ! -name '*.gz' -exec cat {} + 2>/dev/null | wc -l)
  if [ -z "$(ls -A "$dir")" ]; then
    rmdir "$dir"; say "${D}нет записей за период${N}"
  else
    got=$((got + 1)); say "${G}$(human "$bytes")${N} · $(printf "%'d" "$lines" 2>/dev/null || echo "$lines") строк"
  fi
done
[ -z "$PY" ] && warn "  python3 не найден: текстовые журналы взяты целиком, без отбора по периоду."
if [ $got -eq 0 ]; then warn "За выбранный период записей нет — архив не создан."; exit 12; fi

if [ "$MODE" = stream ]; then
  progress pack
  tar -czf - -C "$WORK" . >&3 || die "Не удалось упаковать журналы"
  exit 0
fi

say ""
printf "  Упаковка… "
tar -czf "$ARCHIVE" -C "$WORK" . || die "Не удалось создать архив $ARCHIVE"
chmod 600 "$ARCHIVE"
if [ $CLIENT = 1 ]; then
  ln -sfn "$(basename "$ARCHIVE")" "$OUTDIR/last.tar.gz"
  printf 'file=%s\nname=%s\ntz=%s\n' "$(basename "$ARCHIVE")" "$CASE" "$OFFMIN" > "$OUTDIR/last.meta"
  chmod 600 "$OUTDIR/last.meta"
  if [ -n "${SUDO_USER:-}" ]; then
    chown "$SUDO_USER": "$OUTDIR" "$ARCHIVE" "$OUTDIR/last.meta" 2>/dev/null
    chown -h "$SUDO_USER": "$OUTDIR/last.tar.gz" 2>/dev/null
  fi
  find "$OUTDIR" -maxdepth 1 -name '*-logs-*.tar.gz' -mtime +7 -delete 2>/dev/null
fi
say "${G}$(human "$(stat -c %s "$ARCHIVE")")${N}"
say "  Архив: ${B}${ARCHIVE}${N}"

# ------------------------------------------------------------------ 4. отправка
if [ -z "$UPLOAD" ] && [ $YES = 0 ] && [ $CLIENT = 0 ]; then
  UPLOAD="$(ask "Адрес LogAtlas для отправки (например http://192.168.1.10:8484; Enter — не отправлять): ")"
fi
if [ -z "$UPLOAD" ]; then
  say ""; say "Загрузите архив в LogAtlas: «+ Загрузить» → выбрать файл → «Загрузить и разобрать»."
  [ $CLIENT = 1 ] && exit 10; exit 0
fi
[[ "$UPLOAD" =~ ^https?:// ]] || UPLOAD="http://$UPLOAD"
UPLOAD="${UPLOAD%/}"
command -v curl >/dev/null 2>&1 || { warn "На хосте нет curl — архив не отправлен."; [ $CLIENT = 1 ] && exit 10; exit 0; }

say ""
say "Отправка в ${UPLOAD}…"
resp="$WORK/.resp"
code=$(curl --progress-bar --connect-timeout 8 -o "$resp" -w '%{http_code}' \
  -F "files=@${ARCHIVE};type=application/gzip" --form-string "name=${CASE}" \
  -F "tz=${OFFMIN}" -F "tz_mode=fixed" -F "platform=linux" "${UPLOAD}/api/cases") || code=000
cid=$(grep -o '"id":"[^"]*"' "$resp" 2>/dev/null | head -1 | cut -d'"' -f4)
if [ "$code" = 200 ] && [ -n "$cid" ]; then
  say "${G}Отправлено.${N} Дело «${CASE}» разбирается: ${B}${UPLOAD}/#/c/${cid}/overview${N}"
  exit 0
fi
warn "Не удалось отправить (HTTP ${code}): $(head -c 300 "$resp" 2>/dev/null)"
warn "Архив остался на хосте: ${ARCHIVE}"
[ $CLIENT = 1 ] && exit 10
exit 0
