"""Журналы macOS: unified log (NDJSON/JSON), launchd.log, wifi.log, pmset -g log."""
from __future__ import annotations

import json
import re

from ..timeutil import ISO_RE, MONTHS, day_ms, infer_year, iso_match_to_ms, naive_ms, parse_iso, parse_offset
from .base import MAX_MSG, Parser, ev, first_lines, jdumps, jloads
from .classify import Classifier, NOISE, R

# ---------------------------------------------------------------------------
# Unified log
# ---------------------------------------------------------------------------
TCC_SENSITIVE = {
    "Camera": "камера", "Microphone": "микрофон", "ScreenCapture": "запись экрана",
    "Accessibility": "универсальный доступ", "ListenEvent": "мониторинг ввода",
    "SystemPolicyAllFiles": "полный доступ к диску", "PostEvent": "управление вводом",
    "AddressBook": "контакты", "Calendar": "календарь", "Photos": "фото", "Location": "геопозиция",
    "SystemPolicyDownloadsFolder": "папка Загрузки", "SystemPolicyDocumentsFolder": "папка Документы",
    "SystemPolicyDesktopFolder": "Рабочий стол", "AppleEvents": "Apple Events (автоматизация)",
    "DeveloperTool": "инструменты разработчика", "SystemPolicyRemovableVolumes": "съёмные носители",
}

