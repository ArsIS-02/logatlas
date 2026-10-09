#!/bin/bash
# Сбор журналов macOS для LogAtlas. Запуск: sudo bash collect-macos.sh [дней]
# Результат: ~/Desktop/<host>-logs-<дата>.tar.gz
set -euo pipefail
DAYS="${1:-3}"
HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
OUT_DIR="$(mktemp -d)"
STAMP="$(date +%Y%m%d-%H%M)"
DEST="${SUDO_USER:+/Users/$SUDO_USER}/Desktop"; DEST="${DEST:-$HOME/Desktop}"
ARCHIVE="$DEST/${HOST}-logs-${STAMP}.tar.gz"

echo "Unified log за ${DAYS} дн. (без самых шумных подсистем)…"
# Фильтр убирает служебный шум (mDNSResponder, trustd, runningboard и т.п.) —
# объём падает в разы, а события входа, sudo, USB, TCC, установок сохраняются.
log show --last "${DAYS}d" --style ndjson --info \
  --predicate 'NOT (process IN {"mDNSResponder","trustd","cfprefsd","gamepolicyd","mds","mds_stores","mdworker_shared","WeatherWidget","WallpaperAerialsExtension","MTLCompilerService"}) AND NOT (subsystem == "com.apple.runningboard" AND NOT (eventMessage CONTAINS "Now tracking process"))' \
  > "$OUT_DIR/unified.ndjson" || true

echo "Журнал питания (pmset)…"
pmset -g log > "$OUT_DIR/pmset.log" 2>/dev/null || true

echo "Файлы /var/log…"
mkdir -p "$OUT_DIR/var_log"
cd /var/log
for f in system.log* install.log* wifi.log* fsck_apfs*.log keybagd.log* shutdown_monitor.log com.apple.xpc.launchd; do
  [ -e "$f" ] && cp -R "$f" "$OUT_DIR/var_log/" 2>/dev/null || true
done

tar -czf "$ARCHIVE" -C "$OUT_DIR" .
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER" "$ARCHIVE"
rm -rf "$OUT_DIR"
echo "Готово: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
