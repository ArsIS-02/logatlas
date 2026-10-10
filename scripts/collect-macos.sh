#!/bin/bash
# Сбор журналов macOS для LogAtlas.
#
#   sudo bash collect-macos.sh [дней]     архив ~/Desktop/<host>-logs-<дата>.tar.gz за N дней (по умолчанию 3)
#
# Режимы для веб-панели LogAtlas (сбор по SSH, см. «Хосты» в панели):
#   --scan-json                                   найденные журналы в JSON (stdout)
#   --stream --since @EPOCH --until @EPOCH --sources a,b   архив tar.gz в stdout, ход — строки LOGATLAS-PROGRESS в stderr
#
# Совместим с /bin/bash 3.2 из macOS.
set -uo pipefail

MODE=''; SINCE=''; UNTIL=''; SOURCES=''; DAYS=3
while [ $# -gt 0 ]; do
  case "$1" in
    --scan-json) MODE=scan ;;
    --stream)    MODE=stream ;;
    --since)     SINCE="${2:-}"; shift ;;
    --until)     UNTIL="${2:-}"; shift ;;
    --sources)   SOURCES="${2:-}"; shift ;;
    [0-9]*)      DAYS="$1" ;;
    -h|--help)   sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 1 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "Нужны права root: sudo bash $0" >&2; exit 1; }
if [ -n "$MODE" ]; then exec 3>&1 1>&2; fi
progress() { [ -n "$MODE" ] && printf 'LOGATLAS-PROGRESS %s\n' "$*" >&2; return 0; }
result()   { [ -n "$MODE" ] && printf 'LOGATLAS-RESULT %s %s\n' "$1" "$2" >&2; return 0; }   # ok|empty|err, «журнал|подробности»

HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
TZNAME="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')"
OFF="$(date +%z)"
OFFMIN=$(( (10#${OFF:1:2} * 60 + 10#${OFF:3:2}) * (${OFF:0:1}1) ))

# Фильтр убирает служебный шум (mDNSResponder, trustd, runningboard и т.п.) —
# объём падает в разы, а события входа, sudo, USB, TCC, установок сохраняются.
QUIET='NOT (process IN {"mDNSResponder","trustd","cfprefsd","gamepolicyd","mds","mds_stores","mdworker_shared","WeatherWidget","WallpaperAerialsExtension","MTLCompilerService"}) AND NOT (subsystem == "com.apple.runningboard" AND NOT (eventMessage CONTAINS "Now tracking process"))'

# id|название|по умолчанию|описание|файлы
SRC_LIST='unified|Unified log|1|журнал macOS без самых шумных подсистем: входы, sudo, USB, TCC, установки|
unified_full|Unified log полностью|0|весь журнал без фильтра шума (в разы больше)|
pmset|Питание|1|сон, пробуждение, перезагрузки (pmset -g log)|
install|Установки|1|install.log: пакеты и обновления|/var/log/install.log*
system|system.log|0|классический syslog|/var/log/system.log*
wifi|Wi-Fi|0|wifi.log|/var/log/wifi.log*
launchd|launchd|0|журнал launchd|/var/log/com.apple.xpc.launchd/launchd.log*'

files_of() {  # $1 — шаблоны через пробел
  local g f
  for g in $1; do for f in $g; do [ -f "$f" ] && [ -s "$f" ] && printf '%s\n' "$f"; done; done
}
jstr() { local v="${1//\\/\\\\}"; v="${v//\"/\\\"}"; printf '"%s"' "$v"; }
lt() { date -r "$1" '+%Y-%m-%d %H:%M:%S'; }   # эпоха → местное время хоста

# ------------------------------------------------------------------ найденные журналы
scan_json() {
  local first=1 id label def desc globs size a b list
  local tv_min='' tv_max=''
  if [ -d /var/db/diagnostics ]; then
    tv_min=$(find /var/db/diagnostics -name '*.tracev3' -exec stat -f %m {} + 2>/dev/null | sort -n | head -1)
    tv_max=$(date +%s)
  fi
  printf '{"os":"macos","version":"1.2","host":%s,"tz_offset_min":%d,"tz_name":%s,"now":%d,"sources":[' \
    "$(jstr "$HOST")" "$OFFMIN" "$(jstr "$TZNAME")" "$(date +%s)"
  while IFS='|' read -r id label def desc globs; do
    [ -n "$id" ] || continue
    size=0; a=''; b=''
    case "$id" in
      unified|unified_full)
        size=$(du -sk /var/db/diagnostics 2>/dev/null | cut -f1); size=$(( ${size:-0} * 1024 )); a="$tv_min"; b="$tv_max" ;;
      pmset) size=0 ;;
      *)
        list="$(files_of "$globs")"
        [ -n "$list" ] || continue
        size=$(printf '%s\n' "$list" | tr '\n' '\0' | xargs -0 du -ck 2>/dev/null | tail -1 | cut -f1); size=$(( ${size:-0} * 1024 ))
        a=$(printf '%s\n' "$list" | tr '\n' '\0' | xargs -0 stat -f %m 2>/dev/null | sort -n | head -1)
        b=$(printf '%s\n' "$list" | tr '\n' '\0' | xargs -0 stat -f %m 2>/dev/null | sort -n | tail -1) ;;
    esac
    [ $first = 1 ] || printf ','; first=0
    printf '{"id":%s,"label":%s,"desc":%s,"size":%d,"first":%s,"last":%s,"default":%s}' \
      "$(jstr "$id")" "$(jstr "$label")" "$(jstr "$desc")" "$size" "${a:-null}" "${b:-null}" \
      "$([ "$def" = 1 ] && echo true || echo false)"
  done <<< "$SRC_LIST"
  printf ']}\n'
}

