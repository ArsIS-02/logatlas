"""Linux: auditd (audit.log, в т.ч. формат ENRICHED) и экспорт journald (journalctl -o json)."""
from __future__ import annotations

import ipaddress
import re

from .base import Parser, ev, first_lines, jloads
from .classify import SYSLOG

AUDIT_HEAD = re.compile(r"^(?:node=(?P<node>\S+) )?type=(?P<type>\w+) msg=audit\((?P<ts>\d+\.\d+):(?P<serial>\d+)\):\s?(?P<body>.*)$")
FIELD = re.compile(r"(\w[\w-]*)=(\"[^\"]*\"|'[^']*'|\S+)")
HEX = re.compile(r"^[0-9A-Fa-f]+$")
UNSET = {"4294967295", "-1", "unset", "(unset)"}

PERSIST_PATHS = re.compile(r"^(?:/etc/cron|/var/spool/cron|/etc/systemd/|/usr/lib/systemd/system|/lib/systemd/system|"
                           r"/etc/rc\.local|/etc/init\.d|/etc/profile|/etc/ld\.so\.preload|/etc/xdg/autostart|"
                           r".*/\.ssh/authorized_keys|.*/\.bashrc|.*/\.bash_profile|.*/\.profile|.*/\.config/autostart)")
ACCOUNT_PATHS = re.compile(r"^/etc/(?:passwd|shadow|group|gshadow|sudoers(?:\.d/.*)?|security/.*|pam\.d/.*)$")
MODULE_SYSCALLS = {"init_module", "finit_module", "delete_module"}

ACCT_TYPES = {"ADD_USER": "Создан пользователь", "DEL_USER": "Удалён пользователь", "ADD_GROUP": "Создана группа",
              "DEL_GROUP": "Удалена группа", "USER_MGMT": "Изменение пользователя", "GRP_MGMT": "Изменение группы",
              "USER_CHAUTHTOK": "Смена пароля", "ROLE_ASSIGN": "Назначение роли", "ROLE_REMOVE": "Снятие роли",
              "ACCT_LOCK": "Блокировка учётной записи", "ACCT_UNLOCK": "Разблокировка учётной записи"}

# aarch64 / x86_64 — номера execve на случай отсутствия расшифровки
EXECVE_NR = {"221", "59"}


