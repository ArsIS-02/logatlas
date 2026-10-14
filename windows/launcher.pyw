"""LogAtlas без Docker: сервер для окна LogAtlas (Windows и macOS) и служба Windows.

Обычно этот файл запускает окно: LogAtlas.exe (windows/LogAtlas.cpp) или LogAtlas.app (macos/LogAtlas.swift)
с параметром --window. На macOS данные — ~/Library/Application Support/LogAtlas, окно узнаёт порт из
стандартного вывода, а закрытие окна (конец стандартного ввода) завершает сервер.

Режимы:
  --window          бэкенд окна LogAtlas: панель на 127.0.0.1, номер порта сообщается окну через канал
                    LOGATLAS_HOST_PIPE; данные — %LOCALAPPDATA%\\LogAtlas (или .\\data, если рядом portable.txt)
  (без параметров)  запасной режим без окна (нет WebView2): панель в браузере и значок в области уведомлений
  --autostart       то же при входе в Windows, но без открытия браузера
  --service         служба Windows (запускает WinSW): панель доступна из сети, данные — %ProgramData%\\LogAtlas
  --quit            закрыть запущенные приложения LogAtlas (нужно установщику при обновлении)

Настройки — settings.json в папке данных: {"port": 8484, "host": "127.0.0.1", "workers": 0}.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# в сборке launcher.pyw лежит рядом с app\, в репозитории — в windows\
ROOT = HERE if os.path.isdir(os.path.join(HERE, "app")) else os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# pythonw.exe запускается без консоли: sys.stdout и sys.stderr = None, а запись в них роняет программу.
# Код ниже выполняется и в процессах-воркерах разбора (multiprocessing «spawn» заново загружает этот файл).
if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

APP = "LogAtlas"
WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"
DEFAULT_PORT = 8484
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
QUIT_EVENT = "Local\\LogAtlas-quit"

log = logging.getLogger("logatlas.windows")


# ---------------------------------------------------------------------------
# папки и настройки
def portable() -> bool:
    return os.path.exists(os.path.join(ROOT, "portable.txt"))


def data_dir(mode: str) -> str:
    if mode == "service":
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
        return os.path.join(base, APP)
    if portable():
        return os.path.join(ROOT, "data")
    if MACOS:
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP)


def load_settings(ddir: str, mode: str) -> dict:
    defaults = {"port": DEFAULT_PORT, "host": "0.0.0.0" if mode == "service" else "127.0.0.1", "workers": 0}
    path = os.path.join(ddir, "settings.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
        if isinstance(user, dict):
            defaults.update({k: user[k] for k in defaults if k in user})
    except FileNotFoundError:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(defaults, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    except (OSError, ValueError) as e:
        log.warning("settings.json не прочитан (%s), беру значения по умолчанию", e)
    try:
        defaults["port"] = int(defaults["port"])
    except (TypeError, ValueError):
        defaults["port"] = DEFAULT_PORT
    return defaults


def setup_logging(ddir: str, mode: str):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if mode == "service":
        # Службу запускает сам LogAtlas.exe (pythonw без консоли), поэтому писать в stdout некуда:
        # ведём журнал в %ProgramData%\LogAtlas\logs — туда же смотрит диагностика службы.
        logs = os.path.join(ddir, "logs")
        try:
            os.makedirs(logs, exist_ok=True)
        except OSError:
            logs = ddir
        h: logging.Handler = logging.handlers.RotatingFileHandler(
            os.path.join(logs, "logatlas.log"), maxBytes=5 << 20, backupCount=3, encoding="utf-8")
    else:
        h = logging.handlers.RotatingFileHandler(os.path.join(ddir, "logatlas.log"), maxBytes=5 << 20,
                                                 backupCount=3, encoding="utf-8")
    h.setFormatter(fmt)
    root.addHandler(h)


# ---------------------------------------------------------------------------
# Windows API
def _kernel32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.SetEvent.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.GetCurrentProcess.restype = wintypes.HANDLE
    return k


_lock_fd = None


def single_instance(ddir: str):
    """Мьютекс на папку данных: установленное и переносное приложения могут работать одновременно,
    а второй запуск того же приложения только открывает браузер. На macOS — блокировка файла."""
    if not WINDOWS:
        import fcntl
        global _lock_fd
        fd = os.open(os.path.join(ddir, ".instance.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        _lock_fd = fd
        return fd
    import ctypes
    import hashlib
    k = _kernel32()
    tag = hashlib.sha1(os.path.normcase(ddir).encode("utf-8")).hexdigest()[:16]
    h = k.CreateMutexW(None, False, f"Local\\LogAtlas-{tag}")
    if ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
        return None
    return h


def kill_children_with_me():
    """Процессы-воркеры разбора завершаются вместе с LogAtlas (объект задания «убить при закрытии»).
    Браузер и Проводник открываются с выходом из задания (CREATE_BREAKAWAY_FROM_JOB) и не закрываются."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = _kernel32()
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT()
    info.Basic.LimitFlags = 0x2000 | 0x0800      # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    if not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
        log.info("объект задания не назначен (код %s) — воркеры завершатся сами", ctypes.get_last_error())
    return job


