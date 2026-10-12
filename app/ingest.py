"""Приём файлов: распаковка архивов, выбор парсера, загрузка в базу дела, пост-обработка."""
from __future__ import annotations

import bz2
import gzip
import io
import json
import logging
import lzma
import os
import queue
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import traceback
import zipfile
from collections import Counter
from datetime import datetime, timedelta

from . import analytics, db, detections, sigma
from .parsers import choose
from .parsers.base import Ctx, Source

log = logging.getLogger("logatlas.ingest")

BATCH = 5000
MAX_DEPTH = 4
SKIP_NAMES = re.compile(r"(^|/)(\._[^/]*|\.DS_Store|__MACOSX/.*|Thumbs\.db)$")
ROTATE_SUFFIX = re.compile(r"(\.\d+)?(\.(gz|bz2|xz|zst|tgz))?$")
GENERIC_PARSERS = {"syslog", "text", "jsonl", "win_xml", "win_json", "win_csv"}


# ---------------------------------------------------------------------------
# Чтение архивов
# ---------------------------------------------------------------------------
class Peek(io.RawIOBase):
    """Позволяет заглянуть в начало потока и продолжить чтение с начала."""

    def __init__(self, f, n=1024):
        self.f = f
        self.buf = f.read(n) or b""
        self.head = self.buf

    def readable(self):
        return True

    def readinto(self, b):
        if self.buf:
            n = min(len(b), len(self.buf))
            b[:n] = self.buf[:n]
            self.buf = self.buf[n:]
            return n
        data = self.f.read(len(b))
        if not data:
            return 0
        n = len(data)
        b[:n] = data
        return n


class Counting(io.RawIOBase):
    def __init__(self, f):
        self.f = f
        self.n = 0

    def readable(self):
        return True

    def readinto(self, b):
        data = self.f.read(len(b))
        if not data:
            return 0
        self.n += len(data)
        b[:len(data)] = data
        return len(data)


def _strip_ext(name, ext):
    return name[: -len(ext)] if name.lower().endswith(ext) else name


def explode(f, name, size, mtime, tmpdir, depth=0, notes=None):
    """Генератор Source для всех «листовых» файлов (рекурсивно по архивам и сжатию)."""
    if SKIP_NAMES.search(name):
        return
    p = Peek(f, 1024)
    h = p.head
    if depth > MAX_DEPTH:
        yield Source(name, io.BufferedReader(p, 1 << 20), size, mtime)
        return
    if h[:2] == b"\x1f\x8b":
        inner = gzip.GzipFile(fileobj=io.BufferedReader(p, 1 << 20))
        n = _strip_ext(_strip_ext(name, ".gz"), ".tgz")
        if name.lower().endswith(".tgz"):
            n += ".tar"
        yield from explode(inner, n, None, mtime, tmpdir, depth + 1, notes)
        return
    if h[:3] == b"BZh":
        inner = bz2.BZ2File(io.BufferedReader(p, 1 << 20))
        yield from explode(inner, _strip_ext(name, ".bz2"), None, mtime, tmpdir, depth + 1, notes)
        return
    if h[:6] == b"\xfd7zXZ\x00":
        inner = lzma.LZMAFile(io.BufferedReader(p, 1 << 20))
        yield from explode(inner, _strip_ext(name, ".xz"), None, mtime, tmpdir, depth + 1, notes)
        return
    if h[:4] == b"(\xb5/\xfd":
        try:
            import zstandard
        except ImportError:
            if notes is not None:
                notes.append(f"{name}: формат zstd не поддерживается")
            return
        inner = zstandard.ZstdDecompressor().stream_reader(io.BufferedReader(p, 1 << 20))
        yield from explode(inner, _strip_ext(name, ".zst"), None, mtime, tmpdir, depth + 1, notes)
        return
    if h[:6] == b"7z\xbc\xaf\x27\x1c" or h[:4] == b"Rar!":
        if notes is not None:
            notes.append(f"{name}: архивы 7z/RAR не поддерживаются — перепакуйте в zip или tar.gz")
        return
    if len(h) > 262 and h[257:262] == b"ustar":
        with tarfile.open(fileobj=io.BufferedReader(p, 1 << 20), mode="r|") as tf:
            for m in tf:
                if not m.isfile():
                    continue
                mf = tf.extractfile(m)
                if mf is None:
                    continue
                yield from explode(mf, f"{name}/{m.name}" if "/" in name else m.name, m.size, m.mtime, tmpdir, depth + 1, notes)
        return
    if h[:4] == b"PK\x03\x04":
        fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(p, out, 1 << 20)
            with zipfile.ZipFile(tmp) as zf:
                for zi in zf.infolist():
                    if zi.is_dir():
                        continue
                    zm = time.mktime(zi.date_time + (0, 0, -1)) if zi.date_time else mtime
                    with zf.open(zi) as zfile:
                        yield from explode(zfile, f"{name}/{zi.filename}" if "/" in name else zi.filename, zi.file_size, zm,
                                           tmpdir, depth + 1, notes)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return
    yield Source(name, io.BufferedReader(p, 1 << 20), size, mtime)


