"""Статичная демо-копия панели для сайта: только просмотр, без Python на сервере.

Запусти панель в демо (python run.py --demo --port=8810), затем:
    python tools/build_static.py http://127.0.0.1:8810 output/site
Скрипт обходит GET-адреса панели, сохраняет ответы в data/*.json, скриншоты в files/,
и подкладывает demo-shim.js: он подменяет fetch на чтение этих файлов. Кнопки, которые
в рабочей панели отправляют решения в UNU, здесь ничего не отправляют.
Ключ UNU и база в копию не попадают — только то, что панель показывает на экране.
"""
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import requests

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8810"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "output/site")
# 3-й аргумент — свой текст плашки сверху (для копии на живых данных); без него — текст про демо
BANNER = sys.argv[3] if len(sys.argv) > 3 else ""
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
S = requests.Session()
S.headers["Host"] = "127.0.0.1"


def key(path: str) -> str:
    """Тот же ключ считает demo-shim.js: адрес без limit, всё кроме букв и цифр → _."""
    path = re.sub(r"[?&]limit=\d+", "", path).replace("/api/", "", 1)
    return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") + ".json"


def fnv(s: str) -> str:
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def get(path: str):
    r = S.get(BASE + path, timeout=60)
    r.raise_for_status()
    data = r.json()
    (OUT / "data" / key(path)).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data").mkdir(parents=True)
    (OUT / "files").mkdir()
    (OUT / "sheets").mkdir()

    summary = get("/api/summary")
    get("/api/positions/status")
    get("/api/promo")
    get("/api/actions")
    get("/api/services")
    get("/api/learning")
    get("/api/tasks/unassigned")      # состояние ключа в копию не пишем: на /api/key отвечает demo-shim.js
    projects = get("/api/projects")
    for p in projects:
        get(f"/api/projects/{p['id']}")
    for s in ("risk", "volume", "bad"):
        get(f"/api/workers?sort={s}")

    files = set()
    for v in ("manual", "rework", "reject", "approve"):
        for path in [f"/api/reports?verdict={v}&limit=40"] + \
                    [f"/api/reports?verdict={v}&limit=15&project_id={p['id']}" for p in projects]:
            for item in get(path)["items"]:
                files.update(item["files"])
                for f in item["reasons"]:
                    files.update((f.get("ref") or {}).get("files", []))
    for f in files:
        r = S.get(f"{BASE}/api/file?url={quote(f, safe='')}", timeout=60)
        if r.ok:
            (OUT / "files" / fnv(f)).write_bytes(r.content)
    for b in summary.get("agent_batches", []):
        r = S.get(f"{BASE}/api/agent/sheet", params={"batch": b["name"], "sheet": "sheet-01.png"}, timeout=60)
        if r.ok:
            (OUT / "sheets" / f"{b['name']}.png").write_bytes(r.content)

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    v = fnv((STATIC / "app.js").read_text(encoding="utf-8") + (STATIC / "style.css").read_text(encoding="utf-8"))
    html = html.replace('href="static/style.css"', f'href="style.css?v={v}"')   # ?v= — чтобы браузер не держал старую версию
    html = html.replace('<script src="static/app.js"></script>',
                        f'<script src="demo-shim.js?v={v}"></script>\n<script src="app.js?v={v}"></script>')
    if BANNER:
        html = html.replace('<script src="demo-shim.js', '<script>window.STATIC_BANNER=' + json.dumps(BANNER, ensure_ascii=False)
                            + ';window.STATIC_READONLY="Копия только для просмотра: в UNU ничего не отправляется.";</script>\n'
                            + '<script src="demo-shim.js', 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    for name in ("style.css", "app.js"):
        shutil.copy(STATIC / name, OUT / name)
    shutil.copytree(STATIC / "fonts", OUT / "fonts")
    shutil.copy(Path(__file__).with_name("demo-shim.js"), OUT / "demo-shim.js")
    n = sum(1 for _ in OUT.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"Готово: {OUT} · {n} файлов · {size / 1e6:.1f} МБ · скриншотов {len(files)}")


if __name__ == "__main__":
    main()
