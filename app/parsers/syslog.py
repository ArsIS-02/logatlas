"""BSD syslog: system.log, install.log (macOS), secure / auth.log / messages (Linux)."""
from __future__ import annotations

import re
from collections import Counter

from ..timeutil import MONTHS, infer_year, iso_match_to_ms, naive_ms, ISO_RE
from .base import Parser, ev, first_lines
from .classify import SYSLOG, MAC_PROCS, LINUX_PROCS

PROC = r"(?P<proc>[^\[:\s][^\[:]*?)(?:\[(?P<pid>\d+)\])?(?: \([^)]*\))?"
CLASSIC = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2}) {1,2}(?P<d>\d{1,2}) (?P<h>\d\d):(?P<mi>\d\d):(?P<s>\d\d)(?:\.(?P<f>\d+))? "
    r"(?P<host>\S+) " + PROC + r": (?P<msg>.*)$")
ISO = re.compile(
    r"^(?P<iso>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d(?::?\d\d)?)?) "
    r"(?P<host>\S+) " + PROC + r": (?P<msg>.*)$")
REPEATED = re.compile(r"^[A-Z][a-z]{2} {1,2}\d{1,2} \d\d:\d\d:\d\d --- last message repeated")


class SyslogParser(Parser):
    name = "syslog"
    label = "syslog"
    platform = "any"
    splittable = True       # построчный формат: большой файл режется на части и разбирается параллельно

    @classmethod
    def sniff(cls, base, head):
        lines = [l for l in first_lines(head, 40) if l.strip()]
        if not lines:
            return 0
        hits = sum(1 for l in lines if CLASSIC.match(l) or ISO.match(l))
        if hits >= max(2, len(lines) // 3):
            return 60
        return 0

    def parse(self, src, ctx):
        anchor = ctx.anchor
        cur = None          # [t, z, host, proc, pid, msg]
        procs = Counter()
        year_cache = {}
        clf = SYSLOG.match

        def flush(c):
            t, z, host, proc, pid, msg = c
            r = clf(proc, msg)
            if r == "noise":
                ctx.noise += 1
                return None
            cat = action = subj = user = None
            level = "info"
            if r:
                cat, action, subj, user, lv = r
                if lv:
                    level = lv
            return ev(t, z, host, None, proc, pid, user, level, cat, action, subj, None, msg)

        for line in src.text_lines():
            if not line:
                continue
            m = CLASSIC.match(line)
            if m:
                mon = MONTHS.get(m.group("mon").lower())
                d = int(m.group("d"))
                key = (mon, d)
                y = year_cache.get(key)
                if y is None:
                    y = year_cache[key] = infer_year(mon, d, anchor)
                f = m.group("f")
                t = naive_ms(y, mon, d, int(m.group("h")), int(m.group("mi")), int(m.group("s")),
                             int((f + "000")[:3]) if f else 0)
                z = 0
            else:
                m = ISO.match(line)
                if m:
                    r = iso_match_to_ms(ISO_RE.match(m.group("iso")))
                    if not r:
                        m = None
                    else:
                        t, off = r
                        if off is None:
                            z = 0
                        else:
                            t -= off * 60000
                            z = 1
                            ctx.tz_votes[off] += 1
            if m:
                if cur:
                    e = flush(cur)
                    if e:
                        yield e
                host = m.group("host")
                proc = m.group("proc").strip()
                pid = m.group("pid")
                procs[proc] += 1
                if host not in ("localhost", "-"):
                    ctx.host_votes[host] += 1
                cur = [t, z, host, proc, int(pid) if pid else None, m.group("msg")]
            elif REPEATED.match(line):
                ctx.noise += 1
            elif cur:
                if len(cur[5]) < 4000:
                    cur[5] += "\n" + line
            else:
                ctx.bad += 1
        if cur:
            e = flush(cur)
            if e:
                yield e
        mac = sum(v for k, v in procs.items() if k in MAC_PROCS)
        lin = sum(v for k, v in procs.items() if k in LINUX_PROCS)
        ctx.platform = "macos" if mac > lin else ("linux" if lin else "any")
        ctx.label = src.base