UNIFIED_RULES = {
    "loginwindow": [
        R(r"screenIsLocked", "access", "Экран заблокирован"),
        R(r"screenIsUnlocked", "access", "Экран разблокирован"),
        R(r"sessionDidLogin|Login Window done|loginwindow did finish login", "access", "Вход в систему", i=True),
        R(r"sessionDidLogout|-\[\w+ logout\w*\]|performLogout", "access", "Выход из системы"),
        NOISE(r"Failed to get cache main folder"),
    ],
    "runningboardd": [
        R(r"Now tracking process: \[app<application\.(?P<subj>[\w.\-]+?)\.\d+\.\d+(?:\((?P<uid>\d+)\))?>", "process",
          "Запуск приложения"),
        R(r"Now tracking process: \[(?:anon|daemon|xpcservice)<(?P<subj>[^>(]+)", "process", "Запуск процесса"),
    ],
    "sudo": [
        R(r"^\s*(?P<user>\S+) : (?:.*?; )?TTY=\S+ ; PWD=.*? ; USER=(?P<target>\S+) ;(?: \S+=.*? ;)* COMMAND=(?P<subj>.*)$",
          "access", "Команда sudo от имени {target}"),
        R(r"incorrect password attempt", "access", "Ошибка пароля sudo", "warning"),
    ],
    "sshd|sshd-session|sshd-auth": [
        R(r"Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<ip>[\da-fA-F.:]+)", "access", "Вход по SSH ({method})"),
        R(r"Failed (?P<method>\S+) for (?:invalid user )?(?P<user>\S+) from (?P<ip>[\da-fA-F.:]+)", "access",
          "Неудачный вход SSH", "warning"),
        R(r"Invalid user (?P<user>\S*) from (?P<ip>[\da-fA-F.:]+)", "access", "Неизвестный пользователь SSH", "warning"),
    ],
    "screensharingd|ScreensharingAgent|ARDAgent": [
        R(r"[Aa]uthentication.*(?:succeeded|success)|Authenticated", "access", "Удалённый доступ: вход"),
        R(r"[Aa]uthentication.*fail", "access", "Удалённый доступ: ошибка входа", "warning"),
        R(r"(?:[Cc]onnection|[Cc]lient) (?:from|connected)\s*(?P<ip>[\da-fA-F.:]+)?", "access", "Удалённый доступ: подключение"),
    ],
    "nxserver.bin|nxnode.bin|nxd": [
        R(r"(?i)session.*(?:started|created|connected|accepted)", "access", "NoMachine: сеанс"),
        NOISE(r"^VCPRateControlSession"),
    ],
    "opendirectoryd": [
        R(r"Authentication allowed by SecureToken and account policy for (?P<user>\S+)", "access", "Проверка пароля: успешно"),
        R(r"(?i)authentication (?:failed|denied)|password (?:verification )?failed|Failed to authenticate", "access",
          "Проверка пароля: ошибка", "warning"),
        R(r"(?i)\b(?:created|deleted) (?:user|record) (?P<subj>\S+)", "access", "Изменение учётной записи", "warning"),
    ],
    "authd": [
        R(r"Succeeded authorizing right '(?P<subj>[^']+)'", "access", "Авторизация: разрешено"),
        R(r"Failed to authorize right '(?P<subj>[^']+)'", "access", "Авторизация: отказ", "warning"),
    ],
    "coreauthd|biometrickitd": [
        R(r"(?i)(?:touch ?id|biometric).*(?:match|success)", "access", "Touch ID: успешно"),
    ],
    "backgroundtaskmanagementd|BackgroundTaskManagementAgent": [
        NOISE(r"store saved to|copyJobWithLabel .* failed|monitor: got an update"),
        R(r"(?i)(?:registerLaunchItem|registered|adding|added|new item|enabled|disabled|removing|removed)\b.*?"
          r"(?:identifier|url|path|item)[:= ]+(?P<subj>\S+)", "persistence", "Фоновые объекты (BTM): изменение"),
    ],
    "smd": [R(r"(?i)SMAppService.*(?:register|enable)|(?:registered|enabled) .*(?P<subj>[\w.\-]+\.plist)",
              "persistence", "Регистрация службы (SMAppService)")],
    "syspolicyd": [
        R(r"(?i)\b(?:disallow\w*|malware|blocked|xprotect\w*.*(?:detect|match))\b", "security", "Gatekeeper / XProtect"),
        R(r"(?i)(?:quarantine).*(?:open|launch)", "security", "Запуск файла из карантина"),
    ],
    "XProtect|XprotectService|XProtectRemediatorPirrit|XProtectPluginService|MRT": [
        R(r"(?i)detect|remediat|match|malware", "security", "XProtect: обнаружение", "warning"),
    ],
    "tccd": [],  # обрабатывается отдельно (связка CTX/ATTRIBUTION/RESULT)
    "diskarbitrationd": [
        R(r"(?i)mount(?:ed)?\b.*?(?P<subj>/Volumes/[^\s,'\"]+)", "device", "Монтирование тома"),
        R(r"(?i)unmount(?:ed)?\b.*?(?P<subj>/Volumes/[^\s,'\"]+)", "device", "Отключение тома"),
    ],
    "kernel": [
        R(r"(?i)USB\w*.*\b(?:attached|connected|enumerated)\b.*?(?:\"(?P<subj>[^\"]+)\")?", "device", "USB: подключение"),
        R(r"(?i)USB\w*.*\b(?:detached|disconnected|terminated)\b", "device", "USB: отключение"),
        NOISE(r"^\d+\.\d+ InterfaceLogs\."),
    ],
    "installd|system_installd|softwareupdated|Installer|installer": [
        R(r'Installed "(?P<subj>[^"]+)"', "software", "Установлено ПО"),
    ],
    "nesessionmanager|neagent|NEIKEv2Provider": [
        R(r"(?i)(?:status changed|session).*\b(?P<st>connected|disconnected)\b", "network", "VPN: {st}"),
    ],
    "airportd": [
        R(r"(?i)(?:associated|joined|join).*?SSID[\"'=: ]+(?P<subj>[^\"',\s]+)", "network", "Wi-Fi: подключение"),
        R(r"(?i)disassociat", "network", "Wi-Fi: отключение"),
    ],
    "powerd": [
        R(r"(?i)Entering (?:Sleep|DarkWake)|Sleep (?:reason|cause)", "system", "Сон"),
        R(r"(?i)\bWake (?:from|reason)|Wake \[", "system", "Пробуждение"),
    ],
    "ReportCrash|osanalyticshelper|SubmitDiagInfo": [
        R(r"(?i)Saved (?:crash )?report for (?P<subj>[^\[\s]+)", "system", "Сбой приложения", "warning"),
        R(r"Formulating (?:fatal )?\S* ?report for corpse\[\d+\] (?P<subj>\S+)", "system", "Сбой приложения", "warning"),
    ],
    "kav_agent|kavd|kav": [
        R(r"(?i)\b(?:threat|detected|infected|malware|quarantin\w*|обнаружен\w*|угроз\w*)\b", "security",
          "Антивирус: обнаружение", "error"),
    ],
}
UNIFIED = Classifier(UNIFIED_RULES)

