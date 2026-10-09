"""Реестр парсеров. Выбирается парсер с наибольшей оценкой sniff()."""
from .generic import JsonLinesParser, TextParser
from .linux import AuditdParser, JournaldParser
from .macos import LaunchdParser, PmsetParser, UnifiedParser, WifiParser
from .syslog import SyslogParser
from .windows import EvtxParser, WinCsvParser, WinJsonParser, WinXmlParser

PARSERS = [EvtxParser, WinXmlParser, WinJsonParser, WinCsvParser, UnifiedParser, AuditdParser, JournaldParser,
           PmsetParser, WifiParser, LaunchdParser, SyslogParser, JsonLinesParser, TextParser]


def choose(base: str, head: bytes):
    best, score = None, 0
    for p in PARSERS:
        try:
            s = p.sniff(base, head)
        except Exception:  # noqa: BLE001
            s = 0
        if s > score:
            best, score = p, s
    return best, score


SUPPORTED = {
    "macos": ["unified log (log show/stream --style ndjson|json)", "system.log", "install.log", "wifi.log",
              "launchd.log", "pmset -g log", "любые текстовые журналы со временем"],
    "windows": ["EVTX", "XML (wevtutil qe /f:xml, «Сохранить как XML»)", "JSON (Get-WinEvent | ConvertTo-Json)",
                "CSV из «Просмотра событий»"],
    "linux": ["audit.log (auditd, RAW и ENRICHED)", "journalctl -o json", "secure / auth.log / messages",
              "любые текстовые журналы со временем"],
}
