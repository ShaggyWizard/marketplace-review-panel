"""Синхронизация с площадкой, проверка и применение решений.

Цикл раз в SYNC_INTERVAL_SEC (5 мин по умолчанию):
  1. задания площадки -> tasks (по папке узнаём проект);
  2. отчёты активных заданий -> reports (новые и изменившиеся);
  3. проверка новых отчётов (checks.py);
  4. в режиме auto уверенные решения сразу уходят в площадку.
На 3 000 отчётов в сутки это ~10 новых отчётов за цикл и несколько вызовов API.
"""
import json
import os
import threading
import time
import traceback

from . import agent, checks, config, db, learning, marketplaces, platforms, vision

_lock = threading.Lock()
last_sync = {"at": None, "new": 0, "checked": 0, "applied": 0, "error": None, "seconds": 0}


def seed_demo():
    from .platforms import demo
    if db.one("SELECT 1 FROM projects LIMIT 1"):
        return
    for p in demo.PROJECTS:
        db.run("INSERT INTO projects(name, marketplace, sku, product_url, keywords, platform, folder_ext_id, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (p["name"], p["marketplace"], p["sku"],
                f"https://www.wildberries.ru/catalog/{p['sku']}/detail.aspx" if p["marketplace"] == "wb"
                else f"https://www.ozon.ru/product/{p['sku']}/",
                json.dumps(p["keywords"], ensure_ascii=False), "demo", p["key"], db.now()))
    for t, w in zip(demo.TASKS, demo.TASK_WEIGHTS):
        q, answers = demo.control_for(t)
        pid = db.one("SELECT id FROM projects WHERE folder_ext_id=?", (t[1],))["id"]
        kw = demo.project(t[1])["keywords"][0] if t[2] != "review_like" else None
        db.run("INSERT INTO tasks(project_id, platform, ext_id, name, kind, descr, need_for_report, price, "
               "need_screen, control_expected, min_seconds, keyword, per_day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (pid, "demo", t[0], t[3], t[2], t[3], q, t[4], 1, json.dumps(answers, ensure_ascii=False), t[6],
                kw, round(86400 / demo.STEP_SEC * w / sum(demo.TASK_WEIGHTS) * 1.05)))


def sync_tasks(p) -> list[dict]:
    folders = {r["folder_ext_id"]: r["id"] for r in db.q("SELECT id, folder_ext_id FROM projects WHERE platform=?", (p.name,))}
    for t in p.get_tasks():
        pid = folders.get(t.folder_ext_id)
        db.conn().execute(
            "INSERT INTO tasks(project_id, platform, ext_id, name, price, status, limit_total, done_total, check_total, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform, ext_id) DO UPDATE SET "
            "name=excluded.name, price=excluded.price, status=excluded.status, limit_total=excluded.limit_total, "
            "done_total=excluded.done_total, check_total=excluded.check_total, synced_at=excluded.synced_at, "
            "project_id=COALESCE(tasks.project_id, excluded.project_id)",
            (pid, p.name, t.ext_id, t.name, t.price, t.status, t.limit_total, t.done_total, t.check_total, db.now()))
    db.conn().commit()
    if config.AUTO_ATTACH and p.name != "demo":
        auto_attach(p.name)
    return db.q("SELECT * FROM tasks WHERE platform=? AND project_id IS NOT NULL AND service_id IS NULL", (p.name,))


AUTO_SCENARIO = ("Вероятный сценарий (текст задания площадка не отдаёт): найти товар поиском по ключевому запросу, открыть "
                 "карточку, добавить в корзину и в избранное. Если исполнитель пришёл к товару другим путём (например, через "
                 "карточку конкурента и блок «Похожие»), но один и тот же товар открыт, лежит в корзине и отмечен сердечком — "
                 "ставь unsure, а не bad: возможно, так написано в задании.")


