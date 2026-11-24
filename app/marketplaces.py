"""Позиции товара в выдаче маркетплейсов. Четыре источника, выбор — POSITIONS_SOURCE:

  mpstats    — сервис аналитики MPStats по API (нужен MPSTATS_TOKEN). Позиции по ключам
               для WB и Ozon без борьбы с защитой маркетплейса. Формат API записан по памяти:
               документация закрыта логином — сверить с первым живым ответом.
  agent      — агент (Claude в Chrome) открывает выдачу как обычный покупатель и находит
               место артикула. Панель готовит задание data/positions/job-*/job.json,
               агент пишет results.json, панель забирает сама. Проверено 23.11 на WB.
  wb_public  — прямой запрос к поиску WB. С нашего ПК отвечает 403; оставлен на случай сервера,
               где он открыт. Защиту не обходим.
  demo       — сгенерированная динамика для демо.

Moneyplace открытого API не имеет (проверка позиций — в кабинете и расширении браузера), поэтому
не подключён. Новый источник = функция position(keyword, sku, marketplace) + строка в SOURCES.
"""
import json
import math
import os
import random
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests

from . import config, db

MPSTATS_TOKEN = os.getenv("MPSTATS_TOKEN", "")
MPSTATS_URL = os.getenv("MPSTATS_URL", "https://mpstats.io/api")
SOURCE = os.getenv("POSITIONS_SOURCE", "demo" if config.DEMO else ("mpstats" if MPSTATS_TOKEN else "agent"))
POS_DIR = config.DB_PATH.parent / "positions"
WB_URL = "https://search.wb.ru/exactmatch/ru/common/v18/search"


