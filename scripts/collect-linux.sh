#!/bin/bash
# Сбор журналов Linux (РЕД ОС, RHEL, Debian) для LogAtlas. Запуск: sudo bash collect-linux.sh [дней]
set -euo pipefail
DAYS="${1:-3}"
OUT_DIR="$(mktemp -d)"
ARCHIVE="${PWD}/$(hostname -s)-logs-$(date +%Y%m%d-%H%M).tar.gz"
mkdir -p "$OUT_DIR/var_log"
cp -R /var/log/audit "$OUT_DIR/var_log/" 2>/dev/null || true
for f in /var/log/secure* /var/log/auth.log* /var/log/messages* /var/log/syslog* /var/log/cron* /var/log/dnf.log*; do
  [ -e "$f" ] && cp "$f" "$OUT_DIR/var_log/" 2>/dev/null || true
done
journalctl --since "${DAYS} days ago" -o json > "$OUT_DIR/journal.json" 2>/dev/null || true
tar -czf "$ARCHIVE" -C "$OUT_DIR" .
rm -rf "$OUT_DIR"
echo "Готово: $ARCHIVE"