def auto_attach(platform: str) -> int:
    """Задание, заведённое в кабинете руками, панель не может отнести к товару: API отдаёт только название, цену и
    счётчики. Раньше такие задания ждали, пока человек заведёт товар и привяжет их, — и панель после ввода ключа
    выглядела пустой. Теперь каждому заводится черновик товара «товар не определён»: отчёты сразу загружаются и
    проверяются, а артикул, название и запрос черновику подставляет агент по скринам (agent.learn_products)."""
    n = 0
    for t in db.q("SELECT id, ext_id FROM tasks WHERE platform=? AND project_id IS NULL AND service_id IS NULL", (platform,)):
        pid = db.run("INSERT INTO projects(name, marketplace, sku, product_url, keywords, platform, folder_ext_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (f"Задание № {t['ext_id']}: товар не определён", "wb", "", "", "[]",
                                                 platform, None, db.now()))
        db.run("UPDATE tasks SET project_id=?, descr=COALESCE(descr, ?) WHERE id=?", (pid, AUTO_SCENARIO, t["id"]))
        n += 1
    if n:
        db.log_action("auto_attach", platform, {"tasks": n})
    return n


ORDER_STATUS = {"Pending": "moderation", "In progress": "active", "Completed": "done",
                "Partial": "partial", "Canceled": "canceled"}


def sync_orders(p) -> int:
    """ТЗ-услуги (API v2): раз в сутки ставим заказ на per_day штук, пока не пройдёт days дней,
    и обновляем статусы уже поставленных заказов."""
    from datetime import date
    today, placed = date.today().isoformat(), 0
    for t in db.q("SELECT t.*, pr.product_url FROM tasks t JOIN projects pr ON pr.id=t.project_id "
                  "WHERE t.platform=? AND t.service_id IS NOT NULL AND t.status='active'", (p.name,)):
        n = db.one("SELECT COUNT(*) n FROM orders WHERE task_id=? AND ext_id IS NOT NULL", (t["id"],))["n"]
        if n >= (t["days"] or 0):
            continue
        if db.one("SELECT 1 FROM orders WHERE task_id=? AND date=?", (t["id"], today)):
            continue
        try:
            ext = p.order_add(t["service_id"], t["per_day"], t["product_url"])
            db.run("INSERT INTO orders(task_id, ext_id, date, quantity, status, remains) VALUES (?,?,?,?,?,?)",
                   (t["id"], ext, today, t["per_day"], "moderation", t["per_day"]))
            db.log_action("order_add", ext, {"task": t["name"], "quantity": t["per_day"]})
            placed += 1
        except Exception as e:            # нет денег, услуга выключена — пишем один раз в день
            db.run("INSERT OR IGNORE INTO orders(task_id, ext_id, date, quantity, status, remains) VALUES (?,?,?,?,?,?)",
                   (t["id"], None, today, 0, "error: " + str(e)[:200], 0))
            db.log_action("order_error", t["name"], str(e), ok=False)
    open_orders = db.q("SELECT ext_id FROM orders WHERE ext_id IS NOT NULL AND status NOT IN ('done','partial','canceled')")
    if open_orders:
        for ext, st in p.order_status([o["ext_id"] for o in open_orders]).items():
            db.conn().execute("UPDATE orders SET status=?, remains=?, charge=? WHERE ext_id=?",
                              (ORDER_STATUS.get(st["status"], st["status"]), st["remains"], st["charge"], ext))
        db.conn().commit()
    for t in db.q("SELECT id FROM tasks WHERE service_id IS NOT NULL"):
        db.run("UPDATE tasks SET done_total=(SELECT COALESCE(SUM(quantity-remains),0) FROM orders WHERE task_id=?) "
               "WHERE id=?", (t["id"], t["id"]))
    return placed


