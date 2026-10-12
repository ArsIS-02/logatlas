"""Справочник MITRE ATT&CK: тактики и техники.

Нужен странице «Покрытие», чтобы показывать не голые коды вроде T1059.001, а названия техник и
тактик, и считать, какая часть матрицы закрыта включёнными правилами.

Встроенный снимок (mitre-attack.json рядом с этим файлом) кладётся в каталог при первом запуске —
справочник работает сразу и без интернета. Кнопка «Обновить» забирает свежий официальный набор
из репозитория MITRE на GitHub (mitre-attack/attack-stix-data, формат STIX 2.1).
Снимок пересобирается скриптом scripts/make-mitre.py.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from . import db, net

log = logging.getLogger("logatlas.mitre")

UPSTREAM = os.environ.get(
    "LOGATLAS_MITRE_UPSTREAM",
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/enterprise-attack.json")
UPDATE_ENABLED = os.environ.get("LOGATLAS_MITRE_UPDATE", "1") != "0"
MAX_DOWNLOAD = 80 << 20                   # по сети идёт около 6 МБ (сжатие), в распакованном виде ~55 МБ
SNAPSHOT = os.path.join(os.path.dirname(__file__), "mitre-attack.json")
OFFLINE_HINT = ("Если у этого компьютера нет доступа в интернет, справочник остаётся встроенным — "
                "он уже работает, просто без самых свежих техник.")

# Названия тактик по-русски: в ATT&CK они только по-английски, а в интерфейсе нужен русский.
# Ключи — short name тактики; незнакомая тактика покажется английским названием из справочника.
TACTIC_RU = {
    "reconnaissance": "Разведка",
    "resource-development": "Подготовка ресурсов",
    "initial-access": "Первоначальный доступ",
    "execution": "Выполнение кода",
    "persistence": "Закрепление",
    "privilege-escalation": "Повышение привилегий",
    "stealth": "Скрытность",
    "defense-impairment": "Подавление защиты",
    "defense-evasion": "Обход защиты",
    "credential-access": "Доступ к учётным данным",
    "discovery": "Изучение системы",
    "lateral-movement": "Перемещение по сети",
    "collection": "Сбор данных",
    "command-and-control": "Управление",
    "exfiltration": "Вывод данных",
    "impact": "Воздействие",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mitre_tactics(
  id TEXT PRIMARY KEY, name TEXT, ord INTEGER);
CREATE TABLE IF NOT EXISTS mitre_techniques(
  id TEXT PRIMARY KEY, name TEXT, tactics TEXT, deprecated INTEGER DEFAULT 0, url TEXT, replaced_by TEXT);
CREATE TABLE IF NOT EXISTS mitre_meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), version TEXT, released TEXT, source TEXT, updated REAL);
"""

_lock = threading.RLock()
_cache: dict = {"key": None, "tactics": [], "techniques": {}}

UPDATE = {"state": "idle", "stage": None, "received": 0, "size": 0, "error": None, "report": None,
          "started": None, "finished": None}


# ---------------------------------------------------------------------------
# разбор официального набора STIX
def parse_bundle(data: bytes) -> dict:
    """STIX-набор ATT&CK -> {version, released, tactics: [...], techniques: [...]}."""
    try:
        objects = json.loads(data)["objects"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"Это не набор ATT&CK в формате STIX: {e}") from None

    tactics, techniques, matrices, revoked, collection = {}, [], [], [], None
    for x in objects:
        t = x.get("type")
        if t == "relationship":
            if x.get("relationship_type") == "revoked-by":
                revoked.append((x.get("source_ref"), x.get("target_ref")))
            continue
        if t == "x-mitre-tactic":
            tactics[x["id"]] = x
        elif t == "x-mitre-matrix":
            matrices.append(x)
        elif t == "x-mitre-collection" and collection is None:
            collection = x
        elif t == "attack-pattern":
            techniques.append(x)
    if not techniques:
        raise RuntimeError("В наборе ATT&CK нет ни одной техники")

    order, seen = [], set()
    for m in matrices:                                   # порядок тактик — как в матрице ATT&CK
        for ref in m.get("tactic_refs") or []:
            if ref in tactics and ref not in seen:
                seen.add(ref)
                order.append(tactics[ref])
    for tid, x in tactics.items():                       # тактики вне матрицы — в конец
        if tid not in seen:
            order.append(x)

    out_tactics = [{"id": x.get("x_mitre_shortname") or "", "name": x.get("name") or "", "ord": i}
                   for i, x in enumerate(order) if x.get("x_mitre_shortname")]

    code_of = {}
    for x in techniques:
        ref = next((r for r in x.get("external_references") or [] if r.get("source_name") == "mitre-attack"), None)
        if ref and ref.get("external_id"):
            code_of[x["id"]] = ref["external_id"]
    # чем заменили отозванную технику: ATT&CK изредка переделывает тактики целиком (в 19.0 так
    # разобрали Defense Evasion), и правила со старыми кодами надо чинить — подсказываем замену
    replaced = {code_of[src]: code_of[dst] for src, dst in revoked if src in code_of and dst in code_of}

    out_tech = []
    for x in techniques:
        code = code_of.get(x["id"])
        if not code:
            continue
        ref = next((r for r in x.get("external_references") or [] if r.get("source_name") == "mitre-attack"), {})
        out_tech.append({
            "id": code,
            "name": x.get("name") or code,
            "tactics": [p.get("phase_name") for p in x.get("kill_chain_phases") or [] if p.get("phase_name")],
            "deprecated": 1 if (x.get("revoked") or x.get("x_mitre_deprecated")) else 0,
            "url": ref.get("url") or "",
            "replaced_by": replaced.get(code, ""),
        })
    out_tech.sort(key=lambda r: r["id"])
    version = str((collection or {}).get("x_mitre_version") or "")
    released = str((collection or {}).get("modified") or "")[:10]
    return {"version": version, "released": released, "tactics": out_tactics, "techniques": out_tech}


