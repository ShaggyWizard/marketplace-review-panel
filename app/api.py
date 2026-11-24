"""HTTP API панели + отдача страницы."""
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, checks, config, db, geo, images, learning, marketplaces, platforms, sync
from .platforms.base import NewTask

app = FastAPI(title="Панель заданий")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Панель без входа и рассчитана на локальный запуск. Чтобы чужая страница в браузере оператора
# не могла нажать «принять / в ЧС» за него (CSRF, DNS rebinding, фрейм) — аудит Cloudflare 23.11:
#  - Host только локальный (или из PANEL_HOSTS, если панель за прокси с паролем);
#  - для запросов, которые меняют данные, Origin обязан совпадать с адресом панели;
#  - страницу нельзя встроить во фрейм, скрипты — только свои и cdnjs.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"} | {
    h.strip() for h in os.getenv("PANEL_HOSTS", "").split(",") if h.strip()}
CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.exception_handler(RequestValidationError)
async def no_echo(request: Request, exc: RequestValidationError):
    """Стандартный ответ 422 повторяет присланное значение в поле input — для ключа API это утечка
    (ревью Codex 24.11). Отдаём только место и текст ошибки."""
    return JSONResponse({"detail": "Неверные данные: " + "; ".join(
        f"{'.'.join(str(x) for x in e.get('loc', [])[1:])} — {e.get('msg', '')}" for e in exc.errors()[:5])},
        status_code=422)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Недопустимый Host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        here = f"{request.url.scheme}://{(request.headers.get('host') or '').lower()}"
        if origin is not None and origin.rstrip("/").lower() != here:      # схема тоже: http того же имени — чужой
            return JSONResponse({"detail": "Запрос с чужой страницы"}, status_code=403)
        if origin is None and "application/json" not in (request.headers.get("content-type") or "") \
                and request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return JSONResponse({"detail": "Запрос с чужой страницы"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@app.on_event("startup")
def startup():
    db.init()
    if config.DEMO:
        sync.seed_demo()
    if not config.HOSTED:
        threading.Thread(target=sync.loop, daemon=True).start()


def kick_sync():
    """Запустить синхронизацию и не ждать её. Обычно — поток; на хостинге (CGI) — отдельный процесс,
    потому что поток умрёт вместе с запросом."""
    if not config.HOSTED:
        threading.Thread(target=sync.run_once, daemon=True).start()
        return
    import subprocess
    import sys
    if not sync.claim(sync.kickfile(), ttl=120):    # запуск уже заказан другим запросом — второй процесс не нужен
        return
    subprocess.Popen([sys.executable, str(config.ROOT / "tools" / "hosted" / "cron_sync.py")],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True, cwd=str(config.ROOT))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# --- сводка ---
@app.get("/api/summary")
def summary():
    d24 = since(24)
    verdicts = {r["verdict"] or "none": r["n"] for r in db.q(
        "SELECT verdict, COUNT(*) n FROM reports WHERE submitted_at>=? GROUP BY verdict", (d24,))}
    queue = db.one("SELECT COUNT(*) n FROM reports WHERE status='pending' AND verdict='manual'")["n"]
    ready = db.one("SELECT COUNT(*) n FROM reports WHERE status='pending' AND decision IS NULL "
                   "AND verdict IN ('approve','rework','reject')")["n"]
    hours = db.q(
        "SELECT substr(submitted_at,1,13) h, verdict, COUNT(*) n FROM reports "
        "WHERE submitted_at>=? GROUP BY h, verdict ORDER BY h", (d24,))
    days = db.q(
        "SELECT substr(submitted_at,1,10) d, COUNT(*) n, ROUND(SUM(price)) spent FROM reports "
        "WHERE submitted_at>=? AND status='paid' GROUP BY d ORDER BY d", (since(24 * 7),))
    reasons = {}
    for r in db.q("SELECT reasons FROM reports WHERE checked_at IS NOT NULL AND submitted_at>=?", (d24,)):
        for f in json.loads(r["reasons"] or "[]"):
            reasons[f["code"]] = reasons.get(f["code"], 0) + 1
    projects = db.q(
        "SELECT p.id, p.name, p.marketplace, COUNT(r.id) n, "
        "SUM(r.verdict='approve') ok, SUM(r.verdict IN ('reject','rework')) bad, SUM(r.verdict='manual') manual, "
        "ROUND(SUM(r.price)) spent FROM projects p LEFT JOIN reports r ON r.project_id=p.id AND r.submitted_at>=? "
        "GROUP BY p.id", (d24,))
    p = platforms.get(platforms.default_name())
    try:
        # на хостинге баланс берём из последней синхронизации: запрос страницы не должен висеть, если площадка молчит
        saved = json.loads(db.get_setting("balance", "") or "{}") if config.HOSTED else {}
        bal = saved if saved else p.balance()
    except Exception as e:
        bal = {"error": str(e)}
    if config.HOSTED:                   # вместо cron: открыли панель, а данные старше цикла — обновляем в фоне
        st = sync.status()
        try:
            old = not st.get("at") or (datetime.now(timezone.utc) - datetime.fromisoformat(st["at"])).total_seconds() \
                > config.SYNC_INTERVAL_SEC
        except ValueError:
            old = True
        if st.get("error") and st.get("at"):          # упала — следующая попытка не раньше чем через 15 минут
            try:
                old = (datetime.now(timezone.utc) - datetime.fromisoformat(st["at"])).total_seconds() > 900
            except ValueError:
                pass
        if old and not st.get("running"):
            kick_sync()
    f = db.one(
        "SELECT COUNT(*) total, SUM(agent_batch IS NULL AND verdict IN ('approve','reject','rework')) rules, "
        "SUM(agent_batch IS NOT NULL) to_agent, SUM(agent_verdict IN ('ok','bad')) agent_decided, "
        "SUM(agent_batch IS NOT NULL AND agent_verdict IS NULL) agent_wait, "
        "SUM(verdict='manual' AND (agent_batch IS NULL OR agent_verdict='unsure')) human "
        "FROM reports WHERE checked_at>=? AND verdict IS NOT NULL", (d24,))
    return {"funnel": f, "spot": agent.spot_stats(), "agent_batches": [_no_path(b) for b in agent.batches()], "verdicts": verdicts, "queue": queue, "ready": ready, "hours": hours, "days": days,
            "reasons": sorted(([checks.RULE_NAMES.get(k, k), v, k] for k, v in reasons.items()), key=lambda x: -x[1]),
            "projects": projects, "balance": bal, "sync": sync.status(), "platform": p.name,
            "read_only": config.READ_ONLY and p.name != "demo", "hosted": config.HOSTED, "agent": agent_seen(),
            "mode": db.get_setting("decision_mode", config.DECISION_MODE),
            "alerts": worker_alerts()[:5]}


# --- проекты и задания ---
class ProjectIn(BaseModel):
    name: str
    marketplace: str
    sku: str = ""
    product_url: str = ""
    keywords: list[str] = []


@app.get("/api/projects")
def projects():
    return db.q("SELECT * FROM projects ORDER BY id")


@app.post("/api/projects")
def create_project(p: ProjectIn):
    plat = platforms.get(platforms.default_name())
    # «только чтение»: папку на площадке не создаём, берём существующую с таким именем, если она есть
    folder = plat.find_folder(p.name) if config.READ_ONLY and plat.name != "demo" else plat.ensure_folder(p.name)
    pid = db.run("INSERT INTO projects(name, marketplace, sku, product_url, keywords, platform, folder_ext_id, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (p.name, p.marketplace, p.sku, p.product_url,
                                               json.dumps(p.keywords, ensure_ascii=False), plat.name, folder, db.now()))
    db.log_action("project_create", p.name, {"folder": folder})
    return {"id": pid}


class ProjectPatch(BaseModel):
    sku: str = Field(pattern=r"^\d{5,12}$")


@app.patch("/api/projects/{pid}")
def patch_project(pid: int, p: ProjectPatch):
    """Указать артикул товару, который завёлся автоматически и остался «не определён» (ИИ не прочитал его со скринов).
    Название панель берёт с маркетплейса. Если товар с таким артикулом уже есть — задания переходят к нему."""
    pr = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not pr:
        raise HTTPException(404, "Нет такого товара")
    same = db.one("SELECT id FROM projects WHERE sku=? AND id<>?", (p.sku, pid))
    if same:
        db.run("UPDATE tasks SET project_id=? WHERE project_id=?", (same["id"], pid))
        db.run("UPDATE reports SET project_id=? WHERE project_id=?", (same["id"], pid))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        return {"ok": True, "merged": same["id"]}
    name = (marketplaces.wb_name(p.sku) if pr["marketplace"] == "wb" else None) or f"Товар {p.sku}"
    db.run("UPDATE projects SET sku=?, name=?, product_url=? WHERE id=?",
           (p.sku, name, f"https://www.wildberries.ru/catalog/{p.sku}/detail.aspx" if pr["marketplace"] == "wb" else "", pid))
    if not json.loads(pr["keywords"] or "[]"):          # запросы берём из заданий товара, если ИИ их уже прочитал
        kws = [r["keyword"] for r in db.q("SELECT DISTINCT keyword FROM tasks WHERE project_id=? AND keyword<>''", (pid,))]
        if kws:
            db.run("UPDATE projects SET keywords=? WHERE id=?", (json.dumps(kws, ensure_ascii=False), pid))
    db.log_action("product_set", p.sku, {"project": pid, "name": name})
    return {"ok": True, "name": name}


@app.get("/api/projects/{pid}")
def project(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    tasks = db.q(
        "SELECT t.*, COUNT(r.id) n24, SUM(r.verdict='approve') ok24, SUM(r.verdict IN ('reject','rework')) bad24, "
        "SUM(r.status='pending') pending FROM tasks t LEFT JOIN reports r ON r.task_id=t.id AND r.submitted_at>=? "
        "WHERE t.project_id=? GROUP BY t.id", (since(24), pid))
    days = db.q("SELECT substr(submitted_at,1,10) d, COUNT(*) n FROM reports WHERE project_id=? "
                "AND submitted_at>=? GROUP BY d ORDER BY d", (pid, since(24 * 14)))
    pos = db.q("SELECT keyword, date, position FROM positions WHERE project_id=? ORDER BY date", (pid,))
    top = db.q("SELECT worker_id, COUNT(*) n FROM reports WHERE project_id=? AND submitted_at>=? "
               "GROUP BY worker_id ORDER BY n DESC LIMIT 5", (pid, since(24)))
    return {"project": p, "tasks": tasks, "days": days, "positions": pos, "top_workers": top}


TEMPLATES = {
    # ТЗ «как у заказчика» (видео-инструкция менеджера, 24.11): ссылка — на главное фото товара,
    # узкий фильтр цены, чтобы товар выпал сразу; в отчёте — скрины всех этапов.
    "card_boost": ("Необходимо зайти на маркетплейс {mp} и найти по запросу «{kw}» товар с фото по ссылке. "
                   "Чтобы поиск был максимально простым, в фильтре поставьте диапазон цены от {pf} до {pt} — "
                   "и сразу вам выпадет искомый товар. После этого перейдите на страницу товара, добавьте его "
                   "в корзину, поставьте лайк (избранное — сердечко), откройте описание товара, пролистайте "
                   "пару фото в карточке. На этом задание будет считаться выполненным!",
                   "Скрины этапов: поиск по ключу с фильтром цены (на скрине должна быть видна строка поиска с запросом), "
                   "карточка товара, корзина, избранное. "
                   "В тексте отчёта — первое слово из описания товара."),
    # Сценарий «как живой покупатель» (замечания ревью Gemini): прогрев на конкурентах, время в
    # карточке, отзывы, без прямой ссылки, со своего аккаунта. Иначе маркетплейс может не засчитать действие.
    "search_cart": ("Войдите в свой аккаунт {mp} (Москва или ваш город — укажите в отчёте). В поиске введите "
                    "«{kw}» — НЕ переходите по ссылке. Откройте 2–3 похожих товара конкурентов, полистайте их. "
                    "Затем найдите наш товар в выдаче, пробудьте в карточке не меньше минуты: фото, описание, "
                    "отзывы. Выберите размер/цвет, если есть, и добавьте в корзину.",
                    "Скриншот корзины с товаром, цена в карточке (только число) и ваш город"),
    "favorite": ("Войдите в свой аккаунт {mp}. Найдите товар через поиск по запросу «{kw}» (не по ссылке), "
                 "посмотрите 2–3 похожих товара, затем откройте наш, полистайте фото и отзывы не меньше минуты "
                 "и добавьте в избранное.",
                 "Скриншот избранного и последние 3 цифры артикула"),
    "review_like": ("Откройте товар, перейдите к отзывам, прочитайте 3–4 отзыва и отметьте полезным самый "
                    "верхний отзыв.",
                    "Скриншот и первое слово этого отзыва"),
}


class TaskIn(BaseModel):
    kind: str = "search_cart"
    name: str = ""
    keyword: str = ""
    price: float = Field(ge=1, le=10000)
    per_day: int = Field(ge=1, le=5000)       # сколько выполнений в сутки
    days: int = Field(ge=1, le=90)            # сколько дней
    control_expected: list[str] = []
    min_seconds: int = 120
    limit_per_user: int = 1
    limit_per_user_folder: int | None = 1     # один человек — одно задание из папки товара
    descr: str = ""
    price_from: int | None = Field(default=None, ge=0, le=10_000_000)   # фильтр цены для card_boost
    price_to: int | None = Field(default=None, ge=0, le=10_000_000)


# Сколько скринов этапов ждём в отчёте. На живых отчётах заказчика (24.11) честный отчёт — 3 скрина:
# поиск, карточка с сердечком «в избранном», корзина. Отдельный скрин избранного шлют не все.
STAGES = {"card_boost": 3}


@app.post("/api/projects/{pid}/tasks")
def create_task(pid: int, t: TaskIn):
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    plat = platforms.get(p["platform"])
    mp = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс Маркета"}.get(p["marketplace"], p["marketplace"])
    link = ""
    if t.kind == "card_boost":
        if t.price_from is None or t.price_to is None or t.price_from > t.price_to:
            raise HTTPException(422, "Укажите диапазон цены для фильтра: от и до")
        if not t.control_expected:
            word = marketplaces.wb_control_word(p["sku"]) if p["marketplace"] == "wb" else None
            if not word:
                raise HTTPException(422, "Не удалось взять описание товара — укажите контрольный ответ сами")
            t.control_expected = [word]
        link = marketplaces.photo_url(p["marketplace"], p["sku"])
        if not link:                  # по видео заказчика в задание идёт именно фото — без него не создаём
            raise HTTPException(422, "Фото товара по артикулу не открылось — проверьте артикул WB")
    elif t.kind == "review_like":
        link = p["product_url"] or ""
    if not t.control_expected:
        raise HTTPException(422, "Укажите контрольный ответ — по нему проверяются отчёты")
    descr, need = TEMPLATES.get(t.kind, (t.descr, "Скриншот выполнения"))
    descr = t.descr or descr.format(kw=t.keyword or "", mp=mp, pf=t.price_from, pt=t.price_to)
    name = t.name or {"card_boost": f"Прокачать карточку по запросу «{t.keyword}»",
                      "search_cart": f"Найти «{t.keyword}» → в корзину", "favorite": f"Найти «{t.keyword}» → в избранное",
                      "review_like": "Лайк полезного отзыва"}.get(t.kind, "Задание") + f" · {p['name']}"
    limit = t.per_day * t.days
    ext = plat.create_task(NewTask(name=name, descr=descr, need_for_report=need, price=t.price, limit=limit, kind=t.kind,
                                   link=link, limit_per_user=t.limit_per_user, limit_per_day=t.per_day,
                                   limit_per_user_folder=t.limit_per_user_folder,
                                   time_for_check=24 if t.kind == "card_boost" else 48,
                                   folder_ext_id=p["folder_ext_id"]))
    db.run("INSERT INTO tasks(project_id, platform, ext_id, name, kind, descr, need_for_report, price, need_screen, "
           "limit_total, status, control_expected, min_seconds, keyword, per_day, stages) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (pid, plat.name, ext, name, t.kind, descr, need, t.price, 1, limit, "moderation",
            json.dumps(t.control_expected, ensure_ascii=False), t.min_seconds, t.keyword or None, t.per_day,
            STAGES.get(t.kind, 0)))
    db.log_action("task_create", ext, t.model_dump())
    return {"ext_id": ext}


class TaskPatch(BaseModel):
    control_expected: list[str] | None = None
    min_seconds: int | None = None
    project_id: int | None = None
    keyword: str | None = None
    descr: str | None = Field(default=None, max_length=4000)     # сценарий ТЗ: API площадки текст задания не отдаёт
    move: bool = False        # перенести задание на площадке в папку товара: там действует лимит «1 человек — 1 задание»


@app.get("/api/tasks/unassigned")
def unassigned_tasks():
    """Задания, которые уже заведены в кабинете площадки, но не привязаны к товару панели.
    На живом кабинете заказчика так лежали все 11 заданий: 8 — в общей папке «Мои задачи»."""
    return db.q("SELECT id, ext_id, name, price, status, limit_total, done_total, check_total FROM tasks "
                "WHERE project_id IS NULL AND service_id IS NULL AND platform=? ORDER BY ext_id DESC",
                (platforms.default_name(),))


@app.patch("/api/tasks/{tid}")
def patch_task(tid: int, t: TaskPatch):
    task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not task:
        raise HTTPException(404, "Нет такого задания")
    pr = None
    if t.project_id is not None:
        pr = db.one("SELECT * FROM projects WHERE id=?", (t.project_id,))
        if not pr:
            raise HTTPException(404, "Нет такого товара")
        if t.move:                      # единственное, что привязка меняет на площадке, и только по галочке.
            plat = platforms.get(task["platform"])      # Делаем до локальных правок: не вышло — ничего не меняем
            try:
                folder = pr["folder_ext_id"] or plat.ensure_folder(pr["name"])
                plat.move_task(task["ext_id"], folder)
            except Exception as e:
                raise HTTPException(502, f"Перенос в папку не удался, привязка не выполнена: {e}")
            db.run("UPDATE projects SET folder_ext_id=? WHERE id=?", (folder, pr["id"]))
            db.log_action("task_move", task["ext_id"], {"folder": folder, "project": pr["name"]})
    if t.control_expected is not None:
        db.run("UPDATE tasks SET control_expected=? WHERE id=?", (json.dumps(t.control_expected, ensure_ascii=False), tid))
    if t.min_seconds is not None:
        db.run("UPDATE tasks SET min_seconds=? WHERE id=?", (t.min_seconds, tid))
    if t.keyword is not None:
        db.run("UPDATE tasks SET keyword=? WHERE id=?", (t.keyword.strip() or None, tid))
    if t.descr is not None:
        db.run("UPDATE tasks SET descr=? WHERE id=?", (t.descr.strip() or None, tid))
    if pr is not None:
        # привязка в панели; отчёты подтянет ближайшая синхронизация
        db.run("UPDATE tasks SET project_id=? WHERE id=?", (t.project_id, tid))
        db.run("UPDATE reports SET project_id=? WHERE task_id=?", (t.project_id, tid))
        db.log_action("task_attach", str(tid), {"project": t.project_id, "keyword": t.keyword})
        kick_sync()
    return {"ok": True}


@app.post("/api/tasks/{tid}/{action}")
def task_action(tid: int, action: str):
    t = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    plat = platforms.get(t["platform"])
    if action == "pause":
        plat.pause_task(t["ext_id"]); st = "paused"
    elif action == "play":
        plat.play_task(t["ext_id"]); st = "active"
    else:
        raise HTTPException(400)
    db.run("UPDATE tasks SET status=? WHERE id=?", (st, tid))
    db.log_action("task_" + action, t["ext_id"], t["name"])
    return {"ok": True}


# --- проверка отчётов ---
@app.get("/api/reports")
def reports(verdict: str = "manual", project_id: int | None = None, status: str = "pending",
            limit: int = 50, offset: int = 0, worker_id: str | None = None, order: str = "score"):
    where, args = ["1=1"], []
    if verdict != "all":
        where.append("r.verdict=?"); args.append(verdict)
    if status != "all":
        where.append("r.status=?"); args.append(status)
    if project_id:
        where.append("r.project_id=?"); args.append(project_id)
    if worker_id:
        where.append("r.worker_id=?"); args.append(worker_id)
    sql = ("SELECT r.*, t.name task_name, t.need_for_report, t.control_expected, p.name project_name, p.sku, "
           "p.marketplace FROM reports r JOIN tasks t ON t.id=r.task_id JOIN projects p ON p.id=r.project_id WHERE "
           + " AND ".join(where))
    total = db.one(f"SELECT COUNT(*) n FROM ({sql})", args)["n"]
    # «старые сверху» — под срок проверки площадки (24–48 ч), иначе сомнительные сверху
    order_sql = " ORDER BY r.submitted_at, r.score" if order == "age" else " ORDER BY r.score ASC, r.submitted_at"
    rows = db.q(sql + order_sql + " LIMIT ? OFFSET ?", args + [min(limit, 500), offset])
    for r in rows:
        r["files"] = json.loads(r["files"] or "[]")
        r["reasons"] = json.loads(r["reasons"] or "[]")
        for f in r["reasons"]:
            f["name"] = checks.RULE_NAMES.get(f["code"], f["code"])
            if f["code"] in ("dup_near", "dup_exact"):      # оригинал рядом — человеку сравнить за секунду
                m = re.search(r"#(\d+)", f["text"])
                o = db.one("SELECT files, worker_id FROM reports WHERE id=?", (int(m.group(1)),)) if m else None
                if o:
                    f["ref"] = {"id": int(m.group(1)), "worker": o["worker_id"], "files": json.loads(o["files"] or "[]")}
        r["control_expected"] = json.loads(r["control_expected"] or "[]")
        w = db.one("SELECT COUNT(*) n, SUM(verdict IN ('reject','rework')) bad FROM reports WHERE worker_id=?",
                   (r["worker_id"],))
        r["worker_stats"] = w
    return {"total": total, "items": rows}


class Decision(BaseModel):
    action: str                     # approve / rework / reject
    comment: str | None = None


@app.post("/api/reports/{rid}/decide")
def decide(rid: int, d: Decision):
    if d.action not in ("approve", "rework", "reject"):
        raise HTTPException(400)
    try:
        sync.apply_decision(rid, d.action, "человек", d.comment)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


class LearnChange(BaseModel):
    code: str = Field(max_length=40)
    change: dict = {}


@app.get("/api/learning")
def learning_summary():
    return learning.summary()


@app.post("/api/learning/apply")
def learning_apply(c: LearnChange):
    # применять можно только то, что сейчас предлагает разбор, — не произвольный вес
    if not any(s["code"] == c.code and s["change"] == c.change for s in learning.suggestions()):
        raise HTTPException(409, "Это предложение уже неактуально — обновите страницу")
    learning.apply(c.code, c.change)
    return {"ok": True}


@app.post("/api/learning/rollback")
def learning_rollback(c: LearnChange):
    try:
        learning.rollback(c.code)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/reports/apply-confident")
def apply_confident():
    return {"applied": sync.apply_confident()}


# --- исполнители ---
def worker_alerts():
    d24 = since(24)
    out = []
    for r in db.q("SELECT project_id, worker_id, COUNT(*) n FROM reports WHERE submitted_at>=? "
                  "GROUP BY project_id, worker_id HAVING n>=? ORDER BY n DESC LIMIT 20", (d24, config.WORKER_DAY_MAX * 2)):
        total = db.one("SELECT COUNT(*) n FROM reports WHERE project_id=? AND submitted_at>=?", (r["project_id"], d24))["n"]
        if r["n"] > max(config.WORKER_DAY_MAX, total * config.WORKER_DAY_SHARE):
            pname = db.one("SELECT name FROM projects WHERE id=?", (r["project_id"],))["name"]
            out.append({"kind": "concentration", "worker_id": r["worker_id"],
                        "text": f"Исполнитель {r['worker_id']} сделал {r['n']} из {total} отчётов по «{pname}» за сутки "
                                f"({r['n'] * 100 / max(total, 1):.1f}%). Поставьте limit_per_user=1 в заданиях проекта."})
    for r in db.q("SELECT ip, COUNT(DISTINCT worker_id) w, COUNT(*) n FROM reports WHERE submitted_at>=? AND ip<>'' "
                  "GROUP BY ip HAVING w>=3 ORDER BY w DESC LIMIT 10", (since(24 * 7),)):
        out.append({"kind": "farm", "worker_id": None,
                    "text": f"IP {r['ip']}: {r['w']} аккаунтов, {r['n']} отчётов за неделю — похоже на ферму. "
                            f"Поставьте limit_per_ip=1."})
    # по живым отчётам заказчика (24.11): больше половины потока — не из России
    by, total = {}, 0
    for r in db.q("SELECT ip, COUNT(*) n FROM reports WHERE submitted_at>=? AND ip<>'' GROUP BY ip", (since(24 * 7),)):
        cc = geo.country(r["ip"])
        if cc:
            by[cc] = by.get(cc, 0) + r["n"]
            total += r["n"]
    away = sum(n for cc, n in by.items() if cc not in config.GEO_HOME)
    if total >= 20 and away / total >= 0.3:
        top = max((cc for cc in by if cc not in config.GEO_HOME), key=lambda cc: by[cc])
        out.insert(0, {"kind": "geo", "worker_id": None,
                       "text": f"{away * 100 // total}% отчётов за неделю пришли не из России (больше всего — {geo.name(top)}, "
                               f"{by[top] * 100 // total}%). Если такие исполнители вам не подходят, включите в кабинете UNU таргетинг по стране: через API он не задаётся."})
    for r in db.q("SELECT t.name, t.ext_id, t.keyword, COUNT(*) n FROM reports r JOIN tasks t ON t.id=r.task_id "
                  "WHERE r.submitted_at>=? AND r.reasons LIKE '%not_found_msg%' GROUP BY t.id", (since(24 * 7),)):
        out.insert(0, {"kind": "price", "worker_id": None,
                       "text": f"Задание № {r['ext_id']}" + (f" («{r['keyword']}»)" if r["keyword"] else "") +
                               f": исполнители пишут, что товар не находится по фильтру цены ({r['n']} за неделю). "
                               f"Проверьте цену товара и диапазон в ТЗ."})
    many = db.q("SELECT worker_id, COUNT(DISTINCT task_id) k FROM reports WHERE submitted_at>=? GROUP BY worker_id "
                "HAVING k>? ORDER BY k DESC", (d24, config.WORKER_TASKS_DAY))
    if many:
        out.append({"kind": "many", "worker_id": many[0]["worker_id"],
                    "text": f"{len(many)} исполнителей за сутки прошли больше {config.WORKER_TASKS_DAY} разных заданий кабинета "
                            f"(максимум — {many[0]['k']} у {many[0]['worker_id']}). Лимит «1 на человека» действует внутри задания. "
                            f"Чтобы он действовал на весь товар, держите задания товара в одной папке с лимитом "
                            f"«1 исполнитель — 1 задание из папки»: новые ТЗ панель создаёт так сама."})
    return out


@app.get("/api/workers")
def workers(sort: str = "risk", limit: int = 100):
    d24 = since(24)
    rows = db.q(
        "SELECT r.worker_id, COUNT(*) n, SUM(r.submitted_at>=?) n24, COUNT(DISTINCT r.project_id) projects, "
        "COUNT(DISTINCT r.task_id) tasks, COUNT(DISTINCT r.ip) ips, "
        "(SELECT x.ip FROM reports x WHERE x.worker_id=r.worker_id AND x.platform=r.platform "
        " ORDER BY x.submitted_at DESC, x.id DESC LIMIT 1) ip, SUM(r.verdict='approve') ok, SUM(r.verdict IN ('reject','rework')) bad, "
        "SUM(r.verdict='manual') manual, AVG(r.score) avg_score, SUM(r.reasons LIKE '%dup_exact%') stolen, "
        "COALESCE(w.blacklisted,0) blacklisted "
        "FROM reports r LEFT JOIN workers w ON w.worker_id=r.worker_id AND w.platform=r.platform "
        "WHERE r.verdict IS NOT NULL GROUP BY r.worker_id", (d24,))
    for r in rows:
        checked = (r["ok"] or 0) + (r["bad"] or 0) + (r["manual"] or 0)
        r["bad_rate"] = round((r["bad"] or 0) / checked, 2) if checked else 0
        r["ip_mates"] = db.one("SELECT COUNT(DISTINCT worker_id) n FROM reports WHERE ip=?", (r["ip"],))["n"] - 1
        r["cc"] = geo.country(r["ip"])
        r["country"] = geo.name(r["cc"]) if r["cc"] else ""
        r["risk"] = round(min(100, r["bad_rate"] * 100 + (r["stolen"] or 0) * 30 + r["ip_mates"] * 8
                              + max(0, (r["n24"] or 0) - config.WORKER_DAY_MAX * 3) * 1.5
                              + (100 - (r["avg_score"] or 100)) * 0.5))
    key = {"risk": "risk", "volume": "n24", "bad": "bad_rate"}.get(sort, "risk")
    rows.sort(key=lambda r: -(r[key] or 0))
    # откуда идут отчёты: считаем по отчётам, а не по людям — так видна доля потока
    by = {}
    for x in db.q("SELECT ip, COUNT(*) n FROM reports WHERE ip<>'' GROUP BY ip"):
        cc = geo.country(x["ip"])
        by[cc] = by.get(cc, 0) + x["n"]
    known = sum(n for cc, n in by.items() if cc)
    countries = [{"cc": cc, "name": geo.name(cc), "n": n, "home": cc in config.GEO_HOME}
                 for cc, n in sorted(by.items(), key=lambda kv: -kv[1]) if cc]
    return {"total": len(rows), "items": rows[:limit], "countries": countries, "geo_known": known}


@app.post("/api/workers/{wid}/blacklist")
def blacklist(wid: str):
    plat = platforms.get(platforms.default_name())
    plat.blacklist(wid)
    db.run("INSERT INTO workers(platform, worker_id, blacklisted) VALUES (?,?,1) "
           "ON CONFLICT(platform, worker_id) DO UPDATE SET blacklisted=1", (plat.name, wid))
    # его ещё не принятые отчёты — на повторную проверку, правило чёрного списка их отклонит
    db.run("UPDATE reports SET checked_at=NULL WHERE worker_id=? AND status='pending' AND decision IS NULL", (wid,))
    db.log_action("blacklist", wid, "добавлен в чёрный список")
    kick_sync()
    return {"ok": True}


# --- прочее ---
@app.get("/api/actions")
def actions(limit: int = 100):
    return db.q("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/sync")
def run_sync():
    if config.HOSTED:                  # на хостинге запрос не может ждать минуты — запускаем и отвечаем сразу
        kick_sync()
        return {**sync.status(), "running": True}
    return sync.run_once()


# --- ключ площадки: ввод в панели вместо правки .env ---
class KeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_\-]+$")