def sync_reports(p, tasks: list[dict]) -> int:
    by_ext = {t["ext_id"]: t for t in tasks}
    known = {r["ext_id"]: r for r in db.q(
        "SELECT ext_id, status, decision, files, text FROM reports WHERE platform=?", (p.name,))}
    new = 0
    c = db.conn()
    for r in p.get_reports(list(by_ext)):
        t = by_ext.get(r.task_ext_id)
        if not t:
            continue
        old = known.get(r.ext_id)
        if old is None:
            c.execute("INSERT INTO reports(platform, ext_id, task_id, project_id, worker_id, ip, status, price, text, "
                      "files, taken_at, submitted_at, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (p.name, r.ext_id, t["id"], t["project_id"], r.worker_id, r.ip, r.status, r.price, r.text,
                       json.dumps(r.files, ensure_ascii=False), r.taken_at, r.submitted_at, db.now()))
            new += 1
        elif old["status"] != r.status:
            # исполнитель прислал исправленный отчёт после доработки — проверяем заново
            recheck = r.status == "pending" and old["status"] in ("rework", "in_progress")
            c.execute("UPDATE reports SET status=?, text=?, files=?, submitted_at=COALESCE(?, submitted_at)"
                      + (", checked_at=NULL, verdict=NULL, decision=NULL, score=NULL, reasons='[]', "
                         "agent_batch=NULL, agent_verdict=NULL, agent_shadow=NULL" if recheck else "")
                      + " WHERE platform=? AND ext_id=?",
                      (r.status, r.text, json.dumps(r.files, ensure_ascii=False), r.submitted_at, p.name, r.ext_id))
        elif r.status == "pending" and old["decision"] is None and \
                (json.dumps(r.files, ensure_ascii=False) != (old["files"] or "[]") or (r.text or "") != (old["text"] or "")):
            # исполнитель дослал скрины вторым сообщением, статус тот же (на живых отчётах — через 4–30 с):
            # берём новую версию и проверяем заново, старый ответ агента к ней не относится
            c.execute("UPDATE reports SET text=?, files=?, submitted_at=COALESCE(?, submitted_at), checked_at=NULL, "
                      "verdict=NULL, score=NULL, reasons='[]', agent_batch=NULL, agent_verdict=NULL, agent_shadow=NULL "
                      "WHERE platform=? AND ext_id=? AND decision IS NULL AND status='pending'",
                      (r.text, json.dumps(r.files, ensure_ascii=False), r.submitted_at, p.name, r.ext_id))
    c.commit()
    return new


def apply_decision(report_id: int, action: str, by: str, comment: str | None = None) -> None:
    """approve / rework / reject -> вызов площадки + отметка в базе + журнал."""
    # сначала «занимаем» отчёт: два одновременных решения (кнопка и авто) не отправятся дважды
    cur = db.conn().execute("UPDATE reports SET status='deciding' WHERE id=? AND status='pending' "
                            "AND decision IS NULL", (report_id,))
    db.conn().commit()
    if cur.rowcount != 1:
        raise ValueError("Отчёт уже не ждёт проверки")
    r = db.one("SELECT * FROM reports WHERE id=?", (report_id,))
    p = platforms.get(r["platform"])
    try:
        if action == "approve":
            p.approve(r["ext_id"])
            status, decision = "paid", "approved"
        else:
            comment = comment or checks.comment_for(json.loads(r["reasons"] or "[]"))
            p.reject(r["ext_id"], comment, rework=(action == "rework"))
            status, decision = ("rework", "rework") if action == "rework" else ("rejected", "rejected")
    except Exception:
        db.run("UPDATE reports SET status='pending' WHERE id=?", (report_id,))
        raise
    db.run("UPDATE reports SET status=?, decision=?, decided_at=?, decided_by=? WHERE id=?",
           (status, decision, db.now(), by, report_id))
    if by == "человек":                    # материал для обучения проверки (learning.py)
        learning.record(r, action, comment)
    db.log_action(action, r["ext_id"], {"by": by, "worker": r["worker_id"], "comment": comment})


def apply_confident(limit: int = 3000) -> int:
    """Отправить в площадку все уверенные решения (approve/rework/reject), ручные не трогаем."""
    rows = db.q("SELECT id, verdict, worker_id, agent_batch, agent_verdict, reasons FROM reports WHERE status='pending' "
                "AND decision IS NULL AND verdict IN ('approve','rework','reject') ORDER BY submitted_at LIMIT ?", (limit,))
    trusted = agent.trusted_counts()
    n = 0
    for r in rows:
        if r["agent_batch"] and not r["agent_verdict"]:
            continue                       # отчёт у агента — ждём его ответа
        if r["verdict"] == "approve" and trusted.get(r["worker_id"], 0) < agent.NEW_WORKER_CHECKS:
            continue                       # новичок: деньги только после проверки агентом
        if r["verdict"] == "approve" and r["agent_verdict"] != "ok" and agent.needs_eyes(r["reasons"]):
            continue                       # нет контрольного ответа: платим только после взгляда на скрины

        try:
            apply_decision(r["id"], r["verdict"], "auto")
            n += 1
        except Exception as e:
            db.log_action("apply_error", str(r["id"]), str(e), ok=False)
    return n