def source_label(parser, ctx, base):
    if parser.name in GENERIC_PARSERS or not ctx.label:
        return ROTATE_SUFFIX.sub("", base) or base
    return ctx.label


# ---------------------------------------------------------------------------
# Очередь обработки
# ---------------------------------------------------------------------------
# Сборы, которые просили удалить прямо во время разбора. Разбор проверяет этот список на переходах
# между файлами и прекращает работу сам: насильно снять поток Python нельзя, а держать удаление
# заложником зависшего разбора — хуже всего, иначе такой сбор не убрать уже никогда.
CANCELLED: set[str] = set()
_cancel_lock = threading.Lock()


class Cancelled(Exception):
    pass


def cancel(case_id: str):
    with _cancel_lock:
        CANCELLED.add(case_id)


def cancelled(case_id: str) -> bool:
    with _cancel_lock:
        return case_id in CANCELLED


def _forget(case_id: str):
    with _cancel_lock:
        CANCELLED.discard(case_id)


def check_cancelled(case_id: str):
    if cancelled(case_id):
        raise Cancelled(case_id)


class Worker:
    def __init__(self):
        self.q = queue.Queue()
        self.current = None
        self.started = None
        self.th = threading.Thread(target=self._run, daemon=True, name="ingest")
        self.th.start()

    def submit(self, case_id, paths, tz_default, tz_mode):
        self.q.put((case_id, paths, tz_default, tz_mode))

    def _run(self):
        while True:
            job = self.q.get()
            cid = job[0]
            if cancelled(cid):                     # сбор удалили, пока он ждал очереди
                _forget(cid)
                continue
            self.current = cid
            self.started = time.time()
            try:
                run_ingest(*job)
            except Cancelled:
                log.info("ingest %s cancelled", cid)
            except Exception as e:  # noqa: BLE001
                log.exception("ingest failed")
                if not cancelled(cid):
                    db.set_case(cid, status="error", message=f"{type(e).__name__}: {e}")
                    db.add_log(cid, "err", f"Разбор не удался: {type(e).__name__}: {e}")
            finally:
                self.current = None
                self.started = None
                if cancelled(cid):                 # убрать всё, что разбор успел создать
                    _forget(cid)
                    db.drop_case_files(cid)


WORKER: Worker | None = None


def worker():
    global WORKER
    if WORKER is None:
        WORKER = Worker()
    return WORKER


# ---------------------------------------------------------------------------
# Параллельный разбор
#
# Главный процесс читает загруженные архивы и раздаёт работу процессам-воркерам
# (по числу ядер): члены tar сохраняются во временные файлы как есть (сжатые — сжатыми),
# члены zip читаются воркерами прямо из архива, а большие построчные журналы
# (unified log NDJSON, journalctl -o json) режутся на куски по границам строк.
# Каждый воркер пишет события в свою временную базу; в конце главный процесс копирует
# их в базу сбора одним INSERT … SELECT, сразу со сдвигом часового пояса и подписью
# источника — без полных перезаписей таблицы.
# ---------------------------------------------------------------------------
SPILL_MAX = 64 << 20        # обычные файлы из tar до этого размера уходят воркеру целиком
CHUNK = 48 << 20            # размер куска большого построчного журнала
PART_COLS = ("fidx",) + db.EVENT_COLS
PART_SCHEMA = f"CREATE TABLE IF NOT EXISTS events({', '.join(PART_COLS)})"
PART_INSERT = f"INSERT INTO events({','.join(PART_COLS)}) VALUES ({','.join('?' * len(PART_COLS))})"
COMPRESSED = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"(\xb5/\xfd")

