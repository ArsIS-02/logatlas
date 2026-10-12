"""Правила Sigma (https://sigmahq.io): импорт, перевод в проверки LogAtlas и запуск на сборах.

Правило переводится в две части:
  1. SQL-отбор кандидатов — журнал из logsource (EventID Sysmon/Security, канал, auditd…) и обязательные
     подстроки из значений полей (LIKE по message/subject/process, без учёта регистра латиницы);
  2. точная проверка на Python для отобранных событий: поля Sigma берутся из пар «Поле=значение»
     в сообщении (EventData Windows, записи auditd) и из нормализованных полей LogAtlas (process, subject,
     user, code, host). Поддерживаются модификаторы contains, startswith, endswith, all, re (с i/m/s), cidr,
     exists, gt/gte/lt/lte, cased, base64, base64offset, wide (utf16le), windash, а условия — and/or/not,
     скобки, «1 of …», «all of …», «… of them».
Не поддерживаются агрегации (| count() …), корреляции, fieldref и expand — такие правила сохраняются
с пометкой «не применяется» и причиной.

Находки правил Sigma хранятся в таблице findings сбора с ключом «sigma:<id правила>».
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import logging
import re
import sqlite3
import threading
import time
import zipfile

from . import db

log = logging.getLogger("logatlas.sigma")

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PREFIX = "sigma:"
LEVELS = {"informational": "info", "info": "info", "low": "low", "medium": "medium", "high": "high",
          "critical": "critical"}
MAX_FILE = 5 << 20
MAX_RULES = 10000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sigma_rules(
  id TEXT PRIMARY KEY, title TEXT, level TEXT, status TEXT, description TEXT, author TEXT, tags TEXT,
  product TEXT, category TEXT, service TEXT, yaml TEXT, error TEXT, enabled INTEGER DEFAULT 1,
  source TEXT, added REAL, updated REAL);
"""


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------------------
# поля события
_WIN_SPLIT = re.compile(r"; (?=[A-Za-z_][\w .\-]{0,60}=)")
_KV = re.compile(r"(?<![\w.])([A-Za-z_][\w\-]{0,40})=(\"[^\"]*\"|'[^']*'|[^\s;,]*)")

ALIASES = {
    "image": ["image", "newprocessname", "exe", "processpath", "@process_path"],
    "parentimage": ["parentimage", "parentprocessname"],
    "commandline": ["commandline", "processcommandline", "@subject"],
    "parentcommandline": ["parentcommandline"],
    "user": ["user", "subjectusername", "targetusername", "@user"],
    "targetusername": ["targetusername", "@user"],
    "subjectusername": ["subjectusername", "@user"],
    "eventid": ["@code"],
    "provider_name": ["@provider"],
    "channel": ["@channel"],
    "computer": ["@host"],
    "computername": ["@host"],
    "hostname": ["@host", "hostname"],
    "type": ["@audit_type", "type"],
    "processname": ["processname", "@process"],
    "originalfilename": ["originalfilename"],
}
SPECIAL_SQL = {"@code"}          # для поля EventID отбор идёт прямо по колонке code


def parse_fields(code, process, subject, user, host, message, extra) -> dict:
    """Поля события в нижнем регистре имён: пары из сообщения и служебные @-поля."""
    f: dict = {}
    msg = message or ""
    head, _, rest = msg.partition("\n")
    if head.startswith("["):                       # Windows: «[канал] провайдер EventID — действие\nК=З; К=З»
        ch_end = head.find("]")
        f["@channel"] = head[1:ch_end] if ch_end > 0 else ""
        parts = head[ch_end + 1:].split()
        f["@provider"] = parts[0] if parts else ""
        for line in rest.split("\n"):
            for pair in _WIN_SPLIT.split(line):
                k, sep, v = pair.partition("=")
                if sep and k and len(k) <= 64:
                    f.setdefault(k.strip().lower(), v)
    else:                                          # auditd и прочие «ключ=значение»
        for k, v in _KV.findall(msg):
            if v[:1] in ('"', "'"):
                v = v[1:-1]
            f.setdefault(k.lower(), v)
    if extra:
        try:
            ex = json.loads(extra)
            if isinstance(ex, dict) and ex.get("ch"):
                f.setdefault("@channel", ex["ch"])
        except ValueError:
            pass
    f["@code"] = "" if code is None else str(code)
    f["@audit_type"] = str(code or "").split(":")[0]
    f["@process"] = process or ""
    f["@subject"] = subject or ""
    f["@user"] = user or ""
    f["@host"] = host or ""
    f["@process_path"] = process or ""
    f["@msg"] = msg
    return f


def full_text(fields: dict) -> str:
    """Весь текст события для ключевых слов (собирается только когда нужен)."""
    t = fields.get("@text")
    if t is None:
        t = fields["@text"] = " ".join(x for x in (fields.get("@msg"), fields.get("@subject"), fields.get("@process")) if x)
    return t


def get_field(fields: dict, name: str):
    k = name.lower()
    for cand in ALIASES.get(k, [k]):
        v = fields.get(cand)
        if v not in (None, ""):
            return v
    return None


