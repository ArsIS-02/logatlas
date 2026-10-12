#!/usr/bin/env bash
# Проверка сборки LogAtlas для macOS (GitHub Actions, после macos/build.sh): образ диска открывается,
# сервер из приложения разбирает демо-журналы и завершается вместе с окном, окно открывает интерфейс.
#   bash macos/smoke.sh <версия>
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${1:?версия}
ARCH=$(uname -m)
DMG=dist/LogAtlas-$VERSION-macos-$ARCH.dmg
export LOGATLAS_NO_DIALOGS=1
DATA="$HOME/Library/Application Support/LogAtlas"
step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mПРОВЕРКА НЕ ПРОЙДЕНА: %s\033[0m\n' "$*"; show_logs; exit 1; }
show_logs() { for f in "$DATA/window.log" "$DATA/logatlas.log"; do [ -f "$f" ] && { echo "---- $f"; tail -n 40 "$f"; }; done; true; }
healthy() { curl -fs -m 2 "http://127.0.0.1:$1/healthz" >/dev/null 2>&1; }

rm -rf "$DATA"
WORK=$(mktemp -d)
MNT=$WORK/mnt
mkdir -p "$MNT"

step "образ диска ($ARCH)"
hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG" >/dev/null
trap 'hdiutil detach "$MNT" -force >/dev/null 2>&1 || true' EXIT
APP=$MNT/LogAtlas.app
[ -d "$APP" ] || fail 'в образе нет LogAtlas.app'
[ -L "$MNT/Applications" ] || fail 'в образе нет ссылки на «Программы»'
codesign --verify "$APP" || fail 'подпись приложения не сходится'
[ -s "$APP/Contents/Resources/AppIcon.icns" ] || fail 'в приложении нет значка AppIcon.icns'
[ -s "$APP/Contents/Resources/Assets.car" ] && echo '   значок: AppIcon.icns и Assets.car' || echo '   значок: AppIcon.icns'
file "$APP/Contents/MacOS/LogAtlas" | grep -q "$ARCH" || fail "LogAtlas собран не под $ARCH"
file -L "$APP/Contents/Resources/python/bin/python3" | grep -q "$ARCH" || fail "Python не под $ARCH"
echo "   $(defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString)"

step 'сервер приложения: демо-журналы и завершение по закрытию ввода'
PY=$APP/Contents/Resources/python/bin/python3
FIFO=$WORK/stdin
OUT=$WORK/server.out
mkfifo "$FIFO"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$APP/Contents/Resources/logatlas/launcher.pyw" --window <"$FIFO" >"$OUT" 2>&1 &
SRV=$!
exec 3>"$FIFO"             # ввод сервера открыт, пока открыт дескриптор 3
PORT=
for _ in $(seq 1 90); do
  PORT=$(sed -n 's/^LOGATLAS-READY \([0-9]*\) .*/\1/p' "$OUT" | head -n 1)
  [ -n "$PORT" ] && break
  sleep 1
done
[ -n "$PORT" ] || { cat "$OUT"; fail 'сервер не сообщил порт'; }
META=$(curl -fs "http://127.0.0.1:$PORT/api/meta")
echo "$META" | grep -q '"runtime":"macos-app"' || fail "runtime: $(echo "$META" | head -c 300)"
ID=$(curl -fsS -F "files=@demo/macos-demo.tar.gz" -F "name=smoke" "http://127.0.0.1:$PORT/api/cases" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
ST=
for _ in $(seq 1 240); do
  C=$(curl -fs "http://127.0.0.1:$PORT/api/cases/$ID")
  ST=$(echo "$C" | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d["status"], d["events"])')
  case $ST in ready*|error*) break ;; esac
  sleep 1
done
case $ST in ready\ 0|error*|"") fail "разбор демо: $ST" ;; ready*) echo "   демо: ${ST#ready } событий" ;; *) fail "разбор демо не закончился: $ST" ;; esac
curl -fs "http://127.0.0.1:$PORT/api/status" | grep -q '"busy":false' || fail 'после разбора панель занята'
exec 3>&-                  # «окно закрыто»
for _ in $(seq 1 20); do kill -0 "$SRV" 2>/dev/null || break; sleep 1; done
kill -0 "$SRV" 2>/dev/null && fail 'сервер не завершился после закрытия ввода'
sleep 1
healthy "$PORT" && fail 'порт сервера всё ещё отвечает'
pgrep -f "$APP/Contents/Resources/python" >/dev/null && fail 'остались процессы встроенного Python'
echo '   сервер завершился, процессы разбора тоже'

step 'окно: интерфейс открывается'
RESULT=$WORK/selftest.txt
"$APP/Contents/MacOS/LogAtlas" --selftest "$RESULT" &
WIN=$!
for _ in $(seq 1 180); do kill -0 "$WIN" 2>/dev/null || break; sleep 1; done
if kill -0 "$WIN" 2>/dev/null; then kill "$WIN"; fail 'окно не завершилось за 3 минуты'; fi
[ -f "$RESULT" ] || fail 'окно не записало результат проверки'
echo "   окно: $(cat "$RESULT")"
grep -q "^ok v$VERSION" "$RESULT" || fail "интерфейс не открылся: $(cat "$RESULT")"
sleep 2
pgrep -f "$APP/Contents/Resources/python" >/dev/null && fail 'сервер окна остался после выхода'

echo "Все проверки пройдены ($ARCH)"