def _key_state():
    k = config.UNU_API_KEY
    return {"has_key": bool(k), "tail": k[-4:] if len(k) >= 12 else "", "source": config.KEY_SOURCE,
            "mode": "demo" if config.DEMO else "live", "read_only": config.READ_ONLY, "hosted": config.HOSTED}


@app.get("/api/key")
def key_state():
    """Сам ключ панель не отдаёт никогда — только «есть / нет» и 4 последних знака."""
    return _key_state()


@app.post("/api/key")
def key_save(k: KeyIn):
    if config.KEY_SOURCE == "env":
        raise HTTPException(409, "Ключ задан в .env — меняйте его там")
    from .platforms.base import PlatformError
    from .platforms.unu import UnuPlatform
    try:                                # проверяем ключ одним запросом на чтение — балансом
        bal = UnuPlatform(api_key=k.key).balance()
    except PlatformError as e:
        raise HTTPException(400, "UNU не принял ключ: " + str(e).replace(k.key, "***")[:200])
    config.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.KEY_FILE.with_suffix(".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    # создаём сразу с правами «только владелец», а не правим их после записи
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"unu_api_key": k.key}))
    os.replace(tmp, config.KEY_FILE)
    db.log_action("key_set", "unu", "ключ площадки сохранён")        # сам ключ в журнал не пишем
    if config.HOSTED:                   # следующий запрос — уже новый процесс с этим ключом
        kick_sync()
    return {"ok": True, "balance": bal["balance"], "restart": not config.HOSTED}


