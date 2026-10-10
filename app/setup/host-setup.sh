#!/bin/bash
# LogAtlas: подготовка хоста Linux или macOS к сбору журналов из веб-панели.
#
#   curl -fsSL @@BASE@@/setup/host.sh | sudo bash             подготовить
#   curl -fsSL @@BASE@@/setup/host.sh | sudo bash -s -- --remove   убрать всё, что сделал этот скрипт
#
# Что делает:
#   • создаёт системного пользователя logatlas-collect без пароля;
#   • кладёт ключ панели в его authorized_keys (файл принадлежит root) с ограничениями:
#     вход только с адреса панели, без терминала и переадресаций, единственная команда — logatlas-ssh;
#   • ставит скрипт сбора в /usr/local/libexec/logatlas и разрешает пользователю sudo только на него.
# Скрипт сбора только читает журналы.
set -euo pipefail

PANEL_HOST='@@PANEL_HOST@@'
PUBKEY='@@PUBKEY@@'
U=logatlas-collect
D=/usr/local/libexec/logatlas
SUDOERS=/etc/sudoers.d/logatlas

say()  { printf '%s\n' "$*"; }
die()  { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "запустите через sudo"
OS="$(uname -s)"
case "$OS" in Linux|Darwin) ;; *) die "поддерживаются Linux и macOS, а это $OS" ;; esac

home_of() {
  if [ "$OS" = Darwin ]; then dscl . -read "/Users/$1" NFSHomeDirectory 2>/dev/null | awk '{print $2}'
  else getent passwd "$1" | cut -d: -f6; fi
}

if [ "${1:-}" = --remove ]; then
  say "Удаляю пользователя $U, $D и ${SUDOERS}…"
  rm -f "$SUDOERS"; rm -rf "$D"
  if id "$U" >/dev/null 2>&1; then
    if [ "$OS" = Darwin ]; then sysadminctl -deleteUser "$U" >/dev/null 2>&1 || dscl . -delete "/Users/$U"
    else userdel -r "$U" 2>/dev/null || userdel "$U"; fi
  fi
  say "Готово."
  exit 0
fi

