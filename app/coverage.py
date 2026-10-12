"""Покрытие: насколько полно размечены события и насколько закрыта матрица MITRE ATT&CK.

Три вопроса, на которые отвечает страница «Покрытие»:
  1. Разметка. Сколько событий парсер не распознал (action пуст) и какие это процессы и журналы —
     готовый список того, что стоит добавить в парсер или закрыть своим правилом Sigma.
     События с распознанным действием, но без категории — не пробел: так помечен заведомый шум
     (служебные входы, учётные записи компьютеров), он намеренно убран из статистики по категориям.
  2. Матрица ATT&CK. Какие тактики и техники закрыты включёнными правилами (встроенными и Sigma),
     а какие пусты — и что из закрытого действительно срабатывало на сборах.
  3. Молчащие правила. Включённые правила, не давшие ни одной находки ни на одном сборе.

Считается по готовым сборам; результат по каждому сбору кэшируется в его базе (таблица kv).
"""
from __future__ import annotations

import json
import logging
import sqlite3

from . import db, detections, mitre, sigma

log = logging.getLogger("logatlas.coverage")

VERSION = 1                      # меняется вместе с составом кэша
TOP = 40                         # сколько нераспознанных групп показывать
SAMPLE = 160                     # длина примера сообщения


def _case_stats(cid: str) -> dict | None:
    """Разметка и сработавшие правила одного сбора (с кэшем в базе сбора)."""
    key = f"cov:{VERSION}"
    try:
        con = db.case_db(cid)
    except (FileNotFoundError, sqlite3.Error):
        return None
    try:
        try:
            r = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            if r:
                return json.loads(r[0])
        except sqlite3.Error:
            pass
        total = con.execute("SELECT count(*) FROM events").fetchone()[0]
        unlabelled = con.execute("SELECT count(*) FROM events WHERE action IS NULL").fetchone()[0]
        noise = con.execute("SELECT count(*) FROM events WHERE action IS NOT NULL AND category IS NULL").fetchone()[0]
        groups = [{"source": r[0] or "", "process": r[1] or "", "n": r[2], "sample": (r[3] or "")[:SAMPLE]}
                  for r in con.execute(
                      "SELECT source, process, count(*) n, min(message) FROM events WHERE action IS NULL "
                      "GROUP BY source, process ORDER BY n DESC LIMIT ?", (TOP,))]
        fired = {r[0]: r[1] for r in con.execute("SELECT rule, count(*) FROM findings GROUP BY rule")}
        out = {"total": total, "unlabelled": unlabelled, "noise": noise, "groups": groups, "fired": fired}
        try:
            con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
            con.execute("DELETE FROM kv WHERE k LIKE 'cov:%'")
            con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (key, json.dumps(out, ensure_ascii=False)))
            con.commit()
        except sqlite3.Error:
            pass                                  # кэш не обязателен, сбор мог открыться только на чтение
        return out
    finally:
        con.close()


def _rules() -> dict:
    """Все включённые правила: id -> {title, mitre, kind}."""
    out = {}
    for rid, r in detections.ALL.items():
        out[rid] = {"id": rid, "title": r["title"], "mitre": r["mitre"] or "", "kind": "builtin",
                    "severity": r["severity"]}
    try:
        for r in sigma.list_rules():
            if not r["enabled"] or r["error"]:
                continue
            out[sigma.PREFIX + r["id"]] = {"id": sigma.PREFIX + r["id"], "title": r["title"],
                                           "mitre": r["mitre"] or "", "kind": "sigma", "severity": r["level"]}
    except sqlite3.Error:
        pass
    return out


def _codes(s: str) -> list[str]:
    """Коды техник из поля mitre правила ('T1059.001', 'T1003.001, T1552')."""
    return [c.strip().upper() for c in (s or "").replace(";", ",").split(",") if c.strip().upper().startswith("T")]