def status() -> dict:
    """Итог последней синхронизации. Хранится и в базе: на хостинге каждый запрос — новый процесс."""
    if last_sync["at"] is None:
        try:
            saved = json.loads(db.get_setting("last_sync", "") or "{}")
        except ValueError:
            saved = {}
        return {**last_sync, **saved, "running": _alive(_lockfile(), LOCK_TTL)}
    return {**last_sync, "running": _alive(_lockfile(), LOCK_TTL)}


LOCK_TTL = 1800       # синхронизация дольше получаса — зависла: замок считается брошенным


def _lockfile():
    return config.DB_PATH.with_name(config.DB_PATH.name + ".sync.lock")


def kickfile():
    return config.DB_PATH.with_name(config.DB_PATH.name + ".sync.kick")


def _alive(f, ttl: float) -> bool:
    try:
        return time.time() - f.stat().st_mtime < ttl
    except OSError:
        return False


def claim(f, ttl: float) -> bool:
    """Занять файл-замок атомарно: создаёт его только если его нет (O_EXCL) — два процесса не пройдут вместе.
    Брошенный замок (старше ttl) снимается, и берётся одна повторная попытка. Не вышло — False."""
    f.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            if _alive(f, ttl):
                return False
            try:
                f.unlink()
            except OSError:
                return False
        except OSError:
            return False
    return False


def run_once() -> dict:
    if not _lock.acquire(blocking=False):
        return last_sync
    lf = _lockfile()
    if not claim(lf, LOCK_TTL):             # между процессами (cron, кнопка «Обновить») — файл-замок; нет замка — не работаем
        _lock.release()
        return status()
    t0 = time.time()
    try:
        p = platforms.get(platforms.default_name())
        try:                                 # баланс запоминаем здесь: страница его только читает и не ждёт площадку
            db.set_setting("balance", json.dumps({**p.balance(), "at": db.now()}))
        except Exception as e:
            db.set_setting("balance", json.dumps({"error": str(e)[:200], "at": db.now()}))
        tasks = sync_tasks(p)
        new = sync_reports(p, tasks)
        sync_orders(p)
        checked = checks.check_pending(p, vision=vision.make_checker(p))
        agent.import_results()               # агент мог дописать verdicts.json между циклами
        marketplaces.import_agent_results()  # и позиции из выдачи
        applied = apply_confident() if db.get_setting("decision_mode", config.DECISION_MODE) == "auto" else 0
        from datetime import date
        last = db.one("SELECT MAX(date) d FROM positions")["d"]
        if not last or last < date.today().isoformat():     # позиции — раз в сутки, независимо от отчётов
            marketplaces.refresh(config.DEMO)
        last_sync.update(at=db.now(), ok_at=db.now(), new=new, checked=checked, applied=applied, error=None,
                         seconds=round(time.time() - t0, 1))
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if any(x in msg for x in ("ConnectTimeout", "Max retries", "ConnectionError", "timed out", "Read timed out")):
            msg = "площадка не отвечает этому серверу (нет связи). Показаны данные последней удачной загрузки"
        try:                                  # время последней удачной синхронизации не теряем
            prev = json.loads(db.get_setting("last_sync", "") or "{}")
        except Exception:
            prev = {}
        last_sync.update(at=db.now(), ok_at=last_sync.get("ok_at") or prev.get("ok_at") or (prev.get("at") if not prev.get("error") else None),
                         error=msg[:300])
        db.log_action("sync_error", "", str(e)[:500], ok=False)
    finally:
        try:
            db.set_setting("last_sync", json.dumps(last_sync, ensure_ascii=False))
        except Exception:
            pass
        try:
            lf.unlink()
        except OSError:
            pass
        _lock.release()
    return last_sync


def loop():
    while True:
        run_once()
        time.sleep(config.SYNC_INTERVAL_SEC if not config.DEMO else 60)