# ------------------------------------------------------------------ адрес панели для ограничения from=
PANEL_IP="${PANEL_IP:-}"
if [ -z "$PANEL_IP" ]; then
  if [[ "$PANEL_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then PANEL_IP="$PANEL_HOST"
  elif [ "$OS" = Darwin ]; then PANEL_IP="$(dscacheutil -q host -a name "$PANEL_HOST" 2>/dev/null | awk '/^ip_address:/ && !f {print $2; f=1}')"
  else PANEL_IP="$(getent ahostsv4 "$PANEL_HOST" 2>/dev/null | awk 'NR==1 {print $1}')"; fi
fi
[ -n "$PANEL_IP" ] || die "не удалось узнать IP панели $PANEL_HOST — задайте его: curl … | sudo PANEL_IP=192.168.x.x bash"
# 172.16.0.0/12 — сеть Docker: так панель ходит на собственный хост
FROM="$PANEL_IP,172.16.0.0/12,127.0.0.1,::1"

# ------------------------------------------------------------------ пользователь
if ! id "$U" >/dev/null 2>&1; then
  say "Создаю пользователя ${U}…"
  if [ "$OS" = Darwin ]; then
    # пароль никто не узнает: вход только по ключу (без head в конвейере — иначе set -o pipefail роняет скрипт)
    pw="$(openssl rand -base64 33 2>/dev/null | tr -dc 'A-Za-z0-9')"
    [ ${#pw} -ge 20 ] || pw="$(date +%s)$RANDOM$RANDOM$RANDOM"
    sysadminctl -addUser "$U" -fullName "LogAtlas collector" -shell /bin/sh -home "/Users/$U" -password "$pw" >/dev/null 2>&1 \
      || die "sysadminctl не смог создать пользователя"
    dscl . -create "/Users/$U" IsHidden 1
    createhomedir -c -u "$U" >/dev/null 2>&1 || true
  else
    useradd --system --create-home --home-dir "/var/lib/$U" --shell /bin/sh --comment "LogAtlas collector" "$U"
  fi
fi
# пароля нет совсем («*»), но учётка не «заблокирована» («!»): иначе sshd без PAM откажет и во входе по ключу
[ "$OS" = Linux ] && usermod -p '*' "$U"
H="$(home_of "$U")"
[ -n "$H" ] || die "не найден домашний каталог $U"
mkdir -p "$H"

# macOS: если «Удалённый вход» разрешён только части пользователей — добавить в группу доступа
if [ "$OS" = Darwin ] && dseditgroup -o read com.apple.access_ssh >/dev/null 2>&1; then
  dseditgroup -o edit -a "$U" -t user com.apple.access_ssh >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------ скрипты
install -d -m 755 -o 0 -g 0 "$D"
if [ "$OS" = Darwin ]; then printf '%s' '@@COLLECT_MACOS_B64@@' | base64 -D > "$D/collect.sh"
else printf '%s' '@@COLLECT_LINUX_B64@@' | base64 -d > "$D/collect.sh"; fi
cat > "$D/logatlas-ssh" <<'EOF'
#!/bin/bash
# Принудительная команда ключа панели LogAtlas: разрешены только version, scan и collect.
set -f
read -r -a a <<< "${SSH_ORIGINAL_COMMAND:-}"
C=/usr/local/libexec/logatlas/collect.sh
case "${a[0]:-}" in
  version) [ ${#a[@]} -eq 1 ] && { echo "logatlas-unix @@VERSION@@"; exit 0; } ;;
  scan)    [ ${#a[@]} -eq 1 ] && exec sudo -n "$C" --scan-json ;;
  collect)
    if [ ${#a[@]} -eq 4 ] && [[ "${a[1]}" =~ ^[0-9]{9,11}$ ]] && [[ "${a[2]}" =~ ^[0-9]{9,11}$ ]] && [[ "${a[3]}" =~ ^[a-z0-9_,-]+$ ]]; then
      exec sudo -n "$C" --stream --since "@${a[1]}" --until "@${a[2]}" --sources "${a[3]}"
    fi ;;
esac
echo "LogAtlas: разрешены только команды version, scan, collect" >&2
exit 1
EOF
chown 0:0 "$D/collect.sh" "$D/logatlas-ssh"
chmod 755 "$D/collect.sh" "$D/logatlas-ssh"

# ------------------------------------------------------------------ ключ панели
install -d -m 755 -o 0 -g 0 "$H/.ssh"
printf 'restrict,from="%s",command="%s/logatlas-ssh" %s\n' "$FROM" "$D" "$PUBKEY" > "$H/.ssh/authorized_keys"
chown 0:0 "$H/.ssh/authorized_keys"
chmod 644 "$H/.ssh/authorized_keys"
command -v restorecon >/dev/null 2>&1 && restorecon -R "$H/.ssh" 2>/dev/null || true

# ------------------------------------------------------------------ sudo только на скрипт сбора
tmp="$(mktemp)"
printf 'Defaults:%s !requiretty\n%s ALL=(root) NOPASSWD: %s/collect.sh\n' "$U" "$U" "$D" > "$tmp"
visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "visudo отверг правило sudo"; }
install -m 440 -o 0 -g 0 "$tmp" "$SUDOERS"
rm -f "$tmp"

# ------------------------------------------------------------------ проверка
say "Проверяю…"
if [ "$OS" = Darwin ]; then
  launchctl print system/com.openssh.sshd >/dev/null 2>&1 || say "  ВНИМАНИЕ: «Удалённый вход» выключен — Системные настройки → Основные → Общий доступ → Удалённый вход."
fi
out="$(cd / && sudo -u "$U" env SSH_ORIGINAL_COMMAND=scan "$D/logatlas-ssh" </dev/null 2>/dev/null || true)"
n="$(printf "%s" "$out" | grep -o "\"id\":" | wc -l | tr -d " " || true)"
if [ "${n:-0}" -gt 0 ]; then
  say "  скрипт сбора работает, найдено журналов: $n"
else
  say "  ВНИМАНИЕ: тестовый запуск скрипта сбора не удался — проверьте sudo: sudo -u $U sudo -n $D/collect.sh --scan-json"
fi
say ""
say "Готово. Хост $(hostname) подготовлен: пользователь $U, вход только с $PANEL_IP по ключу панели LogAtlas."
say "Добавьте его в панели: «Хосты» → «+ Хост» (адрес этого хоста, система: $( [ "$OS" = Darwin ] && echo macOS || echo Linux ))."
