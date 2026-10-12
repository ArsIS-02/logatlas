#!/usr/bin/env bash
# Сборка LogAtlas.app и образа диска .dmg для macOS. Архитектура — та, на которой идёт сборка
# (arm64 — Apple Silicon, x86_64 — Intel; в GitHub Actions: macos-15 и macos-15-intel).
#
#   bash macos/build.sh [версия]        результат: dist/LogAtlas-<версия>-macos-<арх>.dmg
#
# Нужно: Xcode Command Line Tools (swiftc, codesign, hdiutil), curl, python3 и интернет: встроенный Python
# берётся из python-build-standalone (GitHub), зависимости — с PyPI. GH_TOKEN или GITHUB_TOKEN — по желанию
# (лимит запросов к API GitHub без токена — 60 в час).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
VERSION=${1:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' app/main.py)}
ARCH=$(uname -m)
case $ARCH in
  arm64) TRIPLE=aarch64-apple-darwin ;;
  x86_64) TRIPLE=x86_64-apple-darwin ;;
  *) echo "неизвестная архитектура: $ARCH" >&2; exit 1 ;;
esac
PYVER=${LOGATLAS_PYTHON:-3.12}
step() { printf '\033[36m==> %s\033[0m\n' "$*"; }

BUILD=$REPO/build/macos-$ARCH
DIST=$REPO/dist
APP=$BUILD/LogAtlas.app
RES=$APP/Contents/Resources
rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$RES" "$DIST"
step "LogAtlas $VERSION для macOS $ARCH"

# --- встроенный Python: переносимая сборка python-build-standalone
step "Python $PYVER ($TRIPLE)"
TOKEN=${GH_TOKEN:-${GITHUB_TOKEN:-}}
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
URL=$(curl -fsSL ${AUTH[@]+"${AUTH[@]}"} https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest |
  python3 -c '
import json, re, sys
ver, triple = sys.argv[1], sys.argv[2]
pat = re.compile(r"^cpython-%s\.\d+\+\d+-%s-install_only_stripped\.tar\.gz$" % (re.escape(ver), re.escape(triple)))
for a in json.load(sys.stdin)["assets"]:
    if pat.match(a["name"]):
        print(a["browser_download_url"])
        break
' "$PYVER" "$TRIPLE")
[ -n "$URL" ] || { echo "в последнем релизе python-build-standalone нет Python $PYVER для $TRIPLE" >&2; exit 1; }
echo "   $URL"
curl -fsSL "$URL" -o "$BUILD/python.tar.gz"
tar -xzf "$BUILD/python.tar.gz" -C "$RES"          # → Contents/Resources/python
PY=$RES/python/bin/python3
"$PY" -c 'import platform, sys; print("  ", sys.version.split()[0], platform.machine())'

# --- зависимости: обязательные; orjson и evtx — по возможности готовыми колёсами
step 'зависимости'
PIP=("$PY" -m pip install --disable-pip-version-check --no-compile --quiet)
grep -vE '^[[:space:]]*(#|$)' macos/requirements.txt | grep -vE '^(orjson|evtx)' > "$BUILD/req.txt"
"${PIP[@]}" -r "$BUILD/req.txt"
"${PIP[@]}" --only-binary=:all: "$(grep -E '^orjson' macos/requirements.txt)" ||
  echo 'orjson: нет готового колеса — ответы API будут собираться модулем json (медленнее)'
"${PIP[@]}" --only-binary=:all: evtx || {
  echo 'evtx: нет готового колеса — беру python-evtx (чистый Python, медленнее)'
  "${PIP[@]}" python-evtx
}
"$PY" -m pip uninstall -y -q pip || true

# лишнее для LogAtlas: tkinter, тесты стандартной библиотеки, IDLE, заголовки, скрипты bin/
LIB=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')
rm -rf "$LIB"/{test,idlelib,tkinter,turtledemo,turtle.py,ensurepip,lib2to3} "$RES/python/include" "$RES/python/share"
rm -rf "$RES"/python/lib/{tcl,tk,itcl,thread}* "$LIB"/lib-dynload/_tkinter* 2>/dev/null || true
find "$RES/python/bin" -type f ! -name 'python3*' -delete

# --- файлы LogAtlas
step 'файлы LogAtlas'
L=$RES/logatlas
mkdir -p "$L"
cp -R app static scripts demo "$L/"
cp windows/launcher.pyw LICENSE THIRD_PARTY.txt "$L/"
find "$L" -name __pycache__ -type d -prune -exec rm -rf {} +

# --- байт-код заранее: программа подписана, писать в неё при работе нельзя
step 'байт-код'
"$PY" -m compileall -q -j 0 "$L/app" "$LIB" >/dev/null || true

# --- окно (Swift + WebKit)
step 'LogAtlas (Swift)'
swiftc -O -target "$ARCH-apple-macos12.0" -o "$APP/Contents/MacOS/LogAtlas" macos/LogAtlas.swift
sed "s/@VERSION@/$VERSION/g" macos/Info.plist > "$APP/Contents/Info.plist"
cp macos/AppIcon.icns "$RES/AppIcon.icns"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# --- проверка: встроенный Python видит все модули
step 'проверка модулей'
(
  cd /tmp
  LOGATLAS_DATA="$BUILD/check-data" PYTHONDONTWRITEBYTECODE=1 "$PY" -c "
import sys; sys.path.insert(0, '$L')
import app.main, starlette, uvicorn, multipart, importlib.util as u
assert u.find_spec('evtx') or u.find_spec('Evtx'), 'evtx'
print('modules ok, LogAtlas', app.main.VERSION, 'orjson' if u.find_spec('orjson') else 'json')"
)
rm -rf "$BUILD/check-data"

# --- подпись без сертификата (ad-hoc): на Apple Silicon неподписанный код не запускается вовсе
step 'подпись (ad-hoc)'
find "$RES" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 |
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q 'Mach-O'; then codesign --force --sign - "$f"; fi
  done
codesign --force --sign - "$APP"
codesign --verify --verbose "$APP"

# --- образ диска
step 'образ диска'
STAGE=$BUILD/dmg
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/LogAtlas.app"
ln -s /Applications "$STAGE/Applications"
DMG=$DIST/LogAtlas-$VERSION-macos-$ARCH.dmg
rm -f "$DMG"
for i in 1 2 3; do       # hdiutil на сборочных машинах иногда отвечает «Resource busy»
  if hdiutil create -volname "LogAtlas $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"; then break; fi
  [ "$i" = 3 ] && exit 1
  sleep 5
done
(cd "$DIST" && shasum -a 256 "$(basename "$DMG")" > "SHA256SUMS-macos-$ARCH.txt")
du -sh "$APP" "$DMG"
