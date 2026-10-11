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

from . import analytics, collect, db, detections, ingest
from .parsers import SUPPORTED
from .parsers.base import CATEGORIES
from .timeutil import fmt_tz, ms_to_str

VERSION = "1.2.2"
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
log = logging.getLogger("logatlas")


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
    return J({"version": VERSION, "categories": CATEGORIES, "supported": SUPPORTED,
              "rules": list(detections.ALL.values()), "inbox": db.INBOX_DIR})


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
    ingest.worker().submit(cid, paths, tz, tz_mode)
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
    if ingest.WORKER and ingest.WORKER.current == cid:
        return _err("Сбор сейчас разбирается — удалите после завершения")
    collect.cancel(cid)
    with db.catalog() as cat:
        cat.execute("DELETE FROM files WHERE case_id=?", (cid,))
        cat.execute("DELETE FROM cases WHERE id=?", (cid,))
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db.case_path(cid) + suffix)
        except OSError:
            pass
    shutil.rmtree(os.path.join(db.UPLOADS_DIR, cid), ignore_errors=True)
    return J({"ok": True})


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
    return J(analytics.findings_summary(con, t0, t1))


@_with_case
def finding(req, con, case):
    t0, t1 = _rng(req)
    return J(analytics.finding_detail(con, req.path_params["rule"], t0, t1))


@_with_case
def do_search(req, con, case):
    t0, t1 = _rng(req)
    q = req.query_params.get("q", "")
    page = int(req.query_params.get("page") or 0)
    order = req.query_params.get("order", "desc")
    t = time.time()
    try:
        res = analytics.do_search(con, q, t0, t1, page=max(page, 0), size=100, order=order)
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
    e = analytics.event(con, int(req.path_params["eid"]))
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
    ingest.worker().submit(cid, paths, tz, tz_mode)
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


def _base(req: Request) -> str:
    host = req.headers.get("host") or ""
    return f"{req.url.scheme}://{host}" if re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host) else ""


def collect_info(req):
    base = _base(req)
    return J({
        "available": collect.available(), "pubkey": collect.pubkey(), "user": collect.DEFAULT_USER,
        "panel_host": _panel_host(req), "base": base,
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
        text = collect.setup_script("host" if kind == "host.sh" else "windows", _base(req), _panel_host(req))
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


def health(req):
    return Response("ok")


routes = [
    Route("/", index),
    Route("/healthz", health),
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
    Route("/api/inbox", inbox),
    Route("/api/inbox/import", inbox_import, methods=["POST"]),
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
    ingest.worker()
    threading.Thread(target=_warm_overviews, daemon=True, name="overview-warm").start()
    try:
        collect.ensure_key()
    except Exception:  # noqa: BLE001
        logging.getLogger("logatlas").exception("cannot create panel key")
    return Starlette(routes=routes, middleware=[Middleware(GZipMiddleware, minimum_size=2048)])


app = create_app()
