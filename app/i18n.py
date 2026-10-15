"""Язык интерфейса и серверных сообщений.

Клиент присылает язык заголовком X-LogAtlas-Lang (или параметром ?lang=). Значения: ru (по умолчанию) и en.
Здесь — утилиты и словари английских переводов для названий/описаний правил обнаружения и категорий.
"""


def lang_of(req) -> str:
    """Язык запроса: заголовок X-LogAtlas-Lang, затем параметр ?lang, иначе русский."""
    h = (req.headers.get("x-logatlas-lang") or "").strip().lower()
    if h == "en" or req.query_params.get("lang") == "en":
        return "en"
    return "ru"


def T(lang, ru, en):
    return en if lang == "en" else ru


CATEGORIES_EN = {
    "system": "System",
    "access": "Access & sessions",
    "process": "Processes & apps",
    "persistence": "Persistence",
    "software": "Software install",
    "device": "Devices",
    "network": "Network",
    "security": "Security",
}


# id → (title_en, desc_en) для встроенных правил обнаружения.
RULES_EN = {
    "log_cleared": ("Log clearing", "An event log was cleared — a classic sign of covering tracks."),
    "lsass": ("LSASS memory access", "A process opened lsass.exe memory — possible credential theft."),
    "av_off": ("Antivirus protection disabled", "Defender reported real-time protection turned off."),
    "av_detect": ("Security product alert", "Antivirus, XProtect, Gatekeeper or Defender reported a threat or block."),
    "susp_cmd": ("Suspicious commands",
                 "Command lines with signs of code download and execution, defense evasion, credential access or covering tracks."),
    "ps_susp": ("Suspicious PowerShell", "A PowerShell script block contains download or obfuscation markers."),
    "audit_tamper": ("Audit tampering", "auditd stopped or audit rules changed."),
    "new_account": ("Account creation and changes", "User accounts and groups created, deleted or modified."),
    "remote_access": ("Remote access", "SSH, RDP, Screen Sharing, NoMachine and other remote sessions."),
    "persistence": ("Persistence mechanisms", "Services, scheduled tasks, LaunchAgents/Daemons, autostart, crontab."),
    "removable": ("Removable media and USB", "USB devices attached and external volumes mounted."),
    "tcc_sensitive": ("Camera, microphone, screen, input access",
                      "TCC decisions on applications accessing sensitive macOS resources."),
    "install_downloads": ("Software installed from Downloads and temp folders",
                          "Installers launched from the downloads folder or a temporary directory."),
    "kmod": ("Kernel modules", "Kernel modules loaded or unloaded."),
    "auth_errors": ("Authentication errors", "Wrong passwords, authorization failures, sudo/su errors."),
    "bruteforce": ("Password guessing", "Repeated failed logins against the same account."),
    "night": ("Night activity (00:00–05:59)", "Events in the middle of the night."),
    "crash_loop": ("Service restart loop", "A service repeatedly crashes and restarts."),
    "log_gap": ("Log ends earlier than the rest", "A log source stops earlier than the other sources."),
}
