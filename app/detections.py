"""Правила обнаружения.

Правило — SQL-условие по нормализованным полям событий (category, action,
subject, message…). Агрегирующие правила (подбор пароля, ночная активность,
обрыв журнала) реализованы функциями.
"""
from __future__ import annotations

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

SUSPICIOUS_CMD = [
    "%curl %|%sh%", "%wget %|%sh%", "%curl %-o /tmp/%", "%wget %-O /tmp/%", "%base64 -d%", "%base64 --decode%",
    "%/dev/tcp/%", "%nc -e %", "%nc -l%", "%ncat %", "%socat %", "%python% -c %import socket%", "%bash -i%",
    "%sh -i%", "%chmod +x /tmp/%", "%chmod 777 %", "%chmod u+s%", "%mkfifo %", "%history -c%", "%unset HISTFILE%",
    "%rm -rf /var/log%", "%shred %", "%nmap %", "%tcpdump %", "%xattr -d com.apple.quarantine%",
    "%xattr -c %", "%xattr -cr %", "%csrutil disable%", "%spctl --master-disable%", "%spctl --global-disable%",
    "%osascript -e%", "%launchctl load %", "%launchctl bootstrap %", "%dscl . -create /Users/%",
    "%dscl . -append /Groups/admin%", "%security dump-keychain%", "%security find-generic-password%",
    "%security find-internet-password%", "%defaults write %LoginHook%", "%sqlite3 %TCC.db%", "%tccutil reset%",
    "%systemsetup -setremotelogin on%", "%kickstart -activate%", "%/etc/sudoers%", "%insmod %", "%modprobe %",
    "% -enc %", "% -e JAB%", "%-encodedcommand%", "%frombase64string%", "%downloadstring%", "%downloadfile%",
    "%invoke-expression%", "%iex(%", "%iex (%", "%invoke-webrequest%", "%certutil%-urlcache%",
    "%certutil%-decode%", "%bitsadmin%/transfer%", "%mshta %", "%regsvr32%/i:http%", "%rundll32%javascript%",
    "%vssadmin%delete shadows%", "%wmic%shadowcopy delete%", "%bcdedit%recoveryenabled%no%", "%wbadmin delete%",
    "%mimikatz%", "%sekurlsa%", "%procdump%lsass%", "%comsvcs%minidump%", "%net user %/add%",
    "%net localgroup administrators%/add%", "%net1 user%", "%schtasks%/create%", "%sc create %", "%sc.exe create%",
    "%reg add%\\run%", "%wevtutil cl%", "%clear-eventlog%", "%set-mppreference%-disable%", "%add-mppreference%-exclusion%",
    "%netsh advfirewall set%off%", "%whoami /priv%", "%nltest /domain_trusts%", "%adfind%", "%psexec%",
]

