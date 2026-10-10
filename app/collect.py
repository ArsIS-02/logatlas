"""Сбор журналов с хостов по SSH: список хостов, ключ панели, поиск журналов и запуск сбора.

Панель входит на хост своим ключом под пользователем logatlas-collect. На хосте ключ привязан к
принудительной команде (logatlas-ssh или ForceCommand на Windows), которая пропускает только
version, scan и collect. Архив приходит в stdout SSH-сессии и сразу ставится в очередь разбора.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid

from . import db, ingest

log = logging.getLogger("logatlas.collect")

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS_DIR = os.environ.get("LOGATLAS_SCRIPTS", os.path.join(ROOT, "scripts"))
SETUP_DIR = os.path.join(os.path.dirname(__file__), "setup")
KEYS_DIR = os.path.join(db.DATA_DIR, "keys")
KEY = os.path.join(KEYS_DIR, "logatlas_ed25519")
KNOWN_HOSTS = os.path.join(db.DATA_DIR, "known_hosts")
HOSTS_FILE = os.path.join(db.DATA_DIR, "hosts.json")
COLLECT_VERSION = "1.2"
DEFAULT_USER = "logatlas-collect"
OSES = ("linux", "macos", "windows")

RE_ADDR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}")
# Имя учётной записи: буквы любого алфавита (в т.ч. «администратор»), цифры, «.», «_», «-»,
# пробелы внутри (локальные имена Windows); для домена — «ДОМЕН\имя» или «имя@домен».
# Имя уходит в ssh отдельным аргументом (-l), без оболочки, поэтому эти символы безопасны.
RE_USER = re.compile(r"(?:\w[\w.-]{0,63}\\)?\w(?:[\w. -]{0,62}[\w.-])?(?:@\w[\w.-]{0,252})?")
RE_SRC = re.compile(r"[a-z][a-z0-9_-]{0,31}")

_lock = threading.RLock()
JOBS: dict[str, subprocess.Popen] = {}


# ---------------------------------------------------------------------------
# ключ панели
def ensure_key():
    os.makedirs(KEYS_DIR, exist_ok=True)
    try:
        os.chmod(KEYS_DIR, 0o700)
    except OSError:
        pass
    if os.path.exists(KEY) and os.path.exists(KEY + ".pub"):
        return
    if not shutil.which("ssh-keygen"):
        log.warning("ssh-keygen not found: collection from hosts is unavailable")
        return
    for p in (KEY, KEY + ".pub"):
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"logatlas@{socket.gethostname()}", "-f", KEY],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.chmod(KEY, 0o600)


def pubkey() -> str:
    try:
        with open(KEY + ".pub", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def available() -> bool:
    return bool(shutil.which("ssh")) and os.path.exists(KEY)


# ---------------------------------------------------------------------------
# хосты
def _load() -> list[dict]:
    try:
        with open(HOSTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(hosts: list[dict]):
    tmp = HOSTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hosts, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HOSTS_FILE)


def list_hosts() -> list[dict]:
    with _lock:
        return _load()


def get_host(hid: str) -> dict | None:
    return next((h for h in list_hosts() if h.get("id") == hid), None)


def _clean(body: dict, old: dict | None = None) -> dict:
    old = old or {}
    name = str(body.get("name", old.get("name", "")) or "").strip()[:64]
    addr = str(body.get("address", old.get("address", "")) or "").strip()
    if not RE_ADDR.fullmatch(addr):
        raise ValueError("Адрес: имя хоста или IP, без пробелов")
    try:
        port = int(body.get("port", old.get("port", 22)) or 22)
    except (TypeError, ValueError):
        raise ValueError("Порт — число") from None
    if not 1 <= port <= 65535:
        raise ValueError("Порт — от 1 до 65535")
    os_ = str(body.get("os", old.get("os", "linux")) or "linux")
    if os_ not in OSES:
        raise ValueError("Система: linux, macos или windows")
    user = str(body.get("user", old.get("user", "")) or "").strip()
    if not user:
        if os_ == "windows":
            raise ValueError("Укажите учётную запись Windows, к которой скрипт подготовки добавил ключ панели")
        user = DEFAULT_USER
    if not RE_USER.fullmatch(user):
        raise ValueError("Пользователь: буквы (можно русские), цифры, «.», «_», «-», пробел внутри; "
                         "доменная учётная запись — «ДОМЕН\\имя» или «имя@домен»")
    note = str(body.get("note", old.get("note", "")) or "").strip()[:200]
    return {"name": name or addr, "address": addr, "port": port, "os": os_, "user": user, "note": note}


def add_host(body: dict) -> dict:
    h = _clean(body)
    with _lock:
        hosts = _load()
        if any(x["address"] == h["address"] and x["port"] == h["port"] for x in hosts):
            raise ValueError("Такой хост уже есть")
        h.update(id=uuid.uuid4().hex[:8], added=time.time(), scan=None)
        hosts.append(h)
        _save(hosts)
    return h


def update_host(hid: str, body: dict) -> dict:
    with _lock:
        hosts = _load()
        for i, x in enumerate(hosts):
            if x["id"] == hid:
                new = _clean(body, x)
                if (new["address"], new["port"], new["os"]) != (x["address"], x["port"], x["os"]):
                    x["scan"] = None
                x.update(new)
                hosts[i] = x
                _save(hosts)
                return x
    raise KeyError(hid)


def delete_host(hid: str):
    with _lock:
        hosts = _load()
        rest = [x for x in hosts if x["id"] != hid]
        if len(rest) == len(hosts):
            raise KeyError(hid)
        _save(rest)


def _set_scan(hid: str, scan: dict | None):
    with _lock:
        hosts = _load()
        for x in hosts:
            if x["id"] == hid:
                x["scan"] = scan
        _save(hosts)


# ---------------------------------------------------------------------------
# SSH
def ssh_cmd(h: dict, command: str) -> list[str]:
    return ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6", "-o", "LogLevel=ERROR",
            "-p", str(h["port"]), "-l", h["user"], "--", h["address"], command]


def explain(rc: int, err: str, h: dict) -> str:
    e = (err or "").strip()
    low = e.lower()
    if rc == 255 or "ssh:" in low:
        if "host key verification failed" in low or "remote host identification has changed" in low:
            return ("Отпечаток SSH-ключа хоста изменился (переустановка или подмена). Если это ваш хост — "
                    "«Сбросить отпечаток» в карточке хоста.")
        if "permission denied" in low:
            return (f"Ключ панели не принят пользователем {h['user']}. Хост не подготовлен, ключ панели сменился "
                    "или панель подключается с другого адреса (ограничение from=). Повторите подготовку хоста.")
        if "could not resolve" in low or "name or service not known" in low:
            return f"Имя {h['address']} не находится в DNS — укажите IP-адрес."
        if "connection refused" in low:
            return "Порт SSH закрыт: служба sshd не запущена или порт закрыт firewall."
        if "timed out" in low or "no route to host" in low:
            return "Хост не отвечает: выключен, другой адрес или порт закрыт firewall."
        return "Ошибка SSH: " + (e.splitlines()[-1] if e else f"код {rc}")
    if "разрешены только команды" in e:
        return "Хост подготовлен для другой панели или старой версией — повторите подготовку хоста."
    if "sudo" in low and ("password" in low or "пароль" in low):
        return "sudo требует пароль: на хосте нет правила /etc/sudoers.d/logatlas — повторите подготовку хоста."
    tail = [l for l in e.splitlines() if l.strip() and not l.startswith("LOGATLAS-PROGRESS")]
    return (tail[-1] if tail else f"Сбор завершился с кодом {rc}")[:500]


def scan(hid: str) -> dict:
    h = get_host(hid)
    if not h:
        raise KeyError(hid)
    if not available():
        raise RuntimeError("В контейнере нет ssh или ключа панели — пересоберите образ LogAtlas")
    try:
        p = subprocess.run(ssh_cmd(h, "scan"), capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Хост не ответил за 2 минуты") from None
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0 or not out.startswith("{"):
        msg = explain(p.returncode, err or out, h)
        _set_scan(hid, {"at": time.time(), "ok": False, "error": msg})
        raise RuntimeError(msg)
    try:
        data = json.loads(out.splitlines()[-1] if not out.startswith("{") else out)
    except ValueError:
        raise RuntimeError("Хост вернул непонятный ответ: " + out[:200]) from None
    if data.get("os") and data["os"] != h["os"]:
        data["os_mismatch"] = True
    data["outdated"] = str(data.get("version") or "1.0") != COLLECT_VERSION
    _set_scan(hid, {"at": time.time(), "ok": True, "os": data.get("os"), "host": data.get("host"),
                    "tz_offset_min": data.get("tz_offset_min"), "tz_name": data.get("tz_name"),
                    "sources": len(data.get("sources") or [])})
    return data


def reset_hostkey(hid: str):
    h = get_host(hid)
    if not h:
        raise KeyError(hid)
    name = h["address"] if h["port"] == 22 else f"[{h['address']}]:{h['port']}"
    if os.path.exists(KNOWN_HOSTS):
        subprocess.run(["ssh-keygen", "-R", name, "-f", KNOWN_HOSTS], capture_output=True)


# ---------------------------------------------------------------------------
# сбор
def _human(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return (f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1024
    return str(n)


def start(body: dict) -> str:
    h = get_host(str(body.get("host_id") or ""))
    if not h:
        raise ValueError("Хост не найден")
    if not available():
        raise ValueError("В контейнере нет ssh или ключа панели — пересоберите образ LogAtlas")
    try:
        since, until = int(body["since"]), int(body["until"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Укажите период") from None
    if not (10 ** 8 < since < until < 10 ** 11):
        raise ValueError("Конец периода должен быть позже начала")
    sources = [str(s) for s in body.get("sources") or []]
    if not sources or not all(RE_SRC.fullmatch(s) for s in sources):
        raise ValueError("Выберите журналы")
    try:
        tz = int(body.get("tz") or 0)
    except (TypeError, ValueError):
        tz = 0
    if not -14 * 60 <= tz <= 14 * 60:
        tz = 0
    name = str(body.get("name") or "").strip()[:200] or h["name"]

    cid = uuid.uuid4().hex[:10]
    now = time.time()
    with db.catalog() as c:
        c.execute("INSERT INTO cases(id,name,platform,host,tz,tz_mode,created,updated,status,progress,stage) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, name, h["os"], h["name"], tz, "fixed", now, now, "collecting", 0.01, "Подключение к хосту…"))
    def _f(e):
        return time.strftime("%d.%m.%Y %H:%M", time.gmtime(e + tz * 60))
    db.add_log(cid, "info", f"Сбор запущен из панели: журналов {len(sources)} ({', '.join(sources)}), "
                            f"период {_f(since)} — {_f(until)} (UTC{'+' if tz >= 0 else '-'}{abs(tz) // 60:02d}:{abs(tz) % 60:02d})")
    command = f"collect {since} {until} {','.join(sources)}"
    threading.Thread(target=_run, args=(cid, h, command, tz), daemon=True, name=f"collect-{cid}").start()
    return cid


def _run(cid: str, h: dict, command: str, tz: int):
    updir = os.path.join(db.UPLOADS_DIR, cid)
    os.makedirs(updir, exist_ok=True)
    ext = "zip" if h["os"] == "windows" else "tar.gz"
    safe = re.sub(r"[^\w.-]+", "_", h["name"])[:60] or "host"
    path = os.path.join(updir, f"{int(time.time() * 1000)}_{safe}-logs.{ext}")
    state = {"stage": "Подключение к хосту…", "progress": 0.02, "done": False, "connected": False}
    tail: list[str] = []
    t0 = time.time()
    os_name = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}.get(h["os"], h["os"])
    db.add_log(cid, "info", f"Подключение по SSH: {h['user']}@{h['address']}:{h['port']} ({os_name}), ключ панели")

    def monitor():
        last = None
        while not state["done"]:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            stage = state["stage"] + (f" · получено {_human(size)}" if size else "")
            if stage != last and db.get_case(cid):
                db.set_case(cid, stage=stage, progress=state["progress"])
                last = stage
            time.sleep(1)

    try:
        with open(path, "wb") as out:
            p = subprocess.Popen(ssh_cmd(h, command), stdout=out, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            JOBS[cid] = p
            mon = threading.Thread(target=monitor, daemon=True)
            mon.start()
            for raw_line in p.stderr:
                line = raw_line.decode("utf-8", "replace").rstrip()
                if line.startswith("LOGATLAS-") and not state["connected"]:
                    state["connected"] = True
                    db.add_log(cid, "ok", f"Подключено, хост начал сбор (через {time.time() - t0:.0f} с)")
                if line.startswith("LOGATLAS-RESULT "):
                    parts = line.split(" ", 2)
                    status = parts[1] if len(parts) > 1 else ""
                    label, _, detail = (parts[2] if len(parts) > 2 else "").partition("|")
                    if status == "ok":
                        db.add_log(cid, "ok", f"{label}: {detail}" if detail else label)
                    elif status == "empty":
                        db.add_log(cid, "info", f"{label}: нет записей за период")
                    else:
                        db.add_log(cid, "warn", f"{label}: {detail or 'не удалось собрать'}")
                elif line.startswith("LOGATLAS-PROGRESS "):
                    parts = line.split(" ", 3)
                    if parts[1] == "pack":
                        state["stage"] = "Упаковка и передача архива"
                        state["progress"] = 0.09
                        db.add_log(cid, "info", "Упаковка архива на хосте и передача в панель")
                    elif len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                        i, n = int(parts[1]), max(1, int(parts[2]))
                        label = parts[3] if len(parts) > 3 else ""
                        state["stage"] = f"Сбор на хосте: {i} из {n} — {label}"
                        state["progress"] = 0.02 + 0.07 * (i - 1) / n
                        db.add_log(cid, "info", f"Собираю «{label}» ({i} из {n})…")
                elif line.strip():
                    tail.append(line)
                    del tail[:-40]
            rc = p.wait()
    except Exception as e:  # noqa: BLE001
        log.exception("collect failed")
        rc, tail = -1, [str(e)]
    finally:
        state["done"] = True
        JOBS.pop(cid, None)
    try:
        mon.join(3)
    except NameError:
        pass

    if not db.get_case(cid):            # сбор удалили, пока он шёл
        shutil.rmtree(updir, ignore_errors=True)
        return
    size = os.path.getsize(path) if os.path.exists(path) else 0
    took = time.time() - t0
    if rc == 0 and size > 0:
        db.add_log(cid, "ok", f"Архив получен: {_human(size)}, сбор занял {int(took // 60)}:{int(took % 60):02d}")
        if ext != "zip":                # сборщики 1.2 присылают tar без общего сжатия
            with open(path, "rb") as f:
                h0 = f.read(512)
            if h0[:2] != b"\x1f\x8b" and h0[257:262] == b"ustar":
                os.rename(path, path[: -len(".gz")])
                path = path[: -len(".gz")]
        w = ingest.worker()
        ahead = w.q.qsize() + (1 if w.current else 0)
        db.set_case(cid, status="queued", stage="В очереди на разбор", progress=0)
        if ahead:
            db.add_log(cid, "info", f"В очереди на разбор: сначала будут разобраны другие сборы ({ahead}) — "
                                    "панель разбирает сборы по одному")
        w.submit(cid, [path], tz, "fixed")
        return
    shutil.rmtree(updir, ignore_errors=True)
    if rc == 12:
        msg = "За выбранный период в журналах нет записей — выберите период шире."
    elif rc < 0 and not tail:
        msg = "Сбор остановлен."
    else:
        msg = explain(rc, "\n".join(tail), h)
    db.add_log(cid, "err", msg)
    db.set_case(cid, status="error", stage="Сбор не удался", message=msg)


def cancel(cid: str):
    p = JOBS.get(cid)
    if p and p.poll() is None:
        p.terminate()


# ---------------------------------------------------------------------------
# скрипты подготовки хостов
def _b64(name: str) -> str:
    with open(os.path.join(SCRIPTS_DIR, name), "rb") as f:
        data = f.read()
    if name.endswith(".sh"):
        data = data.replace(b"\r\n", b"\n")
    return base64.b64encode(data).decode("ascii")


def setup_script(kind: str, base: str, panel_host: str) -> str:
    key = pubkey()
    if not key:
        raise RuntimeError("Ключ панели ещё не создан")
    if kind == "host":
        with open(os.path.join(SETUP_DIR, "host-setup.sh"), encoding="utf-8") as f:
            t = f.read()
        repl = {"@@COLLECT_LINUX_B64@@": _b64("collect-linux.sh"), "@@COLLECT_MACOS_B64@@": _b64("collect-macos.sh")}
    else:
        with open(os.path.join(SETUP_DIR, "windows-setup.ps1"), encoding="utf-8") as f:
            t = f.read()
        with open(os.path.join(SCRIPTS_DIR, "collect-windows-remote.ps1"), encoding="utf-8-sig") as f:
            text = f.read().replace("\r\n", "\n").rstrip("\n")
        if re.search(r"^'@", text, re.M):
            raise RuntimeError("collect-windows-remote.ps1 нельзя встроить в here-string")
        repl = {"@@COLLECT_WINDOWS_TEXT@@": text}
    repl.update({"@@BASE@@": base, "@@PANEL_HOST@@": panel_host, "@@PUBKEY@@": key, "@@VERSION@@": COLLECT_VERSION})
    for k, v in repl.items():
        t = t.replace(k, v)
    if kind == "windows":
        # BOM — чтобы Windows PowerShell 5.1 прочитал кириллицу; CRLF — привычные окончания строк Windows
        t = "\ufeff" + t.replace("\r\n", "\n").replace("\n", "\r\n")
    return t
