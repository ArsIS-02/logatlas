"""Общие типы для парсеров."""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime

try:  # orjson заметно быстрее; в Docker-образе ставится, иначе — stdlib
    import orjson as _oj

    def jloads(b):
        return _oj.loads(b)

    def jdumps(o):
        return _oj.dumps(o).decode()
except ImportError:  # pragma: no cover
    def jloads(b):
        return json.loads(b)

    def jdumps(o):
        return json.dumps(o, ensure_ascii=False, separators=(",", ":"))

# Порядок колонок события (см. db.EVENT_COLS)
# t, z, host, source, process, pid, user, level, category, action, subject, code, message, extra
LEVELS = ("debug", "info", "notice", "warning", "error", "critical")

# Восемь групп категорий. Ключи используются и на фронтенде.
CATEGORIES = {
    "system": "Система",
    "access": "Доступ и сеансы",
    "process": "Процессы и приложения",
    "persistence": "Закрепление",
    "software": "Установка ПО",
    "device": "Устройства",
    "network": "Сеть",
    "security": "Безопасность",
}

MAX_MSG = 4000


def ev(t, z=0, host=None, source=None, process=None, pid=None, user=None, level="info",
       category=None, action=None, subject=None, code=None, message="", extra=None):
    if message and len(message) > MAX_MSG:
        message = message[:MAX_MSG] + " …"
    return (t, z, host, source, process, pid, user, level, category, action, subject, code,
            message, jdumps(extra) if extra else None)


class Source:
    """Поток байтов файла: голова для определения формата + остальное."""

    HEAD = 256 * 1024

    def __init__(self, name: str, fileobj, size: int | None = None, mtime: float | None = None):
        self.name = name                       # виртуальный путь внутри архива
        self.base = os.path.basename(name)
        self._f = fileobj
        self.head = fileobj.read(self.HEAD) or b""
        self._consumed = False
        self.size = size
        self.mtime = mtime
        self.bytes_read = len(self.head)

    def _chunks(self):
        if self._consumed:
            raise RuntimeError("source already consumed")
        self._consumed = True
        yield self.head
        while True:
            b = self._f.read(1 << 20)
            if not b:
                break
            self.bytes_read += len(b)
            yield b

    def raw(self):
        """Файлоподобный объект для потокового чтения (tar/zip/xml)."""
        return _ChainReader(self._chunks())

    def raw_chunks_text(self):
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        for chunk in self._chunks():
            yield dec.decode(chunk)
        yield dec.decode(b"", final=True)

    def lines(self):
        """Итератор строк bytes без \n."""
        buf = b""
        for chunk in self._chunks():
            buf += chunk
            parts = buf.split(b"\n")
            buf = parts.pop()
            for p in parts:
                yield p.rstrip(b"\r")
        if buf:
            yield buf.rstrip(b"\r")

    def text_lines(self):
        for b in self.lines():
            yield b.decode("utf-8", "replace")

    def materialize(self, tmpdir: str) -> str:
        """Записать во временный файл (нужно для бинарных форматов, например EVTX)."""
        fd, path = tempfile.mkstemp(dir=tmpdir, suffix="_" + self.base[-40:])
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(self.raw(), out, 1 << 20)
        return path


class _ChainReader(io.RawIOBase):
    def __init__(self, gen):
        self._gen = gen
        self._buf = b""

    def readable(self):
        return True

    def readinto(self, b):
        while not self._buf:
            try:
                self._buf = next(self._gen)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


class Ctx:
    """Контекст разбора одного файла."""

    def __init__(self, source: Source, anchor: datetime, tmpdir: str):
        self.source = source
        self.anchor = anchor           # дата изменения файла (для года в syslog)
        self.tmpdir = tmpdir
        self.noise = 0                 # пропущенные «шумовые» строки
        self.bad = 0                   # строки без распознанного времени
        self.tz_votes: Counter = Counter()
        self.host_votes: Counter = Counter()
        self.label = None              # подпись источника (system.log, unified log, …)
        self.platform = None


class Parser:
    name = "base"
    label = "Файл"
    platform = "any"
    binary = False

    @classmethod
    def sniff(cls, base: str, head: bytes) -> int:
        return 0

    def parse(self, src: Source, ctx: Ctx):
        raise NotImplementedError
        yield  # noqa


def first_lines(head: bytes, n=20):
    out = []
    for ln in head.split(b"\n")[:n]:
        out.append(ln.decode("utf-8", "replace").rstrip("\r"))
    return out


def looks_binary(head: bytes) -> bool:
    if not head:
        return False
    sample = head[:4096]
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        return e.start < len(sample) - 4