RULES = [
    # id, severity, title, description, where, mitre
    ("log_cleared", "critical", "Очистка журналов",
     "Журнал событий был очищен — классический признак сокрытия следов.",
     "action LIKE '%урнал%очищен%'", "T1070.001"),
    ("lsass", "critical", "Доступ к памяти LSASS",
     "Процесс открыл память lsass.exe — возможна кража учётных данных.",
     "action = 'Доступ к памяти LSASS'", "T1003.001"),
    ("av_off", "critical", "Защита антивируса отключена",
     "Defender сообщил об отключении защиты в реальном времени.",
     "action = 'Defender: защита отключена'", "T1562.001"),
    ("av_detect", "high", "Срабатывание средств защиты",
     "Антивирус, XProtect, Gatekeeper или Defender сообщили об угрозе или блокировке.",
     "category='security' AND (action LIKE 'Антивирус%' OR action LIKE 'Defender: %угроз%' OR action LIKE 'XProtect%' "
     "OR action LIKE 'Gatekeeper%')", "T1204"),
    ("susp_cmd", "high", "Подозрительные команды",
     "Командные строки с признаками загрузки и запуска кода, обхода защиты, сбора учётных данных или сокрытия следов.",
     "(category='process' OR action LIKE 'Команда sudo%') AND ({susp})", "T1059"),
    ("ps_susp", "high", "Подозрительный PowerShell",
     "Блок PowerShell-скрипта содержит признаки загрузки или обфускации кода.",
     "action LIKE 'PowerShell:%' AND (message LIKE '%frombase64string%' OR message LIKE '%downloadstring%' OR "
     "message LIKE '%invoke-expression%' OR message LIKE '%iex(%' OR message LIKE '%net.webclient%' OR "
     "message LIKE '%-encodedcommand%' OR message LIKE '%amsiutils%' OR message LIKE '%virtualalloc%')", "T1059.001"),
    ("audit_tamper", "high", "Вмешательство в аудит",
     "Остановка auditd или изменение правил аудита.",
     "action IN ('Остановка auditd','Изменение правил аудита')", "T1562.012"),
    ("new_account", "high", "Создание и изменение учётных записей",
     "Созданы, удалены или изменены учётные записи и группы.",
     "category='access' AND action IN ('Создан пользователь','Удалён пользователь','Создана группа','Удалена группа',"
     "'Изменение пользователя','Изменение группы','Добавлен в группу','Добавление в группу','Удаление из группы',"
     "'Изменение учётной записи','Создана учётная запись','Удалена учётная запись','Изменена учётная запись',"
     "'Переименована учётная запись','Назначение роли','Учётная запись включена')",
     "T1136"),
    ("remote_access", "medium", "Удалённый доступ",
     "Входы по SSH, RDP, Screen Sharing, NoMachine и другие удалённые сеансы.",
     "category='access' AND (action LIKE 'Вход по SSH%' OR action LIKE 'Удалённый доступ%' OR action LIKE 'RDP:%' "
     "OR action LIKE 'NoMachine%' OR action LIKE 'Успешный вход (RDP)%' OR action LIKE 'Успешный вход (сетевой%')",
     "T1021"),
    ("persistence", "medium", "Механизмы закрепления",
     "Службы, задачи планировщика, LaunchAgents/Daemons, автозапуск, crontab.",
     "category='persistence'", "T1543"),
    ("removable", "medium", "Внешние носители и USB",
     "Подключение USB-устройств и монтирование внешних томов.",
     "category='device'", "T1091"),
    ("tcc_sensitive", "medium", "Доступ к камере, микрофону, экрану, вводу",
     "Решения TCC о доступе приложений к чувствительным ресурсам macOS.",
     "action LIKE 'TCC: %доступ — %' AND action NOT LIKE 'TCC: проверка%' AND (action LIKE '%камера%' OR "
     "action LIKE '%микрофон%' OR action LIKE '%запись экрана%' OR action LIKE '%универсальный доступ%' OR "
     "action LIKE '%мониторинг ввода%' OR action LIKE '%управление вводом%' OR action LIKE '%полный доступ к диску%')",
     "T1123"),
    ("install_downloads", "medium", "Установка ПО из «Загрузок» и временных папок",
     "Установочные пакеты запущены из папки загрузок или временного каталога.",
     "category='software' AND (subject LIKE '%/Downloads/%' OR subject LIKE '%\\Downloads\\%' OR subject LIKE '/tmp/%' "
     "OR subject LIKE '/private/tmp/%' OR subject LIKE '%\\Temp\\%')", "T1204.002"),
    ("kmod", "medium", "Модули ядра",
     "Загрузка или выгрузка модулей ядра.", "action LIKE '%модул% ядра%' OR action = 'Модуль ядра'", "T1547.006"),
    ("time_change", "medium", "Изменение системного времени", "Системное время было изменено.",
     "action = 'Изменено системное время'", "T1070.006"),
    ("auth_errors", "low", "Ошибки аутентификации",
     "Неверные пароли, отказы авторизации, ошибки sudo/su.",
     "category='access' AND (action LIKE 'Неудачн%' OR action LIKE 'Ошибка%' OR action LIKE '%: ошибка%' OR "
     "action LIKE '%отказ%' OR action LIKE 'Неизвестный пользователь%' OR action LIKE '%заблокирована')", "T1110"),
    ("sudo", "low", "Повышение привилегий (sudo/su)", "Команды и сеансы sudo, переключение пользователя.",
     "category='access' AND (action LIKE 'Команда sudo%' OR action LIKE 'Сеанс sudo открыт%' OR "
     "action LIKE 'Переключение пользователя%' OR action LIKE 'Вход с правами администратора%')", "T1548.003"),
    ("software", "info", "Установленное ПО", "Установки и обновления программ.",
     "category='software' AND (action LIKE 'Установлен%' OR action LIKE 'Удалено ПО' OR action LIKE 'Удалён пакет')",
     None),
    ("power", "info", "Загрузки, выключения и сбои питания", "Хронология включений и выключений хоста.",
     "category='system' AND (action LIKE 'Загрузка%' OR action LIKE 'Выключ%' OR action LIKE 'Некорректное%' OR "
     "action LIKE 'Неожиданное%' OR action LIKE 'Запуск ОС' OR action LIKE 'Завершение работы ОС')", None),
    ("crashes", "info", "Сбои приложений и служб", "Аварийные завершения.",
     "action LIKE 'Сбой приложения%' OR action LIKE 'Аварийное завершение%'", None),
    ("selinux", "low", "Запреты SELinux / AppArmor", "Политика безопасности заблокировала действие.",
     "action IN ('SELinux: запрет','AppArmor: запрет')", None),
]