# строки вида «2026-10-10 10:59:01…» (местное время) — отбор по первым 19 символам
iso_filter() { awk -v s="$1" -v u="$2" '{ k = substr($0, 1, 19); if (k ~ /^[0-9]{4}-[0-9][0-9]-[0-9][0-9] [0-9:]{8}$/) keep = (k >= s && k <= u); if (keep) print }'; }

# log show работает в один поток. Период режется на отрезки по числу ядер (не больше 8),
# отрезки выгружаются одновременно и сразу сжимаются gzip -1.
NPAR=$(sysctl -n hw.logicalcpu 2>/dev/null || echo 1); [ "$NPAR" -gt 8 ] && NPAR=8; [ "$NPAR" -lt 1 ] && NPAR=1

unified_slices() {  # $1 каталог, $2 имя файла, $3 — «quiet», чтобы убрать шум
  local dir="$1" base="$2" n=$NPAR step k a b cut
  [ $((UNTIL - SINCE)) -lt 3600 ] && n=1
  step=$(( (UNTIL - SINCE + n - 1) / n )); [ "$step" -lt 1 ] && step=1
  k=0
  while [ "$k" -lt "$n" ]; do
    a=$((SINCE + k * step)); b=$((a + step)); [ "$b" -gt "$UNTIL" ] && b=$UNTIL
    [ "$a" -ge "$b" ] && break
    # секунда на границе попадает в оба отрезка — из этого отрезка её убираем
    cut="$(lt "$b")"; [ "$b" -ge "$UNTIL" ] && cut="-"
    if [ "${3:-}" = quiet ]; then
      ( log show --start "$(lt "$a")" --end "$(lt "$b")" --style ndjson --info --predicate "$QUIET" 2>/dev/null \
          | grep -vF -e "\"timestamp\":\"$cut" -e "\"timestamp\" : \"$cut" \
          | gzip -1 > "$dir/$base-$(printf %02d "$k").ndjson.gz" ) &
    else
      ( log show --start "$(lt "$a")" --end "$(lt "$b")" --style ndjson --info 2>/dev/null \
          | grep -vF -e "\"timestamp\":\"$cut" -e "\"timestamp\" : \"$cut" \
          | gzip -1 > "$dir/$base-$(printf %02d "$k").ndjson.gz" ) &
    fi
    k=$((k + 1))
  done
  wait
  for f in "$dir/$base"-*.ndjson.gz; do
    [ -f "$f" ] && [ "$(gzip -dc "$f" | head -c 1 | wc -c | tr -d ' ')" = 0 ] && rm -f "$f"
  done
  return 0
}

