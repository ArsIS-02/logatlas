"""Язык запросов строки поиска → SQL.

  слово                 — подстрока в сообщении, процессе или объекте
  "фраза целиком"       — то же для фразы
  process:sudo          — поле (точное совпадение, без учёта регистра)
  process:kav*          — шаблон (* — любые символы)
  process:sshd|sudo     — несколько значений
  action:вход           — для action / subject / message — поиск подстроки
  -process:mdworker     — исключить
  /regex/               — регулярное выражение по сообщению
  rule:remote_access    — события, попавшие в находку
  has:category          — только классифицированные события
"""
from __future__ import annotations

import re
from functools import lru_cache

FIELDS = {
    "process": "process", "proc": "process", "p": "process", "процесс": "process",
    "source": "source", "src": "source", "file": "source", "источник": "source",
    "category": "category", "cat": "category", "категория": "category",
    "action": "action", "act": "action", "событие": "action",
    "subject": "subject", "subj": "subject", "obj": "subject", "объект": "subject",
    "user": "user", "u": "user", "пользователь": "user",
    "host": "host", "хост": "host",
    "level": "level", "lvl": "level", "уровень": "level",
    "pid": "pid",
    "code": "code", "id": "code", "eventid": "code", "subsystem": "code", "код": "code",
    "message": "message", "msg": "message", "сообщение": "message",
}
CONTAINS_FIELDS = {"action", "subject", "message"}
CAT_ALIASES = {
    "система": "system", "доступ": "access", "сеансы": "access", "процессы": "process", "приложения": "process",
    "закрепление": "persistence", "автозапуск": "persistence", "по": "software", "установка": "software",
    "устройства": "device", "usb": "device", "сеть": "network", "безопасность": "security",
}
LEVEL_ORDER = ["debug", "info", "notice", "warning", "error", "critical"]

TOKEN = re.compile(r'(-?)(?:(\w+):("(?:[^"]*)"|\S+)|("(?:[^"]*)")|(/(?:[^/\\]|\\.)+/)|(\S+))', re.U)


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pattern(v: str) -> str:
    return _like_escape(v).replace("*", "%")


def _is_ascii(s):
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _contains(col, v, params):
    if _is_ascii(v):
        params.append(f"%{_like_escape(v)}%")
        return f"{col} LIKE ? ESCAPE '\\'"
    params.append(f"%{_like_escape(v.lower())}%")
    return f"ulower({col}) LIKE ? ESCAPE '\\'"


def parse(q: str):
    """-> (sql_where, params, highlight_terms)"""
    clauses, params, terms = [], [], []
    for m in TOKEN.finditer(q or ""):
        neg, field, fval, phrase, rx, word = m.groups()
        sql = None
        if field is not None:
            f = field.lower()
            v = fval[1:-1] if fval.startswith('"') and fval.endswith('"') and len(fval) > 1 else fval
            if f == "rule":
                vals = [x for x in v.split("|") if x]
                params.extend(vals)
                sql = f"id IN (SELECT event_id FROM findings WHERE rule IN ({','.join('?' * len(vals))}))"
            elif f == "has":
                col = FIELDS.get(v.lower())
                if col:
                    sql = f"{col} IS NOT NULL"
            elif f in FIELDS:
                col = FIELDS[f]
                ors = []
                for val in v.split("|"):
                    if not val:
                        continue
                    if col == "category":
                        val = CAT_ALIASES.get(val.lower(), val.lower())
                    if col == "level" and val.endswith("+"):
                        base = val[:-1].lower()
                        if base in LEVEL_ORDER:
                            lv = LEVEL_ORDER[LEVEL_ORDER.index(base):]
                            params.extend(lv)
                            ors.append(f"level IN ({','.join('?' * len(lv))})")
                            continue
                    if col == "pid":
                        if val.isdigit():
                            params.append(int(val))
                            ors.append("pid = ?")
                        continue
                    if "*" in val:
                        params.append(_pattern(val))
                        ors.append(f"{col} LIKE ? ESCAPE '\\'")
                    elif col in CONTAINS_FIELDS:
                        ors.append(_contains(col, val, params))
                        terms.append(val)
                    else:
                        params.append(val)
                        ors.append(f"{col} = ? COLLATE NOCASE")
                if ors:
                    sql = "(" + " OR ".join(ors) + ")"
            else:  # неизвестное поле — считаем обычным текстом
                word = m.group(0).lstrip("-")
        if sql is None and phrase:
            word = phrase[1:-1]
        if sql is None and rx:
            params.append(rx[1:-1])
            sql = "message REGEXP ?"
        if sql is None and word:
            terms.append(word)
            ps = []
            parts = [_contains(c, word, ps) for c in ("message", "subject", "process")]
            params.extend(ps)
            sql = "(" + " OR ".join(parts) + ")"
        if sql:
            clauses.append(("NOT " if neg else "") + sql)
    return " AND ".join(clauses) or "1", params, terms


@lru_cache(maxsize=64)
def _rx(p):
    try:
        return re.compile(p, re.I)
    except re.error:
        return re.compile(re.escape(p), re.I)


def regexp(pattern, value):
    if value is None:
        return False
    return _rx(pattern).search(value) is not None


def ulower(v):
    return v.lower() if isinstance(v, str) else v


def register(con):
    con.create_function("REGEXP", 2, regexp, deterministic=True)
    con.create_function("ulower", 1, ulower, deterministic=True)