AGG_RULES = {
    "bruteforce": ("high", "Подбор пароля",
                   "5 и более неудачных попыток входа для одной пары «пользователь/адрес».", "T1110"),
    "night": ("medium", "Активность ночью (00:00–05:59)",
              "Входы, удалённые сеансы и повышение привилегий в ночные часы по времени хоста.", None),
    "crash_loop": ("low", "Циклический перезапуск службы",
                   "Сторонняя служба аварийно завершается 20 и более раз — проверьте её конфигурацию.", None),
    "log_gap": ("low", "Журнал обрывается раньше остальных",
                "Плотный источник событий заканчивается заметно раньше прочих — возможна остановка сбора или ротация.",
                None),
}

ALL = {r[0]: {"id": r[0], "severity": r[1], "title": r[2], "desc": r[3], "mitre": r[5]} for r in RULES}
def _RULES_EN():
    from .i18n import RULES_EN
    return RULES_EN


for k, (sev, title, desc, mitre) in AGG_RULES.items():
    ALL[k] = {"id": k, "severity": sev, "title": title, "desc": desc, "mitre": mitre}


def _susp_sql():
    parts = []
    for p in SUSPICIOUS_CMD:
        if "|" in p and not p.startswith("%|"):
            a, b = p.split("|", 1)
            parts.append(f"(subject LIKE '{a}' AND subject LIKE '%|{b}')")
        else:
            parts.append(f"subject LIKE '{p}'")
    return " OR ".join(parts)


def meta(rule_id: str, lang: str = "ru") -> dict:
    """Сведения о правиле: встроенное или импортированное правило Sigma."""
    m = ALL.get(rule_id)
    if m is None and rule_id.startswith("sigma:"):
        from . import sigma
        m = sigma.meta(rule_id)
    m = m or {"id": rule_id, "severity": "info", "title": rule_id, "desc": ""}
    if lang == "en" and rule_id in _RULES_EN():
        en = _RULES_EN()[rule_id]
        m = {**m, "title": en[0], "desc": en[1]}
    return m


def run(con) -> dict:
    con.execute("DELETE FROM findings WHERE rule NOT LIKE 'sigma:%'")
    susp = _susp_sql()
    for rid, sev, title, desc, where, mitre in RULES:
        w = where.replace("{susp}", susp)
        con.execute(f"INSERT OR IGNORE INTO findings(rule, event_id, grp) SELECT ?, id, subject FROM events WHERE {w}",
                    (rid,))
    # подбор пароля
    con.execute("""
        INSERT OR IGNORE INTO findings(rule, event_id, grp)
        SELECT 'bruteforce', e.id, e.subject FROM events e JOIN (
            SELECT coalesce(subject, user, '?') AS s FROM events
            WHERE category='access' AND (action LIKE 'Неудачн%' OR action LIKE '%: ошибка' OR action LIKE 'Ошибка%'
                  OR action LIKE 'Неизвестный пользователь%')
            GROUP BY s HAVING count(*) >= 5) b ON coalesce(e.subject, e.user, '?') = b.s
        WHERE e.category='access' AND (e.action LIKE 'Неудачн%' OR e.action LIKE '%: ошибка' OR e.action LIKE 'Ошибка%'
              OR e.action LIKE 'Неизвестный пользователь%')""")
    # ночная активность
    con.execute("""
        INSERT OR IGNORE INTO findings(rule, event_id, grp)
        SELECT 'night', id, action FROM events
        WHERE category='access' AND ((t / 3600000) % 24) < 6 AND (
              action LIKE 'Вход%' OR action LIKE 'Успешный вход%' OR action LIKE 'Удалённый доступ%' OR
              action LIKE 'RDP:%' OR action LIKE 'Команда sudo%' OR action LIKE 'Сеанс sudo открыт%' OR
              action LIKE 'Экран разблокирован' OR action LIKE 'NoMachine%' OR action LIKE 'Переключение%' OR
              action LIKE 'Активность пользователя%')""")
    # циклические падения служб
    con.execute("""
        INSERT OR IGNORE INTO findings(rule, event_id, grp)
        SELECT 'crash_loop', e.id, e.subject FROM events e JOIN (
            SELECT subject s FROM events WHERE action='Аварийное завершение службы' GROUP BY subject HAVING count(*)>=20
        ) c ON e.subject=c.s WHERE e.action='Аварийное завершение службы'""")
    # обрыв плотного журнала
    rows = con.execute("SELECT source, count(*), min(t), max(t) FROM events GROUP BY source").fetchall()
    if rows:
        tmax = max(r[3] for r in rows)
        for src, n, a, b in rows:
            span_h = max((b - a) / 3600000, 0.01)
            if n >= 2000 and n / span_h >= 500 and tmax - b > 3600000:
                con.execute("INSERT OR IGNORE INTO findings(rule, event_id, grp) "
                            "SELECT 'log_gap', id, source FROM events WHERE source=? ORDER BY t DESC LIMIT 1", (src,))
    con.commit()
    out = {}
    for rid, n in con.execute("SELECT rule, count(*) FROM findings WHERE rule NOT LIKE 'sigma:%' GROUP BY rule"):
        out[rid] = n
    return out
