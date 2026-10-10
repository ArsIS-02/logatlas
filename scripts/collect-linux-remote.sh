#!/bin/bash
# Сбор журналов с Linux-хоста по SSH и отправка в LogAtlas — запуск с macOS или Linux.
#
#   bash collect-linux-remote.sh [-h хост] [-u пользователь] [-l http://адрес-logatlas:8484]
#
# 1. выбираете хост (из /etc/hosts — отвечающие на ping — или вводите имя/адрес) и пользователя SSH;
# 2. collect-linux.sh передаётся на хост внутри той же SSH-команды и запускается через sudo;
# 3. на хосте — диалог: какие журналы и за какой период (время — по часам хоста);
# 4. хост сам отправляет архив в LogAtlas; если не смог — архив скачивается сюда и отправляется отсюда.
# Пароли не сохраняются. Запоминаются адрес LogAtlas и пользователь для каждого хоста: ~/.config/logatlas/linux-remote.conf
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COLLECTOR="$HERE/collect-linux.sh"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/logatlas"
CONF="$CONF_DIR/linux-remote.conf"

if [ -t 1 ]; then B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else B=''; D=''; G=''; Y=''; R=''; C=''; N=''; fi
say() { printf '%s\n' "$*"; }
ask() { local v; read -r -p "$1" v </dev/tty || v=''; printf '%s' "$v"; }
no()  { case "$1" in n|N|no|нет|н) return 0 ;; esac; return 1; }
yes() { case "$1" in y|Y|yes|да|д) return 0 ;; esac; return 1; }

HOST_ARG=''; USER_ARG=''; URL=''
while getopts 'h:u:l:' o; do
  case "$o" in h) HOST_ARG="$OPTARG" ;; u) USER_ARG="$OPTARG" ;; l) URL="$OPTARG" ;; *) sed -n '2,10p' "$0"; exit 1 ;; esac
done

[ -f "$COLLECTOR" ] || { say "${R}Не найден $COLLECTOR — он должен лежать рядом с этим скриптом.${N}"; exit 1; }

# ------------------------------------------------------------------ настройки: url=…, users=a b c, host.<имя>=пользователь
conf_get() { [ -f "$CONF" ] && grep -m1 "^$1=" "$CONF" | cut -d= -f2-; }
conf_set() {
  mkdir -p "$CONF_DIR"; touch "$CONF"; chmod 600 "$CONF"
  grep -v "^$1=" "$CONF" > "$CONF.tmp" 2>/dev/null; printf '%s=%s\n' "$1" "$2" >> "$CONF.tmp"; mv "$CONF.tmp" "$CONF"
}

say ""
say "${B}${C}LogAtlas — сбор журналов с Linux-хоста${N}"
say ""

