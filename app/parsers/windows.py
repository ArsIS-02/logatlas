"""Журналы Windows: EVTX, XML (wevtutil / «Сохранить как XML»), JSON (Get-WinEvent | ConvertTo-Json), CSV."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import xml.etree.ElementTree as ET

from ..timeutil import naive_ms, parse_iso
from .base import Parser, ev, first_lines, jloads

NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

LOGON_TYPES = {"2": "интерактивный", "3": "сетевой", "4": "пакетный", "5": "служба", "7": "разблокировка",
               "8": "сетевой, открытый текст", "9": "NewCredentials", "10": "RDP", "11": "кэшированный",
               "12": "кэшированный RDP", "13": "кэшированная разблокировка"}
LEVELS = {0: "info", 1: "critical", 2: "error", 3: "warning", 4: "info", 5: "debug"}

SUSPICIOUS_RUN = re.compile(r"(?i)\\(?:CurrentVersion\\(?:Run|RunOnce|RunServices|Policies\\Explorer\\Run)|"
                            r"Winlogon\\(?:Userinit|Shell)|Image File Execution Options|Services\\[^\\]+\\ImagePath)")
STARTUP_DIR = re.compile(r"(?i)\\Start Menu\\Programs\\Startup\\")


def _d(data, *keys):
    for k in keys:
        v = data.get(k)
        if v not in (None, "", "-", "%%1793"):
            return str(v)
    return None


def _acct(data, user_key="TargetUserName", dom_key="TargetDomainName"):
    u = _d(data, user_key)
    dmn = _d(data, dom_key)
    if u and dmn and dmn not in ("-", "NT AUTHORITY", "Window Manager", "Font Driver Host"):
        return f"{dmn}\\{u}"
    return u


def _with_ip(who, ip):
    if ip and ip not in ("-", "::1", "127.0.0.1", "0.0.0.0", "::"):
        return f"{who or '?'}@{ip}"
    return who


def classify(eid: int, provider: str, data: dict):
    """-> (category, action, subject, user, level_override, process)"""
    p = (provider or "").lower()
    sec = "security-auditing" in p
    if sec:
        if eid == 4624:
            lt = _d(data, "LogonType") or "?"
            user = _acct(data)
            if lt == "5" or (user or "").endswith("$") or (user or "").upper() in ("SYSTEM", "NT AUTHORITY\\SYSTEM",
                                                                                   "ANONYMOUS LOGON"):
                return None, f"Вход ({LOGON_TYPES.get(lt, lt)})", user, user, None, _proc(data, "ProcessName")
            return ("access", f"Успешный вход ({LOGON_TYPES.get(lt, 'тип ' + lt)})",
                    _with_ip(user, _d(data, "IpAddress")), user, None, _proc(data, "ProcessName"))
        if eid == 4625:
            lt = _d(data, "LogonType") or "?"
            user = _acct(data)
            return ("access", f"Неудачный вход ({LOGON_TYPES.get(lt, 'тип ' + lt)})",
                    _with_ip(user, _d(data, "IpAddress")), user, "warning", _proc(data, "ProcessName"))
        if eid in (4634, 4647):
            user = _acct(data)
            if (user or "").endswith("$"):
                return None, "Выход", user, user, None, None
            return "access", "Выход из системы", user, user, None, None
        if eid == 4648:
            return ("access", "Вход с явными учётными данными",
                    _with_ip(_acct(data), _d(data, "IpAddress", "TargetServerName")), _acct(data, "SubjectUserName", "SubjectDomainName"),
                    None, _proc(data, "ProcessName"))
        if eid == 4672:
            user = _acct(data, "SubjectUserName", "SubjectDomainName")
            if (user or "").endswith("$") or (user or "").upper().endswith("SYSTEM"):
                return None, "Особые привилегии", user, user, None, None
            return "access", "Вход с правами администратора", user, user, None, None
        if eid in (4720, 4722, 4723, 4724, 4725, 4726, 4738, 4740, 4767, 4781):
            names = {4720: "Создана учётная запись", 4722: "Учётная запись включена", 4723: "Смена пароля",
                     4724: "Сброс пароля", 4725: "Учётная запись отключена", 4726: "Удалена учётная запись",
                     4738: "Изменена учётная запись", 4740: "Учётная запись заблокирована",
                     4767: "Учётная запись разблокирована", 4781: "Переименована учётная запись"}
            return "access", names[eid], _acct(data), _acct(data, "SubjectUserName", "SubjectDomainName"), "warning", None
        if eid in (4728, 4732, 4756, 4729, 4733, 4757):
            add = eid in (4728, 4732, 4756)
            member = _d(data, "MemberName", "MemberSid")
            grp = _acct(data)
            return ("access", "Добавление в группу" if add else "Удаление из группы", f"{member} → {grp}",
                    _acct(data, "SubjectUserName", "SubjectDomainName"), "warning", None)
        if eid in (4800, 4801, 4802, 4803):
            return ("access", "Экран заблокирован" if eid in (4800, 4802) else "Экран разблокирован", _acct(data),
                    _acct(data), None, None)
        if eid in (4778, 4779):
            return ("access", "RDP: переподключение" if eid == 4778 else "RDP: отключение",
                    _with_ip(_acct(data, "AccountName", "AccountDomain"), _d(data, "ClientAddress")),
                    _acct(data, "AccountName", "AccountDomain"), None, None)
        if eid == 4688:
            img = _d(data, "NewProcessName")
            cmd = _d(data, "CommandLine") or img
            return "process", "Создание процесса", cmd, _acct(data, "SubjectUserName", "SubjectDomainName"), None, _base(img)
        if eid == 4697:
            return ("persistence", "Установлена служба", f"{_d(data, 'ServiceName')}: {_d(data, 'ServiceFileName')}",
                    _acct(data, "SubjectUserName", "SubjectDomainName"), "warning", None)
        if eid in (4698, 4702):
            return ("persistence", "Создана задача планировщика" if eid == 4698 else "Изменена задача планировщика",
                    _d(data, "TaskName"), _acct(data, "SubjectUserName", "SubjectDomainName"), "warning", None)
        if eid == 1102:
            return "security", "Журнал безопасности очищен", None, _acct(data, "SubjectUserName", "SubjectDomainName"), "critical", None
        if eid == 4616:
            return "security", "Изменено системное время", None, _acct(data, "SubjectUserName", "SubjectDomainName"), "warning", None
        if eid == 6416:
            return "device", "Подключено внешнее устройство", _d(data, "DeviceDescription", "DeviceId"), None, None, None
        if eid in (4776,):
            st = _d(data, "Status")
            ok = st in (None, "0x0")
            return ("access", "Проверка учётных данных NTLM" + ("" if ok else ": ошибка"),
                    _with_ip(_d(data, "TargetUserName"), _d(data, "Workstation")), _d(data, "TargetUserName"),
                    None if ok else "warning", None)
        if eid in (4768, 4769, 4771):
            st = _d(data, "Status")
            ok = st in (None, "0x0")
            return ("access", ("Kerberos: билет TGT" if eid == 4768 else "Kerberos: билет службы" if eid == 4769 else
                               "Kerberos: ошибка предаутентификации") + ("" if ok else " (ошибка)"),
                    _with_ip(_d(data, "TargetUserName"), (_d(data, "IpAddress") or "").replace("::ffff:", "")),
                    _d(data, "TargetUserName"), None if ok and eid != 4771 else "warning", None)
        if eid == 5140:
            return "network", "Доступ к общей папке", f"{_d(data, 'ShareName')} ← {_d(data, 'IpAddress')}", _acct(data, "SubjectUserName", "SubjectDomainName"), None, None
        return None, None, None, _acct(data, "SubjectUserName", "SubjectDomainName"), None, None

    if "sysmon" in p:
        img = _d(data, "Image")
        user = _d(data, "User")
        if eid == 1:
            return "process", "Создание процесса (Sysmon)", _d(data, "CommandLine") or img, user, None, _base(img)
        if eid == 3:
            dst = f"{_d(data, 'DestinationIp')}:{_d(data, 'DestinationPort')}"
            return "network", "Сетевое соединение (Sysmon)", dst, user, None, _base(img)
        if eid == 22:
            return "network", "DNS-запрос (Sysmon)", _d(data, "QueryName"), user, None, _base(img)
        if eid in (12, 13, 14):
            obj = _d(data, "TargetObject") or ""
            if SUSPICIOUS_RUN.search(obj):
                return "persistence", "Изменение ключа автозапуска", obj, user, "warning", _base(img)
            return None, "Изменение реестра (Sysmon)", obj, user, None, _base(img)
        if eid == 11:
            tf = _d(data, "TargetFilename") or ""
            if STARTUP_DIR.search(tf):
                return "persistence", "Файл в папке автозагрузки", tf, user, "warning", _base(img)
            return None, "Создание файла (Sysmon)", tf, user, None, _base(img)
        if eid in (6, 7):
            return None, "Загрузка драйвера/библиотеки (Sysmon)", _d(data, "ImageLoaded"), user, None, _base(img)
        if eid == 8:
            return "security", "Удалённый поток (Sysmon)", _d(data, "TargetImage"), user, "warning", _base(_d(data, "SourceImage"))
        if eid == 10:
            tgt = _d(data, "TargetImage") or ""
            if tgt.lower().endswith("lsass.exe"):
                return "security", "Доступ к памяти LSASS", tgt, user, "error", _base(_d(data, "SourceImage"))
            return None, "Доступ к процессу (Sysmon)", tgt, user, None, _base(_d(data, "SourceImage"))
        if eid == 19 or eid == 20 or eid == 21:
            return "persistence", "WMI-подписка (Sysmon)", _d(data, "Name", "Consumer"), user, "warning", None
        return None, None, None, user, None, _base(img)

    if eid == 7045 and ("service control manager" in p or p == "service control manager"):
        return ("persistence", "Установлена служба", f"{_d(data, 'ServiceName', 'param1')}: {_d(data, 'ImagePath', 'param2')}",
                _d(data, "AccountName", "param5"), "warning", None)
    if eid == 7040 and "service control manager" in p:
        return "persistence", "Изменён тип запуска службы", _d(data, "param1"), None, None, None
    if "eventlog" in p and eid == 104:
        return "security", "Журнал очищен", _d(data, "Channel", "param1"), _d(data, "SubjectUserName"), "critical", None
    if "eventlog" in p and eid == 1102:
        return "security", "Журнал безопасности очищен", None, _d(data, "SubjectUserName"), "critical", None
    if eid in (6005, 6006, 6008, 6009) and "eventlog" in p:
        return ("system", {6005: "Запуск журнала событий (загрузка)", 6006: "Остановка журнала событий (выключение)",
                           6008: "Неожиданное выключение", 6009: "Загрузка ОС"}[eid], None, None,
                "warning" if eid == 6008 else None, None)
    if eid == 1074 and "user32" in p:
        return "system", "Инициировано выключение/перезагрузка", _d(data, "param1", "param7"), _d(data, "param7"), None, None
    if eid == 41 and "kernel-power" in p:
        return "system", "Некорректное выключение (Kernel-Power 41)", None, None, "error", None
    if eid in (42, 506) and "kernel-power" in p:
        return "system", "Переход в сон", None, None, None, None
    if eid in (1, 507) and ("power-troubleshooter" in p or "kernel-power" in p):
        return "system", "Пробуждение", None, None, None, None
    if eid in (12, 13) and "kernel-general" in p:
        return "system", "Запуск ОС" if eid == 12 else "Завершение работы ОС", None, None, None, None
    if "terminalservices-localsessionmanager" in p and eid in (21, 22, 23, 24, 25):
        act = {21: "RDP: вход", 22: "RDP: оболочка запущена", 23: "RDP: выход", 24: "RDP: отключение",
               25: "RDP: переподключение"}[eid]
        return "access", act, _with_ip(_d(data, "User", "Param1"), _d(data, "Address", "Param3")), _d(data, "User"), None, None
    if "terminalservices-remoteconnectionmanager" in p and eid == 1149:
        return ("access", "RDP: успешная аутентификация", _with_ip(_d(data, "Param1"), _d(data, "Param3")),
                _d(data, "Param1"), None, None)
    if "windows defender" in p:
        if eid in (1116, 1117, 1006, 1007, 1015):
            return ("security", "Defender: обнаружена угроза" if eid in (1116, 1006, 1015) else "Defender: угроза обезврежена",
                    _d(data, "Threat Name", "ThreatName", "Path"), _d(data, "Detection User"), "error", None)
        if eid in (5001, 5010, 5012):
            return "security", "Defender: защита отключена", None, None, "critical", None
        if eid == 5007:
            return "security", "Defender: изменение настроек", _d(data, "New Value"), None, "warning", None
    if "powershell" in p:
        if eid == 4104:
            sb = _d(data, "ScriptBlockText") or ""
            return "process", "PowerShell: блок скрипта", sb[:300], None, None, "powershell.exe"
        if eid in (400, 403, 600, 800):
            ha = _d(data, "HostApplication")
            if not ha:
                for v in data.values():
                    m = re.search(r"HostApplication=([^\r\n]+)", str(v))
                    if m:
                        ha = m.group(1)
                        break
            if eid == 400 and ha:
                return "process", "PowerShell: запуск", ha, None, None, "powershell.exe"
    if "taskscheduler" in p:
        if eid == 106:
            return "persistence", "Зарегистрирована задача", _d(data, "TaskName"), _d(data, "UserContext"), "warning", None
        if eid == 141:
            return "persistence", "Удалена задача", _d(data, "TaskName"), None, None, None
        if eid in (200, 201):
            return "process", "Выполнение задачи", _d(data, "TaskName"), None, None, _base(_d(data, "ActionName"))
    if "wmi-activity" in p and eid == 5861:
        return "persistence", "Постоянная WMI-подписка", _d(data, "Consumer", "PossibleCause"), None, "warning", None
    if "wlan-autoconfig" in p and eid in (8001, 8003):
        return "network", "Wi-Fi: подключение" if eid == 8001 else "Wi-Fi: отключение", _d(data, "SSID"), None, None, None
    if "networkprofile" in p and eid in (10000, 10001):
        return "network", "Сеть подключена" if eid == 10000 else "Сеть отключена", _d(data, "Name"), None, None, None
    if "windows firewall" in p and eid in (2004, 2005, 2006, 2033, 2097, 2099):
        return "network", "Изменение правил firewall", _d(data, "RuleName"), _d(data, "ModifyingUser"), None, _base(_d(data, "ModifyingApplication"))
    if ("driverframeworks-usermode" in p and eid in (2003, 2100, 2101)) or ("partition" in p and eid == 1006):
        return ("device", "USB-устройство", _d(data, "Model", "SerialNumber", "InstanceId", "LifetimeId"), None, None, None)
    if "kernel-pnp" in p and eid in (400, 410, 420, 430):
        did = _d(data, "DeviceInstanceId") or ""
        if "USB" in did.upper():
            return "device", "USB-устройство", did, None, None, None
    if "msiinstaller" in p and eid in (11707, 1033, 1034, 11724):
        return ("software", "Установлено ПО" if eid in (11707, 1033) else "Удалено ПО",
                _d(data, "param1", "ProductName") or _first_val(data), None, None, "msiexec.exe")
    if eid == 1000 and "application error" in p:
        return "system", "Сбой приложения", _d(data, "AppName", "param1") or _first_val(data), None, "warning", None
    if "bits-client" in p and eid == 59:
        return "network", "BITS: загрузка", _d(data, "url", "name"), None, None, None
    return None, None, None, None, None, None


def _first_val(data):
    for v in data.values():
        if v not in (None, ""):
            return str(v)[:200]
    return None


def _base(path):
    if not path:
        return None
    return re.split(r"[\\/]", path)[-1]


def _proc(data, key):
    return _base(_d(data, key))


def normalize(rec: dict):
    """rec: eid, provider, channel, computer, t(ms UTC), level(int), pid, data, message"""
    eid = rec.get("eid") or 0
    data = rec.get("data") or {}
    cat, action, subj, user, lvl, proc = classify(eid, rec.get("provider") or "", data)
    level = lvl or LEVELS.get(rec.get("level") or 0, "info")
    msg = rec.get("message")
    if not msg:
        parts = [f"{k}={v}" for k, v in data.items() if v not in (None, "", "-")]
        msg = "; ".join(parts)
    head = f"[{rec.get('channel') or '?'}] {rec.get('provider') or ''} {eid}"
    if action:
        head += f" — {action}"
    if subj:
        subj = str(subj)[:300]
    return ev(rec["t"], rec.get("z", 1), rec.get("computer"), None, proc or _short_provider(rec.get("provider")),
              rec.get("pid"), user, level, cat, action, subj, str(eid), head + "\n" + (msg or ""),
              {"ch": rec.get("channel")} if rec.get("channel") else None)


def _short_provider(p):
    if not p:
        return None
    return p.replace("Microsoft-Windows-", "")


# ---------------------------------------------------------------------------
def _text(v):
    if isinstance(v, dict):
        if "#text" in v:
            return _text(v["#text"])
        return None
    if isinstance(v, list):
        return ", ".join(str(_text(x)) for x in v if x is not None)
    return v


def _flatten(prefix, v, out):
    if isinstance(v, dict):
        if "#text" in v and len(v) <= 2:
            out[prefix] = _text(v)
            return
        for k, x in v.items():
            if k.startswith("#") or k == "xmlns":
                continue
            _flatten(k, x, out)
    else:
        out[prefix] = v


def rec_from_pyevtx(obj: dict):
    e = obj.get("Event", obj)
    s = e.get("System", {})
    prov = s.get("Provider", {})
    prov_name = (prov.get("#attributes") or {}).get("Name") if isinstance(prov, dict) else prov
    eid = _text(s.get("EventID"))
    tc = s.get("TimeCreated", {})
    st = (tc.get("#attributes") or {}).get("SystemTime") if isinstance(tc, dict) else tc
    r = parse_iso(st or "")
    if not r:
        return None
    t, off = r
    if off:
        t -= off * 60000
    exe = s.get("Execution") or {}
    pid = (exe.get("#attributes") or {}).get("ProcessID") if isinstance(exe, dict) else None
    data = {}
    ed = e.get("EventData")
    if isinstance(ed, dict):
        if "Data" in ed and not isinstance(ed["Data"], dict):
            d = ed["Data"]
            if isinstance(d, list):
                for i, x in enumerate(d):
                    data[f"param{i + 1}"] = _text(x)
            else:
                data["param1"] = _text(d)
        else:
            for k, v in ed.items():
                if k.startswith("#"):
                    continue
                if k == "Data" and isinstance(v, dict):
                    data["param1"] = _text(v)
                else:
                    data[k] = _text(v)
    ud = e.get("UserData")
    if isinstance(ud, dict):
        _flatten("UserData", ud, data)
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        eid = 0
    lv = _text(s.get("Level"))
    return {"eid": eid, "provider": prov_name, "channel": _text(s.get("Channel")), "computer": _text(s.get("Computer")),
            "t": t, "z": 1, "level": int(lv) if str(lv).isdigit() else 0,
            "pid": int(pid) if str(pid or "").isdigit() else None, "data": data, "message": None}


def rec_from_xml(el):
    sysel = el.find(NS + "System")
    if sysel is None:
        return None
    prov = sysel.find(NS + "Provider")
    eid_el = sysel.find(NS + "EventID")
    tc = sysel.find(NS + "TimeCreated")
    st = tc.get("SystemTime") if tc is not None else None
    r = parse_iso(st or "")
    if not r:
        return None
    t, off = r
    if off:
        t -= off * 60000
    exe = sysel.find(NS + "Execution")
    lvl = sysel.findtext(NS + "Level")
    data = {}
    ed = el.find(NS + "EventData")
    if ed is not None:
        i = 0
        for d in ed:
            i += 1
            name = d.get("Name") or f"param{i}"
            data[name] = (d.text or "").strip()
    ud = el.find(NS + "UserData")
    if ud is not None:
        for d in ud.iter():
            if len(d) == 0 and d.text and d.text.strip():
                data[d.tag.split("}")[-1]] = d.text.strip()
    msg = None
    ri = el.find(NS + "RenderingInfo")
    if ri is not None:
        msg = ri.findtext(NS + "Message")
    try:
        eid = int((eid_el.text or "0").strip()) if eid_el is not None else 0
    except ValueError:
        eid = 0
    pid = exe.get("ProcessID") if exe is not None else None
    return {"eid": eid, "provider": prov.get("Name") if prov is not None else None,
            "channel": sysel.findtext(NS + "Channel"), "computer": sysel.findtext(NS + "Computer"), "t": t, "z": 1,
            "level": int(lvl) if (lvl or "").isdigit() else 0, "pid": int(pid) if (pid or "").isdigit() else None,
            "data": data, "message": msg}


class EvtxParser(Parser):
    name = "win_evtx"
    label = "EVTX"
    platform = "windows"
    binary = True

    @classmethod
    def sniff(cls, base, head):
        return 100 if head.startswith(b"ElfFile\x00") else 0

    def parse(self, src, ctx):
        path = src.materialize(ctx.tmpdir)
        ctx.platform = "windows"
        ctx.label = os.path.splitext(src.base)[0] + ".evtx"
        try:
            try:
                from evtx import PyEvtxParser  # pyevtx-rs (быстрый, на Rust)
            except ImportError:
                PyEvtxParser = None
            if PyEvtxParser is not None:
                p = PyEvtxParser(path)
                for r in p.records_json():
                    try:
                        rec = rec_from_pyevtx(json.loads(r["data"]))
                    except Exception:  # noqa: BLE001
                        ctx.bad += 1
                        continue
                    if rec:
                        _vote(ctx, rec)
                        yield normalize(rec)
                return
            try:
                import Evtx.Evtx as pyevtx  # python-evtx (чистый Python, медленнее)
            except ImportError as e:
                raise RuntimeError("Для EVTX нужен пакет 'evtx' или 'python-evtx' (есть в Docker-образе)") from e
            with pyevtx.Evtx(path) as log:
                for record in log.records():
                    try:
                        el = ET.fromstring(record.xml())
                        rec = rec_from_xml(el)
                    except Exception:  # noqa: BLE001
                        ctx.bad += 1
                        continue
                    if rec:
                        _vote(ctx, rec)
                        yield normalize(rec)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def _vote(ctx, rec):
    if rec.get("computer"):
        ctx.host_votes[rec["computer"]] += 1


class WinXmlParser(Parser):
    name = "win_xml"
    label = "Windows XML"
    platform = "windows"

    @classmethod
    def sniff(cls, base, head):
        h = head[:8192]
        return 92 if b"schemas.microsoft.com/win/2004/08/events/event" in h else 0

    def parse(self, src, ctx):
        ctx.platform = "windows"
        ctx.label = src.base
        raw = src.raw()
        # wevtutil выдаёт поток <Event> без корня — оборачиваем
        stream = _WrapRoot(raw) if not src.head.lstrip().startswith((b"<?xml", b"<Events")) else raw
        for evname, el in ET.iterparse(stream, events=("end",)):
            if el.tag == NS + "Event":
                try:
                    rec = rec_from_xml(el)
                except Exception:  # noqa: BLE001
                    rec = None
                    ctx.bad += 1
                if rec:
                    _vote(ctx, rec)
                    yield normalize(rec)
                el.clear()


class _WrapRoot(io.RawIOBase):
    def __init__(self, f):
        self.parts = [b"<Events>", None, b"</Events>"]
        self.f = f
        self.stage = 0
        self.buf = b""

    def readable(self):
        return True

    def readinto(self, b):
        while not self.buf:
            if self.stage == 0:
                self.buf = self.parts[0]
                self.stage = 1
            elif self.stage == 1:
                chunk = self.f.read(1 << 16)
                if chunk:
                    self.buf = chunk
                else:
                    self.stage = 2
            elif self.stage == 2:
                self.buf = self.parts[2]
                self.stage = 3
            else:
                return 0
        n = min(len(b), len(self.buf))
        b[:n] = self.buf[:n]
        self.buf = self.buf[n:]
        return n


# Поля, которые извлекаем из текстового Message (Get-WinEvent, CSV из «Просмотра событий»)
MSG_KEYS = {
    "Account Name": "TargetUserName", "Имя учетной записи": "TargetUserName", "Имя учётной записи": "TargetUserName",
    "Account Domain": "TargetDomainName", "Домен учетной записи": "TargetDomainName",
    "Source Network Address": "IpAddress", "Сетевой адрес источника": "IpAddress", "Адрес сети источника": "IpAddress",
    "Logon Type": "LogonType", "Тип входа": "LogonType",
    "New Process Name": "NewProcessName", "Имя нового процесса": "NewProcessName",
    "Process Command Line": "CommandLine", "Командная строка процесса": "CommandLine",
    "Service Name": "ServiceName", "Имя службы": "ServiceName",
    "Service File Name": "ImagePath", "Имя файла службы": "ImagePath",
    "Task Name": "TaskName", "Имя задачи": "TaskName",
    "Process Name": "ProcessName", "Имя процесса": "ProcessName",
    "Member": "MemberName", "Group Name": "TargetUserName",
}
KV = re.compile(r"^\s*([^:\r\n]{2,40}):\s+(.+?)\s*$")


def data_from_message(msg: str) -> dict:
    data = {}
    for line in (msg or "").splitlines():
        m = KV.match(line)
        if m and m.group(1).strip() in MSG_KEYS:
            data[MSG_KEYS[m.group(1).strip()]] = m.group(2)
    return data


DATE_JSON = re.compile(r"/Date\((-?\d+)")


class WinJsonParser(Parser):
    name = "win_json"
    label = "Windows JSON"
    platform = "windows"

    @classmethod
    def sniff(cls, base, head):
        h = head[:16384]
        if (b'"ProviderName"' in h or b'"Provider"' in h) and (b'"Id"' in h or b'"EventID"' in h) and b'"TimeCreated"' in h:
            return 85
        if b'"Event"' in h and b'"System"' in h and b'"EventID"' in h:
            return 85
        return 0

    def parse(self, src, ctx):
        ctx.platform = "windows"
        ctx.label = src.base
        head = src.head.lstrip()
        if head.startswith(b"\xef\xbb\xbf"):
            head = head[3:]
        if head[:1] == b"[":
            data = src.raw().read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            try:
                items = json.loads(data.decode("utf-8-sig", "replace"))
            except ValueError:
                ctx.bad += 1
                return
            it = items if isinstance(items, list) else [items]
        else:
            it = (jloads(l) for l in src.lines() if l.strip().startswith(b"{"))
        for o in it:
            if not isinstance(o, dict):
                continue
            rec = self._rec(o)
            if rec:
                _vote(ctx, rec)
                yield normalize(rec)
            else:
                ctx.bad += 1

    def _rec(self, o):
        if "Event" in o and isinstance(o["Event"], dict):
            return rec_from_pyevtx(o)
        tc = o.get("TimeCreated")
        t = None
        if isinstance(tc, str):
            m = DATE_JSON.search(tc)
            if m:
                t = int(m.group(1))
            else:
                r = parse_iso(tc)
                if r:
                    t = r[0] - (r[1] or 0) * 60000
        elif isinstance(tc, dict) and "value" in tc:
            m = DATE_JSON.search(str(tc["value"]))
            t = int(m.group(1)) if m else None
        if t is None:
            return None
        msg = o.get("Message") or ""
        data = data_from_message(msg)
        props = o.get("Properties")
        if isinstance(props, list):
            for i, p in enumerate(props):
                v = p.get("Value") if isinstance(p, dict) else p
                data.setdefault(f"param{i + 1}", v if not isinstance(v, dict) else json.dumps(v)[:200])
        eid = o.get("Id") or o.get("EventID") or 0
        lvl = o.get("Level")
        return {"eid": int(eid) if str(eid).isdigit() else 0, "provider": o.get("ProviderName") or o.get("Provider"),
                "channel": o.get("LogName") or o.get("Channel"), "computer": o.get("MachineName") or o.get("Computer"),
                "t": t, "z": 1, "level": int(lvl) if str(lvl).isdigit() else 0,
                "pid": o.get("ProcessId") if isinstance(o.get("ProcessId"), int) else None,
                "data": data, "message": msg}


CSV_HEAD = re.compile(r"^﻿?\"?(Level|Уровень|Keywords|Ключевые слова)\"?[,;\t]", re.I)
CSV_DATES = [
    re.compile(r"(?P<d>\d{1,2})\.(?P<mo>\d{1,2})\.(?P<y>\d{4}),? (?P<h>\d{1,2}):(?P<mi>\d\d):(?P<s>\d\d)"),
    re.compile(r"(?P<mo>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}),? (?P<h>\d{1,2}):(?P<mi>\d\d):(?P<s>\d\d)(?: (?P<ap>[AP]M))?"),
    re.compile(r"(?P<y>\d{4})-(?P<mo>\d\d)-(?P<d>\d\d)[ T](?P<h>\d\d):(?P<mi>\d\d):(?P<s>\d\d)"),
]


class WinCsvParser(Parser):
    name = "win_csv"
    label = "Windows CSV"
    platform = "windows"

    @classmethod
    def sniff(cls, base, head):
        first = first_lines(head, 1)[0] if head else ""
        if CSV_HEAD.match(first) and re.search(r"(?i)event id|код события|идентификатор события", first):
            return 85
        return 0

    def parse(self, src, ctx):
        ctx.platform = "windows"
        ctx.label = src.base
        text = io.TextIOWrapper(src.raw(), encoding="utf-8-sig", errors="replace", newline="")
        sample = src.head[:4096].decode("utf-8", "replace")
        delim = ";" if sample.count(";") > sample.count(",") else ","
        rd = csv.reader(text, delimiter=delim)
        header = next(rd, None)
        if not header:
            return
        hl = [h.strip().lower() for h in header]

        def col(*names):
            for n in names:
                if n in hl:
                    return hl.index(n)
            return None

        c_lvl = col("level", "уровень")
        c_dt = col("date and time", "дата и время")
        c_src = col("source", "источник")
        c_id = col("event id", "код события", "идентификатор события")
        for row in rd:
            if not row or c_dt is None or c_dt >= len(row):
                continue
            t = None
            for rx in CSV_DATES:
                m = rx.search(row[c_dt])
                if m:
                    h = int(m.group("h"))
                    ap = m.groupdict().get("ap")
                    if ap == "PM" and h < 12:
                        h += 12
                    if ap == "AM" and h == 12:
                        h = 0
                    t = naive_ms(int(m.group("y")), int(m.group("mo")), int(m.group("d")), h, int(m.group("mi")), int(m.group("s")))
                    break
            if t is None:
                ctx.bad += 1
                continue
            msg = row[-1] if len(row) > len(header) - 1 else ""
            eid = row[c_id].strip() if c_id is not None and c_id < len(row) else "0"
            lvl_txt = (row[c_lvl] if c_lvl is not None else "").lower()
            lvl = 2 if ("error" in lvl_txt or "ошиб" in lvl_txt) else 3 if ("warn" in lvl_txt or "предупр" in lvl_txt) else \
                1 if ("crit" in lvl_txt or "крит" in lvl_txt) else 4
            prov = row[c_src] if c_src is not None and c_src < len(row) else None
            if prov == "Microsoft-Windows-Security-Auditing" or (prov or "").lower().startswith("microsoft-windows-security"):
                prov = "Microsoft-Windows-Security-Auditing"
            rec = {"eid": int(eid) if eid.isdigit() else 0, "provider": prov, "channel": None, "computer": None, "t": t,
                   "z": 0, "level": lvl, "pid": None, "data": data_from_message(msg), "message": msg}
            yield normalize(rec)
