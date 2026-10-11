#!/bin/sh
# ssh не работает без записи о пользователе в /etc/passwd. Готовый образ из реестра собран
# с UID 1000; если контейнер запущен от другого UID (user: в docker-compose.yml), запись
# добавляется через nss_wrapper — без прав root и без изменения /etc/passwd.
set -e
if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
  dir="${TMPDIR:-/tmp}"
  mkdir -p "$dir" 2>/dev/null && [ -w "$dir" ] || dir=/tmp
  pw="$dir/.passwd.$(id -u)"
  gr="$dir/.group.$(id -u)"
  { cat /etc/passwd; echo "logatlas:x:$(id -u):$(id -g):LogAtlas:${HOME:-/data}:/usr/sbin/nologin"; } > "$pw"
  { cat /etc/group; getent group "$(id -g)" >/dev/null 2>&1 || echo "logatlas:x:$(id -g):"; } > "$gr"
  export LD_PRELOAD="libnss_wrapper.so${LD_PRELOAD:+ $LD_PRELOAD}" NSS_WRAPPER_PASSWD="$pw" NSS_WRAPPER_GROUP="$gr"
fi
exec "$@"