# ---------- MPStats ----------
def mpstats_position(keyword: str, sku: str, marketplace: str) -> int | None:
    """Позиция артикула по ключу за последний день из MPStats.
    По памяти: GET /api/{wb|oz}/get/item/{sku}/by_keywords?d1=&d2=, заголовок X-Mpstats-TOKEN,
    ответ {"days": [...], "words": {"ключ": {"pos": [по дням], ...}}}. Разбор терпимый к вариантам."""
    if not MPSTATS_TOKEN:
        raise RuntimeError("Нет MPSTATS_TOKEN")
    mp = {"wb": "wb", "ozon": "oz"}.get(marketplace)
    if not mp:
        raise NotImplementedError(f"MPStats не отдаёт позиции для {marketplace}")
    d2 = date.today()
    r = requests.get(f"{MPSTATS_URL}/{mp}/get/item/{sku}/by_keywords",
                     params={"d1": (d2 - timedelta(days=7)).isoformat(), "d2": d2.isoformat()},
                     headers={"X-Mpstats-TOKEN": MPSTATS_TOKEN, "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    words = data.get("words") if isinstance(data, dict) else None
    if not isinstance(words, dict):
        raise ValueError(f"Неожиданный ответ MPStats: {str(data)[:200]}")
    w = words.get(keyword) or next((v for k, v in words.items() if k.lower().strip() == keyword.lower().strip()), None)
    if not w:
        return None                                    # ключа нет среди ключей товара — не в выдаче
    pos = w.get("pos") if isinstance(w, dict) else None
    series = pos if isinstance(pos, list) else [pos]
    vals = [int(p) for p in series if isinstance(p, (int, float)) and p > 0]
    return vals[-1] if vals else None


# ---------- прямой поиск WB ----------
def wb_public_position(keyword: str, sku: str, marketplace: str, pages: int = 5) -> int | None:
    if marketplace != "wb":
        raise NotImplementedError("Прямой поиск есть только для WB")
    pos = 0
    for page in range(1, pages + 1):
        r = requests.get(WB_URL, params={
            "query": keyword, "page": page, "resultset": "catalog", "sort": "popular",
            "dest": -1257786, "appType": 1, "curr": "rub", "spp": 30}, timeout=20)
        r.raise_for_status()
        products = (r.json().get("data") or {}).get("products", [])
        if not products:
            return None
        for p in products:
            pos += 1
            if str(p.get("id")) == str(sku):
                return pos
    return None


SOURCES = {"mpstats": mpstats_position, "wb_public": wb_public_position}


# ---------- ссылка на главное фото товара ----------
def photo_url(marketplace: str, sku) -> str | None:
    """Ссылка на главное фото WB по артикулу — её кладут в поле «Ссылка» задания UNU, чтобы исполнитель
    узнал товар в выдаче (так делает менеджер заказчика, вручную: карточка → фото → открыть в новой вкладке).
    Адрес собирается из артикула; CDN geobasket отдаёт фото с любого номера корзины (проверено 24.11).
    Если фото не открылось — None, тогда в задание идёт ссылка на карточку."""
    if marketplace != "wb" or not str(sku or "").isdigit():
        return None
    n = int(sku)
    url = f"https://mow-basket-cdn-01.geobasket.ru/vol{n // 100000}/part{n // 1000}/{n}/images/big/1.webp"
    try:
        ok = requests.head(url, timeout=8, allow_redirects=True).status_code == 200
    except requests.RequestException:
        ok = False
    return url if ok else None


def wb_name(sku) -> str | None:
    """Название товара WB по артикулу — из открытого файла карточки. None, если артикула нет или карточка не открылась."""
    if not str(sku or "").isdigit():
        return None
    n = int(sku)
    try:
        r = requests.get(f"https://mow-basket-cdn-01.geobasket.ru/vol{n // 100000}/part{n // 1000}/{n}/info/ru/card.json",
                         timeout=8)
        name = (r.json().get("imt_name") or "").strip() if r.ok else ""
    except (requests.RequestException, ValueError):
        return None
    return name[:120] or None


def wb_control_word(sku) -> str | None:
    """Контрольный ответ для ТЗ «по фото»: первое слово описания товара. Его видно только в открытой
    карточке, а 3 последние цифры артикула исполнитель прочтёт прямо из ссылки на фото (замечание ревью Gemini 24.11)."""
    if not str(sku or "").isdigit():
        return None
    n = int(sku)
    try:
        r = requests.get(f"https://mow-basket-cdn-01.geobasket.ru/vol{n // 100000}/part{n // 1000}/{n}/info/ru/card.json",
                         timeout=8)
        desc = r.json().get("description", "") if r.ok else ""
    except (requests.RequestException, ValueError):
        return None
    import re
    words = re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", desc or "")
    return words[0].lower() if words else None


# ---------- агент в браузере ----------
SEARCH_URL = {
    "wb": "https://www.wildberries.ru/catalog/0/search.aspx?search={q}&page={page}",
    "ozon": "https://www.ozon.ru/search/?text={q}&page={page}",
}


def make_agent_job(pages: int = 3) -> dict | None:
    """Задание агенту: по каждому товару и ключу — адреса страниц выдачи и артикул, который ищем."""
    items = []
    for p in db.q("SELECT * FROM projects"):
        if p["marketplace"] not in SEARCH_URL or not p["sku"]:
            continue
        for kw in json.loads(p["keywords"] or "[]"):
            items.append({"project_id": p["id"], "product": p["name"], "marketplace": p["marketplace"],
                          "sku": str(p["sku"]), "keyword": kw,
                          "urls": [SEARCH_URL[p["marketplace"]].format(q=quote(kw), page=n) for n in range(1, pages + 1)]})
    if not items:
        return None
    name = "job-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = POS_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.json").write_text(json.dumps({"date": date.today().isoformat(), "items": items},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    db.log_action("positions_job", name, {"keywords": len(items)})
    return {"name": name, "folder": str(folder), "keywords": len(items)}


def import_agent_results() -> int:
    """results.json: [{"project_id", "keyword", "position" (число или null), "city", "pages_checked"}]."""
    n = 0
    if not POS_DIR.exists():
        return 0
    for folder in sorted(POS_DIR.glob("job-*")):
        rf = folder / "results.json"
        if not rf.exists() or (folder / ".imported").exists():
            continue
        try:
            job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            res = json.loads(rf.read_text(encoding="utf-8"))
            assert isinstance(res, list)
        except Exception as e:
            db.log_action("positions_bad_file", folder.name, str(e), ok=False)
            continue
        wanted = {(i["project_id"], i["keyword"]) for i in job["items"]}
        for r in res:
            key = (int(r.get("project_id", 0)), str(r.get("keyword", "")))
            pos = r.get("position")
            if key not in wanted or not (pos is None or (isinstance(pos, int) and 0 < pos < 10000)):
                continue
            db.conn().execute("INSERT OR REPLACE INTO positions(project_id, keyword, date, position, source, city) "
                              "VALUES (?,?,?,?,?,?)", (key[0], key[1], job["date"], pos, "agent", str(r.get("city", ""))[:80]))
            n += 1
        db.conn().commit()
        (folder / ".imported").write_text(datetime.now().isoformat())
        db.log_action("positions_import", folder.name, {"positions": n})
    return n


def agent_jobs() -> list[dict]:
    out = []
    for folder in sorted(POS_DIR.glob("job-*"), reverse=True)[:5] if POS_DIR.exists() else []:
        state = "забрано" if (folder / ".imported").exists() else \
            "агент ответил" if (folder / "results.json").exists() else "ждёт агента"
        out.append({"name": folder.name, "state": state})
    return out


# ---------- демо ----------
def demo_series(project: dict, keyword: str, days: int = 14) -> list[tuple[str, int]]:
    """Демо: позиция растёт после старта заданий, с шумом."""
    rng = random.Random(f"{project['id']}{keyword}")
    start = rng.randint(90, 260)
    out = []
    for k in range(days):
        d = date.today() - timedelta(days=days - 1 - k)
        trend = start * math.exp(-0.11 * max(0, k - 6)) if k > 6 else start + rng.randint(-8, 8)
        out.append((d.isoformat(), max(1, int(trend + rng.randint(-6, 6)))))
    return out


def refresh(demo: bool) -> int:
    """Раз в сутки: демо — пересчитать; API-источник — запросить; агент — подготовить задание."""
    n = 0
    today = date.today().isoformat()
    source = "demo" if demo else SOURCE
    if source == "agent":                   # одно задание агенту в сутки
        if POS_DIR.exists() and any(POS_DIR.glob("job-" + date.today().strftime("%Y%m%d") + "-*")):
            return 0
        return 1 if make_agent_job() else 0
    for p in db.q("SELECT * FROM projects"):
        for kw in json.loads(p["keywords"] or "[]"):
            if source == "demo":
                for d, pos in demo_series(p, kw):
                    db.conn().execute("INSERT OR REPLACE INTO positions(project_id, keyword, date, position, source) "
                                      "VALUES (?,?,?,?,?)", (p["id"], kw, d, pos, "demo"))
                n += 1
                continue
            try:
                pos = SOURCES[source](kw, p["sku"], p["marketplace"])
                db.conn().execute("INSERT OR REPLACE INTO positions(project_id, keyword, date, position, source) "
                                  "VALUES (?,?,?,?,?)", (p["id"], kw, today, pos, source))
                n += 1
            except Exception as e:
                db.log_action("position_error", p["name"], f"{source} · {kw}: {e}", ok=False)
    db.conn().commit()
    return n
