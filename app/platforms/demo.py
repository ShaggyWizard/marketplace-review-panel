"""Демо-площадка: ведёт себя как UNU, но данные генерирует сама.

~2 400 отчётов в сутки, 7 дней истории, новые отчёты приходят в реальном времени.
Среди исполнителей заложены типовые нарушители, чтобы было что ловить:
  - «крутильщики» — несколько человек делают огромную долю заданий;
  - ферма — 15 аккаунтов на 3 IP, одинаковые тексты;
  - воры скриншотов — прикладывают чужой скрин (точная копия или с другим временем в статус-баре);
  - халтурщики — неверный контрольный ответ, нет скриншота, сдают за секунды.
Генерация детерминирована: отчёт №i всегда одинаковый, поэтому перезапуск ничего не ломает.
"""
import bisect
from functools import lru_cache
import io
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import config
from .base import NewTask, Platform, ReportInfo, TaskInfo

STEP_SEC = 36                     # один отчёт в 36 секунд = 2 400 в сутки
HISTORY_DAYS = 7
DATA = config.DB_PATH.parent

PROJECTS = [
    {"key": "f1", "name": "Термокружка Arctic 450 мл", "marketplace": "wb", "sku": "184467291",
     "price": "1290", "keywords": ["термокружка", "термокружка для кофе", "термос кружка"]},
    {"key": "f2", "name": "Робот-пылесос Vexa S7", "marketplace": "ozon", "sku": "1203384756",
     "price": "18990", "keywords": ["робот пылесос", "робот пылесос моющий"]},
    {"key": "f3", "name": "Детский рюкзак Kiddo", "marketplace": "wb", "sku": "201554873",
     "price": "2450", "keywords": ["рюкзак детский", "рюкзак для школы"]},
]

# kind: search_cart — найти по ключу и положить в корзину, контроль — цена в карточке
#       favorite    — в избранное, контроль — последние 3 цифры артикула
#       review_like — лайк отзыва, контроль — первое слово верхнего отзыва
TASKS = [
    ("d101", "f1", "search_cart", "Найти «термокружка», добавить в корзину", 12, 900, 600),
    ("d102", "f1", "favorite", "Термокружка Arctic — в избранное", 6, 500, 120),
    ("d103", "f1", "review_like", "Термокружка — лайк полезного отзыва", 5, 300, 90),
    ("d201", "f2", "search_cart", "Найти «робот пылесос», добавить в корзину", 15, 700, 600),
    ("d202", "f2", "favorite", "Пылесос Vexa — в избранное", 6, 400, 120),
    ("d301", "f3", "search_cart", "Найти «рюкзак детский», добавить в корзину", 12, 600, 600),
    ("d302", "f3", "review_like", "Рюкзак Kiddo — лайк отзыва", 5, 300, 90),
]
TASK_WEIGHTS = [26, 14, 8, 20, 10, 16, 6]
REVIEW_WORD = {"f1": "отличная", "f3": "удобный", "f2": "мощный"}


def project(key):
    return next(p for p in PROJECTS if p["key"] == key)


def control_for(task) -> tuple[str, list[str]]:
    """Контрольный вопрос и правильные ответы — ответить можно, только реально открыв товар."""
    ext, pkey, kind = task[0], task[1], task[2]
    p = project(pkey)
    if kind == "search_cart":
        return "Укажите цену товара в карточке (только число)", [p["price"]]
    if kind == "favorite":
        return "Укажите последние 3 цифры артикула", [p["sku"][-3:]]
    return "Напишите первое слово самого верхнего отзыва", [REVIEW_WORD[pkey]]


# --- исполнители ---
def _workers():
    rng = random.Random(42)
    ws = []
    for k in range(700):
        wid = str(310000 + k * 7)
        if k < 5:
            kind, w = "grinder", 12.0
        elif k < 20:
            kind, w = "farm", 2.0
        elif k < 34:
            kind, w = "stealer", 2.0
        elif k < 70:
            kind, w = "lazy", 1.5
        elif k < 80:
            kind, w = "faker", 1.5
        else:
            kind, w = "honest", 1.0
        ip = f"185.12.{(k - 5) // 5}.77" if kind == "farm" else \
            f"{rng.randint(31, 213)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        ws.append({"id": wid, "kind": kind, "w": w, "ip": ip})
    return ws


