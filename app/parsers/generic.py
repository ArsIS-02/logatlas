"""Универсальные парсеры: любой текстовый журнал со временем в строке и JSON-lines."""
from __future__ import annotations

import re
from datetime import datetime

from ..timeutil import MONTHS, infer_year, iso_match_to_ms, naive_ms, parse_iso, ISO_RE, dt_to_ms
from .base import Parser, ev, first_lines, jloads, looks_binary

LEVEL_RX = re.compile(r"(?i)(?:\b|<|\[)(fatal|crit(?:ical)?|err(?:or)?|warn(?:ing)?|notice|info|debug|trace)(?:\b|>|\])")
LEVEL_NORM = {"fatal": "critical", "crit": "critical", "critical": "critical", "err": "error", "error": "error",
              "warn": "warning", "warning": "warning", "notice": "notice", "info": "info", "debug": "debug",
              "trace": "debug"}


def _lvl(text):
    m = LEVEL_RX.search(text[:80])
    return LEVEL_NORM.get(m.group(1).lower(), "info") if m else "info"


class _Fmt:
    def __init__(self, name, rx, conv, anywhere=False):
        self.name, self.rx, self.conv, self.anywhere = name, re.compile(rx), conv, anywhere


def _c_iso(m, anchor):
    return iso_match_to_ms(ISO_RE.match(m.group(0).lstrip("[")))


def _c_ctime(m, anchor):
    mon = MONTHS.get(m.group("mon").lower())
    if not mon:
        return None
    h, mi, s = m.group("hms").split(":")
    return naive_ms(int(m.group("y")), mon, int(m.group("d")), int(h), int(mi), int(s)), None


def _c_sys(m, anchor):
    mon = MONTHS.get(m.group("mon").lower())
    if not mon:
        return None
    d = int(m.group("d"))
    h, mi, s = m.group("hms").split(":")
    return naive_ms(infer_year(mon, d, anchor), mon, d, int(h), int(mi), int(s)), None


def _c_dmy(m, anchor):
    s = m.group("s") or "0"
    return naive_ms(int(m.group("y")), int(m.group("mo")), int(m.group("d")), int(m.group("h")), int(m.group("mi")), int(s)), None


def _c_ymd_slash(m, anchor):
    return naive_ms(int(m.group("y")), int(m.group("mo")), int(m.group("d")), int(m.group("h")), int(m.group("mi")), int(m.group("s"))), None


def _c_epoch(m, anchor):
    v = float(m.group("e"))
    if not 946684800 < v < 4102444800:
        return None
    return int(v * 1000), 0


def _c_apache(m, anchor):
    mon = MONTHS.get(m.group("mon").lower())
    r = naive_ms(int(m.group("y")), mon, int(m.group("d")), int(m.group("h")), int(m.group("mi")), int(m.group("s")))
    from ..timeutil import parse_offset
    return r, parse_offset(m.group("tz"))


FORMATS = [
    _Fmt("iso", r"^\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d{1,9})?\s?(?:Z|[+-]\d\d(?::?\d\d)?)?", _c_iso),
    _Fmt("ctime", r"(?:[A-Z][a-z]{2} )?(?P<mon>[A-Z][a-z]{2}) {1,2}(?P<d>\d{1,2}) (?P<hms>\d\d:\d\d:\d\d)(?:\.\d+)? (?P<y>\d{4})\b", _c_ctime, True),
    _Fmt("apache", r"\[(?P<d>\d\d)/(?P<mon>[A-Z][a-z]{2})/(?P<y>\d{4}):(?P<h>\d\d):(?P<mi>\d\d):(?P<s>\d\d) (?P<tz>[+-]\d{4})\]", _c_apache, True),
    _Fmt("syslog", r"^(?P<mon>[A-Z][a-z]{2}) {1,2}(?P<d>\d{1,2}) (?P<hms>\d\d:\d\d:\d\d)", _c_sys),
    _Fmt("dmy", r"^\[?(?P<d>\d\d)\.(?P<mo>\d\d)\.(?P<y>\d{4}),? (?P<h>\d\d):(?P<mi>\d\d)(?::(?P<s>\d\d))?", _c_dmy),
    _Fmt("ymd_slash", r"^\[?(?P<y>\d{4})/(?P<mo>\d\d)/(?P<d>\d\d)[ T](?P<h>\d\d):(?P<mi>\d\d):(?P<s>\d\d)", _c_ymd_slash),
    _Fmt("epoch", r"^\[?(?P<e>1\d{9}(?:\.\d+)?)\b", _c_epoch),
    _Fmt("iso_any", r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d{1,9})?\s?(?:Z|[+-]\d\d(?::?\d\d)?)?", _c_iso, True),
]


def _pick_format(lines):
    best, best_n = None, 0
    for f in FORMATS:
        n = sum(1 for l in lines if (f.rx.search(l) if f.anywhere else f.rx.match(l)))
        if n > best_n:
            best, best_n = f, n
    return best if best_n else None


PROC_AFTER = re.compile(r"^\s*\]?\s*(?:(?P<host>[\w.\-]+) )?(?P<proc>[A-Za-z_][\w.\-/ ]{0,40}?)\[(?P<pid>\d+)\]:\s")


