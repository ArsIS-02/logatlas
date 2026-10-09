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


def parse_offset(s: str | None) -> int | None:
    """'+0500' / '+05:00' / '+05' / 'Z' -> минуты; None если нет смещения."""
    if not s:
        return None
    s = s.strip()
    if s in ("Z", "z", "UTC", "GMT"):
        return 0
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", s)
    if not m:
        return None
    v = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return -v if m.group(1) == "-" else v


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


def iso_match_to_ms(m):
    y, mo, d, hh, mi, ss = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
    frac = m.group(7)
    ms = int((frac + "000")[:3]) if frac else 0
    try:
        base = calendar.timegm((y, mo, d, hh, mi, ss, 0, 0, 0)) * 1000 + ms
    except (ValueError, OverflowError):
        return None
    return base, parse_offset(m.group(8))


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