LEVEL_MAP = {"Default": "info", "Info": "info", "Debug": "debug", "Error": "error", "Fault": "critical"}

TCC_CTX = re.compile(r"AUTHREQ_CTX: msgID=(?P<id>[\d.]+), function=\S+, service=kTCCService(?P<svc>\w+), preflight=(?P<pre>\w+)")
TCC_ATTR = re.compile(r"AUTHREQ_ATTRIBUTION: msgID=(?P<id>[\d.]+), attribution=\{(?:responsible=\{.*?\}, )?accessing=\{TCCDProcess: identifier=(?P<ident>[^,]+), .*?binary_path=(?P<path>[^,}]+)")
TCC_RES = re.compile(r"AUTHREQ_RESULT: msgID=(?P<id>[\d.]+), authValue=(?P<val>\d+)")


class UnifiedParser(Parser):
    name = "macos_unified"
    label = "unified log"
    platform = "macos"
    splittable = True       # только NDJSON; JSON-массив целиком (см. ingest)

    @classmethod
    def sniff(cls, base, head):
        h = head[:8192]
        if b'"eventMessage"' in h and (b'"processImagePath"' in h or b'"subsystem"' in h):
            return 95
        return 0

    def parse(self, src, ctx):
        # Миллионы строк на сбор: всё, что повторяется (сутки, смещение, процесс по пути, поле extra),
        # считается один раз и берётся из словарей.
        tcc = {}
        clf = UNIFIED.match
        procs = {}          # processImagePath → имя процесса
        extras = {}         # (path, category) → готовый JSON поля extra
        tzv = ctx.tz_votes
        level_map = LEVEL_MAP
        for d in _json_records(src):
            if not isinstance(d, dict):
                continue
            et = d.get("eventType")
            if et and et != "logEvent":
                ctx.noise += 1
                continue
            ts = d.get("timestamp") or ""
            # обычный вид: «2026-10-09 19:00:01.123456+0500»
            t = None
            if len(ts) == 31 and ts[10] == " " and ts[19] == "." and ts[4] == "-" and ts[13] == ":" \
                    and ts[11:13].isdigit() and ts[14:16].isdigit() and ts[17:19].isdigit() and ts[20:23].isdigit():
                try:
                    base = day_ms(ts[0:4], ts[5:7], ts[8:10])
                    if base is not False:
                        t = base + int(ts[11:13]) * 3600000 + int(ts[14:16]) * 60000 + int(ts[17:19]) * 1000 \
                            + int(ts[20:23])
                        off = parse_offset(ts[26:])
                except ValueError:
                    t = None
            if t is None:
                m = ISO_RE.match(ts)
                if not m:
                    ctx.bad += 1
                    continue
                r = iso_match_to_ms(m)
                if not r:
                    ctx.bad += 1
                    continue
                t, off = r
            z = 0
            if off is not None:
                t -= off * 60000
                z = 1
                tzv[off] += 1
            path = d.get("processImagePath") or ""
            proc = procs.get(path)
            if proc is None:
                proc = procs[path] = path.rsplit("/", 1)[-1] or None
                if proc is None:
                    procs[path] = ""
            elif proc == "":
                proc = None
            msg = d.get("eventMessage") or ""
            sub = d.get("subsystem") or None
            level = level_map.get(d.get("messageType"), "info")
            uid = d.get("userID")
            res = clf(proc, msg)
            if res == "noise":
                ctx.noise += 1
                continue
            cat = action = subj = user = None
            if res:
                cat, action, subj, user, lv = res
                if lv:
                    level = lv
            elif proc == "tccd" and msg.startswith("AUTHREQ_"):
                cat, action, subj, lv = _tcc(msg, tcc)
                if lv:
                    level = lv
            lcat = d.get("category") if sub else None
            key = (path, lcat or None)
            extra = extras.get(key, False)
            if extra is False:
                ex = {"path": path} if path and not path.startswith("/System/") and not path.startswith("/usr/libexec/") else None
                if lcat:
                    ex = ex or {}
                    ex["cat"] = lcat
                extra = jdumps(ex) if ex else None
                if len(extras) < 50000:
                    extras[key] = extra
            if len(msg) > MAX_MSG:
                msg = msg[:MAX_MSG] + " …"
            yield (t, z, None, None, proc, d.get("processID"),
                   user or (str(uid) if uid not in (None, 0) else None),
                   level, cat, action, subj, sub, msg, extra)
        ctx.platform = "macos"
        ctx.label = "unified log"


