"""Сравнение двух сборов и эталон (базовая линия) хоста.

Сравниваются объекты, которые обычно и ищут при расследовании: процессы и запущенные программы,
пользователи и источники входа, удалённые сеансы, установленное ПО, устройства, сетевые адреса,
механизмы закрепления. Имена нормализуются (регистр, GUID, длинные числа и шестнадцатеричные
хвосты), чтобы один и тот же объект с меняющимися номерами не считался новым.

Эталон — «чистый» сбор хоста. Он хранится в каталоге (таблица baselines, ключ — имя хоста без
домена в нижнем регистре); следующие сборы того же хоста сравниваются с ним на вкладке «Обзор».
Результат сравнения кэшируется в базе сбора B (таблица kv).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from . import analytics, db

VERSION = 1
LIMIT = 300

# kind: (заголовок, условие отбора событий, выражение ключа, нормализация)
KINDS = {
    "processes": ("Процессы", "process IS NOT NULL", "process", "proc"),
    "apps": ("Запуски программ и команды", analytics.ENTITY_SPECS["apps"][1], "coalesce(subject, process)", "cmd"),
    "users": ("Пользователи", "category='access' AND user IS NOT NULL AND user NOT IN ('<private>','-','0')",
              "user", "plain"),
    "remote": ("Удалённые входы", analytics.ENTITY_SPECS["remote"][1], "coalesce(subject, user)", "plain"),
    "software": ("Установленное ПО", analytics.ENTITY_SPECS["software"][1], "coalesce(subject, process)", "cmd"),
    "devices": ("Устройства и носители", analytics.ENTITY_SPECS["devices"][1], "coalesce(subject, process)", "cmd"),
    # адреса своих интерфейсов (DHCP) меняются от недели к неделе — это не отклонение
    "network": ("Сетевые адреса", f"({analytics.ENTITY_SPECS['network'][1]}) AND action NOT IN ('IP-адрес интерфейса')",
                "coalesce(subject, process)", "plain"),
    "persistence": ("Закрепление и службы", analytics.ENTITY_SPECS["persistence"][1], "coalesce(subject, process)",
                    "cmd"),
}
ORDER = list(KINDS)

_GUID = re.compile(r"\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?", re.I)
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{12,}\b", re.I)
_LONGNUM = re.compile(r"\d{5,}")
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def norm(value: str, how: str) -> str:
    s = _WS.sub(" ", str(value).strip()).lower()
    if how == "plain":
        return s
    s = s.strip('"')
    s = _GUID.sub("{guid}", s)
    s = _HEX.sub("#", s)
    if how == "proc":                       # kworker/u8:2, python3.12 — номера в имени процесса не важны
        return _DIGITS.sub("#", s)
    return _LONGNUM.sub("#", s)            # cmd: длинные числа (PID, временные метки, размеры)


def host_key(host: str | None) -> str | None:
    if not host:
        return None
    h = str(host).strip().split(".")[0].lower()
    return h or None


# ---------------------------------------------------------------------------
# объекты одного сбора
def _collect(con, kind: str) -> dict:
    _, where, key, how = KINDS[kind]
    out: dict = {}
    for name, action, n, first, last, eid in con.execute(
            f"SELECT {key} AS name, action, count(*), min(t), max(t), min(id) FROM events "
            f"WHERE ({where}) AND {key} IS NOT NULL GROUP BY name, action"):
        k = norm(name, how)
        if not k or k in ("—", "-"):
            continue
        it = out.get(k)
        if it is None:
            it = out[k] = {"key": k, "name": name, "n": 0, "first": first, "last": last, "eid": eid,
                           "actions": {}, "_best": 0}
        it["n"] += n
        it["first"] = min(it["first"], first)
        it["last"] = max(it["last"], last)
        if n > it["_best"]:                 # показываем самое частое написание
            it["_best"] = n
            it["name"] = name
            it["eid"] = eid
        if action:
            it["actions"][action] = it["actions"].get(action, 0) + n
    return out


def _span_days(con) -> float:
    a, b = con.execute("SELECT min(t), max(t) FROM events").fetchone()
    if a is None:
        return 1.0
    return max((b - a) / 86_400_000, 1.0)


def _public(it: dict) -> dict:
    acts = sorted(it["actions"].items(), key=lambda x: -x[1])[:4]
    return {"name": it["name"], "n": it["n"], "first": it["first"], "last": it["last"], "eid": it.get("eid"),
            "actions": dict(acts)}


def _case_info(c: dict) -> dict:
    return {"id": c["id"], "name": c["name"], "host": c.get("host"), "platform": c.get("platform"),
            "events": c.get("events"), "t_min": c.get("t_min"), "t_max": c.get("t_max"), "created": c.get("created")}


def compare(a_id: str, b_id: str) -> dict:
    """Что в сборе B нового, что пропало и что стало заметно чаще по сравнению со сбором A."""
    ca, cb = db.get_case(a_id), db.get_case(b_id)
    if not ca or not cb:
        raise KeyError(a_id if not ca else b_id)
    if ca.get("status") != "ready" or cb.get("status") != "ready":
        raise ValueError("Оба сбора должны быть разобраны")
    cache_key = f"cmp:{VERSION}:{a_id}:{ca.get('events')}:{ca.get('t_max')}:{cb.get('events')}:{cb.get('t_max')}"
    con_b = db.case_db(b_id)
    try:
        r = con_b.execute("SELECT v FROM kv WHERE k=?", (cache_key,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r:
        con_b.close()
        return json.loads(r[0])

    t = time.time()
    con_a = db.case_db(a_id)
    try:
        days_a, days_b = _span_days(con_a), _span_days(con_b)
        kinds = {}
        for kind in ORDER:
            ea, eb = _collect(con_a, kind), _collect(con_b, kind)
            new = [eb[k] for k in eb.keys() - ea.keys()]
            gone = [ea[k] for k in ea.keys() - eb.keys()]
            more = []
            for k in eb.keys() & ea.keys():
                ra, rb = ea[k]["n"] / days_a, eb[k]["n"] / days_b
                if eb[k]["n"] >= 20 and rb >= 10 * ra:
                    it = _public(eb[k])
                    it["n_a"] = ea[k]["n"]
                    it["ratio"] = round(rb / ra, 1)
                    more.append(it)
            new.sort(key=lambda x: x["first"])
            gone.sort(key=lambda x: -x["n"])
            more.sort(key=lambda x: -x["ratio"])
            kinds[kind] = {
                "title": KINDS[kind][0], "n_a": len(ea), "n_b": len(eb), "common": len(ea.keys() & eb.keys()),
                "new_total": len(new), "gone_total": len(gone), "more_total": len(more),
                "new": [_public(x) for x in new[:LIMIT]], "gone": [_public(x) for x in gone[:LIMIT]],
                "more": more[:LIMIT],
            }
    finally:
        con_a.close()
    out = {"a": _case_info(ca), "b": _case_info(cb), "kinds": kinds, "order": ORDER,
           "days_a": round(days_a, 1), "days_b": round(days_b, 1), "took": round(time.time() - t, 2)}
    try:
        con_b.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        con_b.execute("DELETE FROM kv WHERE k LIKE 'cmp:%' AND k LIKE ?", (f"cmp:{VERSION}:{a_id}:%",))
        con_b.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (cache_key, json.dumps(out, ensure_ascii=False)))
        con_b.commit()
    except sqlite3.Error:
        pass
    finally:
        con_b.close()
    return out


def summary(result: dict) -> dict:
    return {k: {"title": v["title"], "new": v["new_total"], "gone": v["gone_total"], "more": v["more_total"]}
            for k, v in result["kinds"].items()}


# ---------------------------------------------------------------------------
# эталон хоста
SCHEMA = "CREATE TABLE IF NOT EXISTS baselines(host TEXT PRIMARY KEY, case_id TEXT, set_at REAL)"


def init():
    with db.catalog() as c:
        c.execute(SCHEMA)


def case_host(c: dict) -> str | None:
    """Хост сбора: из каталога, иначе самый частый хост в его событиях."""
    if c.get("host"):
        return c["host"]
    try:
        con = db.case_db(c["id"])
    except FileNotFoundError:
        return None
    try:
        ov = analytics.stored_overview(con, c["id"], compute=False) or {}
        hosts = ov.get("hosts") or []
        if hosts:
            return hosts[0]
        r = con.execute("SELECT host, count(*) n FROM events WHERE host IS NOT NULL AND host != 'localhost' "
                        "GROUP BY host ORDER BY n DESC LIMIT 1").fetchone()
        return r[0] if r else None
    finally:
        con.close()


def get_baseline(host: str | None) -> dict | None:
    k = host_key(host)
    if not k:
        return None
    with db.catalog() as c:
        r = c.execute("SELECT case_id, set_at FROM baselines WHERE host=?", (k,)).fetchone()
    if not r:
        return None
    bc = db.get_case(r[0])
    if not bc:
        return None
    return {**_case_info(bc), "set_at": r[1]}


def set_baseline(case_id: str) -> dict:
    c = db.get_case(case_id)
    if not c:
        raise KeyError(case_id)
    if c.get("status") != "ready":
        raise ValueError("Эталоном можно сделать только разобранный сбор")
    host = case_host(c)
    k = host_key(host)
    if not k:
        raise ValueError("В сборе не найдено имя хоста — эталон привязывается к хосту")
    with db.catalog() as cat:
        cat.execute("INSERT OR REPLACE INTO baselines(host, case_id, set_at) VALUES (?,?,?)", (k, case_id, time.time()))
    return {"host": host, "baseline": get_baseline(host)}


def clear_baseline(case_id: str):
    with db.catalog() as cat:
        cat.execute("DELETE FROM baselines WHERE case_id=?", (case_id,))


def baseline_status(case_id: str, with_diff: bool = True) -> dict:
    c = db.get_case(case_id)
    if not c:
        raise KeyError(case_id)
    host = case_host(c)
    base = get_baseline(host)
    out = {"host": host, "baseline": base, "is_baseline": bool(base and base["id"] == case_id), "diff": None}
    if with_diff and base and base["id"] != case_id and c.get("status") == "ready":
        try:
            out["diff"] = summary(compare(base["id"], case_id))
        except (KeyError, ValueError, FileNotFoundError):
            out["diff"] = None
    return out


def candidates(case_id: str) -> list[dict]:
    """Сборы для сравнения: сначала эталон и сборы того же хоста (новые сверху), затем остальные."""
    c = db.get_case(case_id)
    if not c:
        raise KeyError(case_id)
    hk = host_key(case_host(c))
    base = get_baseline(case_host(c))
    with db.catalog() as cat:
        rows = [dict(r) for r in cat.execute(
            "SELECT * FROM cases WHERE status='ready' AND id != ? ORDER BY created DESC", (case_id,))]
    out = []
    for r in rows:
        same = bool(hk) and host_key(r.get("host")) == hk
        out.append({**_case_info(r), "same_host": same, "is_baseline": bool(base and base["id"] == r["id"])})
    out.sort(key=lambda x: (not x["is_baseline"], not x["same_host"]))
    return out