@app.delete("/api/key")
def key_delete():
    if config.KEY_SOURCE == "env":
        raise HTTPException(409, "Ключ задан в .env — уберите его там")
    try:
        config.KEY_FILE.unlink()
    except FileNotFoundError:
        pass
    db.log_action("key_unset", "unu", "ключ площадки убран")
    return {"ok": True, "restart": not config.HOSTED}


class Settings(BaseModel):
    decision_mode: str


@app.post("/api/settings")
def settings(s: Settings):
    if s.decision_mode not in ("advise", "auto"):
        raise HTTPException(400)
    db.set_setting("decision_mode", s.decision_mode)
    db.log_action("settings", "decision_mode", s.decision_mode)
    return {"ok": True}


@app.get("/api/file")
def file(url: str):
    """Скриншоты через панель: кеш + обход запрета на встраивание с чужого домена."""
    if not db.one("SELECT 1 FROM reports WHERE files LIKE ? LIMIT 1", (f'%{url}%',)):
        raise HTTPException(404)
    plat = platforms.get(platforms.default_name())
    data = images.cached_fetch(plat, url)
    return Response(data, media_type="image/png" if data[:4] == b"\x89PNG" else "image/jpeg",
                    headers={"Cache-Control": "max-age=86400"})


# --- продвижение: главная страница ---
@app.get("/api/promo")
def promo():
    out = []
    today, d24, d7 = since(0)[:10], since(24), since(24 * 7)
    for p in db.q("SELECT * FROM projects ORDER BY id"):
        kws = []
        for kw in json.loads(p["keywords"] or "[]"):
            series = db.q("SELECT date, position, depth FROM positions WHERE project_id=? AND keyword=? ORDER BY date",
                          (p["id"], kw))
            now_pos = series[-1]["position"] if series else None
            week = [s for s in series if s["date"] <= (datetime.now() - timedelta(days=7)).date().isoformat()]
            was = week[-1]["position"] if week else (series[0]["position"] if series else None)
            kws.append({"keyword": kw, "now": now_pos, "was": was, "series": [s["position"] for s in series],
                        "checked": bool(series), "depth": series[-1]["depth"] if series else None})       # проверяли, но в первых страницах не нашли — это не «нет данных»
        tasks = db.q("SELECT id, name, kind, keyword, status, per_day, limit_total, done_total, price, service_id "
                     "FROM tasks WHERE project_id=? ORDER BY id", (p["id"],))
        for t in tasks:
            if t["service_id"]:            # услуга v2: выполнено = заказано сегодня минус остаток
                o = db.one("SELECT quantity, remains, status FROM orders WHERE task_id=? ORDER BY date DESC LIMIT 1", (t["id"],))
                t["done24"] = (o["quantity"] - o["remains"]) if o else 0
                t["order_status"] = o["status"] if o else "ждёт первого заказа"
            else:
                t["done24"] = db.one("SELECT COUNT(*) n FROM reports WHERE task_id=? AND submitted_at>=? "
                                     "AND status<>'in_progress'", (t["id"], d24))["n"]
        st = db.one("SELECT COUNT(*) n, ROUND(SUM(price)) spent FROM reports WHERE project_id=? AND submitted_at>=? "
                    "AND status='paid'", (p["id"], d7))
        daily = db.q("SELECT substr(submitted_at,1,10) d, COUNT(*) n FROM reports WHERE project_id=? AND submitted_at>=? "
                     "GROUP BY d ORDER BY d", (p["id"], since(24 * 14)))
        out.append({"project": p, "keywords": kws, "tasks": tasks, "week": st, "daily": daily,
                    "plan_day": sum((t["per_day"] or 0) for t in tasks if t["status"] in ("active", "moderation")),
                    "done24": sum(t["done24"] for t in tasks)})
    return out


