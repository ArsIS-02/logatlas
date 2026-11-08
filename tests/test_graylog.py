"""Находки в Graylog по GELF. Запуск: python3 tests/test_graylog.py"""
import json, os, socket, sys, tempfile, time, zlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LOGATLAS_DATA"] = tempfile.mkdtemp(prefix="la-gl-")
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", 0))
os.environ["LOGATLAS_GELF_HOST"] = "127.0.0.1"
os.environ["LOGATLAS_GELF_PORT"] = str(sock.getsockname()[1])
from app import db, gelf, main  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

res = []
def ok(c, m):
    print(("PASS " if c else "FAIL ") + m); res.append(bool(c))

def recv(wait=1.0):
    sock.settimeout(wait)
    out = []
    while True:
        try:
            out.append(json.loads(zlib.decompress(sock.recvfrom(65535)[0])))
        except socket.timeout:
            return out

db.ensure_dirs(); db.init()
cid, t = "c-test", int(time.time()) - 30 * 86400            # архив месячной давности
with db.catalog() as c:
    c.execute("INSERT INTO cases(id,name,platform,host,created,updated,status) VALUES(?,?,?,?,?,?,?)",
              (cid, "Сбор srv", "linux", "srv-01", time.time(), time.time(), "ready"))
con = db.case_db(cid, write=True)
for i in range(3):
    con.execute("INSERT INTO events(id,t,host,source,category,action,subject,message) VALUES(?,?,?,?,?,?,?,?)",
                (i + 1, t + i, "srv-01", "auth.log", "access", "Неудачный вход", "root", "fail %d" % i))
rule = next(iter(main.detections.ALL))
sev = main.detections.ALL[rule]["severity"]
for i in range(3):
    con.execute("INSERT INTO findings(rule,event_id,grp) VALUES(?,?,?)", (rule, i + 1, "root"))
con.execute("INSERT INTO findings(rule,event_id,grp) VALUES(?,?,?)", ("sigma:x-test", 1, "g1"))
con.commit(); con.close()

c = TestClient(main.app)
g = c.get("/api/graylog").json()
ok(g["enabled"] and g["target"].startswith("127.0.0.1:"), "статус: отправка включена")
before = time.time()
r = c.post("/api/cases/%s/graylog" % cid, json={"min_severity": "info"})
ok(r.status_code == 200 and r.json()["sent"] == 2, "отправлено по сообщению на правило: %s" % r.text)
msgs = {m["_la_rule"]: m for m in recv()}
m = msgs.get(rule, {})
ok(m.get("_la_case") == cid and m.get("_la_host") == "srv-01" and m.get("_la_count") == 3, "сбор, хост и число событий")
ok(m.get("_la_severity") == sev and m.get("level") == gelf.LEVEL[sev], "важность и уровень")
ok(m.get("timestamp", 0) >= before - 1 and m.get("_la_first", "").startswith(time.strftime("%Y-%m-%d", time.gmtime(t))),
   "время сообщения — момент отправки, время событий — в la_first")
ok(m.get("_la_top") == "root" and m.get("_la_source") == "logatlas" and m.get("_homelab_app") == "logatlas", "группы и поля потока")
ok(msgs.get("sigma:x-test", {}).get("_la_sigma") == "true", "правило Sigma помечено la_sigma")
r = c.post("/api/cases/%s/graylog" % cid, json={"min_severity": "critical"})
ok(r.json()["sent"] == (1 if sev == "critical" else 0), "порог важности отсекает находки ниже")
recv(0.2)
ok(c.post("/api/cases/nope/graylog", json={}).status_code == 404, "нет сбора — 404")
ok(c.get("/api/sigma/rules/sigma:nope/yaml").status_code == 404, "нет правила Sigma — 404")
gelf.SENDER = None
ok(c.post("/api/cases/%s/graylog" % cid, json={}).status_code == 400 and c.get("/api/graylog").json() == {"enabled": False},
   "без LOGATLAS_GELF_HOST отправка выключена")
print("\nИТОГО: %d/%d" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