def _tcc(msg, state):
    m = TCC_CTX.match(msg)
    if m:
        state[m.group("id")] = {"svc": m.group("svc"), "pre": m.group("pre") == "yes"}
        if len(state) > 5000:
            state.clear()
        return None, None, None, None
    m = TCC_ATTR.match(msg)
    if m:
        st = state.setdefault(m.group("id"), {})
        st["who"] = m.group("ident")
        st["path"] = m.group("path")
        return None, None, None, None
    m = TCC_RES.match(msg)
    if m:
        st = state.pop(m.group("id"), {})
        svc = st.get("svc", "?")
        allowed = m.group("val") == "2"
        name = TCC_SENSITIVE.get(svc, svc)
        who = st.get("who") or "?"
        if st.get("pre"):
            act = f"TCC: проверка доступа — {name}"
        else:
            act = f"TCC: {'разрешён' if allowed else 'запрещён'} доступ — {name}"
        return "security", act, who, None
    return None, None, None, None


def _json_records(src):
    """NDJSON или JSON-массив (log show --style json) — потоково."""
    head = src.head.lstrip()
    if head[:1] == b"[":
        dec = json.JSONDecoder()
        buf = ""
        started = False
        for chunk in src.raw_chunks_text():
            buf += chunk
            pos = 0
            if not started:
                i = buf.find("[")
                if i < 0:
                    continue
                pos = i + 1
                started = True
            while True:
                while pos < len(buf) and buf[pos] in " \r\n\t,":
                    pos += 1
                if pos >= len(buf) or buf[pos] == "]":
                    break
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    break
                yield obj
                pos = end
            buf = buf[pos:]
        return
    for line in src.lines():
        if not line or line[:1] != b"{":
            continue
        try:
            yield jloads(line)
        except ValueError:
            continue


# ---------------------------------------------------------------------------
# launchd.log
# ---------------------------------------------------------------------------
LAUNCHD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) (?:\((?P<dom>[^)]*)\) )?<(?P<lvl>\w+)>: (?P<msg>.*)$")
DOM_RE = re.compile(r"^(?P<kind>[\w-]+)(?:/(?P<uid>\d+))?(?:/(?P<label>[^\s\[]+))?(?: \[(?P<pid>\d+)\])?")
LAUNCHD_NOISE = re.compile(
    r"^(?:Last log repeated|Remove subscription|internal event: (?:PETRIFIED|SOURCE_ATTACH|INIT)|"
    r"uncorking|created$|cleaning up$|shutting down$|removing child|Could not find job with label|"
    r"service state: |job state = |Successfully spawned|launching: )")