def _store(parsed: dict, source: str):
    with _lock, db.catalog() as c:
        c.executescript(SCHEMA)
        c.execute("DELETE FROM mitre_tactics")
        c.executemany("INSERT INTO mitre_tactics(id, name, ord) VALUES (?,?,?)",
                      [(t["id"], t["name"], t["ord"]) for t in parsed["tactics"]])
        c.execute("DELETE FROM mitre_techniques")
        c.executemany("INSERT INTO mitre_techniques(id, name, tactics, deprecated, url, replaced_by) "
                      "VALUES (?,?,?,?,?,?)",
                      [(t["id"], t["name"], json.dumps(t["tactics"]), t["deprecated"], t["url"],
                        t.get("replaced_by") or "") for t in parsed["techniques"]])
        c.execute("INSERT INTO mitre_meta(id, version, released, source, updated) VALUES (1,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET version=excluded.version, released=excluded.released, "
                  "source=excluded.source, updated=excluded.updated",
                  (parsed["version"], parsed["released"], source, time.time()))
        _cache["key"] = None


def init():
    """Создать таблицы и, если справочника ещё нет, положить встроенный снимок."""
    with db.catalog() as c:
        c.executescript(SCHEMA)
        n = c.execute("SELECT count(*) FROM mitre_techniques").fetchone()[0]
    if n:
        return
    try:
        with open(SNAPSHOT, "rb") as f:
            parsed = json.load(f)
        _store(parsed, "встроенный снимок")
        log.info("mitre: встроенный снимок ATT&CK %s, техник %d", parsed.get("version"), len(parsed["techniques"]))
    except (OSError, ValueError, KeyError) as e:
        log.warning("mitre: встроенный снимок не прочитан: %s", e)


# ---------------------------------------------------------------------------
# чтение
def _load() -> tuple[list, dict]:
    with _lock:
        try:
            with db.catalog() as c:
                key = c.execute("SELECT updated, (SELECT count(*) FROM mitre_techniques) FROM mitre_meta "
                                "WHERE id=1").fetchone()
                if _cache["key"] == key and key is not None:
                    return _cache["tactics"], _cache["techniques"]
                tactics = [{"id": r[0], "name": TACTIC_RU.get(r[0]) or r[1], "en": r[1]}
                           for r in c.execute("SELECT id, name, ord FROM mitre_tactics ORDER BY ord")]
                tech = {r[0]: {"id": r[0], "name": r[1], "tactics": json.loads(r[2] or "[]"),
                               "deprecated": bool(r[3]), "url": r[4], "replaced_by": r[5] or ""}
                        for r in c.execute("SELECT id, name, tactics, deprecated, url, replaced_by "
                                           "FROM mitre_techniques")}
        except sqlite3.OperationalError:
            return [], {}
        _cache.update(key=key, tactics=tactics, techniques=tech)
        return tactics, tech


def tactics() -> list:
    return _load()[0]


def techniques() -> dict:
    return _load()[1]


def technique(tid: str) -> dict | None:
    """Техника по коду. Для подтехники (T1059.001), которой нет в справочнике, берём родителя."""
    tech = _load()[1]
    t = tech.get(tid)
    if t is None and "." in tid:
        t = tech.get(tid.split(".")[0])
    return t


def meta() -> dict | None:
    try:
        with db.catalog() as c:
            r = c.execute("SELECT version, released, source, updated FROM mitre_meta WHERE id=1").fetchone()
            n = c.execute("SELECT count(*) FROM mitre_techniques").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    if not r:
        return None
    return {"version": r[0], "released": r[1], "source": r[2], "updated": r[3], "count": n,
            "upstream": UPSTREAM, "can_update": UPDATE_ENABLED}


# ---------------------------------------------------------------------------
# обновление из репозитория MITRE
def _update():
    try:
        UPDATE["stage"] = "Загрузка справочника ATT&CK из репозитория MITRE…"

        def progress(got, total):
            UPDATE.update(received=got, size=total)

        try:
            data = net.download(UPSTREAM, MAX_DOWNLOAD, progress, timeout=120)
        except Exception as e:  # noqa: BLE001  (сетевые ошибки разных классов)
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(net.error_text(e, UPSTREAM, OFFLINE_HINT)) from None
        UPDATE["stage"] = "Разбор справочника…"
        parsed = parse_bundle(data)
        prev = meta() or {}
        _store(parsed, "MITRE ATT&CK")
        UPDATE["report"] = {
            "version": parsed["version"], "released": parsed["released"],
            "techniques": len(parsed["techniques"]), "tactics": len(parsed["tactics"]),
            "same": bool(prev.get("version")) and prev.get("version") == parsed["version"],
            "was": prev.get("version") or "",
        }
        log.info("mitre: ATT&CK %s, техник %d", parsed["version"], len(parsed["techniques"]))
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, RuntimeError):
            log.exception("mitre update failed")
        UPDATE["error"] = str(e) or type(e).__name__
    finally:
        UPDATE.update(state="idle", stage=None, finished=time.time())


def start_update() -> dict:
    if not UPDATE_ENABLED:
        raise RuntimeError("Обновление справочника ATT&CK отключено (LOGATLAS_MITRE_UPDATE=0)")
    with _lock:
        if UPDATE["state"] == "running":
            raise RuntimeError("Справочник уже загружается")
        UPDATE.update(state="running", stage="Подключение…", received=0, size=0, error=None, report=None,
                      started=time.time(), finished=None)
    threading.Thread(target=_update, daemon=True, name="mitre-update").start()
    return dict(UPDATE)
