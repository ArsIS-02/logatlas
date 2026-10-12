"""LogAtlas для macOS: сбор журналов этого Mac.

Работает в приложении LogAtlas.app (LOGATLAS_DESKTOP=1 на macOS). Сборщик scripts/collect-macos.sh
запускается с правами администратора через стандартное окно macOS (osascript … with administrator
privileges — пароль или Touch ID вводятся в системном окне, LogAtlas его не видит), пишет архив tar
в папку задания, ход — в файл рядом. Готовый архив сразу ставится на разбор.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from . import collect, db, winlocal

log = logging.getLogger("logatlas.local")

ENABLED = sys.platform == "darwin" and os.environ.get("LOGATLAS_DESKTOP") == "1"
LOCAL_DIR = os.path.join(db.DATA_DIR, "local")
PERIODS = (1, 3, 7, 14, 30)

_lock = threading.Lock()
JOBS: dict[str, dict] = {}


def _sh(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


_HOST: list = []


def host() -> str:
    if not _HOST:
        _HOST.append(_host())
    return _HOST[0]


def _host() -> str:
    try:
        r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    import socket
    return socket.gethostname().split(".")[0]


def _set(jid: str, **kw):
    with _lock:
        JOBS[jid].update(kw)


def _watch_progress(jid: str, path: str, stop: threading.Event):
    """Строки LOGATLAS-PROGRESS из сборщика → понятный этап для страницы."""
    pos = 0
    while not stop.is_set():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            chunk = ""
        for line in chunk.splitlines():
            m = re.match(r"LOGATLAS-PROGRESS (\d+) (\d+) (.*)", line)
            if m:
                _set(jid, state="collecting", stage=f"Собираю «{m.group(3)}» ({m.group(1)} из {m.group(2)})…")
            elif line.startswith("LOGATLAS-PROGRESS pack"):
                _set(jid, state="collecting", stage="Упаковка архива…")
        stop.wait(1)


def _run(jid: str, days: int, out_dir: str):
    script = os.path.join(collect.SCRIPTS_DIR, "collect-macos.sh")
    until = int(time.time())
    since = until - days * 86400
    archive = os.path.join(out_dir, "this-mac-logs.tar")
    progress = os.path.join(out_dir, "progress.log")
    uid = os.getuid()
    cmd = (f"/bin/bash {_sh(script)} --stream --since @{since} --until @{until} > {_sh(archive)} 2> {_sh(progress)}; "
           f"rc=$?; chown {uid} {_sh(archive)} {_sh(progress)} 2>/dev/null; exit $rc")
    osa = (f"do shell script {_applescript_str(cmd)} with administrator privileges "
           f"with prompt {_applescript_str('LogAtlas собирает журналы этого Mac: нужны права администратора, чтобы прочитать системные журналы.')}")
    stop = threading.Event()
    threading.Thread(target=_watch_progress, args=(jid, progress, stop), daemon=True).start()
    try:
        _set(jid, state="waiting", stage="Подтвердите права администратора в окне macOS…")
        p = subprocess.run(["/usr/bin/osascript", "-e", osa], capture_output=True, text=True)
        stop.set()
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            if "-128" in err or "canceled" in err.lower() or "отмен" in err.lower():
                _set(jid, state="closed", error="Сбор отменён: без прав администратора системные журналы не прочитать")
            else:
                tail = ""
                try:
                    with open(progress, encoding="utf-8", errors="replace") as f:
                        lines = [x for x in f.read().splitlines() if x.strip() and not x.startswith("LOGATLAS-")]
                        tail = lines[-1] if lines else ""
                except OSError:
                    pass
                msg = "За выбранный период в журналах нет записей" if "(12)" in err or "status 12" in err \
                    else (tail or err or f"код {p.returncode}")
                _set(jid, state="error", error=f"Сбор не удался: {msg}")
            return
        if not os.path.exists(archive) or os.path.getsize(archive) == 0:
            _set(jid, state="error", error="Сборщик не создал архив")
            return
        _set(jid, stage="Ставлю на разбор…")
        cid = winlocal.import_archive(archive, platform="macos", host=_host(),
                                      how=f"на этом Mac за {days} сут.")
        _set(jid, state="done", case=cid, stage="Готово")
    except Exception as e:  # noqa: BLE001
        log.exception("local collect failed")
        _set(jid, state="error", error=str(e))
    finally:
        stop.set()
        shutil.rmtree(out_dir, ignore_errors=True)


def start(days: int = 3) -> str:
    if not ENABLED:
        raise RuntimeError("Сбор журналов этого Mac доступен в приложении LogAtlas для macOS")
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 3
    if days not in PERIODS:
        days = 3
    with _lock:
        if any(j["state"] in ("starting", "waiting", "collecting") for j in JOBS.values()):
            raise RuntimeError("Сбор журналов этого Mac уже идёт")
    if not os.path.isfile(os.path.join(collect.SCRIPTS_DIR, "collect-macos.sh")):
        raise RuntimeError("Не найден сборщик scripts/collect-macos.sh — скачайте LogAtlas заново")
    jid = uuid.uuid4().hex[:8]
    out_dir = os.path.join(LOCAL_DIR, jid)
    os.makedirs(out_dir, exist_ok=True)
    with _lock:
        JOBS[jid] = {"id": jid, "state": "starting", "case": None, "error": None, "stage": "Запуск…",
                     "started": time.time(), "days": days}
    threading.Thread(target=_run, args=(jid, days, out_dir), daemon=True, name=f"local-{jid}").start()
    return jid


def status(jid: str) -> dict | None:
    with _lock:
        j = JOBS.get(jid)
        return dict(j) if j else None


def cleanup():
    shutil.rmtree(LOCAL_DIR, ignore_errors=True)
