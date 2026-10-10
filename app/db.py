"""Хранилище: каталог дел (catalog.db) + отдельная SQLite-база на каждое дело.

Отдельный файл на дело — удаление дела мгновенно освобождает место,
а тяжёлая загрузка одного дела не мешает работе с другими.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("LOGATLAS_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
CASES_DIR = os.path.join(DATA_DIR, "cases")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
INBOX_DIR = os.environ.get("LOGATLAS_INBOX", os.path.join(DATA_DIR, "inbox"))
TMP_DIR = os.path.join(DATA_DIR, "tmp")

EVENT_COLS = ("t", "z", "host", "source", "process", "pid", "user", "level", "category", "action", "subject",
              "code", "message", "extra")

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  id TEXT PRIMARY KEY, name TEXT, platform TEXT, host TEXT, tz INTEGER DEFAULT 0, tz_mode TEXT DEFAULT 'auto',
  created REAL, updated REAL, status TEXT, progress REAL DEFAULT 0, stage TEXT, message TEXT,
  events INTEGER DEFAULT 0, t_min INTEGER, t_max INTEGER, summary TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, case_id TEXT, path TEXT, upload TEXT, size INTEGER, parser TEXT, label TEXT,
  platform TEXT, events INTEGER DEFAULT 0, noise INTEGER DEFAULT 0, bad INTEGER DEFAULT 0,
  t_min INTEGER, t_max INTEGER, status TEXT, error TEXT, added REAL);
CREATE INDEX IF NOT EXISTS files_case ON files(case_id);
"""

CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, t INTEGER NOT NULL, z INTEGER DEFAULT 0, file_id INTEGER, host TEXT, source TEXT,
  process TEXT, pid INTEGER, user TEXT, level TEXT, category TEXT, action TEXT, subject TEXT, code TEXT,
  message TEXT, extra TEXT);
CREATE TABLE IF NOT EXISTS findings(rule TEXT, event_id INTEGER, grp TEXT, PRIMARY KEY(rule, event_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

CASE_INDEXES = """
CREATE INDEX IF NOT EXISTS ev_t ON events(t);
CREATE INDEX IF NOT EXISTS ev_cat ON events(category, t);
CREATE INDEX IF NOT EXISTS ev_proc ON events(process);
CREATE INDEX IF NOT EXISTS ev_file ON events(file_id);
"""

_lock = threading.RLock()


def ensure_dirs():
    for d in (DATA_DIR, CASES_DIR, UPLOADS_DIR, INBOX_DIR, TMP_DIR):
        os.makedirs(d, exist_ok=True)


def _connect(path, readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def catalog():
    con = _connect(os.path.join(DATA_DIR, "catalog.db"))
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    ensure_dirs()
    with catalog() as c:
        c.executescript(CATALOG_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(cases)")}
        if "log" not in cols:           # журнал шагов сбора и разбора (JSON-список)
            c.execute("ALTER TABLE cases ADD COLUMN log TEXT")
        # дела, «зависшие» при перезапуске контейнера
        c.execute("UPDATE cases SET status='error', message='Обработка прервана перезапуском сервиса' "
                  "WHERE status IN ('processing','queued','collecting')")


def case_path(case_id: str) -> str:
    return os.path.join(CASES_DIR, f"{case_id}.db")


def case_db(case_id: str, write=False):
    p = case_path(case_id)
    if not write and not os.path.exists(p):
        raise FileNotFoundError(case_id)
    con = _connect(p)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA cache_size=-65536")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA mmap_size=268435456")
    if write:
        con.executescript(CASE_SCHEMA)
    return con


def set_case(case_id, **kw):
    if not kw:
        return
    kw["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    with _lock, catalog() as c:
        c.execute(f"UPDATE cases SET {cols} WHERE id=?", (*kw.values(), case_id))


LOG_MAX = 400


def add_log(case_id, level, text):
    """Шаг сбора/разбора для журнала на странице сбора. level: info, ok, warn, err."""
    with _lock, catalog() as c:
        r = c.execute("SELECT log FROM cases WHERE id=?", (case_id,)).fetchone()
        if r is None:
            return
        try:
            items = json.loads(r[0]) if r[0] else []
        except ValueError:
            items = []
        items.append({"t": round(time.time(), 1), "l": level, "m": str(text)[:500]})
        if len(items) > LOG_MAX:
            items = items[:20] + [{"t": items[20]["t"], "l": "info", "m": "… часть записей пропущена …"}] + items[-(LOG_MAX - 21):]
        c.execute("UPDATE cases SET log=? WHERE id=?", (json.dumps(items, ensure_ascii=False), case_id))


def get_case(case_id):
    with catalog() as c:
        r = c.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return dict(r) if r else None