# --- агент проверки ---
@app.post("/api/agent/batch")
def agent_batch(limit: int = 240):
    b = agent.make_batch(limit)
    if not b:
        raise HTTPException(409, "Нечего отправлять: серая зона пуста")
    return _no_path(b)


@app.post("/api/agent/import")
def agent_import():
    return agent.import_results()


# --- мост для агента на другом компьютере ---
# Панель на хостинге не может сама позвать Claude: ключа API нет, проверка идёт по подписке через Claude Code
# на ПК. Поэтому панель отдаёт пачку (описание + листы), агент на ПК пишет вердикты, tools/remote_agent.py
# возвращает их сюда. Вердикты — данные: принимаются только номера из пачки и три значения.
def _batch_dir(name: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]*batch-\d{8}-\d{6}", name):
        raise HTTPException(404)
    d = agent.AGENT_DIR / name
    if not (d / "tasks.json").exists():
        raise HTTPException(404)
    return d


def _no_path(b: dict) -> dict:
    """На сайте путь к папке на сервере посетителю не нужен."""
    return {k: v for k, v in b.items() if k != "folder"} if config.HOSTED else b


AGENT_ONLINE_SEC = 120      # наблюдатель опрашивает панель раз в 30 с; молчит 2 минуты — считаем, что он выключен