APP_LABEL = re.compile(r"(?:\.\d+)+$")
ABNORMAL = re.compile(r"exited due to (?:exit\((?!0\))\d+\)|SIG(?!KILL|TERM)\w+)")
LVL = {"Notice": "notice", "Error": "error", "Warning": "warning", "Info": "info", "Debug": "debug",
       "Critical": "critical", "Fault": "critical"}


class LaunchdParser(Parser):
    name = "macos_launchd"
    label = "launchd.log"
    platform = "macos"

    @classmethod
    def sniff(cls, base, head):
        lines = [l for l in first_lines(head, 30) if l.strip()]
        hits = sum(1 for l in lines if LAUNCHD_RE.match(l))
        return 80 if hits and hits >= len(lines) // 2 else 0

    def parse(self, src, ctx):
        last_shutdown = 0
        for line in src.text_lines():
            m = LAUNCHD_RE.match(line)
            if not m:
                ctx.bad += 1 if line.strip() else 0
                continue
            msg = m.group("msg")
            r = parse_iso(m.group("ts"))
            if not r:
                ctx.bad += 1
                continue
            t = r[0]
            if LAUNCHD_NOISE.match(msg):
                ctx.noise += 1
                continue
            dom = m.group("dom") or ""
            dm = DOM_RE.match(dom) if dom else None
            label = dm.group("label") if dm else None
            pid = dm.group("pid") if dm else None
            user = dm.group("uid") if dm and dm.group("uid") and dm.group("kind") in ("gui", "user") else None
            cat = action = subj = None
            level = LVL.get(m.group("lvl"), "info")
            if msg.startswith("hello, launchd"):
                cat, action = "system", "Загрузка системы (launchd)"
            elif "during system shutdown" in msg:
                if t - last_shutdown > 120000:
                    cat, action = "system", "Выключение / перезагрузка"
                last_shutdown = t
            elif msg.startswith("xpcproxy spawned with pid") and label:
                if label.startswith("com.openssh.sshd."):
                    cat, action, subj = "access", "Входящее SSH-подключение", "sshd"
                elif label.startswith("application."):
                    cat, action, subj = "process", "Запуск приложения", APP_LABEL.sub("", label[12:])
                elif not label.startswith("com.apple."):
                    cat, action, subj = "process", "Запуск сторонней службы", label
            elif label and not label.startswith(("com.apple.", "application.", "com.openssh.sshd.")) and (
                    msg.startswith("exited with abnormal code") or ABNORMAL.match(msg)):
                cat, action, subj = "system", "Аварийное завершение службы", label
            elif re.search(r"(?:path|plist) = (?P<p>(?:/Users/[^/]+)?/Library/Launch(?:Agents|Daemons)/[^,\s]+)", msg):
                mp = re.search(r"(?:path|plist) = (?P<p>(?:/Users/[^/]+)?/Library/Launch(?:Agents|Daemons)/[^,\s]+)", msg)
                cat, action, subj = "persistence", "Загрузка LaunchAgent/Daemon", mp.group("p")
            yield ev(t, 0, None, None, "launchd", int(pid) if pid else None, user, level, cat, action, subj,
                     label, (f"({dom}) " if dom else "") + msg)
        ctx.platform = "macos"
        ctx.label = "launchd.log"


# ---------------------------------------------------------------------------
# wifi.log
# ---------------------------------------------------------------------------
WIFI_RE = re.compile(
    r"^(?:[A-Z][a-z]{2}) (?P<mon>[A-Z][a-z]{2}) {1,2}(?P<d>\d{1,2}) (?P<h>\d\d):(?P<mi>\d\d):(?P<s>\d\d)\.(?P<f>\d+) "
    r"\[(?P<comp>\w+)\]/(?P<pid>\d+) @\[[\d.]+\] (?:\((?P<src>[^)]*)\) ?)?(?:dq:\S+ ?)?(?:/?tid\[\w+\] )?(?P<msg>.*)$")
WIFI_CONT = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} {1,2}\d{1,2} \d\d:\d\d:\d\d\.\d+ {2,}(?P<rest>.*)$")
WIFI_NOISE = re.compile(r"^(?:Health check alive|Processed events, count|ARP offload settings|"
                        r"No valid components|Processing IPv|State:/Network|Invalid IPv|SCDynamice store)")