def shell_open(target: str):
    """Открыть адрес или папку через Проводник: браузер запускается от имени пользователя и не входит
    в задание LogAtlas."""
    try:
        subprocess.Popen(["explorer.exe", target], creationflags=CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW)
    except OSError:
        os.startfile(target)  # noqa: S606


def signal_quit() -> bool:
    k = _kernel32()
    h = k.OpenEventW(0x0002, False, QUIT_EVENT)    # EVENT_MODIFY_STATE
    if not h:
        return False
    k.SetEvent(h)
    return True


def watch_quit(on_quit):
    k = _kernel32()
    h = k.CreateEventW(None, True, False, QUIT_EVENT)

    def run():
        k.WaitForSingleObject(h, 0xFFFFFFFF)
        log.info("получен сигнал закрытия (обновление или удаление LogAtlas)")
        on_quit()

    threading.Thread(target=run, daemon=True, name="quit-watch").start()


# ---------------------------------------------------------------------------
# автозапуск при входе (HKCU\...\Run)
def autostart_cmd() -> str:
    exe = os.path.join(ROOT, "LogAtlas.exe")
    if os.path.isfile(exe):
        return f'"{exe}" --autostart'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}" --autostart'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, APP)
            return bool(v)
    except OSError:
        return False


def set_autostart(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# сервер
def port_free(host: str, port: int) -> bool:
    try:                                        # порт уже слушает кто-то другой (например, служба на 0.0.0.0)
        socket.create_connection(("127.0.0.1", port), 0.3).close()
        return False
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(host: str, port: int) -> int:
    for p in range(port, port + 100):
        if port_free(host, p):
            return p
    raise RuntimeError(f"Нет свободного порта в диапазоне {port}–{port + 99}")


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def read_instance(ddir: str) -> dict | None:
    try:
        with open(os.path.join(ddir, "instance.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_instance(ddir: str, port: int, mode: str):
    try:
        with open(os.path.join(ddir, "instance.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "mode": mode, "started": time.time()}, f)
    except OSError:
        pass


def make_server(host: str, port: int):
    import uvicorn
    cfg = uvicorn.Config("app.main:app", host=host, port=port, log_config=None, access_log=False,
                         timeout_keep_alive=30, loop="asyncio", http="h11", ws="none")
    return uvicorn.Server(cfg)


def prepare_env(ddir: str, mode: str, settings: dict):
    os.environ["LOGATLAS_DATA"] = ddir
    os.environ["LOGATLAS_BIND"] = str(settings.get("host") or "")
    os.environ.setdefault("LOGATLAS_INBOX", os.path.join(ddir, "inbox"))
    if MACOS:
        os.environ["LOGATLAS_RUNTIME"] = "macos-app"
    else:
        os.environ["LOGATLAS_RUNTIME"] = {"service": "windows-service"}.get(
            mode, "windows-portable" if portable() else "windows-app")
    if mode != "service":             # приложение: кнопка «Этот компьютер» / «Этот Mac»
        os.environ["LOGATLAS_DESKTOP"] = "1"
    if settings.get("workers"):
        os.environ["LOGATLAS_WORKERS"] = str(settings["workers"])
    tmp = os.path.join(ddir, "tmp")
    os.makedirs(tmp, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = tmp


# ---------------------------------------------------------------------------
# служба
def run_service():
    ddir = data_dir("service")
    os.makedirs(ddir, exist_ok=True)
    setup_logging(ddir, "service")
    settings = load_settings(ddir, "service")
    prepare_env(ddir, "service", settings)
    kill_children_with_me()
    port = settings["port"]
    log.info("LogAtlas (служба): http://%s:%s, данные: %s", settings["host"], port, ddir)
    write_instance(ddir, port, "service")
    make_server(settings["host"], port).run()      # Ctrl+C от WinSW — штатная остановка


# ---------------------------------------------------------------------------
# приложение со значком в области уведомлений
def service_url() -> str | None:
    """Если на компьютере работает служба LogAtlas — ярлык открывает её, а не второй экземпляр."""
    inst = read_instance(data_dir("service"))
    if inst and inst.get("mode") == "service" and healthy(int(inst.get("port") or 0)):
        return f"http://127.0.0.1:{inst['port']}/"
    return None


def message_box(text: str, title: str = APP, icon: int = 0x10):
    if os.environ.get("LOGATLAS_NO_DIALOGS") or not WINDOWS:   # проверки или macOS: окно покажет LogAtlas.app
        log.error("%s", text)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, icon)
    except Exception:  # noqa: BLE001
        pass


def run_app(autostart: bool, force_app: bool):
    if not portable() and not force_app:
        url = service_url()
        if url:
            if not autostart:
                shell_open(url)
            return

    ddir = data_dir("app")
    os.makedirs(ddir, exist_ok=True)
    setup_logging(ddir, "app")

    mutex = single_instance(ddir)
    if mutex is None:                       # уже запущено — только открыть браузер
        inst = read_instance(ddir)
        if not autostart and inst:
            for _ in range(30):
                if healthy(int(inst.get("port") or 0)):
                    break
                time.sleep(0.5)
            shell_open(f"http://127.0.0.1:{inst['port']}/")
        return

    settings = load_settings(ddir, "app")
    prepare_env(ddir, "app", settings)
    job = kill_children_with_me()  # noqa: F841  — держим ссылку до выхода
    try:
        port = pick_port(settings["host"], settings["port"])
    except RuntimeError as e:
        message_box(str(e))
        return
    url = f"http://127.0.0.1:{port}/"
    log.info("LogAtlas (%s): %s, данные: %s", "переносной" if portable() else "приложение", url, ddir)

    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ArsIS02.LogAtlas")
    except Exception:  # noqa: BLE001
        pass

    server = make_server(settings["host"], port)
    th = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    th.start()
    for _ in range(600):
        if server.started or not th.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        message_box("Не удалось запустить LogAtlas. Подробности — в журнале:\n" + os.path.join(ddir, "logatlas.log"))
        return
    write_instance(ddir, port, "app")
    if not autostart:
        shell_open(url)

    tray(url, ddir, server, th)


def tray(url: str, ddir: str, server, th):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.exception("pystray недоступен — работаю без значка; закрыть: LogAtlas.exe --quit")
        stop = threading.Event()
        watch_quit(stop.set)
        stop.wait()
        shutdown(server, th)
        return

    ico = os.path.join(ROOT, "logatlas.ico")
    if not os.path.exists(ico):
        ico = os.path.join(ROOT, "static", "icons", "favicon.ico")
    image = Image.open(ico)

    def open_panel(icon=None, item=None):
        shell_open(url)

    def open_data(icon, item):
        shell_open(ddir)

    def collect_here(icon, item):
        from app import winlocal

        def done(cid):
            try:
                icon.notify("Журналы собраны — идёт разбор", APP)
            except Exception:  # noqa: BLE001
                pass
            shell_open(f"{url}#/c/{cid}/overview")

        def run():                          # окно UAC не должно подвешивать меню значка
            try:
                winlocal.start(url, on_done=done)
            except RuntimeError as e:
                try:
                    icon.notify(str(e), APP)
                except Exception:  # noqa: BLE001
                    message_box(str(e), icon=0x30)

        threading.Thread(target=run, daemon=True, name="collect-here").start()

    def toggle_autostart(icon, item):
        try:
            set_autostart(not autostart_enabled())
        except OSError as e:
            message_box(f"Не удалось изменить автозапуск: {e}")

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть LogAtlas", open_panel, default=True),
        pystray.MenuItem("Собрать журналы этого компьютера…", collect_here),
        pystray.MenuItem("Папка данных", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Запускать при входе в Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon(APP, image, f"LogAtlas — {url}", menu)
    watch_quit(icon.stop)
    icon.run()
    shutdown(server, th)


# ---------------------------------------------------------------------------
# бэкенд окна LogAtlas.exe
_host_fd = None


def host_say(line: str):
    """Строка для окна: LOGATLAS-READY <порт> <app|service|existing> или LOGATLAS-ERROR <текст>.
    Окно передаёт свой конец канала через LOGATLAS_HOST_PIPE (номер наследуемого дескриптора)."""
    global _host_fd
    h = os.environ.get("LOGATLAS_HOST_PIPE")
    if not h:
        print(line, flush=True)
        return
    try:
        if _host_fd is None:
            import msvcrt
            _host_fd = msvcrt.open_osfhandle(int(h), os.O_WRONLY)
        os.write(_host_fd, (line + "\n").encode("utf-8"))
    except (OSError, ValueError) as e:
        log.warning("окно не получило «%s»: %s", line, e)


def watch_host_exit(on_exit):
    """macOS: окно держит стандартный ввод сервера открытым; конец ввода (окно закрыли или оно упало)
    или смена родителя — сигнал завершиться."""
    ppid = os.getppid()

    def stdin_eof():
        try:
            while sys.stdin and sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        log.info("окно закрыто (конец стандартного ввода)")
        on_exit()

    def parent_gone():
        while os.getppid() == ppid:
            time.sleep(1)
        log.info("окно завершилось")
        on_exit()

    threading.Thread(target=stdin_eof, daemon=True, name="host-stdin").start()
    threading.Thread(target=parent_gone, daemon=True, name="host-parent").start()


def stop_workers():
    """Завершить процессы-воркеры разбора (на Windows это делает объект задания)."""
    import multiprocessing
    for p in multiprocessing.active_children():
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def run_window(force_app: bool):
    if WINDOWS and not portable() and not force_app:
        inst = read_instance(data_dir("service"))
        if inst and inst.get("mode") == "service" and healthy(int(inst.get("port") or 0)):
            host_say(f"LOGATLAS-READY {int(inst['port'])} service")
            return

    ddir = data_dir("app")
    os.makedirs(ddir, exist_ok=True)
    setup_logging(ddir, "app")
    if single_instance(ddir) is None:   # работает запасной режим со значком — окно подключается к нему
        inst = read_instance(ddir)
        for _ in range(30):
            if inst and healthy(int(inst.get("port") or 0)):
                host_say(f"LOGATLAS-READY {int(inst['port'])} existing")
                return
            time.sleep(0.5)
            inst = read_instance(ddir)
        host_say("LOGATLAS-ERROR LogAtlas с этой папкой данных уже запущен, но не отвечает. " +
                 ("Закройте его (LogAtlas.exe --quit) и запустите снова." if WINDOWS else
                  "Завершите его в Мониторе активности и запустите снова."))
        return

    settings = load_settings(ddir, "app")
    prepare_env(ddir, "app", settings)
    job = kill_children_with_me() if WINDOWS else None  # noqa: F841
    try:
        port = pick_port(settings["host"], settings["port"])
    except RuntimeError as e:
        host_say(f"LOGATLAS-ERROR {e}")
        return
    log.info("LogAtlas (окно%s): http://127.0.0.1:%s, данные: %s", ", переносной" if portable() else "", port, ddir)
    server = make_server(settings["host"], port)
    th = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    th.start()
    for _ in range(600):
        if server.started or not th.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        host_say("LOGATLAS-ERROR Панель не запустилась. Подробности — в журнале: " + os.path.join(ddir, "logatlas.log"))
        return
    write_instance(ddir, port, "app")
    host_say(f"LOGATLAS-READY {port} app")
    stop = lambda: setattr(server, "should_exit", True)  # noqa: E731
    if WINDOWS:
        watch_quit(stop)                    # установщик просит закрыться
    else:
        watch_host_exit(stop)
    th.join()
    log.info("выход")
    stop_workers()
    logging.shutdown()
    os._exit(0)


def shutdown(server, th):
    log.info("выход")
    server.should_exit = True
    th.join(10)
    logging.shutdown()
    os._exit(0)     # воркеры разбора завершит объект задания


# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if "--quit" in args:
        signal_quit()
        return
    if "--service" in args:
        run_service()
        return
    if "--window" in args:
        try:
            run_window(force_app="--app" in args)
        except Exception as e:  # noqa: BLE001
            log.exception("сбой запуска")
            host_say(f"LOGATLAS-ERROR LogAtlas не запустился: {e}")
        return
    try:
        run_app(autostart="--autostart" in args, force_app="--app" in args)
    except Exception as e:  # noqa: BLE001
        log.exception("сбой запуска")
        message_box(f"LogAtlas не запустился: {e}")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
