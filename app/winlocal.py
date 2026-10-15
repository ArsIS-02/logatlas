"""LogAtlas для Windows: сбор журналов этого компьютера.

Работает только в режиме приложения (LOGATLAS_DESKTOP=1 ставит windows/launcher.pyw): панель запускает
сборщик scripts/collect-windows-gui.ps1 с правами администратора (запрос UAC), сборщик кладёт
архив в DATA_DIR/local/<задание>, а панель забирает его на разбор, как обычную загрузку.
В режиме службы окно показать некому (служба работает вне сеанса пользователя), поэтому там это выключено.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
import zipfile

from . import collect, db, ingest

log = logging.getLogger("logatlas.local")

ENABLED = os.name == "nt" and os.environ.get("LOGATLAS_DESKTOP") == "1"
LOCAL_DIR = os.path.join(db.DATA_DIR, "local")
WAIT_MAX = 6 * 3600          # окно сборщика может быть открыто долго, но не бесконечно

_lock = threading.Lock()
JOBS: dict[str, dict] = {}


def _launch_elevated(script: str, out_dir: str, url: str):
    """Запуск сборщика (collect-windows-gui.ps1) с окном от имени администратора. Возвращает дескриптор
    процесса или None, если пользователь отказался в окне UAC."""
    import ctypes
    from ctypes import wintypes

    args = f'-NoProfile -STA -ExecutionPolicy Bypass -File "{script}"'

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    sei = SEI()
    sei.cbSize = ctypes.sizeof(SEI)
    sei.fMask = 0x00000040                       # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell",
                              "v1.0", "powershell.exe")
    sei.lpParameters = args
    sei.nShow = 0
    ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)   # COINIT_APARTMENTTHREADED | DISABLE_OLE1DDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SEI)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    # Сборщик забирает архив из LA_OUT и знает адрес панели из LA_URL: передаём их переменными окружения —
    # дочерний процесс наследует их при запуске (у ShellExecuteEx нет своего блока окружения).
    saved = {k: os.environ.get(k) for k in ("LA_OUT", "LA_URL")}
    os.environ["LA_OUT"] = out_dir
    os.environ["LA_URL"] = url
    started, err = False, 0
    try:
        started = bool(shell32.ShellExecuteExW(ctypes.byref(sei)))
        err = ctypes.get_last_error()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if not started:
        if err == 1223:                          # ERROR_CANCELLED — «Нет» в окне UAC
            return None
        raise OSError(err, ctypes.FormatError(err))
    return sei.hProcess


def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _alive(handle) -> bool:
    return _k32().WaitForSingleObject(handle, 0) == 0x102     # WAIT_TIMEOUT


def _close(handle):
    try:
        _k32().CloseHandle(handle)
    except OSError:
        pass


def _ready_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return bool(z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _set(jid: str, **kw):
    with _lock:
        JOBS[jid].update(kw)


def _watch(jid: str, out_dir: str, handle, on_done):
    """Ждёт архив от сборщика и ставит его на разбор."""
    t0 = time.time()
    seen: dict[str, int] = {}
    cid = JOBS[jid]["case"]
    try:
        while time.time() - t0 < WAIT_MAX:
            if not case_alive(cid):                    # сбор удалили из списка — дальше не ждём
                _set(jid, state="closed", error="Сбор удалён")
                return
            alive = _alive(handle)
            for n in sorted(os.listdir(out_dir)):
                p = os.path.join(out_dir, n)
                if not n.lower().endswith(".zip") or not os.path.isfile(p):
                    continue
                size = os.path.getsize(p)
                # архив дописан: размер не меняется между проверками и zip читается целиком
                if seen.get(n) == size and size > 0 and _ready_zip(p):
                    import_archive(p, case_id=cid)
                    _set(jid, state="done")
                    if on_done:
                        try:
                            on_done(cid)
                        except Exception:  # noqa: BLE001
                            log.exception("on_done failed")
                    return
                if n not in seen:
                    case_stage(cid, "Сборщик упаковывает журналы в архив…", 0.06, "Сборщик выгрузил журналы и упаковывает архив")
                seen[n] = size
            if not alive and not seen:
                _set(jid, state="closed", error="Окно сборщика закрыто, архив не создан")
                case_fail(cid, "Окно сборщика закрыто, архив не создан")
                return
            _set(jid, state="collecting" if seen else "waiting")
            time.sleep(2)
        _set(jid, state="error", error="Сборщик не закончил работу за 6 часов")
        case_fail(cid, "Сборщик не закончил работу за 6 часов")
    except Exception as e:  # noqa: BLE001
        log.exception("local collect failed")
        _set(jid, state="error", error=str(e))
        case_fail(cid, f"Сбор не удался: {e}")
    finally:
        _close(handle)
        shutil.rmtree(out_dir, ignore_errors=True)


ACTIVE = ("starting", "waiting", "collecting")


def create_case(platform: str, host: str | None, stage: str) -> str:
    """Сбор журналов этого компьютера сразу появляется в списке сборов (статус «collecting»): ход виден на его
    странице, и его не потерять, перейдя на другую вкладку."""
    cid = uuid.uuid4().hex[:10]
    stamp = time.strftime("%d.%m.%Y %H:%M")
    name = f"{host} — этот компьютер, {stamp}" if host else f"Этот компьютер, {stamp}"
    now = time.time()
    with db.catalog() as c:
        c.execute("INSERT INTO cases(id,name,platform,host,tz,tz_mode,created,updated,status,progress,stage) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, name, platform, host or "этот компьютер", 0, "auto", now, now, "collecting", 0.02, stage))
    db.add_log(cid, "info", "Сбор журналов этого компьютера запущен из приложения LogAtlas")
    return cid


def case_alive(cid: str | None) -> bool:
    return bool(cid) and db.get_case(cid) is not None


def case_stage(cid: str, stage: str, progress: float | None = None, log_text: str | None = None):
    if not case_alive(cid):
        return
    kw = {"stage": stage}
    if progress is not None:
        kw["progress"] = progress
    db.set_case(cid, **kw)
    if log_text:
        db.add_log(cid, "info", log_text)


def case_fail(cid: str, msg: str):
    if not case_alive(cid):
        return
    db.add_log(cid, "err", msg)
    db.set_case(cid, status="error", stage="Сбор не удался", message=msg)


def case_drop(cid: str):
    """Сбор так и не начался (отказ в окне прав администратора) — убрать его из списка."""
    with db.catalog() as c:
        c.execute("DELETE FROM cases WHERE id=?", (cid,))


def import_archive(path: str, platform: str = "windows", host: str | None = None, how: str = "окном сборщика",
                   case_id: str | None = None) -> str | None:
    """Поставить архив журналов этого компьютера на разбор (общий для Windows и macOS). Если сбор уже создан
    (case_id), архив разбирается в нём; если его успели удалить — архив отбрасывается."""
    if case_id and not case_alive(case_id):
        return None
    cid = case_id or create_case(platform, host if host is not None else os.environ.get("COMPUTERNAME", ""), "В очереди")
    updir = os.path.join(db.UPLOADS_DIR, cid)
    os.makedirs(updir, exist_ok=True)
    dst = os.path.join(updir, f"{int(time.time() * 1000)}_{os.path.basename(path)}")
    shutil.move(path, dst)
    db.add_log(cid, "ok", f"Журналы этого компьютера собраны {how}: {os.path.basename(path)}, "
                          f"{collect._human(os.path.getsize(dst))}")
    db.set_case(cid, status="queued", stage="В очереди на разбор", progress=0)
    ingest.worker().submit(cid, [dst], 0, "auto")
    return cid


def active_job(jobs: dict, lock) -> dict | None:
    """Идущее задание, если его сбор ещё есть в списке (удалённый сбор задание освобождает)."""
    with lock:
        for j in jobs.values():
            if j["state"] in ACTIVE and case_alive(j.get("case")):
                return dict(j)
    return None


_import = import_archive


def start(url: str, on_done=None) -> str:
    """Открыть окно сборщика. url — адрес панели для кнопки «Открыть» в окне."""
    if not ENABLED:
        raise RuntimeError("Сбор журналов этого компьютера доступен в приложении LogAtlas для Windows")
    busy = active_job(JOBS, _lock)
    if busy:                                       # сбор уже идёт — открыть его, а не запускать второй
        return busy["id"]
    script = os.path.join(collect.SCRIPTS_DIR, "collect-windows-gui.ps1")
    if not os.path.isfile(script):
        raise RuntimeError("Не найден сборщик scripts\\collect-windows-gui.ps1 — переустановите LogAtlas")
    jid = uuid.uuid4().hex[:8]
    out_dir = os.path.join(LOCAL_DIR, jid)
    os.makedirs(out_dir, exist_ok=True)
    cid = create_case("windows", os.environ.get("COMPUTERNAME", ""), "Подтвердите права администратора (окно UAC)…")
    with _lock:
        JOBS[jid] = {"id": jid, "state": "starting", "case": cid, "error": None, "started": time.time()}
    try:
        handle = _launch_elevated(script, out_dir, url)
    except OSError as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        case_drop(cid)
        _set(jid, state="error", error=f"Не удалось запустить сборщик: {e}")
        raise RuntimeError(JOBS[jid]["error"]) from None
    if handle is None:
        shutil.rmtree(out_dir, ignore_errors=True)
        case_drop(cid)
        _set(jid, state="closed", error="Запуск отменён: для чтения журнала Security нужны права администратора")
        raise RuntimeError(JOBS[jid]["error"])
    _set(jid, state="waiting")
    case_stage(cid, "Открыто окно сборщика: выберите журналы и период и нажмите «Собрать»", 0.03,
               "Открыто окно сборщика журналов Windows")
    threading.Thread(target=_watch, args=(jid, out_dir, handle, on_done), daemon=True, name=f"local-{jid}").start()
    return jid


def start_headless(url: str, logs: list[str], since: int | None, until: int | None) -> str:
    """Запустить headless-сбор журналов Windows без GUI.

    Запускает collect-windows-headless.ps1 через ShellExecute runas (UAC).
    logs — список имён журналов (если пустой, используются дефолтные).
    since/until — границы периода в миллисекундах (или None для всего архива).
    """
    import ctypes
    from ctypes import wintypes

    if not ENABLED:
        raise RuntimeError("Сбор журналов этого компьютера доступен в приложении LogAtlas для Windows")
    busy = active_job(JOBS, _lock)
    if busy:
        return busy["id"]

    script = os.path.join(collect.SCRIPTS_DIR, "collect-windows-headless.ps1")
    if not os.path.isfile(script):
        raise RuntimeError("Не найден scripts\\collect-windows-headless.ps1 — переустановите LogAtlas")

    jid = uuid.uuid4().hex[:8]
    out_dir = os.path.join(LOCAL_DIR, jid)
    os.makedirs(out_dir, exist_ok=True)

    host = os.environ.get("COMPUTERNAME", "")
    stage = "Подтвердите права администратора (окно UAC)…"
    cid = create_case("windows", host, stage)

    # Формируем список журналов
    log_list = ",".join(logs) if logs else "*"

    # Устанавливаем переменные окружения для сборщика
    saved = {k: os.environ.get(k) for k in ("LA_OUT_DIR", "LA_URL", "LA_LOGS", "LA_SINCE", "LA_UNTIL", "LA_JOB_ID")}
    os.environ["LA_OUT_DIR"] = out_dir
    os.environ["LA_URL"] = url
    os.environ["LA_LOGS"] = log_list
    os.environ["LA_SINCE"] = str(since // 1000) if since else ""
    os.environ["LA_UNTIL"] = str(until // 1000) if until else ""
    os.environ["LA_JOB_ID"] = jid

    try:
        # Формируем команду для PowerShell
        script_arg = f'-NoProfile -STA -ExecutionPolicy Bypass -File "{script}"'

        class SEI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

        sei = SEI()
        sei.cbSize = ctypes.sizeof(SEI)
        sei.fMask = 0x00000040
        sei.lpVerb = "runas"
        sei.lpFile = os.path.join(os.environ.get("SystemRoot", r"C:\\Windows"), "System32", "WindowsPowerShell",
                                  "v1.0", "powershell.exe")
        sei.lpParameters = script_arg
        sei.nShow = 0
        ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SEI)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL

        started = bool(shell32.ShellExecuteExW(ctypes.byref(sei)))
        err = ctypes.get_last_error()

        if not started:
            if err == 1223:
                case_drop(cid)
                raise RuntimeError("Запуск отменён: для чтения журнала Security нужны права администратора")
            raise OSError(err, ctypes.FormatError(err))

        with _lock:
            JOBS[jid] = {"id": jid, "state": "waiting", "case": cid, "error": None, "started": time.time()}

        case_stage(cid, "Headless-сборщик запущен: журналы выгружаются…", 0.03,
                   "Headless-сборщик журналов Windows запущен")

        # Запускаем монитор в фоне (как для GUI, но без handle — ждём появления zip)
        threading.Thread(target=_watch_headless, args=(jid, out_dir, cid, url), daemon=True, name=f"local-h-{jid}").start()
        return jid

    finally:
        # Восстанавливаем окружение (дочерний процесс уже унаследовал)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _watch_headless(jid: str, out_dir: str, cid: str, url: str):
    """Ждёт zip-архив от headless-сборщика и ставит его на разбор."""
    t0 = time.time()
    seen: dict[str, int] = {}
    try:
        while time.time() - t0 < WAIT_MAX:
            if not case_alive(cid):
                _set(jid, state="closed", error="Сбор удалён")
                return

            for n in sorted(os.listdir(out_dir)):
                p = os.path.join(out_dir, n)
                if not n.lower().endswith(".zip") or not os.path.isfile(p):
                    continue
                size = os.path.getsize(p)
                if seen.get(n) == size and size > 0 and _ready_zip(p):
                    import_archive(p, case_id=cid)
                    _set(jid, state="done")
                    return
                if n not in seen:
                    case_stage(cid, "Сборщик упаковывает журналы…", 0.06)
                seen[n] = size

            if not seen:
                _set(jid, state="collecting")
            time.sleep(2)

        _set(jid, state="error", error="Сборщик не закончил работу за 6 часов")
        case_fail(cid, "Сборщик не закончил работу за 6 часов")
    except Exception as e:  # noqa: BLE001
        log.exception("headless collect failed")
        _set(jid, state="error", error=str(e))
        case_fail(cid, f"Сбор не удался: {e}")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def status(jid: str) -> dict | None:
    with _lock:
        j = JOBS.get(jid)
        return dict(j) if j else None


def cleanup():
    """Остатки прерванных заданий (приложение закрыли во время сбора)."""
    shutil.rmtree(LOCAL_DIR, ignore_errors=True)