_COUNTER = None             # общий счётчик разобранных событий (multiprocessing.Value)


def n_workers() -> int:
    try:
        n = int(os.environ.get("LOGATLAS_WORKERS", "0"))
    except ValueError:
        n = 0
    return max(1, min(n or (os.cpu_count() or 1), 16))


def _init_worker(counter):
    global _COUNTER
    _COUNTER = counter
    # веб-интерфейс остаётся отзывчивым во время разбора
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except (OSError, AttributeError):
        pass


def _open_part(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute(PART_SCHEMA)
    return con


def _is_compressed(h: bytes) -> bool:
    return any(h.startswith(m) for m in COMPRESSED)


def _is_tar(h: bytes) -> bool:
    return len(h) > 262 and h[257:262] == b"ustar"


def _splittable(base: str, head: bytes) -> bool:
    cls, _ = choose(base, head)
    if not cls or not getattr(cls, "splittable", False):
        return False
    return head.lstrip()[:1] != b"["        # JSON-массив (log show --style json) режется только целиком


def parse_source(con, src, fidx, tz_default, tmpdir, default_mtime, counter=None, on_batch=None):
    """Разбор одного файла в часть-базу. Возвращает описание для таблицы files (None — пустой файл)."""
    if not src.head:
        return None
    info = {"name": src.name, "size": src.size, "parser": None, "plabel": None, "label": None, "platform": None,
            "events": 0, "noise": 0, "bad": 0, "error": None, "status": "ok", "tz": {}, "hosts": {}}
    parser_cls, _ = choose(src.base, src.head)
    if not parser_cls:
        info.update(status="skipped", error="Двоичный или неизвестный формат")
        return info
    info["parser"] = parser_cls.name
    info["plabel"] = getattr(parser_cls, "label", "") or parser_cls.name
    anchor = datetime.utcfromtimestamp(src.mtime or default_mtime) + timedelta(minutes=tz_default)
    ctx = Ctx(src, anchor, tmpdir)
    batch, cnt, err = [], 0, None

    def flush():
        nonlocal cnt
        if not batch:
            return
        con.executemany(PART_INSERT, batch)
        cnt += len(batch)
        if counter is not None:
            with counter.get_lock():
                counter.value += len(batch)
        batch.clear()
        if on_batch:
            on_batch()

    try:
        for e in parser_cls().parse(src, ctx):
            batch.append((fidx,) + e)
            if len(batch) >= BATCH:
                flush()
        flush()
    except Exception as ex:  # noqa: BLE001
        err = f"{type(ex).__name__}: {ex}"
        log.warning("parse error %s: %s\n%s", src.name, err, traceback.format_exc())
        flush()
    info.update(label=source_label(parser_cls, ctx, src.base), platform=ctx.platform or parser_cls.platform,
                events=cnt, noise=ctx.noise, bad=ctx.bad, error=err, status="error" if err and not cnt else "ok",
                tz=dict(ctx.tz_votes), hosts=dict(ctx.host_votes))
    return info


def _run_task(task):
    """Воркер: разобрать файл (или член zip) со всеми вложенными архивами в свою часть-базу."""
    kind = task["kind"]
    os.makedirs(task["tmp"], exist_ok=True)
    con = _open_part(task["part"])
    leaves, notes = [], []
    zf = None
    try:
        if kind == "zip":
            zf = zipfile.ZipFile(task["path"])
            fh = zf.open(task["member"])
        else:
            fh = open(task["path"], "rb")
        with fh:
            try:
                for src in explode(fh, task["name"], task["size"], task["mtime"], task["tmp"], task["depth"], notes):
                    info = parse_source(con, src, len(leaves), task["tz_default"], task["tmp"],
                                        task["default_mtime"], _COUNTER)
                    if info:
                        leaves.append(info)
            except Exception as ex:  # noqa: BLE001  повреждённый архив — разобранное до ошибки сохраняем
                log.warning("cannot read %s: %s", task["name"], ex)
                notes.append(f"{task['name'].removeprefix('./')}: не удалось дочитать — {type(ex).__name__}: {ex}")
        con.commit()
    finally:
        con.close()
        if zf is not None:
            zf.close()
        if task.get("cleanup"):
            try:
                os.remove(task["path"])
            except OSError:
                pass
        shutil.rmtree(task["tmp"], ignore_errors=True)
    return {"leaves": leaves, "notes": notes}


class _Job:
    """Состояние одного разбора: план (единицы работы по порядку), воркеры, прогресс."""

    def __init__(self, case_id, tz_default, tmpdir, notes):
        self.case_id = case_id
        self.tz_default = tz_default
        self.tmpdir = tmpdir
        self.notes = notes
        self.units = []                 # по порядку: {"part", "result" | "future", "group", "upload", ...}
        self.seq = 0
        self.pool = None
        self.counter = None
        self.inline_events = 0
        self.read_bytes = 0             # прочитано из загруженных файлов
        self.total_bytes = 1
        self.last_progress = 0.0
        self.progress = 0.02
        self.logged = 0
        self.groups = {}                # кусок большого файла → сколько кусков
        self.counting = None            # счётчик прочитанного из текущего загруженного файла
        self.n = 1

    # -- воркеры ------------------------------------------------------------
    def start_pool(self):
        n = n_workers()
        if n < 2:
            return
        try:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor
            ctx = mp.get_context("spawn")
            self.counter = ctx.Value("q", 0)
            self.pool = ProcessPoolExecutor(max_workers=n, mp_context=ctx, initializer=_init_worker,
                                            initargs=(self.counter,))
            self.n = n
        except Exception:  # noqa: BLE001
            log.exception("process pool unavailable, parsing in one process")
            self.pool = None

    def stop_pool(self):
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None

    def events(self):
        return self.inline_events + (self.counter.value if self.counter is not None else 0)

    def tick(self, stage=None, force=False):
        now = time.time()
        if not force and now - self.last_progress < 0.7:
            return
        self.last_progress = now
        done = sum(1 for u in self.units if "result" in u or (u.get("future") and u["future"].done()))
        read = self.read_bytes + (self.counting.n if self.counting is not None else 0)
        frac = 0.6 * min(1.0, read / self.total_bytes) + 0.4 * (done / max(1, len(self.units)))
        self.progress = max(self.progress, min(0.88, 0.02 + 0.86 * frac))
        if stage is None:
            par = f", параллельно на {self.n} ядрах" if self.pool is not None else ""
            stage = f"Разбор: готово частей {done} из {len(self.units)}{par}"
        db.set_case(self.case_id, progress=self.progress, events=self.events(), stage=stage)

    # -- единицы работы --------------------------------------------------------
    def _part(self):
        self.seq += 1
        return os.path.join(self.tmpdir, f"part-{self.seq:05d}.db")

    def submit(self, kind, path, name, size, mtime, depth, upload, default_mtime, member=None, cleanup=False,
               group=None):
        part = self._part()
        task = {"kind": kind, "path": path, "member": member, "name": name, "size": size, "mtime": mtime,
                "depth": depth, "part": part, "tmp": part + ".tmp", "cleanup": cleanup, "tz_default": self.tz_default,
                "default_mtime": default_mtime}
        unit = {"part": part, "group": group, "upload": upload, "name": name, "size": size}
        if self.pool is not None:
            # не уходим далеко вперёд воркеров: иначе куски большого файла займут весь диск
            while sum(1 for u in self.units if u.get("future") and not u["future"].done()) >= self.n * 2:
                self.wait_some()
            unit["future"] = self.pool.submit(_run_task, task)
            self.units.append(unit)
        else:
            self.units.append(unit)
            unit["result"] = _run_task(task)
            self._done(unit)
        self.tick()

    def inline(self, src, upload, default_mtime):
        """Большой файл без возможности деления — разбирается здесь же, пока воркеры заняты своим."""
        part = self._part()
        con = _open_part(part)
        tmp = part + ".tmp"
        os.makedirs(tmp, exist_ok=True)
        before = self.inline_events
        try:
            def on_batch():
                self.inline_events = before + con.total_changes
                self.tick(stage=f"Разбор: {src.name}")
            info = parse_source(con, src, 0, self.tz_default, tmp, default_mtime, None, on_batch)
            con.commit()
        finally:
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)
        self.inline_events = before + (info["events"] if info else 0)
        unit = {"part": part, "group": None, "upload": upload, "name": src.name, "size": src.size,
                "result": {"leaves": [info] if info else [], "notes": []}}
        self.units.append(unit)
        self._done(unit)

    def wait_some(self):
        from concurrent.futures import FIRST_COMPLETED, wait
        pend = [u["future"] for u in self.units if u.get("future") and not u["future"].done()]
        if pend:
            wait(pend, timeout=0.7, return_when=FIRST_COMPLETED)
        self.collect()
        self.tick()

    def collect(self):
        for u in self.units:
            f = u.get("future")
            if f is not None and f.done() and "result" not in u:
                u["result"] = f.result()        # исключение воркера — ошибка всего разбора
                del u["future"]
                self._done(u)

    def wait_all(self):
        while any(u.get("future") for u in self.units):
            self.wait_some()

    def _done(self, unit):
        """Журнал шагов: по файлу на строку (куски большого файла — одной строкой в конце)."""
        self.notes.extend(unit["result"]["notes"])
        if unit["group"]:
            return
        for info in unit["result"]["leaves"]:
            self.log_file(info)

    def log_file(self, info):
        name = info["name"].removeprefix("./")
        if info["status"] == "skipped":
            return
        if info["status"] == "error":
            db.add_log(self.case_id, "warn", f"{name}: ошибка разбора — {info['error']}")
            return
        self.logged += 1
        if self.logged <= 60:
            n_fmt = f"{info['events']:,}".replace(",", " ")
            db.add_log(self.case_id, "ok", f"{name}: {info['plabel']} — {n_fmt} событий")