# ------------------------------------------------------------------ адрес LogAtlas
if [ -z "$URL" ]; then
  def="$(conf_get url)"; def="${def:-${LOGATLAS_URL:-}}"
  a="$(ask "Адрес LogAtlas${def:+ [$def]}${def:- (например http://192.168.1.10:8484)}, «-» — не отправлять: ")"
  if [ "$a" = - ]; then URL=''; elif [ -n "$a" ]; then URL="$a"; else URL="$def"; fi
fi
if [ -n "$URL" ]; then
  [[ "$URL" =~ ^[a-zA-Z]+:// ]] || URL="http://$URL"
  URL="${URL%/}"
  re_url='^https?://[]A-Za-z0-9.:_/[-]+$'
  [[ "$URL" =~ $re_url ]] || { say "${R}Странный адрес LogAtlas: $URL${N}"; exit 1; }
  conf_set url "$URL"
fi

PAYLOAD="$(tr -d '\r' < "$COLLECTOR" | gzip -c | base64 | tr -d '\n')"

ping1() { if [ "$(uname)" = Darwin ]; then ping -c1 -t1 "$1"; else ping -c1 -W1 "$1"; fi >/dev/null 2>&1; }

choose_host() {
  local names=() addrs=() ok=() i n a seen=" " tmp
  while IFS='=' read -r k v; do
    [[ "$k" == host.* ]] || continue; n="${k#host.}"
    [[ "$seen" == *" $n "* ]] && continue; seen+="$n "; names+=("$n"); addrs+=("$n")
  done < <([ -f "$CONF" ] && cat "$CONF")
  while read -r a n _; do
    [[ "$a" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    [[ "$a" =~ ^(127\.|0\.) ]] && continue
    [[ "$seen" == *" $n "* || "$seen" == *" $a "* ]] && continue
    seen+="$n $a "; names+=("$n"); addrs+=("$a")
  done < <(sed 's/#.*//' /etc/hosts)
  if [ ${#names[@]} -gt 0 ]; then
    say "${D}Проверяю, какие хосты отвечают на ping…${N}" >&2
    tmp="$(mktemp -d)"
    for i in "${!names[@]}"; do ( ping1 "${addrs[$i]}" && touch "$tmp/$i" ) & done; wait
    say "" >&2
    for i in "${!names[@]}"; do
      local u st addr=''
      u="$(conf_get "host.${names[$i]}")"; [ "${addrs[$i]}" != "${names[$i]}" ] && addr="${addrs[$i]}"
      if [ -e "$tmp/$i" ]; then st='отвечает'; printf '  %2d. %-22s %-16s %-11s %s\n' $((i + 1)) "${names[$i]}" "$addr" "$st" "${u:+пользователь: $u}" >&2
      else st='нет ответа'; printf "${D}  %2d. %-22s %-16s %-11s %s${N}\n" $((i + 1)) "${names[$i]}" "$addr" "$st" "${u:+пользователь: $u}" >&2; fi
    done
    rm -rf "$tmp"
    say "" >&2
  fi
  while true; do
    a="$(ask "Номер хоста или имя/адрес: ")"
    [ -z "$a" ] && continue
    if [[ "$a" =~ ^[0-9]+$ ]] && [ "$a" -ge 1 ] && [ "$a" -le ${#names[@]} ]; then printf '%s' "${names[$((a - 1))]}"; return; fi
    re_host='^[]A-Za-z0-9._:[-]+$'
    [[ "$a" =~ $re_host ]] && { printf '%s' "$a"; return; }
    say "${Y}  не понял: $a${N}" >&2
  done
}

choose_user() {
  local h="$1" def users a i=0 menu=''
  def="$(conf_get "host.$h")"; read -r -a users <<< "$(conf_get users)"
  for u in "${users[@]}"; do i=$((i + 1)); menu+="$i) $u   "; done
  [ -n "$menu" ] && say "${D}  Пользователи: $menu${N}" >&2
  while true; do
    a="$(ask "Пользователь SSH на $h${def:+ [$def]} (номер или имя; нужен sudo): ")"
    [ -z "$a" ] && a="$def"
    if [[ "$a" =~ ^[0-9]+$ ]] && [ "$a" -ge 1 ] && [ "$a" -le ${#users[@]} ]; then a="${users[$((a - 1))]}"; fi
    [[ "$a" =~ ^[a-z_][a-z0-9_.-]*\$?$ ]] && { printf '%s' "$a"; return; }
    say "${Y}  имя пользователя: латинские буквы, цифры, «_», «-», «.»${N}" >&2
  done
}

send_from_here() {
  local h="$1" u="$2" dir arc meta file name tz mode code resp
  dir="$HOME/Downloads/logatlas-$h-$(date +%Y%m%d_%H%M%S)"; mkdir -p "$dir"
  say ""; say "Скачиваю архив с $h в $dir (может снова спросить пароль SSH)…"
  ( cd "$dir" && { scp -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=2 "$u@$h:logatlas-collect/last.*" . ||
      { [ -e last.tar.gz ] || scp -O -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=1 "$u@$h:logatlas-collect/last.*" .; }; } )
  arc="$dir/last.tar.gz"
  [ -f "$arc" ] || { say "${R}Не удалось скачать архив.${N}"; return; }
  meta="$dir/last.meta"; file=''; name="$h"; tz=''; mode=auto
  if [ -f "$meta" ]; then
    file="$(grep -m1 '^file=' "$meta" | cut -d= -f2-)"; name="$(grep -m1 '^name=' "$meta" | cut -d= -f2-)"; tz="$(grep -m1 '^tz=' "$meta" | cut -d= -f2-)"
    rm -f "$meta"
  fi
  if [[ "$file" =~ ^[A-Za-z0-9._-]+\.tar\.gz$ ]]; then mv "$arc" "$dir/$file"; arc="$dir/$file"; fi
  say "${G}Архив: $arc${N}"
  if [ -z "$URL" ]; then say "Загрузите его в LogAtlas: «+ Загрузить» → выбрать файл → «Загрузить и разобрать»."; return; fi
  [ -n "$tz" ] && mode=fixed
  say "Отправляю в $URL…"
  resp="$(mktemp)"
  code=$(curl --progress-bar --connect-timeout 8 -o "$resp" -w '%{http_code}' -F "files=@${arc};type=application/gzip" \
    --form-string "name=${name}" -F "tz=${tz:-0}" -F "tz_mode=$mode" -F platform=linux "$URL/api/cases") || code=000
  if [ "$code" = 200 ] && grep -q '"id":"' "$resp"; then
    local link; link="$URL/#/c/$(grep -o '"id":"[^"]*"' "$resp" | head -1 | cut -d'"' -f4)/overview"
    say "${G}Отправлено.${N} Дело разбирается: ${B}$link${N}"
    no "$(ask 'Открыть в браузере? [Y/n]: ')" || { command -v open >/dev/null && open "$link" || xdg-open "$link" >/dev/null 2>&1; }
  else
    say "${R}Не удалось отправить (HTTP $code): $(head -c 300 "$resp")${N}"
    say "Архив остался здесь: $arc — загрузите его в LogAtlas вручную."
  fi
  rm -f "$resp"
}

# ------------------------------------------------------------------ основной цикл
while true; do
  h="${HOST_ARG:-$(choose_host)}"
  u="${USER_ARG:-$(choose_user "$h")}"
  HOST_ARG=''; USER_ARG=''
  conf_set "host.$h" "$u"
  users="$(conf_get users)"; [[ " $users " == *" $u "* ]] || conf_set users "$(echo "$users $u" | sed 's/^ *//')"

  opts='--client'; [ -n "$URL" ] && opts+=" --upload '$URL'"
  remote="umask 077; f=\$(mktemp /tmp/logatlas-collect.XXXXXX) || exit 1; echo $PAYLOAD | base64 -d | gzip -dc > \$f || exit 1; "
  remote+="if [ \$(id -u) = 0 ]; then bash \$f $opts; else sudo bash \$f $opts; fi; rc=\$?; rm -f \$f; exit \$rc"

  say ""
  say "${C}Подключаюсь: $u@$h — сначала пароль SSH (если нет ключа), затем пароль sudo.${N}"
  say ""
  ssh -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30 -o NumberOfPasswordPrompts=2 "$u@$h" "$remote"
  rc=$?
  say ""
  case $rc in
    0)  say "${G}Готово: архив отправлен в LogAtlas — ссылка на дело выше.${N}" ;;
    10) if [ -n "$URL" ]; then say "${Y}Архив собран, но с хоста отправить его не удалось.${N}"; else say "${G}Архив собран.${N}"; fi
        if no "$(ask "Скачать архив на этот компьютер${URL:+ и отправить отсюда}? [Y/n]: ")"; then
          say "Архив лежит на хосте: ~$u/logatlas-collect/last.tar.gz"
        else send_from_here "$h" "$u"; fi ;;
    11) say "${Y}Сбор отменён.${N}" ;;
    12) say "${Y}За выбранный период записей нет — архив не создан.${N}" ;;
    255)
      say "${R}Не удалось подключиться к $h по SSH.${N}"
      say "  • неверный пароль или пользователь;"
      say "  • хост выключен или порт 22 закрыт;"
      say "  • ваш компьютер заблокирован fail2ban после неудачных попыток — на хосте: sudo fail2ban-client unban <ваш IP>;"
      say "  • сменился ключ хоста (переустановка) — убедитесь, что это ваш хост, и выполните: ssh-keygen -R $h" ;;
    *)  say "${R}Сбор завершился с ошибкой (код $rc). Подробности — в выводе выше (часто: у пользователя нет прав sudo).${N}" ;;
  esac
  say ""
  yes "$(ask 'Собрать журналы с другого хоста? [y/N]: ')" || break
  say ""
done