def agent_seen() -> dict:
    at = db.get_setting("agent_seen", "")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() if at else None
    except ValueError:
        age = None
    return {"at": at or None, "online": age is not None and age < AGENT_ONLINE_SEC}


@app.post("/api/agent/poll")
def agent_poll():
    """Опрос от наблюдателя на ПК (tools/remote_agent.py watch): отмечаем, что он на связи, и отдаём пачки,
    которые ждут проверки. Сам наблюдатель запускает Claude Code по подписке и присылает вердикты."""
    db.set_setting("agent_seen", db.now())
    waiting = []
    if agent.AGENT_DIR.exists():
        for d in sorted(agent.AGENT_DIR.glob("batch-*")):
            if (d / "tasks.json").exists() and not (d / "verdicts.json").exists() and not (d / ".imported").exists():
                waiting.append(d.name)
    return {"waiting": waiting[-5:]}


@app.get("/api/agent/batch/{name}/tasks")
def agent_batch_tasks(name: str):
    d = _batch_dir(name)
    ex = d / "examples.json"
    return {"name": name, "tasks": json.loads((d / "tasks.json").read_text(encoding="utf-8")),
            "examples": json.loads(ex.read_text(encoding="utf-8")) if ex.exists() else [],
            "sheets": sorted(f.name for f in d.glob("sheet-*.png")),
            "done": (d / ".imported").exists()}


