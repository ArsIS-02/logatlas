"""Отправка находок сбора в Graylog по GELF (UDP или TCP). Только стандартная библиотека.

LogAtlas работает офлайн и сам ничего не отправляет: сообщения уходят только по кнопке
«Отправить находки в Graylog» и только если задан адрес:
    LOGATLAS_GELF_HOST   адрес входа GELF (пусто — отправка выключена, кнопки нет)
    LOGATLAS_GELF_PORT   порт, по умолчанию 12201
    LOGATLAS_GELF_PROTO  udp (по умолчанию) или tcp
    LOGATLAS_GELF_SOURCE поле source в Graylog, по умолчанию logatlas
"""
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
import zlib

log = logging.getLogger("logatlas.gelf")

CHUNK = 8000          # полезная нагрузка UDP-части, с запасом под MTU и заголовок 12 байт
MAX_CHUNKS = 128      # предел GELF
QUEUE_MAX = 1000


def udp_packets(payload, chunk=CHUNK):
    """Готовые UDP-датаграммы: одна, если влезает, иначе части с заголовком 0x1e 0x0f."""
    data = zlib.compress(payload)
    if len(data) <= chunk:
        return [data]
    parts = [data[i:i + chunk] for i in range(0, len(data), chunk)]
    if len(parts) > MAX_CHUNKS:
        raise ValueError("GELF: сообщение слишком большое (%d частей)" % len(parts))
    mid = os.urandom(8)
    return [b"\x1e\x0f" + mid + struct.pack("BB", i, len(parts)) + p for i, p in enumerate(parts)]


def message(short, fields, source, level=6, ts=None):
    """Словарь GELF 1.1: свои поля получают префикс «_», пустые значения не отправляются."""
    msg = {"version": "1.1", "host": source, "short_message": str(short)[:1000] or "-",
           "timestamp": round(ts if ts is not None else time.time(), 3), "level": int(level)}
    for k, v in fields.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple, set)):
            v = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        key = "_" + "".join(c if c.isalnum() or c == "_" else "_" for c in str(k))
        if key in ("_id", "_source"):
            key = "_f" + key
        msg[key] = v
    return msg


class Sender:
    def __init__(self, host, port=12201, proto="udp", source="logatlas", timeout=2.0):
        self.host, self.port = host, int(port)
        self.proto = (proto or "udp").lower()
        self.source = source or "logatlas"
        self.timeout = timeout
        self.status = {"sent": 0, "errors": 0, "last_ok": None, "last_error": ""}
        self._q = queue.Queue(QUEUE_MAX)
        self._t = None

    @classmethod
    def from_env(cls, env=os.environ):
        host = (env.get("LOGATLAS_GELF_HOST") or "").strip()
        if not host:
            return None
        try:
            port = int(env.get("LOGATLAS_GELF_PORT") or 12201)
        except ValueError:
            port = 12201
        return cls(host, port, env.get("LOGATLAS_GELF_PROTO") or "udp", env.get("LOGATLAS_GELF_SOURCE") or "logatlas")

    def send_now(self, msg):
        """Отправить сразу, в вызывающем потоке. Ошибки сети поднимаются наверх."""
        raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if self.proto == "tcp":
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(raw + b"\0")
        else:
            infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM)
            fam, _, _, _, addr = infos[0]
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                for p in udp_packets(raw):
                    s.sendto(p, addr)

    def submit(self, short, fields, level=6, ts=None):
        """Поставить сообщение в очередь фонового потока. Никогда не бросает исключений."""
        try:
            self._q.put_nowait(message(short, fields, self.source, level, ts))
        except queue.Full:
            self.status["errors"] += 1
            self.status["last_error"] = "очередь переполнена"
            return
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._run, name="logatlas-gelf", daemon=True)
            self._t.start()

    def _run(self):
        while True:
            msg = self._q.get()
            try:
                self.send_now(msg)
                self.status["sent"] += 1
                self.status["last_ok"] = time.time()
            except (OSError, ValueError) as e:
                self.status["errors"] += 1
                self.status["last_error"] = str(e)
                log.warning("GELF %s:%s: %s", self.host, self.port, e)
            finally:
                self._q.task_done()

    def flush(self, timeout=5.0):
        """Дождаться отправки очереди (для тестов и остановки)."""
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.02)


# ---------- находки → Graylog ----------

SENDER = Sender.from_env()
LEVEL = {"critical": 2, "high": 3, "medium": 4, "low": 5, "info": 6}


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else ""


def finding_messages(case, findings, groups_of, min_sev="low"):
    """Сообщения GELF по находкам сбора: одно на правило.

    Время сообщения — момент отправки, а не время событий: сбор может быть архивом за прошлый месяц,
    и алерты Graylog, которые смотрят последние минуты, иначе его бы не увидели. Время самих событий —
    в полях la_first и la_last."""
    order = list(LEVEL)
    lim = order.index(min_sev) if min_sev in order else len(order) - 1
    out = []
    for f in findings:
        sev = f.get("severity", "info")
        if sev in order and order.index(sev) > lim:
            continue
        groups = groups_of(f["id"]) if groups_of else []
        out.append(("LogAtlas: %s — %s (%s)" % (f.get("title", f["id"]), case.get("host") or case.get("name") or "", sev),
                    {"la_source": "logatlas", "homelab_app": "logatlas", "la_event": "finding",
                     "la_case": case.get("id", ""), "la_case_name": case.get("name", ""), "la_host": case.get("host") or "",
                     "la_platform": case.get("platform") or "", "la_rule": f["id"], "la_rule_title": f.get("title", ""),
                     "la_severity": sev, "la_mitre": f.get("mitre") or "", "la_sigma": bool(f.get("sigma")) or f["id"].startswith("sigma:"),
                     "la_count": f.get("n", 0), "la_groups": f.get("groups", 0), "la_first": _iso(f.get("first")),
                     "la_last": _iso(f.get("last")), "la_top": [g["name"] for g in groups[:5]],
                     "la_desc": (f.get("desc") or "")[:500]},
                    LEVEL.get(sev, 6)))
    return out
