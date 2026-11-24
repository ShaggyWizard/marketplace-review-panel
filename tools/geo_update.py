"""Собрать таблицу «диапазон IPv4 -> страна» из открытой статистики регистраторов (NRO).

    python tools/geo_update.py

Источник — официальный сводный файл пяти регистраторов (RIPE, AFRINIC, APNIC, ARIN, LACNIC):
кому и в какой стране выдан блок адресов. Это страна регистрации блока, а не точное место
человека, но «Египет или Россия» различает надёжно. Результат: data/ipcountry.csv
(начало диапазона числом, конец, код страны) — его читает app/geo.py. Обновлять раз в месяц.
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config  # noqa: E402

URL = "https://ftp.ripe.net/pub/stats/ripencc/nro-stats/latest/nro-delegated-stats"


def main():
    out = config.ROOT / "data" / "ipcountry.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with requests.get(URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            p = (line or b"").decode("ascii", "ignore").split("|")
            if len(p) < 7 or p[2] != "ipv4" or p[6] not in ("assigned", "allocated") or len(p[1]) != 2:
                continue
            a = p[3].split(".")
            start = (int(a[0]) << 24) | (int(a[1]) << 16) | (int(a[2]) << 8) | int(a[3])
            rows.append((start, start + int(p[4]) - 1, p[1]))
    rows.sort()
    merged = []
    for s, e, cc in rows:                      # соседние блоки одной страны склеиваем
        if merged and merged[-1][2] == cc and merged[-1][1] + 1 == s:
            merged[-1][1] = e
        else:
            merged.append([s, e, cc])
    tmp = out.with_suffix(".tmp")              # пишем рядом и подменяем разом: панель не прочтёт половину файла
    tmp.write_text("\n".join(f"{s},{e},{cc}" for s, e, cc in merged), encoding="ascii")
    tmp.replace(out)
    print(f"Готово: {out} — {len(merged)} диапазонов")


if __name__ == "__main__":
    main()
