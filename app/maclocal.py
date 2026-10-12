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


def _watch_progress(jid: str, cid: str, path: str, stop: threading.Event):
    """Строки LOGATLAS-PROGRESS / LOGATLAS-RESULT из сборщика → этап и журнал шагов сбора."""
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
                i, n, label = int(m.group(1)), max(1, int(m.group(2))), m.group(3)
                _set(jid, state="collecting")
                winlocal.case_stage(cid, f"Сбор на этом Mac: {i} из {n} — {label}", 0.02 + 0.07 * (i - 1) / n,
                                    f"Собираю «{label}» ({i} из {n})…")
            elif line.startswith("LOGATLAS-PROGRESS pack"):
                winlocal.case_stage(cid, "Упаковка архива", 0.09, "Упаковка архива")
            elif line.startswith("LOGATLAS-RESULT "):
                parts = line.split(" ", 2)
                label, _, detail = (parts[2] if len(parts) > 2 else "").partition("|")
                if winlocal.case_alive(cid):
                    if parts[1] == "ok":
                        db.add_log(cid, "ok", f"{label}: {detail}" if detail else label)
                    elif parts[1] == "empty":
                        db.add_log(cid, "info", f"{label}: нет записей за период")
                    else:
                        db.add_log(cid, "warn", f"{label}: {detail or 'не удалось собрать'}")
        stop.wait(1)


def _run(jid: str, cid: str, days: int, out_dir: str):
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
    threading.Thread(target=_watch_progress, args=(jid, cid, progress, stop), daemon=True).start()
    t0 = time.time()
    try:
        _set(jid, state="waiting")
        p = subprocess.run(["/usr/bin/osascript", "-e", osa], capture_output=True, text=True)
        stop.set()
        time.sleep(1.1)                               # последние строки хода — в журнал шагов
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            if "-128" in err or "canceled" in err.lower() or "отмен" in err.lower():
                msg = "Сбор отменён: без прав администратора системные журналы этого Mac не прочитать"
                _set(jid, state="closed", error=msg)
            else:
                tail = ""
                try:
                    with open(progress, encoding="utf-8", errors="replace") as f:
                        lines = [x for x in f.read().splitlines() if x.strip() and not x.startswith("LOGATLAS-")]
                        tail = lines[-1] if lines else ""
                except OSError:
                    pass
                msg = "За выбранный период в журналах нет записей — выберите период шире" if "(12)" in err \
                    else f"Сбор не удался: {tail or err or f'код {p.returncode}'}"
                _set(jid, state="error", error=msg)
            winlocal.case_fail(cid, msg)
            return
        if not os.path.exists(archive) or os.path.getsize(archive) == 0:
            _set(jid, state="error", error="Сборщик не создал архив")
            winlocal.case_fail(cid, "Сборщик не создал архив")
            return
        took = time.time() - t0
        winlocal.import_archive(archive, platform="macos", host=host(), case_id=cid,
                                how=f"на этом Mac за {days} сут. (сбор занял {int(took // 60)}:{int(took % 60):02d})")
        _set(jid, state="done")
    except Exception as e:  # noqa: BLE001
        log.exception("local collect failed")
        _set(jid, state="error", error=str(e))
        winlocal.case_fail(cid, f"Сбор не удался: {e}")
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
    busy = winlocal.active_job(JOBS, _lock)
    if busy:                                       # сбор уже идёт — открыть его, а не запускать второй
        return busy["id"]
    if not os.path.isfile(os.path.join(collect.SCRIPTS_DIR, "collect-macos.sh")):
        raise RuntimeError("Не найден сборщик scripts/collect-macos.sh — скачайте LogAtlas заново")
    jid = uuid.uuid4().hex[:8]
    out_dir = os.path.join(LOCAL_DIR, jid)
    os.makedirs(out_dir, exist_ok=True)
    cid = winlocal.create_case("macos", host(), "Подтвердите права администратора в окне macOS…")
    db.add_log(cid, "info", f"Период: последние {days} сут.; нужны права администратора — macOS спросит пароль в своём окне")
    with _lock:
        JOBS[jid] = {"id": jid, "state": "starting", "case": cid, "error": None, "started": time.time(), "days": days}
    threading.Thread(target=_run, args=(jid, cid, days, out_dir), daemon=True, name=f"local-{jid}").start()
    return jid


def status(jid: str) -> dict | None:
    with _lock:
        j = JOBS.get(jid)
        return dict(j) if j else None


def cleanup():
    shutil.rmtree(LOCAL_DIR, ignore_errors=True)