WORKERS = _workers()
_CUM = []
_acc = 0.0
for _w in WORKERS:
    _acc += _w["w"]
    _CUM.append(_acc)
_TCUM = []
_acc = 0
for _w in TASK_WEIGHTS:
    _acc += _w
    _TCUM.append(_acc)

HONEST_TEXTS = {
    "search_cart": ["Нашла по запросу «{kw}», цена {ans} ₽, добавила в корзину",
                    "Готово, товар в корзине. Цена {ans}",
                    "{ans} руб, в корзину положил, скрин прикладываю",
                    "Сделано. Цена в карточке {ans}"],
    "favorite": ["Добавила в избранное, артикул заканчивается на {ans}",
                 "{ans}, в избранном", "Готово, последние цифры {ans}"],
    "review_like": ["Первое слово: {ans}. Лайк поставила", "{ans}", "Отзыв начинается со слова «{ans}», лайкнул"],
}


def _anchor() -> datetime:
    f = DATA / "demo_anchor.txt"
    if f.exists():
        return datetime.fromisoformat(f.read_text().strip())
    DATA.mkdir(parents=True, exist_ok=True)
    a = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=HISTORY_DAYS)
    f.write_text(a.isoformat())
    return a


@lru_cache(maxsize=60000)
def gen_report(i: int, anchor: datetime) -> dict:
    """Отчёт №i целиком из номера: задание, исполнитель, текст, скриншот, время."""
    rng = random.Random(i * 7919 + 13)
    task = TASKS[bisect.bisect(_TCUM, rng.random() * _TCUM[-1])]
    w = WORKERS[min(bisect.bisect(_CUM, rng.random() * _CUM[-1]), len(WORKERS) - 1)]
    p = project(task[1])
    _, answers = control_for(task)
    ans = answers[0]
    kind = w["kind"]
    submitted = anchor + timedelta(seconds=i * STEP_SEC + rng.randint(0, STEP_SEC - 1))

    honest_dur = rng.randint(task[6], task[6] * 4)
    dur = honest_dur
    files = [f"demo://shot/{i}"]
    text = rng.choice(HONEST_TEXTS[task[2]]).format(kw=p["keywords"][0], ans=ans)
    # живые люди пишут по-разному: приветствия, подписи, опечатки
    text = rng.choice(["", "Здравствуйте! ", "Добрый день. ", "", "Привет) "]) + text +         rng.choice(["", "", " Спасибо!", " 🙂", " Скрин ниже", f" ({rng.randint(1, 28)}.11)", " проверьте пожалуйста"])

    if kind == "honest":
        if rng.random() < 0.03:
            ans_bad = str(int(ans) + rng.choice([-10, 10, 100])) if ans.isdigit() else "хороший"
            text = text.replace(ans, ans_bad)
        if rng.random() < 0.02:
            files = []
    elif kind == "grinder":
        dur = rng.randint(max(20, task[6] // 4), task[6])
    elif kind == "farm":
        text = f"Задание выполнено полностью по инструкции, ответ {ans}"
        if rng.random() < 0.3:
            text = "готово"
        dur = rng.randint(15, 60)
    elif kind == "stealer":
        if rng.random() < 0.6 and i > 400:
            j = i - rng.randint(30, 400)
            victim = gen_report(j, anchor)["files"]          # берёт реальный скрин из чужого отчёта
            if victim:
                files = [victim[0].split("?")[0] + ("?v=1" if rng.random() < 0.4 else "")]
    elif kind == "faker":
        # ответ списал из чата исполнителей, а скрин — не того товара или без действия:
        # правила это не видят, видно только глазами (агентом)
        r2 = rng.random()
        if r2 < 0.35:
            other = rng.choice([x["key"] for x in PROJECTS if x["key"] != task[1]])
            files = [f"demo://shot/{i}?p={other}"]
        elif r2 < 0.6:
            files = [f"demo://shot/{i}?noact=1"]
    elif kind == "lazy":
        r = rng.random()
        if r < 0.45:
            text = rng.choice(["сделала", "готово", "+", "ок все"])
        elif r < 0.6:
            files = []
        dur = rng.randint(10, 90)
    return {
        "ext_id": f"r{i}", "task": task, "worker": w, "text": text, "files": files,
        "submitted_at": submitted, "taken_at": submitted - timedelta(seconds=dur),
    }


class DemoPlatform(Platform):
    name = "demo"

    def __init__(self):
        self.anchor = _anchor()
        # решения — у каждой базы свои: иначе прогон на одной базе «оплачивает» отчёты в другой
        self._dec_file = config.DB_PATH.with_name(config.DB_PATH.stem + ".decisions.json")
        legacy = DATA / "demo_decisions.json"
        if not self._dec_file.exists() and legacy.exists() and config.DB_PATH.name == "demo.sqlite":
            self._dec_file.write_text(legacy.read_text())     # основная демо-база сохраняет прежние решения
        self.decisions = json.loads(self._dec_file.read_text()) if self._dec_file.exists() else {}
        self.paused: set[str] = set()

    def _save(self):
        self._dec_file.write_text(json.dumps(self.decisions))

    def count(self) -> int:
        return int((datetime.now(timezone.utc) - self.anchor).total_seconds() // STEP_SEC)

    def balance(self) -> dict:
        return {"balance": 48210.5, "blocked": 6120.0}

    def ensure_folder(self, name: str) -> str:
        return "f" + str(abs(hash(name)) % 1000)

    def move_task(self, ext_id, folder_ext_id): pass

    def get_tasks(self) -> list[TaskInfo]:
        n = self.count()
        out = []
        for t in TASKS:
            out.append(TaskInfo(ext_id=t[0], name=t[3], price=float(t[4]),
                                status="paused" if t[0] in self.paused else "active",
                                folder_ext_id=t[1], limit_total=t[5] * 30,
                                done_total=int(n * TASK_WEIGHTS[TASKS.index(t)] / _TCUM[-1]),
                                check_total=0))
        return out

    def get_reports(self, task_ext_ids: list[str]) -> list[ReportInfo]:
        now = datetime.now(timezone.utc)
        want = set(task_ext_ids)
        out = []
        for i in range(self.count()):
            r = gen_report(i, self.anchor)
            if r["task"][0] not in want:
                continue
            age = (now - r["submitted_at"]).total_seconds()
            if r["ext_id"] in self.decisions:
                status = self.decisions[r["ext_id"]]
            elif age < 300 and i % 3 == 0:
                status = "in_progress"
            elif age > 30 * 3600:
                status = "paid"            # старые отчёты уже приняты до появления панели
            else:
                status = "pending"
            out.append(ReportInfo(
                ext_id=r["ext_id"], task_ext_id=r["task"][0], worker_id=r["worker"]["id"],
                ip=r["worker"]["ip"], status=status, price=float(r["task"][4]),
                text="" if status == "in_progress" else r["text"],
                files=[] if status == "in_progress" else r["files"],
                submitted_at=None if status == "in_progress" else r["submitted_at"].isoformat(),
                taken_at=r["taken_at"].isoformat(),
            ))
        return out

    def create_task(self, t: NewTask) -> str:
        return "d" + str(random.randint(500, 9999))

    def pause_task(self, ext_id): self.paused.add(ext_id)
    def play_task(self, ext_id): self.paused.discard(ext_id)

    def approve(self, report_ext_id):
        self.decisions[report_ext_id] = "paid"
        self._save()

    def reject(self, report_ext_id, comment, rework):
        self.decisions[report_ext_id] = "rework" if rework else "rejected"
        self._save()

    def blacklist(self, worker_id): pass

    # готовые услуги — как в UNU API v2 (цены с живого API 23.11.2026)
    SERVICES = [(117, "Add to favorites on Marketplace", 5.0, 10), (118, "Ask question on Marketplace", 5.0, 10),
                (119, "Subscribe to seller on Marketplace", 5.0, 10), (136, "WB cart by keyword", 10.0, 5),
                (137, "WB cart", 9.0, 5), (145, "Marketplace click", 2.0, 5)]

    def services(self):
        return [{"id": i, "name": n, "category": "Marketplace", "price": pr, "min": mn, "max": 1000}
                for i, n, pr, mn in self.SERVICES]

    def order_add(self, service, quantity, link):
        oid = str(random.randint(100000, 999999))
        self.decisions["order:" + oid] = [datetime.now(timezone.utc).isoformat(), quantity]
        self._save()
        return oid

    def order_status(self, ids):
        out = {}
        for i in ids:
            started, q = self.decisions.get("order:" + i, [datetime.now(timezone.utc).isoformat(), 0])
            hours = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 3600
            done = min(q, int(q * hours / 12))           # демо: заказ выполняется за 12 часов
            st = "Pending" if hours < 0.05 else "Completed" if done >= q else "In progress"
            out[i] = {"status": st, "remains": q - done, "charge": done * 1.0}
        return out

    def fetch_file(self, url: str) -> bytes:
        return render_shot(url, self.anchor)


# --- «скриншоты» для демо ---
def _font(size):
    for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


_FONTS = {}


def F(size):
    if size not in _FONTS:
        _FONTS[size] = _font(size)
    return _FONTS[size]


def render_shot(url: str, anchor: datetime) -> bytes:
    """Рисует правдоподобный скрин телефона: шапка площадки, карточка товара, действие.
    У каждого честного отчёта своя прокрутка, баннер и время — как у разных людей."""
    path, _, query = url.removeprefix("demo://shot/").partition("?")
    i = int(path)
    r = gen_report(i, anchor)
    rng = random.Random(i)
    opts = dict(x.split("=") for x in query.split("&") if "=" in x)
    task, p = r["task"], project(opts.get("p", r["task"][1]))
    mp_color = (203, 17, 171) if p["marketplace"] == "wb" else (0, 91, 255)
    W, H = 360, 640
    bg = (rng.randint(235, 255),) * 3
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    t = r["submitted_at"] + timedelta(hours=3)
    if "v" in opts:                    # «подправленная» копия: другое время в статус-баре
        t += timedelta(minutes=17)
    d.rectangle([0, 0, W, 22], fill=(20, 20, 20))
    d.text((10, 4), t.strftime("%H:%M"), font=F(13), fill="white")
    d.rectangle([0, 22, W, 70], fill=mp_color)
    d.text((14, 34), "WILDBERRIES" if p["marketplace"] == "wb" else "OZON", font=F(20), fill="white")
    off = rng.randint(-170, 60)
    # баннер / рекомендации — у всех разные
    for k in range(rng.randint(1, 4)):
        y = 80 + off + k * 60
        d.rectangle([10 + rng.randint(0, 40), y, W - 10, y + 48],
                    fill=tuple(rng.randint(60, 230) for _ in range(3)))
    y0 = 300 + off
    d.rectangle([20, y0 - 150, W - 20, y0 - 10], fill=tuple(rng.randint(120, 220) for _ in range(3)))
    d.text((20, y0), p["name"], font=F(17), fill=(30, 30, 30))
    d.text((20, y0 + 28), f"{int(p['price']):,} ₽".replace(",", " "), font=F(24), fill=(10, 10, 10))
    d.text((20, y0 + 62), f"Артикул: {p['sku']}", font=F(13), fill=(90, 90, 90))
    action = {"search_cart": "Товар в корзине", "favorite": "В избранном",
              "review_like": f"«{REVIEW_WORD.get(p['key'], 'Хороший').capitalize()}…»  Полезно: 1"}[task[2]]
    if "noact" in opts:                # карточка открыта, но действие не сделано
        d.rectangle([20, y0 + 90, W - 20, y0 + 134], outline=mp_color, width=2)
        d.text((34, y0 + 102), "Добавить в корзину", font=F(17), fill=mp_color)
    else:
        d.rectangle([20, y0 + 90, W - 20, y0 + 134], fill=mp_color)
        d.text((34, y0 + 102), action, font=F(17), fill="white")
    for k in range(rng.randint(2, 6)):
        y = y0 + 150 + k * 50
        d.rectangle([20, y, 20 + rng.randint(80, 300), y + 30],
                    fill=tuple(rng.randint(170, 240) for _ in range(3)))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()
