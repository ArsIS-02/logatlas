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
import tarfile
import tempfile
import threading
import time
import traceback
import zipfile
from collections import Counter
from datetime import datetime, timedelta

from . import db, detections
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
class Worker:
    def __init__(self):
        self.q = queue.Queue()
        self.current = None
        self.th = threading.Thread(target=self._run, daemon=True, name="ingest")
        self.th.start()

    def submit(self, case_id, paths, tz_default, tz_mode):
        self.q.put((case_id, paths, tz_default, tz_mode))

    def _run(self):
        while True:
            job = self.q.get()
            self.current = job[0]
            try:
                run_ingest(*job)
            except Exception as e:  # noqa: BLE001
                log.exception("ingest failed")
                db.set_case(job[0], status="error", message=f"{type(e).__name__}: {e}")
            finally:
                self.current = None


WORKER: Worker | None = None


def worker():
    global WORKER
    if WORKER is None:
        WORKER = Worker()
    return WORKER


def run_ingest(case_id, paths, tz_default, tz_mode):
    t_start = time.time()
    case = db.get_case(case_id)
    first_load = not case or not case.get("events")
    if not first_load:
        tz_default = case["tz"]
        tz_mode = "fixed"
    db.set_case(case_id, status="processing", progress=0.01, stage="Распаковка и разбор", message=None)
    total = sum(os.path.getsize(p) for p in paths) or 1
    done_bytes = 0
    tmpdir = tempfile.mkdtemp(dir=db.TMP_DIR)
    con = db.case_db(case_id, write=True)
    con.execute("PRAGMA synchronous=OFF")
    tz_votes = Counter()
    host_votes = Counter()
    notes = []
    n_events = 0
    insert_sql = f"INSERT INTO events(file_id,{','.join(db.EVENT_COLS)}) VALUES (?,{','.join('?' * len(db.EVENT_COLS))})"
    last_progress = 0
    try:
        for path in paths:
            up_name = os.path.basename(path)
            with open(path, "rb") as fh:
                counter = Counting(fh)
                st = os.stat(path)
                for src in explode(io.BufferedReader(counter, 1 << 20), up_name, st.st_size, st.st_mtime, tmpdir, 0, notes):
                    if not src.head:
                        continue
                    parser_cls, score = choose(src.base, src.head)
                    anchor = datetime.utcfromtimestamp(src.mtime or st.st_mtime) + timedelta(minutes=tz_default)
                    with db.catalog() as c:
                        cur = c.execute(
                            "INSERT INTO files(case_id,path,upload,size,parser,status,added) VALUES (?,?,?,?,?,?,?)",
                            (case_id, src.name, up_name, src.size, parser_cls.name if parser_cls else None,
                             "processing", time.time()))
                        file_id = cur.lastrowid
                    if not parser_cls:
                        with db.catalog() as c:
                            c.execute("UPDATE files SET status='skipped', error=? WHERE id=?",
                                      ("Двоичный или неизвестный формат", file_id))
                        continue
                    ctx = Ctx(src, anchor, tmpdir)
                    parser = parser_cls()
                    label = None
                    cnt = 0
                    err = None
                    batch = []
                    try:
                        for e in parser.parse(src, ctx):
                            batch.append((file_id,) + e)
                            if len(batch) >= BATCH:
                                con.executemany(insert_sql, batch)
                                cnt += len(batch)
                                batch.clear()
                                now = time.time()
                                if now - last_progress > 0.7:
                                    last_progress = now
                                    prog = min(0.9, 0.02 + 0.85 * (done_bytes + counter.n) / total)
                                    db.set_case(case_id, progress=prog, events=n_events + cnt,
                                                stage=f"Разбор: {src.name}")
                        if batch:
                            con.executemany(insert_sql, batch)
                            cnt += len(batch)
                            batch.clear()
                    except Exception as ex:  # noqa: BLE001
                        err = f"{type(ex).__name__}: {ex}"
                        log.warning("parse error %s: %s\n%s", src.name, err, traceback.format_exc())
                        if batch:
                            con.executemany(insert_sql, batch)
                            cnt += len(batch)
                    con.commit()
                    label = source_label(parser_cls, ctx, src.base)
                    tz_votes.update(ctx.tz_votes)
                    host_votes.update(ctx.host_votes)
                    n_events += cnt
                    with db.catalog() as c:
                        c.execute("UPDATE files SET label=?, platform=?, events=?, noise=?, bad=?, status=?, error=? "
                                  "WHERE id=?", (label, ctx.platform or parser_cls.platform, cnt, ctx.noise, ctx.bad,
                                                 "error" if err and not cnt else "ok", err, file_id))
                done_bytes += st.st_size

        # --- пост-обработка -------------------------------------------------------
        db.set_case(case_id, progress=0.9, stage="Часовой пояс и индексы", events=n_events)
        tz = tz_default
        if tz_mode == "auto" and first_load:
            nz = [(k, v) for k, v in tz_votes.items() if k is not None]
            if nz:
                tz = max(nz, key=lambda kv: kv[1])[0]
        con.execute("UPDATE events SET t = t + ?, z = 0 WHERE z = 1", (tz * 60000,))
        # подписи источников (одним проходом через временную таблицу)
        with db.catalog() as c:
            labels = c.execute("SELECT id, label FROM files WHERE case_id=?", (case_id,)).fetchall()
        con.execute("CREATE TEMP TABLE IF NOT EXISTS flabel(id INTEGER PRIMARY KEY, label TEXT)")
        con.execute("DELETE FROM flabel")
        con.executemany("INSERT INTO flabel VALUES (?,?)", [(r["id"], r["label"]) for r in labels])
        con.execute("UPDATE events SET source=(SELECT label FROM flabel WHERE flabel.id=events.file_id) "
                    "WHERE source IS NULL")
        con.commit()
        con.executescript(db.CASE_INDEXES)
        stats = con.execute("SELECT file_id, min(t), max(t), count(*) FROM events GROUP BY file_id").fetchall()
        with db.catalog() as c:
            for fid, a, b, n in stats:
                c.execute("UPDATE files SET t_min=?, t_max=? WHERE id=?", (a, b, fid))
        db.set_case(case_id, progress=0.94, stage="Правила обнаружения")
        nf = detections.run(con)
        con.execute("ANALYZE")
        con.commit()

        tot = con.execute("SELECT count(*), min(t), max(t) FROM events").fetchone()
        with db.catalog() as c:
            plats = c.execute("SELECT platform, sum(events) FROM files WHERE case_id=? AND platform IS NOT NULL "
                              "AND platform!='any' GROUP BY platform ORDER BY 2 DESC", (case_id,)).fetchall()
        platform = plats[0][0] if plats else (case or {}).get("platform") or "other"
        if len(plats) > 1 and plats[1][1] > 0.2 * plats[0][1]:
            platform = "mixed"
        host = host_votes.most_common(1)[0][0] if host_votes else (case or {}).get("host")
        if case and case.get("host") and not first_load:
            host = case["host"]
        summary = {"notes": notes, "findings": nf, "took": round(time.time() - t_start, 1)}
        db.set_case(case_id, status="ready", progress=1.0, stage="Готово", events=tot[0], t_min=tot[1], t_max=tot[2],
                    tz=tz, platform=platform, host=host, summary=json.dumps(summary, ensure_ascii=False),
                    message="; ".join(notes) if notes else None)
        log.info("case %s ingested: %s events in %.1fs", case_id, tot[0], time.time() - t_start)
    finally:
        con.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