class TextParser(Parser):
    """Последний рубеж: любой текстовый файл."""
    name = "text"
    label = "текстовый журнал"
    platform = "any"

    @classmethod
    def sniff(cls, base, head):
        if looks_binary(head):
            return 0
        return 5

    def parse(self, src, ctx):
        head_lines = [l for l in first_lines(src.head, 200) if l.strip()]
        fmt = _pick_format(head_lines)
        anchor_ms = dt_to_ms(ctx.anchor)
        cur = None
        if fmt is None:
            # времени нет — каждая строка становится событием со временем изменения файла
            for i, line in enumerate(src.text_lines()):
                if not line.strip():
                    continue
                yield ev(anchor_ms, 0, None, None, None, None, None, _lvl(line), None, None, None, None, line,
                         {"no_ts": 1})
            return
        rx, conv, anywhere = fmt.rx, fmt.conv, fmt.anywhere
        fallback = [f for f in FORMATS if not f.anywhere and f is not fmt and f.name != "syslog"]
        for line in src.text_lines():
            if not line.strip():
                continue
            m = rx.search(line) if anywhere else rx.match(line)
            r = conv(m, ctx.anchor) if m else None
            if r is None:
                for f in fallback:
                    m = f.rx.match(line)
                    if m:
                        r = f.conv(m, ctx.anchor)
                        if r:
                            break
            if r:
                if cur:
                    yield self._emit(cur)
                t, off = r
                z = 0
                if off is not None:
                    t -= off * 60000
                    z = 1
                    if off:
                        ctx.tz_votes[off] += 1
                rest = line[m.end():] if not anywhere or m.start() == 0 else line
                cur = [t, z, rest.strip(" \t]-:|") if rest.strip() else line, line]
            elif cur:
                if len(cur[2]) < 4000:
                    cur[2] += "\n" + line
                else:  # очень длинный блок без времени — режем на части
                    yield self._emit(cur)
                    cur = [cur[0], cur[1], line, line]
            else:
                ctx.bad += 1
        if cur:
            yield self._emit(cur)

    def _emit(self, cur):
        t, z, msg, _ = cur
        proc = pid = None
        pm = PROC_AFTER.match(msg)
        if pm:
            proc = pm.group("proc").strip()
            pid = int(pm.group("pid"))
        return ev(t, z, None, None, proc, pid, None, _lvl(msg), None, None, None, None, msg)


TS_KEYS = ("@timestamp", "timestamp", "time", "ts", "date", "datetime", "TimeCreated", "eventTime", "Timestamp",
           "time_iso8601", "asctime", "created", "logtime")
MSG_KEYS = ("message", "msg", "MESSAGE", "eventMessage", "log", "text", "Message", "event", "short_message")
LVL_KEYS = ("level", "severity", "log.level", "levelname", "lvl", "Level", "priority")
HOST_KEYS = ("host", "hostname", "host.name", "Computer", "_HOSTNAME", "MachineName")
PROC_KEYS = ("process", "proc", "program", "app", "logger", "service", "component", "SYSLOG_IDENTIFIER", "_COMM", "source")


def _get(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        if "." in k:
            cur = d
            for p in k.split("."):
                cur = cur.get(p) if isinstance(cur, dict) else None
            if cur not in (None, ""):
                return cur
    return None


def _json_time(v):
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e14:      # микросекунды
            x /= 1e6
        elif x > 1e11:    # миллисекунды
            x /= 1e3
        if 946684800 < x < 4102444800:
            return int(x * 1000), 0
        return None
    if isinstance(v, dict):
        v = v.get("$date") or v.get("SystemTime")
    if isinstance(v, str):
        if v.isdigit():
            return _json_time(int(v))
        return parse_iso(v.strip())
    return None


class JsonLinesParser(Parser):
    name = "jsonl"
    label = "JSON-журнал"
    platform = "any"

    @classmethod
    def sniff(cls, base, head):
        lines = [l for l in first_lines(head, 10) if l.strip()][:-1] or first_lines(head, 2)
        ok = 0
        for l in lines:
            if l.startswith("{"):
                try:
                    d = jloads(l)
                    if isinstance(d, dict) and _get(d, TS_KEYS) is not None:
                        ok += 1
                except ValueError:
                    pass
        return 50 if ok and ok >= len(lines) // 2 else 0

    def parse(self, src, ctx):
        for line in src.lines():
            if not line.startswith(b"{"):
                continue
            try:
                d = jloads(line)
            except ValueError:
                ctx.bad += 1
                continue
            if not isinstance(d, dict):
                continue
            r = _json_time(_get(d, TS_KEYS))
            if not r:
                ctx.bad += 1
                continue
            t, off = r
            z = 0
            if off is not None:
                t -= off * 60000
                z = 1
                if off:
                    ctx.tz_votes[off] += 1
            msg = _get(d, MSG_KEYS)
            if msg is None:
                msg = line.decode("utf-8", "replace")
            elif not isinstance(msg, str):
                msg = str(msg)
            lvl = _get(d, LVL_KEYS)
            level = LEVEL_NORM.get(str(lvl).lower(), "info") if lvl is not None else "info"
            host = _get(d, HOST_KEYS)
            if isinstance(host, dict):
                host = host.get("name") or host.get("hostname")
            proc = _get(d, PROC_KEYS)
            if isinstance(proc, dict):
                proc = proc.get("name")
            yield ev(t, z, str(host) if host else None, None, str(proc)[:80] if proc else None, None, None, level,
                     None, None, None, None, msg)
