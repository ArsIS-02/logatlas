"""Классификация строк журналов Unix-подобных систем (macOS и Linux).

Правило: регулярное выражение по тексту сообщения конкретного процесса.
Именованные группы:
  subj — главный объект (приложение, команда, пакет, устройство…)
  user — пользователь, ip — адрес; остальные доступны в шаблоне action.
"""
from __future__ import annotations

import re
from urllib.parse import unquote


class Rule:
    __slots__ = ("rx", "cat", "action", "level", "noise")

    def __init__(self, pattern, cat, action, level=None, flags=0, noise=False):
        self.rx = re.compile(pattern, flags)
        self.cat = cat
        self.action = action
        self.level = level
        self.noise = noise


def R(pattern, cat, action, level=None, i=False):
    return Rule(pattern, cat, action, level, re.I if i else 0)


def NOISE(pattern):
    return Rule(pattern, None, None, noise=True)


class Classifier:
    def __init__(self, table: dict[str, list[Rule]]):
        self.table = {}
        for procs, rules in table.items():
            for p in procs.split("|"):
                self.table.setdefault(p, []).extend(rules)
        self.any = self.table.pop("*", [])

    def match(self, process: str | None, msg: str):
        """-> None | 'noise' | (cat, action, subject, user, level)"""
        rules = self.table.get(process or "")
        for group in (rules, self.any) if rules else (self.any,):
            for r in group:
                m = r.rx.search(msg)
                if not m:
                    continue
                if r.noise:
                    return "noise"
                gd = {k: v for k, v in m.groupdict().items() if v is not None}
                subj = gd.get("subj")
                if subj is None:
                    u, ip = gd.get("user"), gd.get("ip")
                    subj = f"{u}@{ip}" if u and ip else (u or ip)
                if gd.get("user"):
                    gd["user"] = gd["user"].rstrip(".,;:")
                if subj:
                    subj = subj.strip().strip("'\"").rstrip(".,;")
                    if subj.startswith("file://"):
                        subj = unquote(subj[7:]).rstrip("/")
                try:
                    action = r.action.format(**gd) if "{" in r.action else r.action
                except (KeyError, IndexError):
                    action = r.action
                return r.cat, action, subj[:300] if subj else None, gd.get("user"), r.level
        return None