collect_one() {  # $1 id, $2 каталог
  local id="$1" dir="$2" s u globs f
  s="$(lt "$SINCE")"; u="$(lt "$UNTIL")"
  case "$id" in
    unified)      unified_slices "$dir" unified quiet ;;
    unified_full) unified_slices "$dir" unified-full ;;
    pmset)        pmset -g log 2>/dev/null | iso_filter "$s" "$u" > "$dir/pmset.log" ;;
    install)
      for f in $(files_of "/var/log/install.log*"); do
        case "$f" in *.gz) gzip -dc "$f" ;; *.bz2) bzip2 -dc "$f" ;; *) cat "$f" ;; esac
      done | iso_filter "$s" "$u" > "$dir/install.log" ;;
    *)
      globs="$(printf '%s\n' "$SRC_LIST" | awk -F'|' -v id="$id" '$1 == id { print $5 }')"
      # формат без года — берём целиком файлы, изменявшиеся за период
      for f in $(files_of "$globs"); do
        [ "$(stat -f %m "$f")" -ge "$SINCE" ] && cp -p "$f" "$dir/" 2>/dev/null
      done ;;
  esac
  find "$dir" -type f -size 0 -delete 2>/dev/null
}

if [ "$MODE" = scan ]; then scan_json >&3; exit 0; fi

if [ "$MODE" = stream ]; then
  [[ "$SINCE" =~ ^@[0-9]{9,11}$ ]] && [[ "$UNTIL" =~ ^@[0-9]{9,11}$ ]] || { echo "Нужны --since @EPOCH и --until @EPOCH" >&2; exit 1; }
  SINCE="${SINCE#@}"; UNTIL="${UNTIL#@}"
  [ "$UNTIL" -gt "$SINCE" ] || { echo "Конец периода раньше начала" >&2; exit 1; }
  [ -n "$SOURCES" ] || SOURCES="$(printf '%s\n' "$SRC_LIST" | awk -F'|' '$3 == 1 { printf "%s,", $1 }')"
else
  UNTIL=$(date +%s); SINCE=$((UNTIL - DAYS * 86400))
  SOURCES="$(printf '%s\n' "$SRC_LIST" | awk -F'|' '$3 == 1 { printf "%s,", $1 }')"
fi

WORK="$(mktemp -d /tmp/logatlas.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
IDS=$(printf '%s' "$SOURCES" | tr ',' ' ')
n=0; for id in $IDS; do n=$((n + 1)); done
i=0; got=0
for id in $IDS; do
  printf '%s\n' "$SRC_LIST" | awk -F'|' -v id="$id" '$1 == id { f = 1 } END { exit !f }' || { echo "Неизвестный журнал: $id" >&2; continue; }
  i=$((i + 1))
  label="$(printf '%s\n' "$SRC_LIST" | awk -F'|' -v id="$id" '$1 == id { print $2 }')"
  progress "$i" "$n" "$label"
  echo "  [$i/$n] ${label}…" >&2
  mkdir -p "$WORK/$id"
  collect_one "$id" "$WORK/$id"
  if [ -n "$(ls -A "$WORK/$id")" ]; then
    got=$((got + 1))
    kb=$(du -sk "$WORK/$id" | cut -f1)
    if ls "$WORK/$id" | grep -q '\.gz$'; then result ok "${label}|${kb} КБ сжато"; else result ok "${label}|${kb} КБ"; fi
  else
    rmdir "$WORK/$id"; echo "        нет записей за период" >&2
    result empty "${label}"
  fi
done
[ $got -gt 0 ] || { echo "За выбранный период записей нет." >&2; exit 12; }

if [ "$MODE" = stream ]; then
  progress pack
  # остальные файлы — тоже сжать по отдельности и параллельно; tar без общего сжатия,
  # чтобы панель разбирала сжатые части на всех ядрах сразу
  find "$WORK" -type f ! -name '*.gz' -print0 | xargs -0 -P "$NPAR" -n 4 gzip -1 2>/dev/null
  tar -cf - -C "$WORK" . >&3
  exit 0
fi

DEST="${SUDO_USER:+/Users/$SUDO_USER}/Desktop"; DEST="${DEST:-$HOME/Desktop}"
ARCHIVE="$DEST/${HOST}-logs-$(date +%Y%m%d-%H%M).tar.gz"
tar -cf - -C "$WORK" . | gzip -1 > "$ARCHIVE"
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER" "$ARCHIVE"
echo "Готово: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