WIFI_RULES = Classifier({"*": [
    R(r"SubType = \"(?P<subj>[^\"]+)\";\s*\n\s*Type = VPN", "network", "VPN-подключение"),
    R(r"^IP Address: (?P<subj>[\d.]+)", "network", "IP-адрес интерфейса"),
    R(r"^Router IP Address: (?P<subj>[\d.]+)", "network", "Шлюз по умолчанию"),
    R(r"^(?P<subj>utun\d+): KEV_DL_IF_ATTACHED", "network", "Туннельный интерфейс (VPN) поднят"),
    R(r"(?i)(?:associated|joined|join\w*) .*?SSID[\"'=: ]+(?P<subj>[^\"',\s]+)", "network", "Wi-Fi: подключение"),
    R(r"(?i)\bSSID[:=] ?['\"]?(?P<subj>[^'\",\s]+).*\b(?:associated|connected)\b", "network", "Wi-Fi: подключение"),
    R(r"(?i)disassociat|Link Down", "network", "Wi-Fi: отключение"),
]})


class WifiParser(Parser):
    name = "macos_wifi"
    label = "wifi.log"
    platform = "macos"

    @classmethod
    def sniff(cls, base, head):
        lines = first_lines(head, 30)
        hits = sum(1 for l in lines if WIFI_RE.match(l))
        return 85 if hits >= 3 else 0

    def parse(self, src, ctx):
        anchor = ctx.anchor
        cur = None
        skipping = False
        for line in src.text_lines():
            m = WIFI_RE.match(line)
            if not m:
                c = WIFI_CONT.match(line)
                if cur and (c or line.startswith(("\t", " "))) and len(cur[2]) < 2000:
                    cur[2] += "\n" + (c.group("rest") if c else line)
                elif skipping or c:
                    ctx.noise += 1
                elif line.strip() and "newsyslog" not in line:
                    ctx.bad += 1
                continue
            skipping = False
            if cur:
                yield self._emit(*cur)
                cur = None
            msg = m.group("msg")
            if WIFI_NOISE.match(msg):
                ctx.noise += 1
                skipping = True
                continue
            mon = MONTHS[m.group("mon").lower()]
            d = int(m.group("d"))
            y = infer_year(mon, d, anchor)
            t = naive_ms(y, mon, d, int(m.group("h")), int(m.group("mi")), int(m.group("s")),
                         int((m.group("f") + "000")[:3]))
            cur = [t, int(m.group("pid")), msg, m.group("src")]
        if cur:
            yield self._emit(*cur)
        ctx.platform = "macos"
        ctx.label = "wifi.log"

    def _emit(self, t, pid, msg, srcfile):
        r = WIFI_RULES.match(None, msg)
        cat = action = subj = None
        if r and r != "noise":
            cat, action, subj, _, _ = r
        return ev(t, 0, None, None, "airportd", pid, None, "info", cat, action, subj,
                  (srcfile or "").split(":")[0] or None, msg)


# ---------------------------------------------------------------------------
# pmset -g log
# ---------------------------------------------------------------------------
PMSET_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (?P<dom>\S+(?: \S+)?)\s*\t(?P<msg>[^\t]*)(?:\t(?P<dur>[^\t]*))?")
PMSET_ASSERT = re.compile(r"PID (?P<pid>\d+)\((?P<proc>[^)]+)\) (?P<op>\w+) (?P<type>\w+) \"(?P<name>[^\"]*)\"")
PMSET_USB = re.compile(r"^\s*id=\d+\s+level=\d+ 0x4=USB creat=(?P<d>\d\d)\.(?P<mo>\d\d)\.(?P<y>\d{4}),? (?P<h>\d\d):(?P<mi>\d\d) "
                       r"description=(?P<desc>\S+) owner=(?P<owner>.+)$")


