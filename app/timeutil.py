"""Разбор временных меток.

Все события хранятся в поле t — миллисекунды «локального времени хоста»,
записанного как будто это UTC (naive). Так удобно группировать и показывать
время ровно в том виде, в каком оно было на исследуемой машине.

Если в исходной строке есть явное смещение (+0500, Z), событие сначала
сохраняется в UTC (флаг z=1), а после загрузки всех файлов дела сдвигается
на часовой пояс дела одним UPDATE.
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_EPOCH = datetime(1970, 1, 1)


def dt_to_ms(dt: datetime) -> int:
    return calendar.timegm(dt.timetuple()) * 1000 + dt.microsecond // 1000


def ms_to_dt(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def ms_to_str(ms: int | None) -> str:
    if ms is None:
        return ""
    d = ms_to_dt(ms)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


_OFF_RE = re.compile(r"([+-])(\d{2}):?(\d{2})?")
_OFF_CACHE: dict = {}


def parse_offset(s: str | None) -> int | None:
    """'+0500' / '+05:00' / '+05' / 'Z' -> минуты; None если нет смещения."""
    if not s:
        return None
    r = _OFF_CACHE.get(s, False)
    if r is not False:
        return r
    v = s.strip()
    if v in ("Z", "z", "UTC", "GMT"):
        r = 0
    else:
        m = _OFF_RE.fullmatch(v)
        if not m:
            r = None
        else:
            r = int(m.group(2)) * 60 + int(m.group(3) or 0)
            if m.group(1) == "-":
                r = -r
    if len(_OFF_CACHE) < 1000:
        _OFF_CACHE[s] = r
    return r


# 2026-10-09 12:27:06.765447+0500 | 2026-09-11 20:32:37+05 | 2026-09-13T08:33:46.663Z
# 2026-10-02 17:21:11 +0500 | 2026-10-09T12:27:06+05:00
ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?"
    r"\s?(Z|[+-]\d{2}(?::?\d{2})?)?")


def parse_iso(s: str):
    """Возвращает (ms_naive, offset_min|None) или None."""
    m = ISO_RE.match(s)
    if not m:
        return None
    return iso_match_to_ms(m)


_DAY_CACHE: dict = {}


def day_ms(y: str, mo: str, d: str):
    """Начало суток в мс (кэш: в журнале обычно несколько дат на миллионы строк)."""
    k = (y, mo, d)
    v = _DAY_CACHE.get(k)
    if v is None:
        try:
            v = calendar.timegm((int(y), int(mo), int(d), 0, 0, 0, 0, 0, 0)) * 1000
        except (ValueError, OverflowError):
            v = False
        if len(_DAY_CACHE) > 20000:
            _DAY_CACHE.clear()
        _DAY_CACHE[k] = v
    return v


def iso_match_to_ms(m):
    y, mo, d, hh, mi, ss, frac, off = m.groups()
    base = day_ms(y, mo, d)
    if base is False:
        return None
    ms = int((frac + "000")[:3]) if frac else 0
    return base + int(hh) * 3600000 + int(mi) * 60000 + int(ss) * 1000 + ms, parse_offset(off)


def naive_ms(y, mo, d, hh, mi, ss, ms=0):
    return calendar.timegm((y, mo, d, hh, mi, ss, 0, 0, 0)) * 1000 + ms


def infer_year(month: int, day: int, anchor: datetime) -> int:
    """Год для строк syslog без года: по дате изменения файла.

    Строка не может быть «позже» файла больше чем на сутки — значит,
    она из прошлого года (ротация через Новый год)."""
    y = anchor.year
    try:
        cand = datetime(y, month, day)
    except ValueError:  # 29 февраля
        return y - 1
    if cand - anchor > timedelta(days=1):
        return y - 1
    return y


def to_local(ms: int, off: int | None) -> tuple[int, int]:
    """(t, z): если смещение известно — переводим в UTC и ставим z=1."""
    if off is None:
        return ms, 0
    return ms - off * 60000, 1


def fmt_tz(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    m = abs(minutes)
    return f"UTC{sign}{m // 60:02d}:{m % 60:02d}"
