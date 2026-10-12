#!/usr/bin/env python3
"""Пересобрать встроенный снимок справочника MITRE ATT&CK (app/mitre-attack.json).

    python3 scripts/make-mitre.py                     скачать свежий набор и записать снимок
    python3 scripts/make-mitre.py enterprise.json     собрать снимок из уже скачанного файла

Снимок нужен, чтобы страница «Покрытие» работала сразу после установки и без интернета.
В приложении его обновляет кнопка «Обновить справочник» (app/mitre.py).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import mitre, net  # noqa: E402

OUT = mitre.SNAPSHOT


def main() -> int:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as f:
            data = f.read()
        where = sys.argv[1]
    else:
        print(f"Загрузка {mitre.UPSTREAM}")
        data = net.download(mitre.UPSTREAM, mitre.MAX_DOWNLOAD,
                            lambda got, total: print(f"\r  {got / 1048576:.1f} МБ", end="", flush=True), timeout=120)
        print()
        where = mitre.UPSTREAM
    parsed = mitre.parse_bundle(data)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    size = os.path.getsize(OUT)
    print(f"{OUT}: ATT&CK {parsed['version']} от {parsed['released']}, "
          f"тактик {len(parsed['tactics'])}, техник {len(parsed['techniques'])}, {size // 1024} КБ")
    print(f"источник: {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