def _unq(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _hexdec(v: str) -> str:
    if v and not v.startswith(("\"", "'")) and HEX.match(v) and len(v) % 2 == 0 and len(v) >= 2:
        try:
            return bytes.fromhex(v).replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except ValueError:
            return v
    return _unq(v)


def _fields(body: str) -> dict:
    enriched = {}
    if "\x1d" in body:
        body, _, enr = body.partition("\x1d")
        for k, v in FIELD.findall(enr):
            enriched[k] = _unq(v)
    out = {}
    for k, v in FIELD.findall(body):
        if k == "msg" and v.startswith("'"):
            for k2, v2 in FIELD.findall(v[1:-1]):
                out[k2] = v2
        else:
            out.setdefault(k, v)
    out["_enr"] = enriched
    return out


def _saddr(h: str):
    try:
        b = bytes.fromhex(h)
    except ValueError:
        return None
    if len(b) >= 8 and b[0] == 2:          # AF_INET
        port = int.from_bytes(b[2:4], "big")
        return f"{ipaddress.IPv4Address(b[4:8])}:{port}"
    if len(b) >= 24 and b[0] == 10:        # AF_INET6
        port = int.from_bytes(b[2:4], "big")
        return f"[{ipaddress.IPv6Address(b[8:24])}]:{port}"
    return None


class AuditdParser(Parser):
    name = "linux_auditd"
    label = "auditd"
    platform = "linux"

    @classmethod
    def sniff(cls, base, head):
        lines = [l for l in first_lines(head, 20) if l.strip()]
        hits = sum(1 for l in lines if AUDIT_HEAD.match(l))
        return 95 if lines and hits >= max(1, len(lines) // 2) else 0

    def parse(self, src, ctx):
        group = []
        serial = None
        for line in src.text_lines():
            m = AUDIT_HEAD.match(line)
            if not m:
                if line.strip():
                    ctx.bad += 1
                continue
            s = (m.group("node"), m.group("ts"), m.group("serial"))
            if s != serial and group:
                e = self._emit(group, ctx)
                if e:
                    yield e
                group = []
            serial = s
            if m.group("type") == "EOE":
                if group:
                    e = self._emit(group, ctx)
                    if e:
                        yield e
                group = []
                serial = None
                continue
            group.append((m.group("type"), m.group("body"), m.group("node"), m.group("ts"), line))
        if group:
            e = self._emit(group, ctx)
            if e:
                yield e
        ctx.platform = "linux"
        ctx.label = "audit.log"

    def _emit(self, group, ctx):
        recs = {}
        for typ, body, node, ts, line in group:
            recs.setdefault(typ, []).append(_fields(body))
        typ0, _, node, ts, _ = group[0]
        t = int(float(ts) * 1000)
        raw = "\n".join(g[4] for g in group)
        if node:
            ctx.host_votes[node] += 1

        main_type = "SYSCALL" if "SYSCALL" in recs else typ0
        f = recs[main_type][0]
        enr = f.get("_enr", {})
        auid = enr.get("AUID") if enr.get("AUID") not in UNSET else None
        auid = auid or (None if f.get("auid") in UNSET else f.get("auid"))
        uid = enr.get("UID") or f.get("uid")
        user = auid or uid
        comm = _unq(f.get("comm", "")) or None
        exe = _unq(f.get("exe", "")) or None
        proc = comm or (exe.rsplit("/", 1)[-1] if exe else None)
        pid = f.get("pid")
        pid = int(pid) if pid and pid.isdigit() else None
        res = _unq(f.get("res", f.get("success", "")))
        failed = res in ("failed", "no", "0")
        level = "warning" if failed else "info"
        cat = action = subj = None
        code = main_type

        if main_type == "SYSCALL":
            sc = enr.get("SYSCALL") or f.get("syscall")
            key = _unq(f.get("key", ""))
            code = f"SYSCALL:{sc}"
            if "EXECVE" in recs:
                ex = recs["EXECVE"][0]
                try:
                    argc = int(ex.get("argc", "0"))
                except ValueError:
                    argc = 0
                args = [_hexdec(ex.get(f"a{i}", "")) for i in range(min(argc, 64))]
                subj = " ".join(a for a in args if a)
                cat, action = "process", "Выполнение команды"
            elif sc in ("execve", "execveat") or sc in EXECVE_NR:
                pt = recs.get("PROCTITLE", [{}])[0].get("proctitle")
                subj = _hexdec(pt) if pt else exe
                cat, action = "process", "Выполнение команды"
            elif sc in MODULE_SYSCALLS:
                cat, action, subj, level = "security", "Загрузка/выгрузка модуля ядра", exe, "warning"
            elif "SOCKADDR" in recs and sc in ("connect", "42", "203"):
                sa = _saddr(recs["SOCKADDR"][0].get("saddr", ""))
                if sa:
                    cat, action, subj = "network", "Сетевое подключение", sa
            if cat is None and "PATH" in recs:
                for p in recs["PATH"]:
                    name = _hexdec(p.get("name", ""))
                    nt = p.get("nametype", "")
                    if nt in ("PARENT",):
                        continue
                    if ACCOUNT_PATHS.match(name):
                        cat, action, subj = "access", "Доступ к учётным данным (файл)", name
                        break
                    if PERSIST_PATHS.match(name) and nt in ("CREATE", "NORMAL", "DELETE", ""):
                        cat, action, subj = "persistence", "Изменение файла автозапуска", name
                        break
            if cat is None and key:
                action, subj = f"Срабатывание правила аудита ({key})", exe
                cat = "security"
            msg = f"{sc or ''} " + (f"key={key} " if key else "") + (f"exe={exe} " if exe else "") + \
                  (f"cmd: {subj} " if action == 'Выполнение команды' else "") + f"success={_unq(f.get('success', ''))}"
        else:
            op = _unq(f.get("op", ""))
            fexe = exe
            addr = _unq(f.get("addr", "")) if f.get("addr") not in (None, "?", "'?'") else None
            hostname = _unq(f.get("hostname", ""))
            acct = enr.get("ACCT") or _unq(f.get("acct", "")) or enr.get("ID") or _unq(f.get("id", ""))
            if addr in ("?", ""):
                addr = None
            who = acct or user
            if main_type == "USER_LOGIN":
                cat, action = "access", "Неудачный вход" if failed else "Вход в систему"
                subj = f"{who}@{addr or hostname}" if (addr or hostname) not in (None, "", "?") else who
            elif main_type == "USER_AUTH":
                cat, action = "access", "Аутентификация: ошибка" if failed else "Аутентификация: успешно"
                subj = f"{who}@{addr}" if addr else who
            elif main_type in ("USER_START", "USER_END"):
                cat, action = "access", "Сеанс открыт" if main_type == "USER_START" else "Сеанс закрыт"
                subj = who
            elif main_type == "USER_LOGOUT":
                cat, action, subj = "access", "Выход из системы", who
            elif main_type == "USER_CMD":
                cmd = _hexdec(f.get("cmd", ""))
                cat, action, subj = "access", "Команда sudo", cmd
            elif main_type in ACCT_TYPES:
                cat, action, level = "access", ACCT_TYPES[main_type], "warning"
                subj = acct or _unq(f.get("op", "")) or who
            elif main_type == "SERVICE_START":
                cat, action, subj = "process", "Запуск службы", _unq(f.get("unit", ""))
            elif main_type == "SYSTEM_BOOT":
                cat, action = "system", "Загрузка системы"
            elif main_type == "SYSTEM_SHUTDOWN":
                cat, action = "system", "Выключение системы"
            elif main_type == "DAEMON_START":
                cat, action = "security", "Запуск auditd"
            elif main_type in ("DAEMON_END", "DAEMON_ABORT"):
                cat, action, level = "security", "Остановка auditd", "warning"
            elif main_type == "CONFIG_CHANGE":
                cat, action, level = "security", "Изменение правил аудита", "warning"
                subj = op or _unq(f.get("key", ""))
            elif main_type in ("AVC", "USER_AVC", "SELINUX_ERR"):
                denied = "denied" in raw
                cat, action = "security", "SELinux: запрет" if denied else "SELinux"
                level = "warning" if denied else "info"
                subj = comm or _unq(f.get("tcontext", ""))
            elif main_type == "ANOM_PROMISCUOUS":
                cat, action, level = "security", "Интерфейс в неразборчивом режиме", "warning"
            elif main_type == "ANOM_ABEND":
                cat, action, subj, level = "system", "Аварийное завершение процесса", comm, "warning"
            elif main_type.startswith("ANOM_"):
                cat, action, level = "security", f"Аномалия: {main_type}", "warning"
            elif main_type == "KERN_MODULE":
                cat, action, subj, level = "security", "Модуль ядра", _unq(f.get("name", "")), "warning"
            elif main_type == "NETFILTER_CFG":
                cat, action = "network", "Изменение правил firewall"
            elif main_type in ("TTY", "USER_TTY"):
                cat, action, subj = "process", "Ввод в терминале", _hexdec(f.get("data", ""))
            msg = " ".join(x for x in [main_type, f"op={op}" if op else "", f"acct={acct}" if acct else "",
                                       f"exe={fexe}" if fexe else "", f"addr={addr}" if addr else "",
                                       f"terminal={_unq(f.get('terminal', ''))}" if f.get("terminal") else "",
                                       f"res={res}" if res else ""] if x)
        msg = msg.strip() + "\n" + raw
        if subj:
            subj = subj[:300]
        return ev(t, 1, node, None, proc, pid, user, level, cat, action, subj, code, msg)


# ---------------------------------------------------------------------------
# journalctl -o json
# ---------------------------------------------------------------------------
PRIO = {"0": "critical", "1": "critical", "2": "critical", "3": "error", "4": "warning", "5": "notice", "6": "info",
        "7": "debug"}


class JournaldParser(Parser):
    name = "linux_journald"
    label = "journald"
    platform = "linux"
    splittable = True

    @classmethod
    def sniff(cls, base, head):
        h = head[:16384]
        return 90 if b'"__REALTIME_TIMESTAMP"' in h and b'"MESSAGE"' in h else 0

    def parse(self, src, ctx):
        clf = SYSLOG.match
        for line in src.lines():
            if not line.startswith(b"{"):
                continue
            try:
                d = jloads(line)
            except ValueError:
                ctx.bad += 1
                continue
            rt = d.get("__REALTIME_TIMESTAMP")
            if not rt:
                continue
            t = int(rt) // 1000
            msg = d.get("MESSAGE")
            if isinstance(msg, list):  # бинарные сообщения journald
                try:
                    msg = bytes(msg).decode("utf-8", "replace")
                except (ValueError, TypeError):
                    msg = str(msg)
            msg = msg or ""
            proc = d.get("SYSLOG_IDENTIFIER") or d.get("_COMM")
            host = d.get("_HOSTNAME")
            if host:
                ctx.host_votes[host] += 1
            pid = d.get("_PID") or d.get("SYSLOG_PID")
            r = clf(proc, msg)
            if r == "noise":
                ctx.noise += 1
                continue
            cat = action = subj = user = None
            level = PRIO.get(str(d.get("PRIORITY", "6")), "info")
            if r:
                cat, action, subj, user, lv = r
                level = lv or level
            unit = d.get("_SYSTEMD_UNIT")
            yield ev(t, 1, host, None, proc, int(pid) if str(pid or "").isdigit() else None, user, level, cat, action,
                     subj, unit, msg)
        ctx.platform = "linux"
        ctx.label = "journald"