class VerdictIn(BaseModel):
    n: int
    verdict: str = Field(pattern="^(ok|bad|unsure)$")
    reason: str = Field(default="", max_length=300)
    # что агент прочитал со скрина: по этому панель подписывает товары, заведённые автоматически
    sku: str = Field(default="", max_length=20)
    query: str = Field(default="", max_length=120)
    product: str = Field(default="", max_length=120)


@app.post("/api/agent/batch/{name}/verdicts")
def agent_batch_verdicts(name: str, verdicts: list[VerdictIn], request: Request):
    d = _batch_dir(name)
    if (d / ".imported").exists():
        raise HTTPException(409, "Эта пачка уже принята")
    known = {i["n"] for i in json.loads((d / "tasks.json").read_text(encoding="utf-8"))}
    extra = [v.n for v in verdicts if v.n not in known]
    if extra or len(verdicts) > 2000:
        raise HTTPException(422, f"Номеров нет в пачке: {extra[:5]}")
    if not sync.claim(d / ".receiving", ttl=600):
        raise HTTPException(409, "Вердикты по этой пачке уже принимаются")
    try:
        tmp = d / "verdicts.tmp"
        tmp.write_text(json.dumps([v.model_dump() for v in verdicts], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "verdicts.json")
        db.log_action("agent_remote", name, {"verdicts": len(verdicts), "from": request.headers.get("x-forwarded-for")
                                             or (request.client.host if request.client else "")})
        return agent.import_results()
    finally:
        try:
            (d / ".receiving").unlink()
        except OSError:
            pass


@app.get("/api/agent/sheet")
def agent_sheet(batch: str, sheet: str):
    f = agent.AGENT_DIR / batch / sheet
    if not (f.exists() and f.parent.parent == agent.AGENT_DIR and f.suffix == ".png"):
        raise HTTPException(404)
    return FileResponse(f)


# --- готовые услуги UNU (API v2): заказ количества без отчётов ---
_services_cache: dict = {}


@app.get("/api/services")
def services():
    plat = platforms.get(platforms.default_name())
    if plat.name not in _services_cache:
        _services_cache[plat.name] = plat.services()
    return [s for s in _services_cache[plat.name] if s["category"] == "Marketplace"]


class OrderIn(BaseModel):
    service: int
    per_day: int = Field(ge=1, le=5000)
    days: int = Field(ge=1, le=90)


@app.post("/api/projects/{pid}/orders")
def create_order(pid: int, o: OrderIn):
    """ТЗ как готовая услуга. В v2 нет лимита в сутки, поэтому панель сама дробит:
    каждый день ставит заказ на per_day штук, пока не пройдёт days дней."""
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p["product_url"]:
        raise HTTPException(400, "У товара нет ссылки — услуге нужна ссылка на карточку")
    svc = next((s for s in services() if s["id"] == o.service), None)
    if not svc:
        raise HTTPException(400, "Нет такой услуги")
    if not svc["min"] <= o.per_day <= svc["max"]:
        raise HTTPException(400, f"В день можно от {svc['min']} до {svc['max']}")
    tid = db.run("INSERT INTO tasks(project_id, platform, ext_id, name, kind, price, limit_total, status, per_day, days, "
                 "service_id, need_screen) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                 (pid, p["platform"], f"svc-{pid}-{db.now()}", f"{svc['name']} · {p['name']}", "order", svc["price"],
                  o.per_day * o.days, "active", o.per_day, o.days, o.service))
    db.log_action("order_plan", svc["name"], {"task": tid, "per_day": o.per_day, "days": o.days,
                                              "budget": round(svc["price"] * o.per_day * o.days)})
    kick_sync()
    return {"ext_id": f"услуга, {o.per_day}×{o.days}"}


# --- позиции в выдаче: источник и задание агенту ---
@app.get("/api/positions/status")
def positions_status():
    last = db.one("SELECT MAX(date) d FROM positions")["d"]
    src = db.one("SELECT source, city FROM positions WHERE date=? AND source IS NOT NULL LIMIT 1", (last,)) if last else None
    return {"source": "demo" if config.DEMO else marketplaces.SOURCE, "last_date": last,
            "last_source": src["source"] if src else None, "city": src["city"] if src else None,
            "jobs": marketplaces.agent_jobs()}


@app.post("/api/positions/agent-job")
def positions_agent_job():
    j = marketplaces.make_agent_job()
    if not j:
        raise HTTPException(409, "Нет товаров с артикулом и ключевыми запросами")
    return j
