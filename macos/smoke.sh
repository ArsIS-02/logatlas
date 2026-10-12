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
STEP=
step() { STEP=$*; printf '\033[36m==> %s\033[0m\n' "$*"; }
# Отчёт о сбое кладём рядом с журналами программы: шаг «Журналы при сбое» в workflow печатает
# всё из этой папки сам, поэтому причина видна, даже если смотреть только его.
report() {
  mkdir -p "$DATA" 2>/dev/null || return 0
  {
    echo "шаг: ${STEP:-—}"
    echo "причина: $1"
    echo "машина: $(uname -m), macOS $(sw_vers -productVersion 2>/dev/null || echo '?'), версия $VERSION"
    if [ -s "${WORK:-}/installer.log" ]; then echo '--- вывод installer:'; tail -n 30 "${WORK}/installer.log"; fi
    if [ -d "${EXP:-}" ]; then echo '--- состав пакета:'; find "$EXP" -maxdepth 3 -print 2>/dev/null | head -n 25; fi
    echo "--- квитанции: $(pkgutil --pkgs 2>/dev/null | grep -i logatlas || echo 'нет')"
    echo "--- /Applications/LogAtlas.app: $([ -d /Applications/LogAtlas.app ] && echo есть || echo нет)"
    if [ -s "${WORK:-}/installed.log" ]; then echo '--- вывод установленной программы:'; tail -n 15 "${WORK}/installed.log"; fi
  } > "$DATA/smoke-fail.log" 2>&1 || true
}
# Сообщение-аннотация с причиной и отчётом: GitHub показывает такие вверху страницы запуска.
annotate() {
  [ -n "${GITHUB_ACTIONS:-}" ] || return 0
  body=$(sed 's/%/%25/g; s/\r//g' "$DATA/smoke-fail.log" 2>/dev/null | head -n 40 |
         awk '{ printf "%s%%0A", $0 }')
  title=$(printf '%s' "${STEP:-проверка}" | tr ',' ' ')
  printf '::error title=Проверка macOS %s — %s::%s\n' "$ARCH" "$title" "${body:-$1}"
}
fail() {
  printf '\033[31mПРОВЕРКА НЕ ПРОЙДЕНА (%s): %s\033[0m\n' "${STEP:-—}" "$*"
  report "$*"
  annotate "$*"
  show_logs
  exit 1
}
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

step 'установщик: состав пакета'
PKG=dist/LogAtlas-$VERSION-macos-$ARCH.pkg
[ -s "$PKG" ] || fail "нет установщика $PKG"
EXP=$WORK/pkg
pkgutil --expand-full "$PKG" "$EXP" || fail 'установщик не распаковывается'
[ -s "$EXP/Distribution" ] || fail 'в пакете нет Distribution'
grep -q 'logatlas_check' "$EXP/Distribution" || fail 'в пакете нет проверки перед установкой'
grep -q "'12.0'" "$EXP/Distribution" || fail 'в пакете нет проверки версии macOS'
SC=$EXP/LogAtlas-component.pkg/Scripts
for s in preinstall postinstall; do
  [ -x "$SC/$s" ] || fail "в пакете нет скрипта $s"
done
grep -q "NEED_ARCH=\"$ARCH\"" "$SC/preinstall" || fail "в preinstall не подставлена архитектура $ARCH"
[ -d "$EXP/LogAtlas-component.pkg/Payload/Applications/LogAtlas.app" ] ||
  fail 'в пакете нет Applications/LogAtlas.app'
echo "   скрипты и проверки на месте"

step 'установщик: установка'
INSTALLED=/Applications/LogAtlas.app
sudo rm -rf "$INSTALLED"
install_pkg() { sudo installer -pkg "$PKG" -target / >"$WORK/installer.log" 2>&1 ||
  { echo '--- вывод установщика:'; tail -n 25 "$WORK/installer.log"; fail "$1"; }; }
install_pkg 'установщик отработал с ошибкой'
[ -d "$INSTALLED" ] || fail 'после установки нет /Applications/LogAtlas.app'
codesign --verify "$INSTALLED" || fail 'подпись установленной программы не сходится'
V=$(defaults read "$INSTALLED/Contents/Info.plist" CFBundleShortVersionString)
[ "$V" = "$VERSION" ] || fail "установилась версия $V вместо $VERSION"
xattr -p com.apple.quarantine "$INSTALLED" >/dev/null 2>&1 && fail 'на установленной программе остался карантин'
pkgutil --pkgs | grep -qx 'io.github.arsis-02.logatlas' || fail 'установщик не оставил квитанцию'
echo "   установлено в «Программы», карантина нет"

step 'установщик: обновление поверх работающей копии'
"$INSTALLED/Contents/MacOS/LogAtlas" >"$WORK/installed.log" 2>&1 &
RUN=$!
for _ in $(seq 1 60); do
  pgrep -f "^$INSTALLED/Contents/MacOS/LogAtlas" >/dev/null 2>&1 && break
  sleep 1
done
if pgrep -f "^$INSTALLED/Contents/MacOS/LogAtlas" >/dev/null 2>&1; then
  install_pkg 'обновление поверх не прошло'
  pgrep -f "^$INSTALLED/Contents/MacOS/LogAtlas" >/dev/null 2>&1 &&
    fail 'установщик не закрыл работающую копию'
  wait "$RUN" 2>/dev/null || true
  sleep 2
  pgrep -f "^$INSTALLED/Contents/Resources/python" >/dev/null 2>&1 &&
    fail 'после обновления остался сервер прежней копии'
  echo '   работающая копия закрыта, программа обновлена'
else
  # окно не поднялось (бывает, когда у сборочной машины нет сеанса рабочего стола) — проверяем
  # хотя бы саму переустановку поверх, а закрытие работающей копии пропускаем
  kill "$RUN" 2>/dev/null || true
  echo '   окно не запустилось на этой машине — проверяем только переустановку поверх'
  [ -s "$WORK/installed.log" ] && tail -n 5 "$WORK/installed.log"
  install_pkg 'повторная установка поверх не прошла'
fi
[ -d "$INSTALLED" ] || fail 'после обновления нет /Applications/LogAtlas.app'

sudo rm -rf "$INSTALLED"
sudo pkgutil --forget io.github.arsis-02.logatlas >/dev/null

echo "Все проверки пройдены ($ARCH)"