def _split_lines(reader, job, name, size, mtime, depth, upload, default_mtime):
    """Резать построчный поток на куски CHUNK по границам строк и раздавать воркерам."""
    group = f"g{job.seq + 1}:{name}"
    n = 0
    rest = b""
    while True:
        block = reader.read(CHUNK)
        eof = not block
        data = rest + block
        if not eof:
            cut = data.rfind(b"\n")
            if cut < 0:
                rest = data
                continue
            rest, data = data[cut + 1:], data[:cut + 1]
        else:
            rest = b""
        if data:
            fd, path = tempfile.mkstemp(dir=job.tmpdir, suffix=".chunk")
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            n += 1
            job.submit("file", path, name, len(data), mtime, max(depth, MAX_DEPTH + 1), upload, default_mtime,
                       cleanup=True, group=group)
        if eof:
            break
    job.groups[group] = {"name": name, "size": size, "chunks": n}
    if n > 1:
        db.add_log(job.case_id, "info", f"{name.removeprefix('./')}: большой журнал разбит на части ({n}) для параллельного разбора")


def _plan_stream(f, name, size, mtime, depth, job, upload, default_mtime):
    """Как explode(), но раздаёт независимые части воркерам; остальное разбирает сам."""
    if SKIP_NAMES.search(name):
        return
    p = Peek(f, 64 * 1024)
    h = p.head
    if depth <= MAX_DEPTH and _is_tar(h):
        with tarfile.open(fileobj=io.BufferedReader(p, 1 << 20), mode="r|") as tf:
            for m in tf:
                if not m.isfile():
                    continue
                mf = tf.extractfile(m)
                if mf is None:
                    continue
                mname = f"{name}/{m.name}" if "/" in name else m.name
                _plan_member(mf, mname, m.size, m.mtime, depth + 1, job, upload, default_mtime)
        return
    if depth <= MAX_DEPTH and h[:2] == b"\x1f\x8b":
        inner = gzip.GzipFile(fileobj=io.BufferedReader(p, 1 << 20))
        n = _strip_ext(_strip_ext(name, ".gz"), ".tgz")
        if name.lower().endswith(".tgz"):
            n += ".tar"
        _plan_stream(inner, n, None, mtime, depth + 1, job, upload, default_mtime)
        return
    if depth <= MAX_DEPTH and (_is_compressed(h) or h[:4] == b"PK\x03\x04"):
        # прочие сжатые потоки и zip внутри потока — как раньше, через explode
        for src in explode(p, name, size, mtime, job.tmpdir, depth, job.notes):
            job.inline(src, upload, default_mtime)
        return
    if job.pool is not None and _splittable(os.path.basename(name), h) and (size is None or size > CHUNK):
        _split_lines(io.BufferedReader(p, 1 << 20), job, name, size, mtime, depth, upload, default_mtime)
        return
    for src in explode(p, name, size, mtime, job.tmpdir, depth, job.notes):
        job.inline(src, upload, default_mtime)