# ---------------------------------------------------------------------------
# значения и модификаторы
def _sigma_regex(value: str, prefix: str = "", suffix: str = "") -> str:
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value) and value[i + 1] in "*?\\":
            out.append(re.escape(value[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "*" else "." if c == "?" else re.escape(c))
        i += 1
    return prefix + "".join(out) + suffix


def _literal_segment(value: str) -> str | None:
    """Самый длинный кусок значения без шаблонных * и ? — для SQL-отбора кандидатов."""
    segs, cur, i = [], [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value) and value[i + 1] in "*?\\":
            cur.append(value[i + 1])
            i += 2
            continue
        if c in "*?":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    best = max(segs, key=len) if segs else ""
    if len(best) < 3:
        return None
    try:
        best.encode("ascii")
    except UnicodeEncodeError:                    # LIKE в SQLite не сравнивает кириллицу без учёта регистра
        return None
    return best


def _b64offsets(raw: bytes) -> list[str]:
    out = []
    for i in range(3):
        enc = base64.b64encode(b" " * i + raw).decode()
        start = (0, 2, 3)[i]
        end = (None, -3, -2)[(len(raw) + i) % 3]
        out.append(enc[start:end])
    return out


def _windash(s: str) -> list[str]:
    if not re.search(r"(^|\s)-", s):
        return [s]
    return [re.sub(r"(^|\s)-", lambda m, ch=ch: m.group(1) + ch, s) for ch in ("-", "/", "–", "—", "―")]


class Value:
    """Одно значение поля: предикат и подстроки для SQL-отбора (None — отбор невозможен)."""

    def __init__(self, mods: list[str], raw):
        self.literals: list[str] | None = []
        self.null = raw is None
        mods = [m.lower() for m in mods]
        cased = "cased" in mods
        kind = "eq"
        re_flags = 0
        for m in mods:
            if m in ("contains", "startswith", "endswith", "re", "cidr", "exists", "gt", "gte", "lt", "lte"):
                kind = m
            elif m in ("i", "m", "s") and "re" in mods:
                re_flags |= {"i": re.I, "m": re.M, "s": re.S}[m]
            elif m in ("all", "cased", "base64", "base64offset", "wide", "utf16le", "utf16", "windash"):
                pass
            elif m in ("fieldref", "expand"):
                raise Unsupported(f"модификатор «{m}» не поддерживается")
            else:
                raise Unsupported(f"неизвестный модификатор «{m}»")
        self.kind = kind
        if self.null:
            self.literals = None
            return
        if kind == "exists":
            self.want = bool(raw)
            self.literals = None
            return
        if kind in ("gt", "gte", "lt", "lte"):
            try:
                self.num = float(raw)
            except (TypeError, ValueError):
                raise Unsupported(f"«{kind}» с нечисловым значением") from None
            self.literals = None
            return
        if isinstance(raw, bool):
            raw = "true" if raw else "false"
        s = str(raw)
        if kind == "cidr":
            try:
                self.net = ipaddress.ip_network(s, strict=False)
            except ValueError:
                raise Unsupported(f"неверная сеть «{s}»") from None
            self.literals = None
            return
        if kind == "re":
            try:
                self.rx = re.compile(s, re_flags)
            except re.error as e:
                raise Unsupported(f"регулярное выражение: {e}") from None
            self.literals = None
            return
        variants = _windash(s) if "windash" in mods else [s]
        if "wide" in mods or "utf16le" in mods or "utf16" in mods:
            raws = [v.encode("utf-16-le") for v in variants]
        else:
            raws = [v.encode("utf-8") for v in variants]
        if "base64offset" in mods:
            variants = [x for r in raws for x in _b64offsets(r)]
            kind = self.kind = "contains" if kind == "eq" else kind
        elif "base64" in mods:
            variants = [base64.b64encode(r).decode() for r in raws]
        flags = re.S | (0 if cased else re.I)
        pre, suf = {"contains": (".*", ".*"), "startswith": ("", ".*"), "endswith": (".*", "")}.get(kind, ("", ""))
        self.rxs = [re.compile(_sigma_regex(v, pre, suf), flags) for v in variants]
        if "base64" in mods or "base64offset" in mods:   # закодированные строки не содержат * и ?
            self.rxs = [re.compile(re.escape(v) if kind == "eq" else ".*" + re.escape(v) + ".*", flags)
                        for v in variants]
        lits = []
        for v in variants:
            seg = v if ("base64" in mods or "base64offset" in mods) else _literal_segment(v)
            if not seg or len(seg) < 3:
                lits = None
                break
            lits.append(seg)
        self.literals = lits
        self.exact = variants[0] if (kind == "eq" and len(variants) == 1 and not re.search(r"(?<!\\)[*?]", s)) else None

    def match(self, v) -> bool:
        if self.null:
            return v in (None, "")
        if self.kind == "exists":
            return (v not in (None, "")) == self.want
        if v in (None, ""):
            return False
        if self.kind in ("gt", "gte", "lt", "lte"):
            try:
                x = float(v)
            except (TypeError, ValueError):
                return False
            return {"gt": x > self.num, "gte": x >= self.num, "lt": x < self.num, "lte": x <= self.num}[self.kind]
        s = str(v)
        if self.kind == "cidr":
            try:
                return ipaddress.ip_address(s.strip("[]")) in self.net
            except ValueError:
                return False
        if self.kind == "re":
            return bool(self.rx.search(s))
        return any(r.fullmatch(s) for r in self.rxs)


def _like(lit: str) -> str:
    return "%" + lit.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# SQL-отбор кандидатов — пара (условие, параметры) или None, если отобрать нельзя (подходит любое событие)
MAX_TERMS = 120          # огромные списки значений не отбираем SQL (дерево выражения SQLite ограничено)


# Часть отбора — (SQL, параметры, запрос FTS5 или None). Запрос FTS5 (индекс триграмм) ускоряет отбор
# на больших сборах; None в нём значит «по тексту не отобрать» (например, условие на EventID).
def _join(op: str, parts: list):
    parts = [p for p in parts if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    sql = "(" + f" {op} ".join(p[0] for p in parts) + ")"
    params = [x for p in parts for x in p[1]]
    ftss = [p[2] for p in parts]
    if op == "AND":
        known = [f for f in ftss if f]
        fts = None if not known else known[0] if len(known) == 1 else "(" + " AND ".join(known) + ")"
    else:
        fts = None if any(f is None for f in ftss) else "(" + " OR ".join(ftss) + ")"
    return sql, params, fts


def _count(part) -> int:
    return len(part[1]) if part else 0


def _text_sql(lits: list[str]):
    if not lits or len(lits) > MAX_TERMS:
        return None
    parts = []
    for lit in lits:
        p = _like(lit)
        parts.append(("(message LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' OR process LIKE ? ESCAPE '\\')",
                      [p, p, p], '"' + lit.replace('"', '""') + '"'))
    return _join("OR", parts)


class FieldItem:
    def __init__(self, key: str, raw):
        name, *mods = key.split("|")
        self.name = name
        self.all = "all" in [m.lower() for m in mods]
        vals = raw if isinstance(raw, list) else [raw]
        if not vals:
            raise Unsupported(f"пустой список значений у поля {name}")
        self.values = [Value(mods, v) for v in vals]
        self.code_field = ALIASES.get(name.lower(), [name.lower()])[0] in SPECIAL_SQL
        # длинный список строк «любое из» — одно регулярное выражение вместо сотни проверок на событие
        self.combined = None
        simple = [v for v in self.values if not v.null and getattr(v, "rxs", None)]
        if not self.all and len(simple) == len(self.values) > 1 and len({r.flags for v in simple for r in v.rxs}) == 1:
            self.combined = re.compile("|".join(f"(?:{r.pattern})" for v in simple for r in v.rxs), simple[0].rxs[0].flags)

    def match(self, fields: dict) -> bool:
        if not self.name:                             # поле без имени — поиск по всему тексту
            v = full_text(fields)
        else:
            v = get_field(fields, self.name)
        if self.combined is not None:
            return v not in (None, "") and self.combined.fullmatch(str(v)) is not None
        ok = (val.match(v) for val in self.values)
        return all(ok) if self.all else any(ok)

    def necessary(self):
        if self.code_field:
            ex = [v.exact for v in self.values]
            if all(x is not None for x in ex) and not self.all:
                return f"code IN ({','.join('?' * len(ex))})", [str(x) for x in ex], None
            return None
        if self.name and any(a in ("@user", "@host") for a in ALIASES.get(self.name.lower(), [self.name.lower()])):
            return None                               # пользователь и хост могут не входить в текст события
        per = [v.literals for v in self.values]
        if self.all:
            return _join("AND", [_text_sql(p) for p in per if p])
        if any(p is None for p in per):
            return None
        return _text_sql([x for p in per for x in p])


class Search:
    def __init__(self, name: str, body):
        self.name = name
        if isinstance(body, dict):
            self.kind, self.maps = "map", [[FieldItem(k, v) for k, v in body.items()]]
        elif isinstance(body, list) and body and all(isinstance(x, dict) for x in body):
            self.kind, self.maps = "map", [[FieldItem(k, v) for k, v in m.items()] for m in body]
        elif isinstance(body, list) or isinstance(body, (str, int)):
            words = body if isinstance(body, list) else [body]
            # ключевые слова — подстроки в любом месте текста события
            self.kind, self.maps = "map", [[FieldItem("|contains", [w for w in words])]]
        else:
            raise Unsupported(f"непонятный блок «{name}»")

    def match(self, fields) -> bool:
        return any(all(it.match(fields) for it in m) for m in self.maps)

    def necessary(self):
        ors = []
        for m in self.maps:
            part = _join("AND", [it.necessary() for it in m])
            if part is None:
                return None
            ors.append(part)
        res = _join("OR", ors)
        return None if _count(res) > 3 * MAX_TERMS else res


# ---------------------------------------------------------------------------
# условие
_TOK = re.compile(r"\s*(\(|\)|\|[^\n]*|[^\s()]+)")


def _tokens(s: str) -> list[str]:
    out, pos = [], 0
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m:
            break
        pos = m.end()
        t = m.group(1)
        if t:
            out.append(t)
    return out


class Cond:
    def __init__(self, text: str, searches: dict):
        self.searches = searches
        toks = _tokens(text)
        if any(t.startswith("|") for t in toks):
            raise Unsupported("агрегации в условии (| count() …) не поддерживаются")
        self.toks, self.i = toks, 0
        self.ast = self._or()
        if self.i != len(self.toks):
            raise Unsupported(f"непонятное условие: {text}")

    def _peek(self):
        return self.toks[self.i].lower() if self.i < len(self.toks) else None

    def _take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def _or(self):
        n = [self._and()]
        while self._peek() == "or":
            self._take()
            n.append(self._and())
        return n[0] if len(n) == 1 else ("or", n)

    def _and(self):
        n = [self._not()]
        while self._peek() == "and":
            self._take()
            n.append(self._not())
        return n[0] if len(n) == 1 else ("and", n)

    def _not(self):
        if self._peek() == "not":
            self._take()
            return ("not", self._not())
        return self._prim()

    def _names(self, pat: str) -> list[str]:
        if pat.lower() == "them":
            names = [n for n in self.searches if not n.startswith("_")]
        else:
            rx = re.compile("^" + re.escape(pat).replace("\\*", ".*") + "$")
            names = [n for n in self.searches if rx.match(n)]
        if not names:
            raise Unsupported(f"в условии нет блоков «{pat}»")
        return names

    def _prim(self):
        t = self._peek()
        if t is None:
            raise Unsupported("условие оборвано")
        if t == "(":
            self._take()
            n = self._or()
            if self._peek() != ")":
                raise Unsupported("в условии не закрыта скобка")
            self._take()
            return n
        if t in ("1", "any", "all") and self.i + 1 < len(self.toks) and self.toks[self.i + 1].lower() == "of":
            q = self._take().lower()
            self._take()
            if self._peek() is None:
                raise Unsupported("условие оборвано после «of»")
            names = self._names(self._take())
            return ("all" if q == "all" else "or", [("id", x) for x in names])
        name = self._take()
        if name not in self.searches:
            raise Unsupported(f"в условии неизвестный блок «{name}»")
        return ("id", name)

    def eval(self, node, fields, memo) -> bool:
        op = node[0]
        if op == "id":
            if node[1] not in memo:
                memo[node[1]] = self.searches[node[1]].match(fields)
            return memo[node[1]]
        if op == "not":
            return not self.eval(node[1], fields, memo)
        if op in ("and", "all"):
            return all(self.eval(x, fields, memo) for x in node[1])
        return any(self.eval(x, fields, memo) for x in node[1])

    def necessary(self, node):
        op = node[0]
        if op == "id":
            return self.searches[node[1]].necessary()
        if op == "not":
            return None
        if op in ("and", "all"):
            res = _join("AND", [self.necessary(x) for x in node[1]])
        else:
            parts = []
            for x in node[1]:
                p = self.necessary(x)
                if p is None:
                    return None
                parts.append(p)
            res = _join("OR", parts)
        return None if _count(res) > 3 * MAX_TERMS else res


# ---------------------------------------------------------------------------
# журнал (logsource) → кандидаты в базе сбора
def _sysmon(*codes) -> str:
    return f"(code IN ({','.join(repr(str(c)) for c in codes)}) AND message LIKE '%Sysmon%')"


WIN_CATEGORY = {
    "process_creation": "(code='4688' OR " + _sysmon(1) + ")",
    "process_termination": "(code='4689' OR " + _sysmon(5) + ")",
    "network_connection": _sysmon(3), "file_change": _sysmon(2), "driver_load": _sysmon(6),
    "image_load": _sysmon(7), "create_remote_thread": _sysmon(8), "raw_access_thread": _sysmon(9),
    "process_access": _sysmon(10), "file_event": _sysmon(11), "registry_add": _sysmon(12),
    "registry_delete": _sysmon(12), "registry_set": _sysmon(13), "registry_rename": _sysmon(14),
    "registry_event": _sysmon(12, 13, 14), "create_stream_hash": _sysmon(15), "pipe_created": _sysmon(17, 18),
    "wmi_event": _sysmon(19, 20, 21), "dns_query": _sysmon(22), "file_delete": _sysmon(23, 26),
    "clipboard_capture": _sysmon(24), "process_tampering": _sysmon(25), "file_block_executable": _sysmon(27),
    "sysmon_status": _sysmon(4, 16), "sysmon_error": _sysmon(255),
    "ps_script": "code='4104'", "ps_module": "code='4103'", "ps_classic_start": "code='400'",
    "ps_classic_provider_start": "code='600'", "ps_classic_script": "code='800'",
}
WIN_SERVICE = {
    "security": "Security", "system": "System", "application": "Application",
    "sysmon": "Microsoft-Windows-Sysmon/Operational", "powershell": "Microsoft-Windows-PowerShell/Operational",
    "powershell-classic": "Windows PowerShell", "windefend": "Microsoft-Windows-Windows Defender/Operational",
    "taskscheduler": "Microsoft-Windows-TaskScheduler/Operational", "wmi": "Microsoft-Windows-WMI-Activity/Operational",
    "terminalservices-localsessionmanager": "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational",
    "bits-client": "Microsoft-Windows-Bits-Client/Operational", "ntlm": "Microsoft-Windows-NTLM/Operational",
    "codeintegrity-operational": "Microsoft-Windows-CodeIntegrity/Operational", "openssh": "OpenSSH/Operational",
    "firewall-as": "Microsoft-Windows-Windows Firewall With Advanced Security/Firewall",
    "dns-client": "Microsoft-Windows-DNS-Client/Operational", "smbclient-security": "Microsoft-Windows-SmbClient/Security",
    "appxdeployment-server": "Microsoft-Windows-AppXDeploymentServer/Operational",
    "printservice-admin": "Microsoft-Windows-PrintService/Admin", "printservice-operational": "Microsoft-Windows-PrintService/Operational",
    "security-mitigations": "Microsoft-Windows-Security-Mitigations", "applocker": "Microsoft-Windows-AppLocker",
    "driver-framework": "Microsoft-Windows-DriverFrameworks-UserMode/Operational",
    "shell-core": "Microsoft-Windows-Shell-Core/Operational", "capi2": "Microsoft-Windows-CAPI2/Operational",
    "rdp": "Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational",
}
LINUX_SERVICE = {
    "auditd": "source LIKE '%audit%'",
    "auth": "(process IN ('sshd','sshd-session','sudo','su','login','useradd','usermod','groupadd','passwd') "
            "OR source LIKE '%secure%' OR source LIKE '%auth%')",
    "sshd": "process IN ('sshd','sshd-session')", "sudo": "process = 'sudo'", "cron": "process IN ('CROND','cron','crond')",
}
LINUX_CATEGORY = {
    "process_creation": "(code='SYSCALL:execve' OR action IN ('Выполнение команды','Команда sudo','Команда sudo от имени root'))",
    "network_connection": "(category='network' OR " + _sysmon(3) + ")",
    "file_event": "(action LIKE 'Изменение файла%' OR " + _sysmon(11) + ")",
}
MACOS_CATEGORY = {
    "process_creation": "(action LIKE 'Команда sudo%' OR action IN ('Выполнение команды','Запуск процесса','Запуск приложения'))",
}
PLATFORMS = {"windows": "windows", "linux": "linux", "macos": "macos"}
# журналы сетевых устройств и серверов приложений без указания системы — к журналам хостов не относятся
NON_HOST = {"webserver", "proxy", "firewall", "dns", "antivirus", "database", "application"}


def logsource_sql(ls: dict) -> tuple[str, str | None]:
    """(условие на события, платформа сбора или None — для любых)."""
    prod = str(ls.get("product") or "").lower()
    cat = str(ls.get("category") or "").lower()
    svc = str(ls.get("service") or "").lower()
    conds = []
    if prod == "windows":
        if cat:
            if cat not in WIN_CATEGORY:
                raise Unsupported(f"категория журнала Windows «{cat}» не поддерживается")
            conds.append(WIN_CATEGORY[cat])
        if svc and svc in WIN_SERVICE:
            ch = WIN_SERVICE[svc].replace("'", "''")
            conds.append(f"message LIKE '[{ch}]%'")
    elif prod == "linux":
        if cat:
            if cat not in LINUX_CATEGORY:
                raise Unsupported(f"категория журнала Linux «{cat}» не поддерживается")
            conds.append(LINUX_CATEGORY[cat])
        if svc and svc in LINUX_SERVICE:
            conds.append(LINUX_SERVICE[svc])
    elif prod == "macos":
        if cat:
            if cat not in MACOS_CATEGORY:
                raise Unsupported(f"категория журнала macOS «{cat}» не поддерживается")
            conds.append(MACOS_CATEGORY[cat])
    elif prod:
        raise Unsupported(f"журналы «{prod}» LogAtlas не разбирает")
    elif cat in NON_HOST:
        raise Unsupported(f"журналы «{cat}» LogAtlas не разбирает")
    return (" AND ".join(conds) or "1"), PLATFORMS.get(prod)


# ---------------------------------------------------------------------------
class Rule:
    def __init__(self, doc: dict):
        det = doc.get("detection")
        if not isinstance(det, dict):
            raise Unsupported("нет раздела detection")
        if doc.get("correlation"):
            raise Unsupported("правила корреляции не поддерживаются")
        if det.get("timeframe"):
            raise Unsupported("timeframe (агрегации) не поддерживается")
        cond = det.get("condition")
        if cond is None:
            raise Unsupported("нет условия condition")
        conds = cond if isinstance(cond, list) else [cond]
        self.searches = {k: Search(k, v) for k, v in det.items() if k not in ("condition", "timeframe")}
        self.conds = [Cond(str(c), self.searches) for c in conds]
        self.where, self.platform = logsource_sql(doc.get("logsource") or {})
        nec = []
        for c in self.conds:
            p = c.necessary(c.ast)
            if p is None:
                nec = None
                break
            nec.append(p)
        pre = _join("OR", nec) if nec else None
        self.prefilter, self.params, self.fts = pre if pre else ("1", [], None)

    def match(self, fields) -> bool:
        return any(c.eval(c.ast, fields, {}) for c in self.conds)


def _mitre(tags) -> str | None:
    for t in tags or []:
        m = re.match(r"attack\.(t\d{4}(?:\.\d{3})?)$", str(t), re.I)
        if m:
            return m.group(1).upper()
    return None


def _rule_id(doc: dict) -> str:
    rid = str(doc.get("id") or "").strip()
    if re.fullmatch(r"[0-9A-Za-z\-]{8,64}", rid):
        return rid.lower()
    return hashlib.sha1(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# хранилище правил
_lock = threading.RLock()
_cache: dict = {"version": None, "meta": {}, "rules_version": None, "rules": {}}


def init():
    with db.catalog() as c:
        c.executescript(SCHEMA)


def _version():
    with db.catalog() as c:
        r = c.execute("SELECT count(*), max(updated), sum(enabled) FROM sigma_rules").fetchone()
    return tuple(r)


def _load_meta() -> dict:
    """Сведения обо всех правилах для находок (без компиляции — быстро)."""
    with _lock:
        v = _version()
        if _cache["version"] == v:
            return _cache["meta"]
        meta = {}
        with db.catalog() as c:
            for rid, title, level, desc, tags, product in c.execute(
                    "SELECT id, title, level, description, tags, product FROM sigma_rules"):
                meta[PREFIX + rid] = {"id": PREFIX + rid, "severity": LEVELS.get(level or "", "medium"), "title": title,
                                      "desc": desc or "", "mitre": _mitre(json.loads(tags or "[]")), "sigma": True,
                                      "product": product}
        _cache.update(version=v, meta=meta)
        return meta


def _load_rules() -> dict:
    """Скомпилированные включённые правила (перекомпилируются после изменений набора)."""
    with _lock:
        v = _version()
        if _cache["rules_version"] == v:
            return _cache["rules"]
        rules = {}
        with db.catalog() as c:
            rows = c.execute("SELECT id, yaml FROM sigma_rules WHERE enabled=1 AND error IS NULL").fetchall()
        for rid, text in rows:
            try:
                rules[rid] = Rule(yaml.safe_load(text))
            except Exception as e:  # noqa: BLE001
                log.warning("sigma rule %s: %s", rid, e)
        _cache.update(rules_version=v, rules=rules)
        return rules


def meta(rule_id: str) -> dict | None:
    if not rule_id.startswith(PREFIX):
        return None
    try:
        return _load_meta().get(rule_id)
    except sqlite3.Error:
        return None


def _docs_from(name: str, data: bytes):
    """(имя файла, документ YAML) из .yml/.yaml или архива .zip с правилами."""
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for zi in z.infolist():
                n = zi.filename
                if zi.is_dir() or not n.lower().endswith((".yml", ".yaml")) or zi.file_size > MAX_FILE:
                    continue
                if "/deprecated/" in n.lower() or "/unsupported/" in n.lower():
                    continue
                yield from _docs_from(n, z.read(zi))
        return
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        yield name, ValueError("файл не в UTF-8")
        return
    try:
        for doc in yaml.safe_load_all(text):
            if doc is not None:
                yield name, doc
    except yaml.YAMLError as e:
        yield name, ValueError(f"ошибка YAML: {str(e).splitlines()[0]}")


def import_files(files: list[tuple[str, bytes]]) -> dict:
    if yaml is None:
        raise RuntimeError("Для правил Sigma нужен пакет PyYAML")
    rep = {"added": 0, "updated": 0, "unsupported": 0, "errors": [], "skipped": 0, "problems": []}
    now = time.time()
    rows = []
    for fname, data in files:
        for name, doc in _docs_from(fname, data):
            if isinstance(doc, Exception):
                rep["errors"].append(f"{name}: {doc}")
                continue
            if not isinstance(doc, dict) or "detection" not in doc and "correlation" not in doc:
                rep["skipped"] += 1                    # не правило (глобальные блоки, справочники)
                continue
            title = str(doc.get("title") or name)[:300]
            err = None
            try:
                Rule(doc)
            except Unsupported as e:
                err = str(e)
            except Exception as e:  # noqa: BLE001
                err = f"ошибка разбора: {type(e).__name__}: {e}"
            ls = doc.get("logsource") or {}
            rows.append((_rule_id(doc), title, str(doc.get("level") or "medium").lower(), str(doc.get("status") or ""),
                         str(doc.get("description") or "")[:2000], str(doc.get("author") or "")[:300],
                         json.dumps([str(t) for t in doc.get("tags") or []], ensure_ascii=False),
                         str(ls.get("product") or ""), str(ls.get("category") or ""), str(ls.get("service") or ""),
                         yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), err, name[-300:]))
            if err:
                rep["unsupported"] += 1
                if len(rep["problems"]) < 200:
                    rep["problems"].append({"file": name, "title": title, "reason": err})
    with _lock, db.catalog() as c:
        have = {r[0] for r in c.execute("SELECT id FROM sigma_rules")}
        if len(have | {r[0] for r in rows}) > MAX_RULES:
            raise RuntimeError(f"Не больше {MAX_RULES} правил Sigma")
        for r in rows:
            if r[0] in have:
                rep["updated"] += 1
                c.execute("UPDATE sigma_rules SET title=?, level=?, status=?, description=?, author=?, tags=?, product=?, "
                          "category=?, service=?, yaml=?, error=?, source=?, updated=? WHERE id=?", (*r[1:], now, r[0]))
            else:
                rep["added"] += 1
                have.add(r[0])
                c.execute("INSERT INTO sigma_rules(id, title, level, status, description, author, tags, product, category, "
                          "service, yaml, error, source, enabled, added, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                          (*r, now, now))
    return rep


def list_rules() -> list[dict]:
    with db.catalog() as c:
        rows = c.execute("SELECT id, title, level, status, product, category, service, error, enabled, source, added, tags "
                         "FROM sigma_rules ORDER BY title COLLATE NOCASE").fetchall()
    return [{"id": r[0], "title": r[1], "level": LEVELS.get(r[2] or "", "medium"), "status": r[3], "product": r[4],
             "category": r[5], "service": r[6], "error": r[7], "enabled": bool(r[8]), "source": r[9], "added": r[10],
             "mitre": _mitre(json.loads(r[11] or "[]"))} for r in rows]


def get_rule(rid: str) -> dict | None:
    with db.catalog() as c:
        r = c.execute("SELECT id, title, level, description, author, yaml, error, enabled FROM sigma_rules WHERE id=?",
                      (rid,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "title": r[1], "level": LEVELS.get(r[2] or "", "medium"), "description": r[3], "author": r[4],
            "yaml": r[5], "error": r[6], "enabled": bool(r[7])}


def set_enabled(ids: list[str], on: bool) -> int:
    with _lock, db.catalog() as c:
        n = 0
        for rid in ids:
            n += c.execute("UPDATE sigma_rules SET enabled=?, updated=? WHERE id=?", (1 if on else 0, time.time(), rid)).rowcount
    return n


def delete(ids: list[str]) -> int:
    with _lock, db.catalog() as c:
        n = 0
        for rid in ids:
            n += c.execute("DELETE FROM sigma_rules WHERE id=?", (rid,)).rowcount
    return n


# ---------------------------------------------------------------------------
# запуск на сборе
def run(con, platform: str | None) -> dict:
    """Пересчитать находки Sigma в базе сбора. platform — платформа сбора (windows/linux/macos/mixed/…)."""
    con.execute("DELETE FROM findings WHERE rule LIKE 'sigma:%'")
    if yaml is None:
        con.commit()
        return {}
    rules = _load_rules()
    if not rules:
        con.commit()
        return {}
    cache: dict = {}
    current: list = [None]

    def la_sigma(eid, code, process, subject, user, host, message, extra):
        f = cache.get(eid)
        if f is None:
            if len(cache) > 100000:
                cache.clear()
            f = cache[eid] = parse_fields(code, process, subject, user, host, message, extra)
        try:
            return 1 if current[0].match(f) else 0
        except Exception:  # noqa: BLE001
            return 0

    con.create_function("la_sigma", 8, la_sigma)
    out = {}
    groups: dict = {}
    for rid, rule in rules.items():
        if rule.platform and platform not in (rule.platform, "mixed", None, "other"):
            continue
        groups.setdefault(rule.where, []).append((rid, rule))
    fts_db = None
    try:
        for where, items in groups.items():
            t_group = time.time()
            # события нужного журнала отбираются один раз на группу правил с одинаковым logsource
            # (во временную таблицу — только номера событий, чтобы не держать тексты в памяти)
            if where == "1":
                src, n = "events e WHERE", None
            else:
                con.execute("DROP TABLE IF EXISTS temp.sigma_cand")
                con.execute("CREATE TEMP TABLE sigma_cand(id INTEGER PRIMARY KEY)")
                n = con.execute(f"INSERT INTO temp.sigma_cand SELECT id FROM events WHERE {where}").rowcount
                if not n:
                    continue
                # CROSS JOIN — обход начинается с кандидатов, а не со всей таблицы событий
                src = "temp.sigma_cand c CROSS JOIN events e ON e.id = c.id WHERE"
            # много правил × много событий — строим индекс триграмм (FTS5) по тексту кандидатов
            use_fts = False
            if n and n * sum(1 for _, r in items if r.fts) > FTS_MIN_WORK and _fts_ok(con):
                fts_db = fts_db or _fts_attach(con)
                con.execute("DELETE FROM sx.fts")
                con.execute("INSERT INTO sx.fts(rowid, txt) SELECT id, coalesce(message,'') || ' ' || "
                            "coalesce(subject,'') || ' ' || coalesce(process,'') FROM events "
                            "WHERE id IN (SELECT id FROM temp.sigma_cand)")
                use_fts = True
            for rid, rule in items:
                current[0] = rule
                try:
                    if use_fts and rule.fts:
                        q = (f"INSERT OR IGNORE INTO findings(rule, event_id, grp) SELECT ?, e.id, coalesce(e.subject, e.process) "
                             f"FROM (SELECT rowid AS rid FROM sx.fts WHERE fts MATCH ?) m CROSS JOIN events e ON e.id = m.rid "
                             f"WHERE ({rule.prefilter}) "
                             f"AND la_sigma(e.id, e.code, e.process, e.subject, e.user, e.host, e.message, e.extra)")
                        cur = con.execute(q, [PREFIX + rid, rule.fts] + rule.params)
                    else:
                        cur = con.execute(
                            f"INSERT OR IGNORE INTO findings(rule, event_id, grp) SELECT ?, e.id, coalesce(e.subject, e.process) "
                            f"FROM {src} ({rule.prefilter}) "
                            f"AND la_sigma(e.id, e.code, e.process, e.subject, e.user, e.host, e.message, e.extra)",
                            [PREFIX + rid] + rule.params)
                    if cur.rowcount:
                        out[PREFIX + rid] = cur.rowcount
                except sqlite3.Error as e:
                    log.warning("sigma rule %s failed: %s", rid, e)
            log.debug("sigma group %.1fs, rules %d, candidates %s, fts %s: %s", time.time() - t_group, len(items), n,
                      use_fts, where[:80])
    finally:
        if fts_db:
            _fts_detach(con, fts_db)
    con.execute("DROP TABLE IF EXISTS temp.sigma_cand")
    con.commit()
    return out


FTS_MIN_WORK = 2_000_000       # событий × правил, начиная с которых выгоднее индекс триграмм
_FTS = {}


def _fts_ok(con) -> bool:
    """Есть ли в этой сборке SQLite FTS5 с токенизатором trigram (SQLite 3.34+)."""
    if "ok" not in _FTS:
        try:
            con.execute("CREATE VIRTUAL TABLE temp.sigma_fts_probe USING fts5(x, tokenize='trigram')")
            con.execute("DROP TABLE temp.sigma_fts_probe")
            _FTS["ok"] = True
        except sqlite3.Error:
            _FTS["ok"] = False
            log.info("FTS5 trigram недоступен — правила Sigma отбираются через LIKE")
    return _FTS["ok"]


def _fts_attach(con) -> str:
    import os
    import tempfile
    fd, path = tempfile.mkstemp(prefix="sigma-", suffix=".db", dir=db.TMP_DIR)
    os.close(fd)
    os.remove(path)
    con.commit()                      # ATTACH нельзя внутри транзакции
    con.execute("ATTACH DATABASE ? AS sx", (path,))
    con.execute("PRAGMA sx.journal_mode=OFF")
    con.execute("PRAGMA sx.synchronous=OFF")
    con.execute("CREATE VIRTUAL TABLE sx.fts USING fts5(txt, tokenize='trigram')")
    return path


def _fts_detach(con, path: str):
    import os
    try:
        con.commit()
        con.execute("DETACH DATABASE sx")
    except sqlite3.Error:
        pass
    for p in (path, path + "-journal"):
        try:
            os.remove(p)
        except OSError:
            pass


# применение ко всем (или одному) сборам — фоном, с ходом для страницы правил
APPLY = {"state": "idle", "done": 0, "total": 0, "current": None, "findings": 0, "started": None, "finished": None}


def apply_cases(case_ids: list[str]):
    from . import analytics
    APPLY.update(state="running", done=0, total=len(case_ids), findings=0, started=time.time(), finished=None)
    for cid in case_ids:
        c = db.get_case(cid)
        APPLY["current"] = c["name"] if c else cid
        try:
            con = db.case_db(cid)
        except FileNotFoundError:
            APPLY["done"] += 1
            continue
        try:
            res = run(con, (c or {}).get("platform"))
            APPLY["findings"] += sum(res.values())
            analytics.drop_overview(con)
            analytics.stored_overview(con, cid)        # сводка с новыми находками
        except Exception:  # noqa: BLE001
            log.exception("sigma apply %s", cid)
        finally:
            con.close()
        APPLY["done"] += 1
    APPLY.update(state="idle", current=None, finished=time.time())


def start_apply(case_id: str | None = None) -> dict:
    if APPLY["state"] == "running":
        raise RuntimeError("Правила уже применяются")
    with db.catalog() as c:
        if case_id:
            ids = [r[0] for r in c.execute("SELECT id FROM cases WHERE id=? AND status='ready'", (case_id,))]
        else:
            ids = [r[0] for r in c.execute("SELECT id FROM cases WHERE status='ready' ORDER BY created DESC")]
    APPLY.update(state="running", done=0, total=len(ids), findings=0)
    threading.Thread(target=apply_cases, args=(ids,), daemon=True, name="sigma-apply").start()
    return dict(APPLY)
