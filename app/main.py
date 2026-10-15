"""LogAtlas — веб-анализатор журналов macOS, Windows и Linux."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import analytics, collect, compare, coverage, db, detections, ingest, maclocal, mitre, sigma, winlocal
from .parsers import SUPPORTED
from .parsers.base import CATEGORIES
from .i18n import CATEGORIES_EN, lang_of
from .timeutil import fmt_tz, ms_to_str

VERSION = "2.0.0"
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
log = logging.getLogger("logatlas")

# Как запущена панель: docker (образ), windows-app / windows-portable / windows-service (LogAtlas для Windows,
# переменную ставит windows/launcher.pyw или служба) или python (запуск из исходников).
RUNTIME = os.environ.get("LOGATLAS_RUNTIME") or ("docker" if os.path.exists("/.dockerenv") else "python")
LOG_HINT = {
    "docker": "журнал контейнера: docker compose logs logatlas",
    "windows-app": "журнал приложения: %LOCALAPPDATA%\\LogAtlas\\logatlas.log",
    "windows-portable": "журнал приложения: data\\logatlas.log рядом с LogAtlas.exe",
    "windows-service": "журнал службы: %ProgramData%\\LogAtlas\\logs",
    "macos-app": "журнал приложения: ~/Library/Application Support/LogAtlas/logatlas.log",
}.get(RUNTIME, "вывод uvicorn в консоли")


class NoCacheStatic(StaticFiles):
    """Статика с проверкой свежести при каждой загрузке страницы: после обновления LogAtlas
    браузер сразу берёт новые JS и CSS (неизменившиеся файлы отдаются ответом 304)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class J(JSONResponse):
    def render(self, content):
        try:
            import orjson
            return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)
        except ImportError:
            return json.dumps(content, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def _rng(req: Request):
    def g(k):
        v = req.query_params.get(k)
        return int(v) if v not in (None, "", "null") and re.fullmatch(r"-?\d+", v) else None
    return g("from"), g("to")


def _case_or_404(cid):
    c = db.get_case(cid)
    if not c:
        return None
    return c


def _human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return (f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1024


def _err(msg, code=400):
    return J({"error": msg}, status_code=code)


def _period(since_value, until_value):
    """Границы периода сбора в миллисекундах. Период задаётся только целиком и только разумный
    (1973 — 2100 годы): иначе возвращаем (None, None) — «весь архив»."""
    def ms(value):
        try:
            x = int(value)
        except (TypeError, ValueError):
            return None
        return x if 10 ** 11 < x < 4102444800000 else None

    since, until = ms(since_value), ms(until_value)
    if not since or not until or since >= until:
        return None, None
    return since, until


def _case_public(c, with_log=False):
    c = dict(c)
    raw_log = c.pop("log", None)
    if with_log:
        try:
            c["log"] = json.loads(raw_log) if raw_log else []
        except ValueError:
            c["log"] = []
    c["tz_label"] = fmt_tz(c.get("tz") or 0)
    try:
        c["summary"] = json.loads(c["summary"]) if c.get("summary") else None
    except ValueError:
        pass
    return c


# ---------------------------------------------------------------------------
def _icon(rel, media_type):
    """Значки и манифест по адресам, которые браузеры запрашивают сами (ярлыки, плитки, «На главный экран»)."""
    return FileResponse(os.path.join(STATIC, rel), media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


async def index(req):
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


def meta(req):
    lang = lang_of(req)
    cats = {k: CATEGORIES_EN.get(k, v) for k, v in CATEGORIES.items()} if lang == "en" else CATEGORIES
    rules = list(detections.ALL.values())
    if lang == "en":
        from .i18n import RULES_EN
        rules = [{**r, "title": RULES_EN[r["id"]][0], "desc": RULES_EN[r["id"]][1]} if r["id"] in RULES_EN else r
                 for r in rules]
    return J({"version": VERSION, "categories": cats, "supported": SUPPORTED,
              "rules": rules, "inbox": db.INBOX_DIR, "runtime": RUNTIME,
              "log_hint": LOG_HINT, "local_collect": _local_kind(),
              "host": (os.environ.get("COMPUTERNAME") or "") if winlocal.ENABLED
              else maclocal.host() if maclocal.ENABLED else ""})


def _local_kind():
    """Сбор журналов этого компьютера из приложения: windows, macos или выключен."""
    return "windows" if winlocal.ENABLED else "macos" if maclocal.ENABLED else False


def list_cases(req):
    plat = req.query_params.get("platform")
    with db.catalog() as c:
        if plat and plat != "all":
            rows = c.execute("SELECT * FROM cases WHERE platform=? ORDER BY created DESC", (plat,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM cases ORDER BY created DESC").fetchall()
    return J([_case_public(r) for r in rows])


async def create_case(req: Request):
    form = await req.form(max_files=5000, max_fields=50)
    files = [f for f in form.getlist("files") if hasattr(f, "filename") and f.filename]
    if not files:
        return _err("Не выбраны файлы")
    cid = form.get("case_id") or None
    name = (form.get("name") or "").strip()
    try:
        tz = int(form.get("tz") or 0)
    except ValueError:
        tz = 0
    tz_mode = form.get("tz_mode") or "auto"
    hint = form.get("platform") or None
    since, until = _period(form.get("since"), form.get("until"))
    if (form.get("since") or form.get("until")) and not since:
        return _err("Проверьте период: начало должно быть раньше конца")
    if cid:
        case = db.get_case(cid)
        if not case:
            return _err("Сбор не найден", 404)
        if case["status"] in ("processing", "queued"):
            return _err("Сбор ещё обрабатывается")
    else:
        cid = uuid.uuid4().hex[:10]
        if not name:
            name = files[0].filename if len(files) == 1 else f"{files[0].filename} и ещё {len(files) - 1}"
        with db.catalog() as c:
            c.execute("INSERT INTO cases(id,name,platform,tz,tz_mode,created,updated,status,progress,stage) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (cid, name[:200], hint, tz, tz_mode, time.time(), time.time(), "queued", 0, "В очереди"))
    updir = os.path.join(db.UPLOADS_DIR, cid)
    os.makedirs(updir, exist_ok=True)
    paths = []

    def save(f):
        safe = re.sub(r"[^\w.\-() ]+", "_", os.path.basename(f.filename))[:150] or "upload.bin"
        dst = os.path.join(updir, f"{int(time.time() * 1000)}_{safe}")
        with open(dst, "wb") as out:
            f.file.seek(0)
            shutil.copyfileobj(f.file, out, 1 << 20)
        return dst

    for f in files:
        paths.append(await run_in_threadpool(save, f))
        await f.close()
    total = sum(os.path.getsize(p) for p in paths)
    db.add_log(cid, "info", f"Загружено через браузер: {len(paths)} файл(ов), {_human(total)}")
    db.set_case(cid, status="queued", stage="В очереди", progress=0)
    ingest.worker().submit(cid, paths, tz, tz_mode, since, until)
    return J({"id": cid})


def get_case(req):
    cid = req.path_params["cid"]
    c = _case_or_404(cid)
    if not c:
        return _err("Сбор не найден", 404)
    with db.catalog() as cat:
        files = [dict(r) for r in cat.execute("SELECT * FROM files WHERE case_id=? ORDER BY id", (cid,))]
    out = _case_public(c, with_log=True)
    out["files"] = files
    return J(out)


async def patch_case(req: Request):
    cid = req.path_params["cid"]
    body = await req.json()
    kw = {k: body[k] for k in ("name", "notes") if k in body}
    if kw:
        db.set_case(cid, **kw)
    return J({"ok": True})


def delete_case(req):
    cid = req.path_params["cid"]
    c = db.get_case(cid)
    if not c:
        return _err("Сбор не найден", 404)
    # Удаление не отказывает никогда, даже если сбор прямо сейчас разбирается: разбор получает отметку
    # «отменён» и прекращается сам на ближайшем переходе между файлами, а остатки он уберёт за собой.
    # Иначе зависший разбор делал бы сбор неудаляемым навсегда.
    busy = bool(ingest.WORKER and ingest.WORKER.current == cid)
    ingest.cancel(cid)
    collect.cancel(cid)
    with db.catalog() as cat:
        cat.execute("DELETE FROM files WHERE case_id=?", (cid,))
        cat.execute("DELETE FROM cases WHERE id=?", (cid,))
    compare.clear_baseline(cid)
    if not busy:                       # занятый сбор дочистит за собой сам разбор, когда остановится
        ingest._forget(cid)
        db.drop_case_files(cid)
    return J({"ok": True, "busy": busy})


def _with_case(fn):
    def handler(req):
        cid = req.path_params["cid"]
        c = db.get_case(cid)
        if not c:
            return _err("Сбор не найден", 404)
        if not os.path.exists(db.case_path(cid)):
            return _err("Данные сбора ещё не готовы", 409)
        con = db.case_db(cid)
        try:
            return fn(req, con, c)
        finally:
            con.close()
    return handler


_OV_CACHE: dict = {}


@_with_case
def overview(req, con, case):
    t0, t1 = _rng(req)
    if t0 is None and t1 is None:
        return J(analytics.stored_overview(con, case["id"]))
    key = (case["id"], case["updated"], t0, t1)
    if key not in _OV_CACHE:
        if len(_OV_CACHE) > 64:
            _OV_CACHE.clear()
        _OV_CACHE[key] = analytics.overview(con, t0, t1)
    return J(_OV_CACHE[key])


@_with_case
def heatmap(req, con, case):
    t0, t1 = _rng(req)
    return J(analytics.heatmap(con, req.query_params.get("cat", "access"), t0, t1))


@_with_case
def timeline(req, con, case):
    t0, t1 = _rng(req)
    cats = [c for c in (req.query_params.get("cats") or "").split(",") if c in CATEGORIES]
    return J(analytics.timeline(con, t0, t1, cats=cats or None))


@_with_case
def entities(req, con, case):
    t0, t1 = _rng(req)
    kind = req.query_params.get("kind", "apps")
    if kind == "processes":
        return J({"kind": "processes", "title": "Процессы в журналах", "items": analytics.processes(con, t0, t1)})
    if kind not in analytics.ENTITY_SPECS:
        return _err("unknown kind")
    return J(analytics.entities(con, kind, t0, t1))


@_with_case
def entity_counts(req, con, case):
    t0, t1 = _rng(req)
    return J(analytics.entity_counts(con, t0, t1))


@_with_case
def findings(req, con, case):
    t0, t1 = _rng(req)
    return J(analytics.findings_summary(con, t0, t1, lang_of(req)))


@_with_case
def finding(req, con, case):
    t0, t1 = _rng(req)
    return J(analytics.finding_detail(con, req.path_params["rule"], t0, t1, lang=lang_of(req)))


@_with_case
def do_search(req, con, case):
    t0, t1 = _rng(req)
    q = req.query_params.get("q", "")
    page = int(req.query_params.get("page") or 0)
    order = req.query_params.get("order", "desc")
    t = time.time()
    try:
        res = analytics.do_search(con, q, t0, t1, page=max(page, 0), size=100, order=order, lang=lang_of(req))
    except Exception as e:  # noqa: BLE001
        return _err(f"Ошибка запроса: {e}")
    res["took_ms"] = int((time.time() - t) * 1000)
    return J(res)


def export_csv(req):
    cid = req.path_params["cid"]
    if not db.get_case(cid):
        return _err("Сбор не найден", 404)
    t0, t1 = _rng(req)
    q = req.query_params.get("q", "")

    def gen():
        con = db.case_db(cid)
        try:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time", "source", "host", "process", "pid", "user", "level", "category", "action", "subject",
                        "code", "message"])
            yield "﻿" + buf.getvalue()
            for r in analytics.search_iter(con, q, t0, t1):
                buf.seek(0)
                buf.truncate()
                w.writerow([ms_to_str(r[1])] + list(r[2:]))
                yield buf.getvalue()
        finally:
            con.close()

    fname = f"logatlas_{cid}.csv"
    return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@_with_case
def get_event(req, con, case):
    e = analytics.event(con, int(req.path_params["eid"]), lang_of(req))
    if not e:
        return _err("Событие не найдено", 404)
    if e.get("extra"):
        try:
            e["extra"] = json.loads(e["extra"])
        except ValueError:
            pass
    with db.catalog() as cat:
        f = cat.execute("SELECT path, upload FROM files WHERE id=?", (e["file_id"],)).fetchone()
    e["file"] = dict(f) if f else None
    return J(e)


@_with_case
def get_context(req, con, case):
    n = min(int(req.query_params.get("n") or 25), 200)
    same = req.query_params.get("same") == "1"
    return J(analytics.context(con, int(req.path_params["eid"]), n, same))


async def ioc(req: Request):
    cid = req.path_params["cid"]
    if not db.get_case(cid):
        return _err("Сбор не найден", 404)
    body = await req.json()
    iocs = body.get("iocs") or []
    if isinstance(iocs, str):
        iocs = re.split(r"[\s,;]+", iocs)
    t0, t1 = body.get("from"), body.get("to")

    def run():
        con = db.case_db(cid)
        try:
            return analytics.ioc_scan(con, iocs, t0, t1)
        finally:
            con.close()
    return J(await run_in_threadpool(run))


def inbox(req):
    items = []
    if os.path.isdir(db.INBOX_DIR):
        for root, _, names in os.walk(db.INBOX_DIR):
            for n in sorted(names):
                p = os.path.join(root, n)
                rel = os.path.relpath(p, db.INBOX_DIR)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                items.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
    return J({"dir": db.INBOX_DIR, "items": items[:2000]})


async def inbox_import(req: Request):
    body = await req.json()
    rels = body.get("files") or []
    paths = []
    base = os.path.realpath(db.INBOX_DIR)
    for r in rels:
        p = os.path.realpath(os.path.join(base, r))
        if p.startswith(base + os.sep) and os.path.isfile(p):
            paths.append(p)
    if not paths:
        return _err("Файлы не выбраны")
    tz = int(body.get("tz") or 0)
    tz_mode = body.get("tz_mode") or "auto"
    cid = body.get("case_id")
    if cid:
        if not db.get_case(cid):
            return _err("Сбор не найден", 404)
    else:
        cid = uuid.uuid4().hex[:10]
        name = body.get("name") or (os.path.basename(paths[0]) if len(paths) == 1 else f"Импорт из inbox ({len(paths)})")
        with db.catalog() as c:
            c.execute("INSERT INTO cases(id,name,tz,tz_mode,created,updated,status,progress,stage) VALUES (?,?,?,?,?,?,?,?,?)",
                      (cid, name, tz, tz_mode, time.time(), time.time(), "queued", 0, "В очереди"))
    db.add_log(cid, "info", f"Импорт из папки inbox: {len(paths)} файл(ов)")
    since, until = _period(body.get("since"), body.get("until"))
    ingest.worker().submit(cid, paths, tz, tz_mode, since, until)
    return J({"id": cid})


# ---------------------------------------------------------------------------
# сбор с хостов
def _panel_host(req: Request) -> str:
    host = (req.headers.get("host") or "").strip()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host if collect.RE_ADDR.fullmatch(host or "") else ""


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _lan_ip() -> str:
    """Адрес этого компьютера в локальной сети (UDP connect пакетов не отправляет)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""
    finally:
        s.close()


def _script_panel_host(req: Request) -> str:
    """Адрес панели для ограничения from= в скрипте подготовки. Панель открыта как 127.0.0.1 (LogAtlas для
    Windows или запуск из исходников) — хост увидит её под адресом в локальной сети, его и подставляем."""
    h = _panel_host(req)
    if h in LOOPBACK and RUNTIME != "docker":
        return _lan_ip() or h
    return h


def _local_only(req: Request) -> bool:
    """Панель слушает только 127.0.0.1: хосты не скачают скрипт по ссылке, его нужно перенести вручную."""
    return os.environ.get("LOGATLAS_BIND", "") in LOOPBACK and _panel_host(req) in LOOPBACK


def _base(req: Request) -> str:
    host = req.headers.get("host") or ""
    return f"{req.url.scheme}://{host}" if re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host) else ""


def collect_info(req):
    base = _base(req)
    return J({
        "available": collect.available(), "pubkey": collect.pubkey(), "user": collect.DEFAULT_USER,
        "unavailable": collect.NO_SSH,
        "panel_host": _script_panel_host(req), "base": base, "local_only": _local_only(req),
        "commands": {
            "unix": f"curl -fsSL {base}/setup/host.sh | sudo bash",
            "unix_remove": f"curl -fsSL {base}/setup/host.sh | sudo bash -s -- --remove",
            "windows_url": f"{base}/setup/windows.ps1",
            "windows": "powershell -ExecutionPolicy Bypass -File .\\logatlas-setup.ps1",
            "windows_remove": "powershell -ExecutionPolicy Bypass -File .\\logatlas-setup.ps1 -Remove",
        },
    })


def setup_script(req):
    kind = req.path_params["kind"]
    if kind not in ("host.sh", "windows.ps1"):
        return Response("not found", status_code=404)
    try:
        text = collect.setup_script("host" if kind == "host.sh" else "windows", _base(req), _script_panel_host(req))
    except (OSError, RuntimeError) as e:
        return Response(f"echo 'LogAtlas: {e}'\n", media_type="text/plain; charset=utf-8", status_code=503)
    headers = {"Cache-Control": "no-store"}
    if kind == "windows.ps1":
        headers["Content-Disposition"] = 'attachment; filename="logatlas-setup.ps1"'
    return Response(text, media_type="text/plain; charset=utf-8", headers=headers)


def hosts_list(req):
    return J(collect.list_hosts())


async def hosts_add(req: Request):
    try:
        return J(collect.add_host(await req.json()))
    except ValueError as e:
        return _err(str(e))


async def hosts_update(req: Request):
    try:
        return J(collect.update_host(req.path_params["hid"], await req.json()))
    except KeyError:
        return _err("Хост не найден", 404)
    except ValueError as e:
        return _err(str(e))


def hosts_delete(req):
    try:
        collect.delete_host(req.path_params["hid"])
    except KeyError:
        return _err("Хост не найден", 404)
    return J({"ok": True})


async def hosts_scan(req):
    try:
        return J(await run_in_threadpool(collect.scan, req.path_params["hid"]))
    except KeyError:
        return _err("Хост не найден", 404)
    except RuntimeError as e:
        return _err(str(e), 502)


def hosts_hostkey(req):
    try:
        collect.reset_hostkey(req.path_params["hid"])
    except KeyError:
        return _err("Хост не найден", 404)
    return J({"ok": True})


async def collect_start(req: Request):
    try:
        cid = collect.start(await req.json())
    except ValueError as e:
        return _err(str(e))
    return J({"id": cid})


# ---------------------------------------------------------------------------
# сравнение сборов и эталон хоста
async def compare_cases(req: Request):
    a, b = req.query_params.get("a", ""), req.query_params.get("b", "")
    if not a or not b or a == b:
        return _err("Выберите два разных сбора")
    try:
        return J(await run_in_threadpool(compare.compare, a, b))
    except KeyError:
        return _err("Сбор не найден", 404)
    except ValueError as e:
        return _err(str(e))


def compare_candidates(req):
    try:
        return J(compare.candidates(req.path_params["cid"]))
    except KeyError:
        return _err("Сбор не найден", 404)


async def baseline_get(req: Request):
    try:
        return J(await run_in_threadpool(compare.baseline_status, req.path_params["cid"],
                                         req.query_params.get("diff", "1") != "0"))
    except KeyError:
        return _err("Сбор не найден", 404)


def baseline_set(req):
    try:
        compare.set_baseline(req.path_params["cid"])
        return J(compare.baseline_status(req.path_params["cid"], with_diff=False))
    except KeyError:
        return _err("Сбор не найден", 404)
    except ValueError as e:
        return _err(str(e))


def baseline_clear(req):
    compare.clear_baseline(req.path_params["cid"])
    return J({"ok": True})


# ---------------------------------------------------------------------------
# правила Sigma
def sigma_list(req):
    rules = sigma.list_rules()
    return J({"rules": rules, "apply": sigma.APPLY, "yaml": sigma.yaml is not None,
              "feed": sigma.feed_info(), "update": sigma.UPDATE, "packages": sigma.packages(),
              "feed_enabled": sigma.FEED_ENABLED, "default_package": sigma.DEFAULT_PACKAGE, "upstream": sigma.UPSTREAM,
              "counts": {"total": len(rules), "enabled": sum(1 for r in rules if r["enabled"] and not r["error"]),
                         "unsupported": sum(1 for r in rules if r["error"])}})


async def sigma_import(req: Request):
    form = await req.form(max_files=2000, max_fields=20)
    files = []
    for f in form.getlist("files"):
        if not hasattr(f, "filename") or not f.filename:
            continue
        data = await f.read()
        await f.close()
        if len(data) > 200 << 20:
            return _err(f"{f.filename}: файл больше 200 МБ")
        files.append((f.filename, data))
    if not files:
        return _err("Выберите файлы правил .yml или архив .zip")
    try:
        rep = await run_in_threadpool(sigma.import_files, files)
    except RuntimeError as e:
        return _err(str(e))
    rep.pop("ids", None)
    return J(rep)


def sigma_get(req):
    r = sigma.get_rule(req.path_params["rid"])
    return J(r) if r else _err("Правило не найдено", 404)


async def sigma_patch(req: Request):
    body = await req.json()
    ids = [str(x) for x in body.get("ids") or []]
    return J({"n": sigma.set_enabled(ids, bool(body.get("enabled")))})


async def sigma_delete(req: Request):
    body = await req.json()
    ids = [str(x) for x in body.get("ids") or []]
    return J({"n": sigma.delete(ids)})


async def sigma_apply(req: Request):
    try:
        body = await req.json()
    except ValueError:
        body = {}
    try:
        return J(sigma.start_apply((body or {}).get("case_id")))
    except RuntimeError as e:
        return _err(str(e))


def sigma_apply_status(req):
    return J(sigma.APPLY)


async def sigma_update(req: Request):
    try:
        body = await req.json()
    except ValueError:
        body = {}
    body = body or {}
    try:
        return J(sigma.start_update(body.get("package"), bool(body.get("apply", True))))
    except RuntimeError as e:
        return _err(str(e))


def sigma_update_status(req):
    return J({**sigma.UPDATE, "feed": sigma.feed_info()})


# ---------------------------------------------------------------------------
# покрытие: разметка событий и матрица MITRE ATT&CK
async def coverage_report(req):
    return J(await run_in_threadpool(coverage.report))


async def mitre_update(req: Request):
    try:
        return J(mitre.start_update())
    except RuntimeError as e:
        return _err(str(e))


def mitre_update_status(req):
    return J({**mitre.UPDATE, "meta": mitre.meta()})


def _is_local(req: Request) -> bool:
    return (req.client.host if req.client else "") in ("127.0.0.1", "::1")


def windows_local_logs():
    """Вернуть список доступных журналов Windows с метаданными.
    Запускает wevtutil enum-logs и enum-channel-list, возвращает JSON."""
    if not winlocal.ENABLED:
        return _err("Доступно только в приложении LogAtlas для Windows", 404)

    # Дефолтные журналы с описаниями (как в GUI)
    log_defs = {
        'Security':              {'label': 'Security',      'desc': 'Входы в систему, учётные записи, аудит'},
        'System':                {'label': 'System',        'desc': 'Службы, драйверы, перезагрузки'},
        'Application':           {'label': 'Application',   'desc': 'Ошибки и сообщения программ'},
        'Microsoft-Windows-Sysmon/Operational':      {'label': 'Sysmon',         'desc': 'Процессы, сеть, файлы'},
        'Microsoft-Windows-PowerShell/Operational':  {'label': 'PowerShell',     'desc': 'Выполненные скрипты'},
        'Microsoft-Windows-Windows Defender/Operational': {'label': 'Defender',    'desc': 'Угрозы и настройки защиты'},
        'Microsoft-Windows-TaskScheduler/Operational': {'label': 'Планировщик',  'desc': 'Задания и их запуск'},
        'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational': {'label': 'RDP: сеансы', 'desc': 'Входы и отключения'},
        'Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational': {'label': 'RDP: подключения', 'desc': 'Попытки соединения'},
    }

    logs = []
    try:
        import subprocess
        # Получаем список журналов через wevtutil
        result = subprocess.run(
            ['wevtutil.exe', 'enum-logs'],
            capture_output=True, text=True, timeout=30,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Формат: name: <имя>
                if line.startswith('name:'):
                    name = line[5:].strip()
                    if name in log_defs:
                        info = log_defs[name]
                        # Получаем количество записей и размер
                        try:
                            info_result = subprocess.run(
                                ['wevtutil.exe', 'gli', name],
                                capture_output=True, text=True, timeout=10,
                                creationflags=0x08000000
                            )
                            if info_result.returncode == 0:
                                for il in info_result.stdout.splitlines():
                                    if 'numberOfLogRecords:' in il:
                                        try: info['records'] = int(il.split(':')[1].strip())
                                        except: info['records'] = 0
                                    elif 'fileSize:' in il:
                                        try: info['size'] = int(il.split(':')[1].strip())
                                        except: info['size'] = 0
                        except Exception:  # noqa: BLE001
                            pass
                        info['exists'] = True
                        logs.append(info)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("cannot enumerate Windows logs: %s", e)

    # Добавляем журналы из channel-list (дополнительные)
    try:
        result = subprocess.run(
            ['wevtutil.exe', 'enum-channels'],
            capture_output=True, text=True, timeout=30,
            creationflags=0x08000000
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith('name:'):
                    name = line[5:].strip()
                    if name not in log_defs and name not in [l.get('name') for l in logs]:
                        logs.append({
                            'name': name,
                            'label': name.split('/')[-1],
                            'desc': '',
                            'exists': True,
                            'records': None,
                            'size': None,
                        })
    except Exception:  # noqa: BLE001
        pass

    return J(logs)


async def local_collect(req: Request):
    if not winlocal.ENABLED and not maclocal.ENABLED:
        return _err("Доступно только в приложении LogAtlas для Windows и macOS", 404)
    if not _is_local(req):
        return _err("Сбор журналов этого компьютера запускается только с него самого", 403)
    try:
        body = await req.json()
    except ValueError:
        body = {}
    body = body or {}
    try:
        if winlocal.ENABLED:
            logs = body.get("logs", [])
            since, until = _period(body.get("since"), body.get("until"))
            jid = await run_in_threadpool(winlocal.start_headless, _base(req), logs, since, until)
        else:
            since, until = _period(body.get("since"), body.get("until"))
            jid = maclocal.start(body.get("days", 3),
                                 since // 1000 if since else None,
                                 until // 1000 if until else None)
    except RuntimeError as e:
        return _err(str(e))
    j = winlocal.status(jid) or maclocal.status(jid) or {}
    return J({"id": jid, "case": j.get("case")})


def local_collect_status(req):
    j = winlocal.status(req.path_params["jid"]) or maclocal.status(req.path_params["jid"])
    return J(j) if j else _err("Задание не найдено", 404)


def status(req):
    """Занята ли панель: окно LogAtlas для Windows спрашивает перед закрытием, если идёт разбор или сбор."""
    w = ingest.WORKER
    cur = w.current if w else None
    queued = w.q.qsize() if w else 0
    collecting = sum(1 for p in list(collect.JOBS.values()) if p.poll() is None)
    local = sum(1 for j in list(winlocal.JOBS.values()) + list(maclocal.JOBS.values())
                if j["state"] in ("starting", "waiting", "collecting"))
    case = (db.get_case(cur) or {}).get("name", "") if cur else ""
    return J({"busy": bool(cur or queued or collecting or local), "case": case, "queued": queued,
              "collecting": collecting, "local": local})


def health(req):
    return Response("ok")


routes = [
    Route("/", index),
    Route("/healthz", health),
    Route("/api/status", status),
    Route("/api/local/windows/logs", windows_local_logs, methods=["GET"]),
    Route("/favicon.ico", lambda r: _icon("icons/favicon.ico", "image/x-icon")),
    Route("/apple-touch-icon.png", lambda r: _icon("icons/apple-touch-icon.png", "image/png")),
    Route("/apple-touch-icon-precomposed.png", lambda r: _icon("icons/apple-touch-icon.png", "image/png")),
    Route("/manifest.webmanifest", lambda r: _icon("manifest.webmanifest", "application/manifest+json")),
    Route("/api/meta", meta),
    Route("/api/cases", list_cases, methods=["GET"]),
    Route("/api/cases", create_case, methods=["POST"]),
    Route("/api/cases/{cid}", get_case, methods=["GET"]),
    Route("/api/cases/{cid}", patch_case, methods=["PATCH"]),
    Route("/api/cases/{cid}", delete_case, methods=["DELETE"]),
    Route("/api/cases/{cid}/overview", overview),
    Route("/api/cases/{cid}/heatmap", heatmap),
    Route("/api/cases/{cid}/timeline", timeline),
    Route("/api/cases/{cid}/entities", entities),
    Route("/api/cases/{cid}/entity-counts", entity_counts),
    Route("/api/cases/{cid}/findings", findings),
    Route("/api/cases/{cid}/findings/{rule}", finding),
    Route("/api/cases/{cid}/search", do_search),
    Route("/api/cases/{cid}/export.csv", export_csv),
    Route("/api/cases/{cid}/events/{eid:int}", get_event),
    Route("/api/cases/{cid}/events/{eid:int}/context", get_context),
    Route("/api/cases/{cid}/ioc", ioc, methods=["POST"]),
    Route("/api/sigma", sigma_list, methods=["GET"]),
    Route("/api/sigma", sigma_import, methods=["POST"]),
    Route("/api/sigma", sigma_patch, methods=["PATCH"]),
    Route("/api/sigma", sigma_delete, methods=["DELETE"]),
    Route("/api/sigma/apply", sigma_apply, methods=["POST"]),
    Route("/api/sigma/apply", sigma_apply_status, methods=["GET"]),
    Route("/api/sigma/update", sigma_update, methods=["POST"]),
    Route("/api/sigma/update", sigma_update_status, methods=["GET"]),
    Route("/api/coverage", coverage_report),
    Route("/api/mitre/update", mitre_update, methods=["POST"]),
    Route("/api/mitre/update", mitre_update_status, methods=["GET"]),
    Route("/api/sigma/rules/{rid}", sigma_get),
    Route("/api/compare", compare_cases),
    Route("/api/cases/{cid}/compare-candidates", compare_candidates),
    Route("/api/cases/{cid}/baseline", baseline_get, methods=["GET"]),
    Route("/api/cases/{cid}/baseline", baseline_set, methods=["PUT"]),
    Route("/api/cases/{cid}/baseline", baseline_clear, methods=["DELETE"]),
    Route("/api/inbox", inbox),
    Route("/api/inbox/import", inbox_import, methods=["POST"]),
    Route("/api/local/collect", local_collect, methods=["POST"]),
    Route("/api/local/collect/{jid}", local_collect_status),
    Route("/api/collect/info", collect_info),
    Route("/api/collect", collect_start, methods=["POST"]),
    Route("/api/hosts", hosts_list, methods=["GET"]),
    Route("/api/hosts", hosts_add, methods=["POST"]),
    Route("/api/hosts/{hid}", hosts_update, methods=["PUT"]),
    Route("/api/hosts/{hid}", hosts_delete, methods=["DELETE"]),
    Route("/api/hosts/{hid}/scan", hosts_scan, methods=["POST"]),
    Route("/api/hosts/{hid}/hostkey", hosts_hostkey, methods=["DELETE"]),
    Route("/setup/{kind}", setup_script),
    Mount("/static", NoCacheStatic(directory=STATIC), name="static"),
]


def _warm_overviews():
    """Фоном готовит сохранённую сводку для сборов, разобранных до её появления."""
    lg = logging.getLogger("logatlas")
    time.sleep(5)
    with db.catalog() as cat:
        ready = [(r[0], r[1]) for r in cat.execute(
            "SELECT id, events FROM cases WHERE status='ready' AND events > 0 ORDER BY created DESC")]
    for cid, events in ready:
        c = {"id": cid, "events": events}
        try:
            con = db.case_db(c["id"])
        except FileNotFoundError:
            continue
        try:
            if analytics.stored_overview(con, c["id"], compute=False) is None:
                t = time.time()
                analytics.stored_overview(con, c["id"])
                lg.info("overview for case %s (%s events) prepared in %.0fs", c["id"], c["events"], time.time() - t)
        except Exception:  # noqa: BLE001
            lg.exception("cannot prepare overview for case %s", c["id"])
        finally:
            con.close()


def create_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    stale = db.recover_stale_cases()
    if stale:
        logging.getLogger("logatlas").warning("прерванных сборов после перезапуска: %d", len(stale))
    compare.init()
    sigma.init()
    mitre.init()
    if winlocal.ENABLED:
        winlocal.cleanup()
    if maclocal.ENABLED:
        maclocal.cleanup()
    ingest.worker()
    threading.Thread(target=_warm_overviews, daemon=True, name="overview-warm").start()
    try:
        collect.ensure_key()
    except Exception:  # noqa: BLE001
        logging.getLogger("logatlas").exception("cannot create panel key")
    return Starlette(routes=routes, middleware=[Middleware(GZipMiddleware, minimum_size=2048)])


app = create_app()