class PmsetParser(Parser):
    name = "macos_pmset"
    label = "pmset"
    platform = "macos"

    @classmethod
    def sniff(cls, base, head):
        h = head[:65536]
        if b"PM ASL data store" in h or (b"Assertions" in h and PMSET_RE.search(h.decode("utf-8", "replace").split("\n", 5)[-1] or "")):
            return 90
        lines = first_lines(head, 40)
        return 85 if sum(1 for l in lines if PMSET_RE.match(l)) >= 5 else 0

    def parse(self, src, ctx):
        last = {}  # частые утверждения (активность, удалённый сеанс) — не чаще раза в 10 минут
        for line in src.text_lines():
            m = PMSET_RE.match(line)
            if not m:
                u = PMSET_USB.match(line)
                if u:
                    t = naive_ms(int(u.group("y")), int(u.group("mo")), int(u.group("d")), int(u.group("h")),
                                 int(u.group("mi")), 0)
                    yield ev(t, 0, None, None, "powerd", None, None, "info", "device", "USB-устройство подключено",
                             u.group("owner").strip(), "USB", line.strip())
                continue
            r = parse_iso(m.group("ts"))
            if not r:
                continue
            t, off = r
            ctx.tz_votes[off] += 1
            t -= off * 60000
            dom = m.group("dom").strip()
            msg = m.group("msg").strip()
            dur = (m.group("dur") or "").strip()
            cat = action = subj = proc = None
            pid = None
            level = "info"
            if dom == "Start":
                cat, action = "system", "Загрузка системы (powerd)"
            elif dom in ("Sleep", "Shutdown", "ShutdownCause"):
                cat, action = "system", "Сон" if dom == "Sleep" else "Выключение"
            elif dom in ("Wake", "DarkWake", "Wake Requests"):
                if dom != "Wake Requests":
                    cat, action = "system", "Пробуждение" if dom == "Wake" else "Тёмное пробуждение (DarkWake)"
            elif dom == "Failure":
                cat, action, level = "system", "Сбой сна/пробуждения", "warning"
            elif dom == "Notification" and msg.startswith("Display is turned"):
                cat, action = "system", "Дисплей включён" if msg.endswith("on") else "Дисплей выключен"
            elif dom == "Assertions":
                a = PMSET_ASSERT.search(msg)
                if a:
                    proc, pid = a.group("proc"), int(a.group("pid"))
                    op, typ, name = a.group("op"), a.group("type"), a.group("name")
                    if op in ("Summary", "Released", "TimedOut", "ClientDied", "TurnedOff"):
                        if proc == "screensharingd" and op in ("Released", "ClientDied") and typ == "PreventSystemSleep":
                            cat, action, subj = "access", "Удалённый доступ (Screen Sharing): конец сеанса", proc
                        else:
                            ctx.noise += 1
                            continue
                    elif proc == "screensharingd":
                        cat, action, subj = "access", "Удалённый доступ (Screen Sharing)", name
                    elif proc.startswith("nx") and typ == "UserIsActive":
                        cat, action, subj = "access", "Удалённый доступ (NoMachine)", name
                    elif proc == "loginwindow" and typ == "UserIsActive":
                        cat, action, subj = "access", "Активность на экране входа", proc
                    elif typ == "UserIsActive" and proc == "WindowServer":
                        cat, action, subj = "access", "Активность пользователя (клавиатура/мышь)", proc
                    elif typ == "SoftwareUpdateTask":
                        cat, action, subj = "software", "Обновление ПО (powerd)", name
                    if cat == "access":
                        key = (proc, typ)
                        if t - last.get(key, -10 ** 15) < 600000:
                            cat = action = None
                        last[key] = t
                else:
                    ctx.noise += 1
                    continue
            yield ev(t, 1, None, None, proc or "powerd", pid, None, level, cat, action, subj, dom,
                     msg + (f"  [{dur}]" if dur else ""))
        ctx.platform = "macos"
        ctx.label = "pmset"