def report() -> dict:
    """Сводка покрытия по всем готовым сборам."""
    with db.catalog() as c:
        cases = [{"id": r[0], "name": r[1], "platform": r[2], "events": r[3]}
                 for r in c.execute("SELECT id, name, platform, events FROM cases WHERE status='ready' "
                                    "ORDER BY created DESC")]

    total = unlabelled = noise = 0
    groups: dict[tuple, dict] = {}
    fired: dict[str, int] = {}
    per_case = []
    for case in cases:
        st = _case_stats(case["id"])
        if not st:
            continue
        total += st["total"]
        unlabelled += st["unlabelled"]
        noise += st["noise"]
        for g in st["groups"]:
            k = (g["source"], g["process"])
            cur = groups.setdefault(k, {"source": g["source"], "process": g["process"], "n": 0,
                                        "sample": g["sample"], "cases": 0})
            cur["n"] += g["n"]
            cur["cases"] += 1
            if not cur["sample"]:
                cur["sample"] = g["sample"]
        for rid, n in st["fired"].items():
            fired[rid] = fired.get(rid, 0) + n
        per_case.append({**case, "total": st["total"], "unlabelled": st["unlabelled"],
                         "noise": st["noise"], "rules": len(st["fired"])})

    top = sorted(groups.values(), key=lambda g: -g["n"])[:TOP]

    # --- матрица ATT&CK
    rules = _rules()
    tech_all = mitre.techniques()
    covered: dict[str, list] = {}                 # код техники -> правила
    unknown_codes: set[str] = set()
    stale: dict[str, dict] = {}                   # правила со ссылками на отозванные техники
    for rid, r in rules.items():
        for code in _codes(r["mitre"]):
            base = code if code in tech_all else code.split(".")[0]
            t = tech_all.get(base)
            if t is None:
                unknown_codes.add(code)
                continue
            if t["deprecated"]:
                # ATT&CK изредка переделывает тактики целиком и отзывает техники; правило с таким
                # кодом выпадает из матрицы, пока его не перепривязать к замене
                new = t.get("replaced_by") or ""
                s = stale.setdefault(code, {"code": code, "name": t["name"], "replaced_by": new,
                                            "replaced_name": (tech_all.get(new) or {}).get("name", ""),
                                            "rules": []})
                if len(s["rules"]) < 12:
                    s["rules"].append({"id": rid, "title": r["title"], "kind": r["kind"]})
                if new and new in tech_all and not tech_all[new]["deprecated"]:
                    base = new                    # считаем покрытие по замене, чтобы матрица не врала
                else:
                    continue
            covered.setdefault(base, []).append({"id": rid, "title": r["title"], "kind": r["kind"],
                                                 "fired": fired.get(rid, 0)})

    by_tactic = []
    seen_tech = set()
    for tac in mitre.tactics():
        items = []
        for tid, t in tech_all.items():
            if t["deprecated"] or tac["id"] not in t["tactics"]:
                continue
            seen_tech.add(tid)
            rs = covered.get(tid) or []
            if rs:
                items.append({"id": tid, "name": t["name"], "url": t["url"], "rules": len(rs),
                              "fired": sum(x["fired"] for x in rs),
                              "titles": [x["title"] for x in sorted(rs, key=lambda x: -x["fired"])[:6]]})
        total_tech = sum(1 for t in tech_all.values() if not t["deprecated"] and tac["id"] in t["tactics"])
        by_tactic.append({"id": tac["id"], "name": tac["name"], "en": tac["en"], "total": total_tech,
                          "covered": len(items), "fired": sum(1 for i in items if i["fired"]),
                          "techniques": sorted(items, key=lambda i: (-i["fired"], i["id"]))})

    live = [t for t in tech_all.values() if not t["deprecated"]]
    silent = sorted(
        ({"id": rid, "title": r["title"], "kind": r["kind"], "severity": r["severity"], "mitre": r["mitre"]}
         for rid, r in rules.items() if not fired.get(rid)),
        key=lambda r: (r["kind"], r["title"].lower()))

    return {
        "cases": per_case,
        "events": {"total": total, "unlabelled": unlabelled, "noise": noise, "groups": top},
        "mitre": {"meta": mitre.meta(), "update": mitre.UPDATE, "tactics": by_tactic,
                  "techniques_total": len(live), "techniques_covered": len([t for t in covered if t in tech_all]),
                  "parents_total": len({t["id"].split(".")[0] for t in live}),
                  "parents_covered": len({t.split(".")[0] for t in covered}),
                  "stale": sorted(stale.values(), key=lambda s: s["code"]),
                  "unknown_codes": sorted(unknown_codes)[:40]},
        "rules": {"total": len(rules), "builtin": sum(1 for r in rules.values() if r["kind"] == "builtin"),
                  "sigma": sum(1 for r in rules.values() if r["kind"] == "sigma"),
                  "fired": sum(1 for rid in rules if fired.get(rid)), "silent": silent[:400],
                  "silent_total": len(silent)},
    }