# ---------------------------------------------------------------------------
# Классический syslog: /var/log/system.log, install.log (macOS), secure, auth.log,
# messages (Linux)
# ---------------------------------------------------------------------------
SYSLOG_RULES = {
    # macOS: записи utmpx в system.log
    "sudo": [
        R(r"USER_PROCESS: \d+ (?P<subj>tty\w+)", "access", "Сеанс sudo открыт"),
        R(r"DEAD_PROCESS: \d+ (?P<subj>tty\w+)", "access", "Сеанс sudo закрыт"),
        R(r"^\s*(?P<user>\S+) : (?:.*?; )?TTY=\S+ ; PWD=.*? ; USER=(?P<target>\S+) ;(?: \S+=.*? ;)* COMMAND=(?P<subj>.*)$",
          "access", "Команда sudo от имени {target}"),
        R(r"incorrect password attempt|authentication failure|NOT in sudoers", "access",
          "Ошибка sudo", "warning", i=True),
        R(r"pam_unix\(sudo:session\): session opened for user (?P<target>\S+?)(?:\(uid=\d+\))? by (?P<user>[^\s(]+)",
          "access", "Сеанс sudo открыт"),
        NOISE(r"pam_unix\(sudo:session\): session closed"),
    ],
    "login": [
        R(r"USER_PROCESS: \d+ (?P<subj>tty\w+)", "access", "Вход в терминал"),
        R(r"DEAD_PROCESS: \d+ (?P<subj>tty\w+)", "access", "Выход из терминала"),
        R(r"FAILED LOGIN|authentication failure", "access", "Неудачный вход", "warning", i=True),
    ],
    "loginwindow": [
        R(r"USER_PROCESS: \d+ (?P<subj>console)", "access", "Вход в систему (консоль)"),
        R(r"DEAD_PROCESS: \d+ (?P<subj>console)", "access", "Выход из системы (консоль)"),
    ],
    "sessionlogoutd": [R(r"DEAD_PROCESS: \d+ (?P<subj>console)", "access", "Выход из системы (консоль)")],
    "bootlog": [R(r"BOOT_TIME", "system", "Загрузка системы")],
    "shutdown|reboot|halt": [R(r"SHUTDOWN_TIME|reboot|shutdown|halt", "system", "Выключение / перезагрузка", i=True)],
    "sshd-session|sshd|sshd-auth": [
        R(r"^(?P<user>\S+) \[postauth\]\[\d+\]: USER_PROCESS: \d+ (?P<subj>tty\w+)", "access", "Вход по SSH"),
        R(r"^(?P<user>\S+) \[postauth\]\[\d+\]: DEAD_PROCESS: \d+ (?P<subj>tty\w+)", "access", "Выход SSH"),
        R(r"Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<ip>[\da-fA-F.:]+)", "access", "Вход по SSH ({method})"),
        R(r"Failed (?P<method>\S+) for (?:invalid user )?(?P<user>\S+) from (?P<ip>[\da-fA-F.:]+)", "access",
          "Неудачный вход SSH", "warning"),
        R(r"Invalid user (?P<user>\S*) from (?P<ip>[\da-fA-F.:]+)", "access", "Неизвестный пользователь SSH", "warning"),
        R(r"Disconnected from (?:authenticating |invalid )?user (?P<user>\S+) (?P<ip>[\da-fA-F.:]+)", "access", "Отключение SSH"),
        R(r"maximum authentication attempts exceeded", "access", "Превышено число попыток SSH", "warning", i=True),
    ],
    "su": [
        R(r"FAILED SU|authentication failure", "access", "Ошибка su", "warning", i=True),
        R(r"\(to (?P<subj>\S+)\) (?P<user>\S+) on", "access", "Переключение пользователя (su)"),
        R(r"session opened for user (?P<subj>\S+?)(?:\(uid=\d+\))? by (?P<user>[^\s(]+)", "access", "Переключение пользователя (su)"),
    ],
    "useradd": [R(r"new user: name=(?P<subj>[^,]+)", "access", "Создан пользователь", "warning")],
    "userdel": [R(r"delete user '(?P<subj>[^']+)'", "access", "Удалён пользователь", "warning")],
    "groupadd": [R(r"new group: name=(?P<subj>[^,]+)", "access", "Создана группа")],
    "usermod|gpasswd": [R(r"add '(?P<user>[^']+)' to (?:shadow )?group '(?P<subj>[^']+)'", "access", "Добавлен в группу", "warning")],
    "passwd|chpasswd": [R(r"password changed for (?P<user>\S+)", "access", "Смена пароля")],
    "systemd-logind": [
        R(r"New session (?P<sid>\S+) of user (?P<user>\S+)", "access", "Новый сеанс"),
        R(r"Removed session (?P<subj>\S+)", "access", "Сеанс завершён"),
        R(r"System is (?:rebooting|powering down)|Power key pressed", "system", "Выключение / перезагрузка"),
        R(r"Lid (?:opened|closed)|suspend|Suspending", "system", "Сон / пробуждение", i=True),
    ],
    "CRON|crond|cron|anacron": [
        R(r"\((?P<user>[^)]+)\) CMD \((?P<subj>.*)\)\s*$", "process", "Задание cron"),
        R(r"\((?P<user>[^)]+)\) (?:REPLACE|BEGIN EDIT|END EDIT|RELOAD)", "persistence", "Изменение crontab"),
    ],
    "crontab": [R(r"\((?P<user>[^)]+)\) (?:REPLACE|BEGIN EDIT|END EDIT|LIST)", "persistence", "Изменение crontab")],
    "kernel": [
        R(r"usb \S+: New USB device found", "device", "Подключено USB-устройство"),
        R(r"usb \S+: Product: (?P<subj>.+)", "device", "USB-устройство"),
        R(r"usb \S+: USB disconnect", "device", "Отключено USB-устройство"),
        R(r"(?:EXT4|XFS|BTRFS)-fs \((?P<subj>[^)]+)\): mounted", "device", "Монтирование ФС"),
        R(r"^Linux version (?P<subj>\S+)", "system", "Загрузка ядра"),
        R(r"audit: type=1400 .*apparmor=\"DENIED\"", "security", "AppArmor: запрет"),
    ],
    "dnf|yum|rpm|apt|dpkg": [
        R(r"(?:Installed|Upgraded|Updated)\s*:\s*(?P<subj>\S+)", "software", "Установлен пакет"),
        R(r"(?:Erased|Removed)\s*:\s*(?P<subj>\S+)", "software", "Удалён пакет"),
        R(r"status installed (?P<subj>\S+)", "software", "Установлен пакет"),
    ],
    "NetworkManager": [
        R(r"device \((?P<subj>[^)]+)\): state change: \w+ -> activated", "network", "Интерфейс активирован"),
        R(r"dhcp4 \((?P<dev>[^)]+)\):\s+address (?P<subj>\S+)", "network", "Получен IP-адрес"),
    ],
    "systemd": [
        R(r"Reached target (?:Shutdown|Power-Off|Reboot)", "system", "Выключение / перезагрузка"),
        R(r"Startup finished in", "system", "Загрузка завершена"),
    ],
    # macOS install.log
    "installd|system_installd|Installer|installer": [
        R(r'Installed "(?P<subj>[^"]+)" \((?P<ver>[^)]*)\)', "software", "Установлено ПО"),
        R(r"Product archive (?P<subj>.+?) trustLevel=(?P<trust>\d+)", "software", "Установочный пакет (trust {trust})"),
        R(r"PackageKit: Registered bundle (?P<subj>file://\S+) for uid (?P<user>\d+)", "software", "Регистрация приложения"),
    ],
    "softwareupdated": [
        R(r"SoftwareUpdate: starting download of (?P<code>\S+) \((?P<subj>[^)]*)\)", "software", "Загрузка обновления"),
    ],
    "syslogd": [NOISE(r"^(?:ASL Sender Statistics|Configuration Notice:)")],
    "*": [
        R(r"pam_unix\((?P<svc>[^:]+):auth\): authentication failure;.*?(?:\buser=(?P<user>\S+))?\s*$", "access",
          "Ошибка аутентификации ({svc})", "warning"),
        R(r"pam_unix\((?P<svc>[^:]+):session\): session opened for user (?P<user>[^\s(]+)", "access", "Сеанс открыт ({svc})"),
        R(r"pam_unix\((?P<svc>[^:]+):session\): session closed for user (?P<user>[^\s(]+)", "access", "Сеанс закрыт ({svc})"),
    ],
}

SYSLOG = Classifier(SYSLOG_RULES)

MAC_PROCS = {"loginwindow", "syslogd", "installd", "system_installd", "softwareupdated", "bootlog",
             "sessionlogoutd", "sshd-session", "opendirectoryd", "Installer", "package_script_service",
             "newsyslog", "SoftwareUpdateSettingsExtension", "OSInstaller", "airportd"}
LINUX_PROCS = {"systemd", "systemd-logind", "NetworkManager", "CRON", "crond", "useradd", "dnf",
               "yum", "rsyslogd", "auditd", "polkitd", "dbus-daemon", "chronyd", "firewalld",
               "sshd-auth", "gdm-password]", "anacron", "rngd", "kernel"}
