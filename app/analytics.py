"""Запросы для дашбордов, хронологии, сущностей, находок и поиска."""
from __future__ import annotations

import json
import re
import sqlite3
import threading

from . import detections, search
from .parsers.base import CATEGORIES

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
BUCKETS = [1000, 5000, 15000, 30000, MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 3 * HOUR, 6 * HOUR,
           12 * HOUR, DAY, 7 * DAY]


def pick_bucket(span_ms: int, target=140) -> int:
    for b in BUCKETS:
        if span_ms / b <= target:
            return b
    return BUCKETS[-1]


def range_clause(t0, t1, alias=""):
    p = f"{alias}." if alias else ""
    cl, params = [], []
    if t0 is not None:
        cl.append(f"{p}t >= ?")
        params.append(int(t0))
    if t1 is not None:
        cl.append(f"{p}t <= ?")
        params.append(int(t1))
    return (" AND ".join(cl) or "1"), params


def bounds(con, t0, t1):
    w, p = range_clause(t0, t1)
    r = con.execute(f"SELECT min(t), max(t), count(*) FROM events WHERE {w}", p).fetchone()
    return r[0], r[1], r[2]


# ---------------------------------------------------------------------------
def overview(con, t0=None, t1=None):
    """Сводка для вкладки «Обзор» и ленты времени.

    На больших сборах (миллионы событий) каждый полный проход по таблице стоит секунды,
    поэтому счётчики по источникам, уровням, категориям и гистограмма считаются одним
    проходом, а действия и поток «источник → категория → действие» — вторым.
    """
    w, p = range_clause(t0, t1)
    tmin, tmax, total = bounds(con, t0, t1)
    out = {"total": total, "t_min": tmin, "t_max": tmax}
    if not total:
        return out

    span = max(tmax - tmin, 1000)
    b = pick_bucket(span)
    start = (tmin // b) * b
    nb = int((tmax - start) // b) + 1
    total_series = [0] * nb
    cat_series = {c: [0] * nb for c in CATEGORIES}
    sources, levels, categories = {}, {}, {}
    categorized = 0
    for k, src, lvl, cat, n in con.execute(
            f"SELECT (t - ?) / ? AS k, source, level, category, count(*) FROM events WHERE {w} "
            f"GROUP BY k, source, level, category", [start, b] + p):
        k = int(k)
        if 0 <= k < nb:
            total_series[k] += n
        sources[src] = sources.get(src, 0) + n
        lv = lvl or "info"
        levels[lv] = levels.get(lv, 0) + n
        if cat is not None:
            categorized += n
            categories[cat] = categories.get(cat, 0) + n
            if cat in cat_series and 0 <= k < nb:
                cat_series[cat][k] += n
    out["categorized"] = categorized
    out["sources"] = [{"source": s_, "n": n} for s_, n in sorted(sources.items(), key=lambda x: -x[1])]
    out["n_processes"] = con.execute(f"SELECT count(DISTINCT process) FROM events WHERE {w}", p).fetchone()[0]
    out["hosts"] = [r[0] for r in con.execute(
        f"SELECT host, count(*) n FROM events WHERE host IS NOT NULL AND host != 'localhost' AND {w} "
        f"GROUP BY host ORDER BY n DESC LIMIT 5", p)]
    out["users"] = [r[0] for r in con.execute(
        f"SELECT user, count(*) n FROM events WHERE category='access' AND user IS NOT NULL AND user NOT IN ('<private>','0','-') AND user GLOB '*[^0-9]*' AND {w} "
        f"GROUP BY user ORDER BY n DESC LIMIT 8", p)]
    out["categories"] = categories
    out["levels"] = levels
    out["bucket"] = b
    out["hist_start"] = start
    out["hist_total"] = total_series
    out["hist_cat"] = cat_series

    out["top_processes"] = [dict(r) for r in con.execute(
        f"SELECT process AS name, count(*) n FROM events WHERE process IS NOT NULL AND {w} "
        f"GROUP BY process ORDER BY n DESC LIMIT 12", p)]
    flows = con.execute(
        f"SELECT source, category, action, count(*) n FROM events WHERE category IS NOT NULL AND {w} "
        f"GROUP BY source, category, action", p).fetchall()
    acts = {}
    for _s, c, a, n in flows:
        acts[(a, c)] = acts.get((a, c), 0) + n
    out["top_actions"] = [{"name": a, "category": c, "n": n}
                          for (a, c), n in sorted(acts.items(), key=lambda x: -x[1])[:12]]
    out["sankey"] = sankey_from(flows)
    out["findings"] = findings_summary(con, t0, t1)
    out["heatmap"] = heatmap(con, "access", t0, t1)
    return out


# Сводка за весь период хранится в самой базе сбора (таблица kv): на сборе из миллионов
# событий её расчёт занимает минуты, а открывать сбор хочется сразу. Считается в конце
# разбора; для сборов, разобранных раньше, — при первом открытии или фоном после запуска.
OV_KEY = "overview:1"
_ov_guard = threading.Lock()
_ov_locks: dict = {}


def _ov_lock(case_id):
    with _ov_guard:
        return _ov_locks.setdefault(case_id, threading.Lock())


def _ov_read(con):
    try:
        r = con.execute("SELECT v FROM kv WHERE k=?", (OV_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(r[0]) if r else None


def save_overview(con, ov):
    con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (OV_KEY, json.dumps(ov, ensure_ascii=False)))
    con.commit()


def drop_overview(con):
    try:
        con.execute("DELETE FROM kv WHERE k=?", (OV_KEY,))
        con.commit()
    except sqlite3.OperationalError:
        pass


def stored_overview(con, case_id, compute=True):
    ov = _ov_read(con)
    if ov is not None or not compute:
        return ov
    with _ov_lock(case_id):
        ov = _ov_read(con)          # пока ждали, мог посчитать другой запрос
        if ov is None:
            ov = overview(con)
            save_overview(con, ov)
    return ov


def sankey(con, w, p, per_cat=4):
    rows = con.execute(
        f"SELECT source, category, action, count(*) n FROM events WHERE category IS NOT NULL AND {w} "
        f"GROUP BY source, category, action", p).fetchall()
    return sankey_from(rows, per_cat)


def sankey_from(rows, per_cat=4):
    # оставляем топ действий в каждой категории
    by_cat = {}
    for s, c, a, n in rows:
        by_cat.setdefault(c, {}).setdefault(a, 0)
        by_cat[c][a] += n
    total = sum(n for d in by_cat.values() for n in d.values()) or 1
    keep = {c: {a for a in sorted(d, key=d.get, reverse=True)[:per_cat] if d[a] >= total * 0.01}
            for c, d in by_cat.items()}
    links = {}
    for s, c, a, n in rows:
        a2 = a if a in keep[c] else "Прочие действия"
        links[("s:" + (s or "?"), "c:" + c)] = links.get(("s:" + (s or "?"), "c:" + c), 0) + n
        links[("c:" + c, "a:" + a2)] = links.get(("c:" + c, "a:" + a2), 0) + n
    nodes = sorted({x for k in links for x in k})
    return {"nodes": nodes, "links": [{"source": a, "target": b, "value": v} for (a, b), v in links.items()]}


def heatmap(con, cat="access", t0=None, t1=None):
    w, p = range_clause(t0, t1)
    if cat == "all":
        cw = "1"
        cp = []
    elif cat == "classified":
        cw, cp = "category IS NOT NULL", []
    else:
        cw, cp = "category = ?", [cat]
    rows = con.execute(
        f"SELECT t / {DAY} AS d, (t / {HOUR}) % 24 AS h, count(*) FROM events WHERE {cw} AND {w} GROUP BY d, h",
        cp + p).fetchall()
    days = sorted({r[0] for r in rows})
    return {"cat": cat, "days": [d * DAY for d in days],
            "cells": [[days.index(d), h, n] for d, h, n in rows]}


# ---------------------------------------------------------------------------
LANE_ORDER = list(CATEGORIES)


def timeline(con, t0=None, t1=None, limit=2500, cats=None):
    w, p = range_clause(t0, t1)
    cw = "category IS NOT NULL"
    cp = []
    if cats:
        cw = f"category IN ({','.join('?' * len(cats))})"
        cp = list(cats)
    n = con.execute(f"SELECT count(*) FROM events WHERE {cw} AND {w}", cp + p).fetchone()[0]
    tmin, tmax, _ = bounds(con, t0, t1)
    out = {"count": n, "t_min": tmin, "t_max": tmax}
    if n == 0:
        out["mode"] = "points"
        out["points"] = []
        return out
    if n <= limit:
        out["mode"] = "points"
        out["points"] = [list(r) for r in con.execute(
            f"SELECT id, t, category, action, subject, process FROM events WHERE {cw} AND {w} ORDER BY t", cp + p)]
    else:
        span = max((t1 or tmax) - (t0 or tmin), 1000)
        b = pick_bucket(span, 220)
        out["mode"] = "buckets"
        out["bucket"] = b
        out["buckets"] = [list(r) for r in con.execute(
            f"SELECT (t / ?) * ? AS k, category, count(*), min(action) FROM events WHERE {cw} AND {w} "
            f"GROUP BY k, category ORDER BY k", [b, b] + cp + p)]
        # «редкие» действия всё равно показываем точками — они самые интересные
        out["points"] = [list(r) for r in con.execute(
            f"""SELECT id, t, category, action, subject, process FROM events
                WHERE {cw} AND {w} AND action IN (
                    SELECT action FROM events WHERE {cw} AND {w} GROUP BY action HAVING count(*) <= 40)
                ORDER BY t LIMIT 3000""", cp + p + cp + p)]
    return out


# ---------------------------------------------------------------------------
ENTITY_SPECS = {
    "apps": ("Приложения и процессы",
             "category='process' AND action IN ('Запуск приложения','Запуск процесса','Создание процесса',"
             "'Создание процесса (Sysmon)','Выполнение команды','Запуск сторонней службы','Задание cron',"
             "'PowerShell: запуск','Выполнение задачи','Запуск службы')"),
    "users": ("Пользователи и входы", "category='access'"),
    "remote": ("Удалённый доступ",
               "category='access' AND (action LIKE 'Вход по SSH%' OR action LIKE 'Удалённый доступ%' OR "
               "action LIKE 'RDP:%' OR action LIKE 'NoMachine%' OR action LIKE '%(RDP)%' OR action LIKE '%(сетевой%' "
               "OR action LIKE 'Входящее SSH%')"),
    "software": ("Установленное ПО", "category='software'"),
    "devices": ("Устройства и носители", "category='device'"),
    "network": ("Сеть", "category='network'"),
    "persistence": ("Закрепление и службы", "category='persistence' OR action IN ('Запуск сторонней службы',"
                    "'Установлена служба','Аварийное завершение службы')"),
    "security": ("Безопасность", "category='security'"),
    "system": ("Питание и система", "category='system'"),
}


def entities(con, kind, t0=None, t1=None, limit=300):
    title, where = ENTITY_SPECS[kind]
    w, p = range_clause(t0, t1)
    key = "coalesce(subject, user, process, '—')"
    if kind == "users":
        key = "coalesce(user, subject, '—')"
    rows = con.execute(
        f"SELECT {key} AS name, action, count(*) n, min(t) first, max(t) last, min(id) eid "
        f"FROM events WHERE ({where}) AND {w} GROUP BY name, action ORDER BY last DESC LIMIT ?",
        p + [limit * 3]).fetchall()
    agg = {}
    for r in rows:
        a = agg.setdefault(r["name"], {"name": r["name"], "n": 0, "first": r["first"], "last": r["last"], "actions": {},
                                       "eid": r["eid"]})
        a["n"] += r["n"]
        a["first"] = min(a["first"], r["first"])
        a["last"] = max(a["last"], r["last"])
        a["actions"][r["action"]] = a["actions"].get(r["action"], 0) + r["n"]
    items = sorted(agg.values(), key=lambda x: x["last"], reverse=True)[:limit]
    total = con.execute(f"SELECT count(*) FROM events WHERE ({where}) AND {w}", p).fetchone()[0]
    return {"kind": kind, "title": title, "total": total, "items": items}


def entity_counts(con, t0=None, t1=None):
    w, p = range_clause(t0, t1)
    out = {}
    for k, (title, where) in ENTITY_SPECS.items():
        out[k] = {"title": title, "n": con.execute(
            f"SELECT count(DISTINCT coalesce(subject, user, process)) FROM events WHERE ({where}) AND {w}", p).fetchone()[0]}
    return out


def processes(con, t0=None, t1=None, limit=400):
    w, p = range_clause(t0, t1)
    return [dict(r) for r in con.execute(
        f"SELECT process AS name, count(*) n, min(t) first, max(t) last, count(DISTINCT source) sources "
        f"FROM events WHERE process IS NOT NULL AND {w} GROUP BY process ORDER BY n DESC LIMIT ?", p + [limit])]


# ---------------------------------------------------------------------------
def findings_summary(con, t0=None, t1=None):
    w, p = range_clause(t0, t1, "e")
    rows = con.execute(
        f"SELECT f.rule, count(*) n, min(e.t) first, max(e.t) last, count(DISTINCT f.grp) groups "
        f"FROM findings f JOIN events e ON e.id = f.event_id WHERE {w} GROUP BY f.rule", p).fetchall()
    out = []
    for r in rows:
        meta = detections.meta(r["rule"])
        out.append({**meta, "n": r["n"], "first": r["first"], "last": r["last"], "groups": r["groups"]})
    out.sort(key=lambda x: (detections.SEV_ORDER.get(x["severity"], 9), -x["n"]))
    return out


def finding_detail(con, rule, t0=None, t1=None, limit=200):
    w, p = range_clause(t0, t1, "e")
    groups = [dict(r) for r in con.execute(
        f"SELECT coalesce(f.grp, e.action, '—') AS name, count(*) n, min(e.t) first, max(e.t) last "
        f"FROM findings f JOIN events e ON e.id=f.event_id WHERE f.rule=? AND {w} GROUP BY name ORDER BY n DESC LIMIT 50",
        [rule] + p)]
    events = [dict(r) for r in con.execute(
        f"SELECT e.id, e.t, e.source, e.process, e.user, e.level, e.category, e.action, e.subject, "
        f"substr(e.message, 1, 400) AS message FROM findings f JOIN events e ON e.id=f.event_id "
        f"WHERE f.rule=? AND {w} ORDER BY e.t DESC LIMIT ?", [rule] + p + [limit])]
    return {"rule": detections.meta(rule), "groups": groups, "events": events}


# ---------------------------------------------------------------------------
ROW_COLS = "id, t, source, host, process, pid, user, level, category, action, subject, code, substr(message, 1, 600) AS message"


def do_search(con, q, t0=None, t1=None, page=0, size=100, order="desc"):
    search.register(con)
    where, params, terms = search.parse(q)
    w, p = range_clause(t0, t1)
    con.execute("DROP TABLE IF EXISTS temp.hits")
    if where == "1" and not params:
        # пустой запрос — работаем прямо с таблицей событий (без копирования)
        if t0 is None and t1 is None:
            hits, hp = "events", []
        else:
            hits, hp = f"(SELECT id, t, process, source, category, level FROM events WHERE {w})", p
    else:
        con.execute(f"CREATE TEMP TABLE hits AS SELECT id, t, process, source, category, level FROM events "
                    f"WHERE {w} AND ({where})", p + params)
        hits, hp = "hits", []
    total = con.execute(f"SELECT count(*) FROM {hits}", hp).fetchone()[0]
    out = {"total": total, "terms": terms, "page": page, "size": size}
    if total:
        tmin, tmax = con.execute(f"SELECT min(t), max(t) FROM {hits}", hp).fetchone()
        span_from = t0 if t0 is not None else tmin
        span_to = t1 if t1 is not None else tmax
        b = pick_bucket(max(span_to - span_from, 1000), 120)
        start = (span_from // b) * b
        nb = int((span_to - start) // b) + 1
        hist = [0] * nb
        for k, n in con.execute(f"SELECT (t - ?) / ? AS k, count(*) FROM {hits} GROUP BY k", [start, b] + hp):
            if 0 <= k < nb:
                hist[int(k)] = n
        out.update({"bucket": b, "hist_start": start, "hist": hist, "t_min": tmin, "t_max": tmax})
        facets = {}
        for col in ("category", "source", "process", "level"):
            facets[col] = [[r[0], r[1]] for r in con.execute(
                f"SELECT {col}, count(*) n FROM {hits} GROUP BY {col} ORDER BY n DESC LIMIT 10", hp)]
        out["facets"] = facets
        od = "DESC" if order != "asc" else "ASC"
        out["rows"] = [dict(r) for r in con.execute(
            f"SELECT {ROW_COLS} FROM events WHERE id IN (SELECT id FROM {hits} ORDER BY t {od}, id {od} LIMIT ? OFFSET ?) "
            f"ORDER BY t {od}, id {od}", hp + [size, page * size])]
    else:
        out["rows"] = []
        out["facets"] = {}
    con.execute("DROP TABLE IF EXISTS temp.hits")
    return out


def search_iter(con, q, t0=None, t1=None, limit=1_000_000):
    search.register(con)
    where, params, _ = search.parse(q)
    w, p = range_clause(t0, t1)
    return con.execute(
        f"SELECT id, t, source, host, process, pid, user, level, category, action, subject, code, message FROM events "
        f"WHERE {w} AND ({where}) ORDER BY t LIMIT ?", p + params + [limit])


def event(con, eid):
    r = con.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["rules"] = [detections.meta(x[0]) for x in
                  con.execute("SELECT rule FROM findings WHERE event_id=?", (eid,))]
    return d


def context(con, eid, n=30, same_source=False):
    r = con.execute("SELECT t, source FROM events WHERE id=?", (eid,)).fetchone()
    if not r:
        return []
    t, src = r
    sw = " AND source = ?" if same_source else ""
    sp = [src] if same_source else []
    before = con.execute(
        f"SELECT {ROW_COLS} FROM events WHERE (t < ? OR (t = ? AND id < ?)){sw} ORDER BY t DESC, id DESC LIMIT ?",
        [t, t, eid] + sp + [n]).fetchall()
    after = con.execute(
        f"SELECT {ROW_COLS} FROM events WHERE (t > ? OR (t = ? AND id >= ?)){sw} ORDER BY t, id LIMIT ?",
        [t, t, eid] + sp + [n + 1]).fetchall()
    return [dict(x) for x in reversed(before)] + [dict(x) for x in after]


def ioc_scan(con, iocs, t0=None, t1=None):
    iocs = [i.strip() for i in iocs if i and i.strip()]
    iocs = list(dict.fromkeys(iocs))[:500]
    if not iocs:
        return []
    rx = re.compile("|".join(re.escape(i) for i in sorted(iocs, key=len, reverse=True)), re.I)
    low = {i.lower(): i for i in iocs}
    w, p = range_clause(t0, t1)
    res = {i: {"ioc": i, "n": 0, "first": None, "last": None, "sample": []} for i in iocs}
    cur = con.execute(f"SELECT id, t, coalesce(message,'') || ' ' || coalesce(subject,'') || ' ' || coalesce(process,'') "
                      f"FROM events WHERE {w}", p)
    for eid, t, text in cur:
        found = rx.findall(text)
        if not found:
            continue
        for f in set(x.lower() for x in found):
            key = low.get(f)
            if key is None:
                continue
            r = res[key]
            r["n"] += 1
            r["first"] = t if r["first"] is None else min(r["first"], t)
            r["last"] = t if r["last"] is None else max(r["last"], t)
            if len(r["sample"]) < 20:
                r["sample"].append(eid)
    return sorted(res.values(), key=lambda x: -x["n"])