def _plan_member(mf, name, size, mtime, depth, job, upload, default_mtime):
    """Член tar: сжатый или небольшой — во временный файл и воркеру; иначе разбор потоком."""
    if SKIP_NAMES.search(name):
        return
    p = Peek(mf, 64 * 1024)
    h = p.head
    small = size is not None and size <= SPILL_MAX
    if depth <= MAX_DEPTH and _is_tar(h):
        _plan_stream(p, name, size, mtime, depth, job, upload, default_mtime)
        return
    if _is_compressed(h) or h[:4] == b"PK\x03\x04" or small:
        fd, path = tempfile.mkstemp(dir=job.tmpdir, suffix=".member")
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(p, out, 1 << 20)
        job.submit("file", path, name, size, mtime, depth, upload, default_mtime, cleanup=True)
        return
    _plan_stream(p, name, size, mtime, depth, job, upload, default_mtime)


def _plan_upload(path, job):
    """Загруженный файл: zip — по членам воркерам; tar/tar.gz — потоком; одиночный файл — воркеру целиком."""
    up_name = os.path.basename(path)
    st = os.stat(path)
    with open(path, "rb") as fh:
        h = fh.read(64 * 1024)
    if SKIP_NAMES.search(up_name):
        job.read_bytes += st.st_size
        return
    if h[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(path) as zf:
                infos = [zi for zi in zf.infolist() if not zi.is_dir()]
        except zipfile.BadZipFile as e:
            job.notes.append(f"{up_name}: повреждённый zip ({e})")
            job.read_bytes += st.st_size
            return
        for zi in infos:
            zm = time.mktime(zi.date_time + (0, 0, -1)) if zi.date_time else st.st_mtime
            job.submit("zip", path, zi.filename, zi.file_size, zm, 1, up_name, st.st_mtime, member=zi.filename)
            job.read_bytes += zi.compress_size
        return
    inner_head = h
    if h[:2] == b"\x1f\x8b":
        try:
            with gzip.open(path, "rb") as g:
                inner_head = g.read(64 * 1024)
        except (OSError, EOFError):
            inner_head = b""
    archive = _is_tar(h) or (h[:2] == b"\x1f\x8b" and _is_tar(inner_head))
    big_lines = job.pool is not None and _splittable(_strip_ext(up_name, ".gz"), inner_head) \
        and st.st_size > (CHUNK if h == inner_head else CHUNK // 8)
    if archive or big_lines:
        with open(path, "rb") as fh:
            counter = Counting(fh)
            job.counting = counter
            _plan_stream(io.BufferedReader(counter, 1 << 20), up_name, st.st_size, st.st_mtime, 0, job, up_name,
                         st.st_mtime)
            job.counting = None
        job.read_bytes += st.st_size
        return
    job.submit("file", path, up_name, st.st_size, st.st_mtime, 0, up_name, st.st_mtime)
    job.read_bytes += st.st_size


def run_ingest(case_id, paths, tz_default, tz_mode):
    t_start = time.time()
    check_cancelled(case_id)
    case = db.get_case(case_id)
    if not case:
        log.info("case %s was deleted before processing", case_id)
        return
    first_load = not case.get("events")
    if not first_load:
        tz_default = case["tz"]
        tz_mode = "fixed"
    db.set_case(case_id, status="processing", progress=0.01, stage="Распаковка и разбор", message=None)
    db.add_log(case_id, "info", f"Разбор: распаковка и определение формата ({len(paths)} файл(ов) на входе)")
    tmpdir = tempfile.mkdtemp(dir=db.TMP_DIR)
    notes = []
    job = _Job(case_id, tz_default, tmpdir, notes)
    job.total_bytes = sum(os.path.getsize(p) for p in paths) or 1
    con = None
    try:
        from concurrent.futures.process import BrokenProcessPool
        job.start_pool()
        if job.pool is not None:
            db.add_log(case_id, "info", f"Разбор идёт параллельно: процессов {job.n}")
        try:
            for path in paths:
                check_cancelled(case_id)
                _plan_upload(path, job)
            job.wait_all()
            check_cancelled(case_id)
            job.collect()
        except BrokenProcessPool:
            # процесс-воркер убит (чаще всего системе не хватило памяти) — начинаем заново в одном процессе
            log.warning("worker process died, re-parsing case %s in one process", case_id)
            db.add_log(case_id, "warn", "Процесс разбора аварийно завершился (возможно, не хватило памяти) — "
                                        "разбираю заново в одном процессе")
            job.stop_pool()
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.makedirs(tmpdir, exist_ok=True)
            del notes[:]
            job = _Job(case_id, tz_default, tmpdir, notes)
            job.total_bytes = sum(os.path.getsize(p) for p in paths) or 1
            for path in paths:
                check_cancelled(case_id)
                _plan_upload(path, job)
        job.stop_pool()
        check_cancelled(case_id)
        t_parse = time.time() - t_start

        # --- сводим результаты ---------------------------------------------------
        tz_votes, host_votes = Counter(), Counter()
        for u in job.units:
            for info in u["result"]["leaves"]:
                tz_votes.update(info["tz"])
                host_votes.update(info["hosts"])
        tz = tz_default
        if tz_mode == "auto" and first_load:
            nz = [(k, v) for k, v in tz_votes.items() if k is not None]
            if nz:
                tz = max(nz, key=lambda kv: kv[1])[0]

        # строки таблицы files: куски одного большого журнала — одна строка
        fmap = []           # по единицам: {fidx: (file_id, label)}
        group_rows = {}
        skipped = 0
        now = time.time()
        with db.catalog() as c:
            for u in job.units:
                m = {}
                for fidx, info in enumerate(u["result"]["leaves"]):
                    g = u["group"]
                    if g and g in group_rows:
                        row = group_rows[g]
                        row["events"] += info["events"]
                        row["noise"] += info["noise"]
                        row["bad"] += info["bad"]
                        if info["error"] and not row["error"]:
                            row["error"] = info["error"]
                        m[fidx] = (row["id"], row["label"])
                        continue
                    size = job.groups[g]["size"] if g else info["size"]
                    cur = c.execute(
                        "INSERT INTO files(case_id,path,upload,size,parser,label,platform,events,noise,bad,status,error,added) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (case_id, info["name"], u["upload"], size, info["parser"], info["label"], info["platform"],
                         info["events"], info["noise"], info["bad"],
                         {"skipped": "skipped"}.get(info["status"], info["status"]), info["error"], now))
                    if info["status"] == "skipped":
                        skipped += 1
                    if g:
                        group_rows[g] = dict(info, id=cur.lastrowid)
                    m[fidx] = (cur.lastrowid, info["label"])
                fmap.append(m)
            for g, row in group_rows.items():
                c.execute("UPDATE files SET events=?, noise=?, bad=?, status=?, error=? WHERE id=?",
                          (row["events"], row["noise"], row["bad"], "error" if row["error"] and not row["events"] else "ok",
                           row["error"], row["id"]))
        for g, row in group_rows.items():
            job.log_file(row)
        if job.logged > 60:
            db.add_log(case_id, "ok", f"… и ещё {job.logged - 60} файл(ов)")
        if skipped:
            db.add_log(case_id, "warn", f"Пропущено файлов неизвестного формата: {skipped} (список — во вкладке «Источники»)")
        for n in notes:
            db.add_log(case_id, "warn", n)

        # --- копирование в базу сбора ---------------------------------------------
        n_events = job.events()
        db.set_case(case_id, progress=0.9, stage="Запись в базу, часовой пояс", events=n_events)
        db.add_log(case_id, "info", f"Разобрано за {int(t_parse // 60)}:{int(t_parse % 60):02d}; запись в базу, "
                                    "часовой пояс, индексы и правила обнаружения")
        con = db.case_db(case_id, write=True)
        con.execute("PRAGMA synchronous=OFF")
        analytics.drop_overview(con)        # сводка пересчитается в конце разбора
        shift = tz * 60000
        cols = ", ".join(db.EVENT_COLS)
        sel = ", ".join(
            "CASE WHEN p.z = 1 THEN p.t + ? ELSE p.t END" if c_ == "t" else "0" if c_ == "z"
            else "coalesce(p.source, m.label)" if c_ == "source" else f"p.{c_}"
            for c_ in db.EVENT_COLS)
        file_stats = {}
        con.execute("CREATE TEMP TABLE IF NOT EXISTS fmap(fidx INTEGER PRIMARY KEY, file_id INTEGER, label TEXT)")
        for u, m in zip(job.units, fmap):
            if not m or not os.path.exists(u["part"]):
                continue
            con.execute("ATTACH DATABASE ? AS p", (u["part"],))
            try:
                con.execute("DELETE FROM temp.fmap")
                con.executemany("INSERT INTO temp.fmap VALUES (?,?,?)", [(k, v[0], v[1]) for k, v in m.items()])
                for fidx, a, b in con.execute(
                        "SELECT fidx, min(CASE WHEN z = 1 THEN t + ? ELSE t END), max(CASE WHEN z = 1 THEN t + ? ELSE t END) "
                        "FROM p.events GROUP BY fidx", (shift, shift)):
                    fid = m[fidx][0]
                    oa, ob = file_stats.get(fid, (a, b))
                    file_stats[fid] = (min(a, oa), max(b, ob))
                con.execute(f"INSERT INTO main.events(file_id, {cols}) SELECT m.file_id, {sel} "
                            f"FROM p.events p JOIN temp.fmap m ON m.fidx = p.fidx ORDER BY p.rowid", (shift,))
                con.commit()
            finally:
                con.execute("DETACH DATABASE p")
            os.remove(u["part"])
        with db.catalog() as c:
            for fid, (a, b) in file_stats.items():
                c.execute("UPDATE files SET t_min=?, t_max=? WHERE id=?", (a, b, fid))
        db.set_case(case_id, progress=0.92, stage="Индексы")
        con.executescript(db.CASE_INDEXES)
        with db.catalog() as c:
            plats = c.execute("SELECT platform, sum(events) FROM files WHERE case_id=? AND platform IS NOT NULL "
                              "AND platform!='any' GROUP BY platform ORDER BY 2 DESC", (case_id,)).fetchall()
        platform = plats[0][0] if plats else (case or {}).get("platform") or "other"
        if len(plats) > 1 and plats[1][1] > 0.2 * plats[0][1]:
            platform = "mixed"
        db.set_case(case_id, progress=0.94, stage="Правила обнаружения")
        nf = detections.run(con)
        try:
            ns = sigma.run(con, platform)
            if ns:
                nf.update(ns)
                db.add_log(case_id, "info", f"Правила Sigma: сработало {len(ns)}, событий {sum(ns.values())}")
        except Exception:  # noqa: BLE001  ошибка в правилах Sigma не должна ломать разбор
            log.exception("sigma rules failed for case %s", case_id)
        con.execute("PRAGMA analysis_limit=2000")
        con.execute("ANALYZE")
        con.commit()
        db.set_case(case_id, progress=0.97, stage="Сводка для обзора")
        db.add_log(case_id, "info", "Сводка для вкладки «Обзор»")
        t_ov = time.time()
        analytics.save_overview(con, analytics.overview(con))
        log.info("case %s overview in %.1fs", case_id, time.time() - t_ov)

        tot = con.execute("SELECT count(*), min(t), max(t) FROM events").fetchone()
        host = host_votes.most_common(1)[0][0] if host_votes else (case or {}).get("host")
        if case and case.get("host") and not first_load:
            host = case["host"]
        summary = {"notes": notes, "findings": nf, "took": round(time.time() - t_start, 1)}
        db.set_case(case_id, status="ready", progress=1.0, stage="Готово", events=tot[0], t_min=tot[1], t_max=tot[2],
                    tz=tz, platform=platform, host=host, summary=json.dumps(summary, ensure_ascii=False),
                    message="; ".join(notes) if notes else None)
        log.info("case %s ingested: %s events in %.1fs (parse %.1fs)", case_id, tot[0], time.time() - t_start, t_parse)
        took = time.time() - t_start
        rules = con.execute("SELECT count(DISTINCT rule) FROM findings").fetchone()[0]
        n_fmt = f"{tot[0]:,}".replace(",", " ")
        db.add_log(case_id, "ok", f"Готово: {n_fmt} событий, сработало правил обнаружения: {rules}, "
                                  f"разбор занял {int(took // 60)}:{int(took % 60):02d}")
    finally:
        job.stop_pool()
        if con is not None:
            con.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
