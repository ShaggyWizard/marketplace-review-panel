"""Проверка отчётов — главная часть панели.

Каждый отчёт проходит набор правил. Правило возвращает находку:
  hard — точно брак, решение принимается сразу (на доработку или отклонить);
  soft — подозрение, снимает баллы; несколько подозрений отправляют отчёт человеку.
Итог: балл 0–100 и вердикт approve / rework / reject / manual.

Принцип: автоматически принимаем только чистые отчёты, автоматически бракуем
только при доказательстве (чужой скрин, неверный контрольный ответ, нет скрина).
Всё спорное — человеку, но уже отсортированное и с причинами.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db, geo, images, learning

# --- нормализация текста ---
_num_space = re.compile(r"(?<=\d)[\s  .,](?=\d{3}\b)")


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return _num_space.sub("", s)


def tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-zа-я0-9]+", norm(s)))


FILLER = {"готово", "сделано", "сделала", "сделал", "ок", "все", "выполнено", "done", "ok"}


def strip_answers(text: str, answers: list[str]) -> str:
    t = norm(text)
    for a in answers:
        t = t.replace(norm(a), "")
    return re.sub(r"[^a-zа-я]+", " ", t).strip()


def _dt(s):
    return datetime.fromisoformat(s) if s else None


class Context:
    """Всё, что нужно правилам о «соседях» отчёта: считается один раз на прогон."""

    def __init__(self):
        since7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        since1 = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.ip_workers = defaultdict(set)
        for r in db.q("SELECT ip, worker_id FROM reports WHERE ip<>'' AND submitted_at>=?", (since7,)):
            self.ip_workers[r["ip"]].add(r["worker_id"])
        self.worker_day = {(r["project_id"], r["worker_id"]): r["n"] for r in db.q(
            "SELECT project_id, worker_id, COUNT(*) n FROM reports WHERE submitted_at>=? "
            "GROUP BY project_id, worker_id", (since1,))}
        self.project_day = {r["project_id"]: r["n"] for r in db.q(
            "SELECT project_id, COUNT(*) n FROM reports WHERE submitted_at>=? GROUP BY project_id", (since1,))}
        self.worker_tasks = {r["worker_id"]: r["n"] for r in db.q(
            "SELECT worker_id, COUNT(DISTINCT task_id) n FROM reports WHERE submitted_at>=? GROUP BY worker_id", (since1,))}
        self.hist = {r["worker_id"]: (r["total"], r["bad"]) for r in db.q(
            "SELECT worker_id, COUNT(*) total, SUM(verdict IN ('reject','rework')) bad "
            "FROM reports WHERE verdict IS NOT NULL GROUP BY worker_id")}
        self.blacklisted = {r["worker_id"] for r in db.q("SELECT worker_id FROM workers WHERE blacklisted=1")}
        self.overrides = learning.overrides()     # поправки к весам, принятые по разбору решений человека
        self.sha = {}
        self.near = images.NearIndex()
        # «оригинал» — самый ранний по времени сдачи, а не первый попавшийся (ревью Codex 24.11)
        for r in db.q("SELECT i.report_id, i.sha, i.dhash, i.worker_id FROM images i "
                      "LEFT JOIN reports r ON r.id=i.report_id ORDER BY r.submitted_at"):
            self.sha.setdefault(r["sha"], (r["report_id"], r["worker_id"]))
            if r["dhash"]:
                self.near.add(int(r["dhash"], 16), r["report_id"], r["worker_id"])
        self.texts = defaultdict(set)     # (task_id, текст без ответа) -> исполнители
        for r in db.q("SELECT r.task_id, r.worker_id, r.text, t.control_expected FROM reports r "
                      "JOIN tasks t ON t.id=r.task_id WHERE r.submitted_at>=? AND r.text<>''", (since7,)):
            key = strip_answers(r["text"], json.loads(r["control_expected"] or "[]"))
            if len(key) >= 12:
                self.texts[(r["task_id"], key)].add(r["worker_id"])


def F(code, kind, weight, text, action=None):
    return {"code": code, "kind": kind, "weight": weight, "text": text, "action": action}


# --- правила ---
def rule_blacklist(r, t, ctx, shots):
    if r["worker_id"] in ctx.blacklisted:
        return [F("blacklist", "hard", 100, "Исполнитель в чёрном списке", "reject")]
    return []


def rule_screenshot(r, t, ctx, shots):
    if t["need_screen"] and not json.loads(r["files"] or "[]"):
        return [F("no_screen", "hard", 60, "Нет скриншота, а он обязателен", "rework")]
    return []


def rule_control(r, t, ctx, shots):
    expected = json.loads(t["control_expected"] or "[]")
    if not expected:
        return [F("no_control", "soft", 10, "У задания нет контрольного вопроса — проверка только по косвенным признакам")]
    toks, text = tokens(r["text"]), norm(r["text"])
    for a in expected:
        a = norm(a).strip()
        if a in toks or (" " in a and a in text):      # ответ из нескольких слов — ищем фразой
            return []
    # цена у каждого покупателя своя (личная скидка СПП), поэтому для цены — допуск:
    # принимаем число от 60% до 105% от цены в карточке
    prices = [int(a) for a in expected if a.isdigit() and int(a) >= 100]
    if prices and t.get("kind") == "search_cart":
        nums = [int(x) for x in re.findall(r"\d+", text)]
        if any(0.6 * p <= n <= 1.05 * p for p in prices for n in nums):
            return []
    if not (toks - FILLER):
        return [F("control_missing", "hard", 60, "Нет ответа на контрольный вопрос", "rework")]
    return [F("control_wrong", "hard", 70,
              f"Неверный контрольный ответ (ждали: {', '.join(expected)})", "rework")]


def rule_dup_image(r, t, ctx, shots):
    out = []
    files = json.loads(r["files"] or "[]")
    if files and len(shots) < len(files):     # не скачался хотя бы один — этап без доказательства
        out.append(F("file_unavailable", "soft", 30, f"Не скачалось скринов: {len(files) - len(shots)} из {len(files)} — "
                                                    "доказательства нет, нужен человек"))
    for _url, sha, h in shots:
        hit = ctx.sha.get(sha)
        if hit and hit[0] != r["id"]:
            if hit[1] == r["worker_id"]:      # свой старый скрин к новому отчёту — вернуть, не отклонять
                out.append(F("dup_exact", "hard", 90, f"Скриншот повторяет свой же отчёт #{hit[0]}", "rework"))
            else:
                out.append(F("dup_exact", "hard", 90, f"Скриншот — точная копия из отчёта #{hit[0]} (исполнитель {hit[1]})", "reject"))
            continue
        if h is not None:
            near = [x for x in ctx.near.find(h, config.NEAR_DUP_HAMMING, r["id"]) if x[2] != r["worker_id"]]
            if near:
                dist, rid, wid = near[0]
                out.append(F("dup_near", "soft", 45,
                             f"Скриншот почти совпадает с отчётом #{rid} исполнителя {wid} (отличие {dist} из 256 точек)"))
    return out


def rule_dup_text(r, t, ctx, shots):
    key = strip_answers(r["text"], json.loads(t["control_expected"] or "[]"))
    if len(key) < 12:
        return []
    others = ctx.texts.get((r["task_id"], key), set()) - {r["worker_id"]}
    if 2 <= len(others) <= 20:      # больше 20 человек — это просто ходовая фраза
        return [F("dup_text", "soft", 15, f"Тот же текст у ещё {len(others)} исполнителей")]
    return []


def rule_shared_ip(r, t, ctx, shots):
    n = len(ctx.ip_workers.get(r["ip"], ()))
    if r["ip"] and n >= 3:
        return [F("shared_ip", "soft", 25, f"С этого IP работают {n} аккаунтов — похоже на ферму")]
    return []


def rule_too_fast(r, t, ctx, shots):
    a, b = _dt(r["taken_at"] or r["first_seen_at"]), _dt(r["submitted_at"])
    if a and b and t["min_seconds"]:
        sec = (b - a).total_seconds()
        if 0 <= sec < t["min_seconds"]:
            return [F("too_fast", "soft", 20, f"Сделано за {int(sec)} с при норме от {t['min_seconds']} с")]
    return []


def rule_concentration(r, t, ctx, shots):
    n = ctx.worker_day.get((r["project_id"], r["worker_id"]), 0)
    total = ctx.project_day.get(r["project_id"], 0)
    cap = max(config.WORKER_DAY_MAX, int(total * config.WORKER_DAY_SHARE))
    out = []
    if n > cap:
        out.append(F("concentration", "soft", 10,
                     f"Исполнитель сделал {n} отчётов по проекту за сутки ({n * 100 // max(total, 1)}% всех) при норме до {cap}"))
    k = ctx.worker_tasks.get(r["worker_id"], 0)
    if k > config.WORKER_TASKS_DAY:
        # считаются только задания, привязанные к товарам: отчёты непривязанных панель не загружает
        out.append(F("many_tasks", "soft", 0 if out else 10,
                     f"Исполнитель за сутки прошёл {k} разных заданий кабинета при норме до {config.WORKER_TASKS_DAY}"))
    return out


def rule_geo(r, t, ctx, shots):
    cc = geo.country(r["ip"])
    if cc and cc not in config.GEO_HOME:
        return [F("foreign_ip", "soft", config.GEO_WEIGHT,
                  f"IP исполнителя — {geo.name(cc)}, а товар продаётся в России")]
    return []


def rule_history(r, t, ctx, shots):
    total, bad = ctx.hist.get(r["worker_id"], (0, 0))
    if total >= 5 and bad / total >= 0.3:
        return [F("bad_history", "soft", 20, f"Плохая история: {bad} из {total} отчётов в браке")]
    return []


def rule_stages(r, t, ctx, shots):
    """ТЗ со скринами этапов (поиск с фильтром, карточка, корзина, избранное): меньше скринов — этап пропущен.
    На реальных отчётах заказчика встречается «Done» и 2 скрина вместо 4."""
    need, n = t.get("stages") or 0, len(json.loads(r["files"] or "[]"))
    if need and 0 < n < need:
        return [F("few_stages", "soft", 25, f"Скринов {n} из {need} этапов — какой-то этап не подтверждён")]
    return []


_WB_CARD = re.compile(r"wildberries\.\w+/catalog/(\d{5,})/detail")
_WB_SEARCH = re.compile(r"wildberries\.\w+/catalog/0/search\.aspx\?[^\s]*?search=([^&\s]+)")


def rule_links(r, t, ctx, shots):
    """Исполнители часто вставляют в отчёт ссылки на поиск и на карточку. Их проверяем без картинок:
    карточка не наша — брак с доказательством; запрос другой — подозрение."""
    from urllib.parse import unquote_plus
    text, out = r["text"] or "", []
    sku = str(t.get("sku") or "")
    cards = set(_WB_CARD.findall(text))
    if sku and cards:
        if sku in cards:
            out.append(F("link_ok", "soft", -10, "В отчёте ссылка на нашу карточку"))
        else:
            out.append(F("link_wrong", "hard", 70, f"Ссылка в отчёте на другой товар (артикул {', '.join(sorted(cards))})", "rework"))
    kw = norm(t.get("keyword") or "")
    for q in _WB_SEARCH.findall(text):
        q = norm(unquote_plus(q))
        if kw and tokens(q) != tokens(kw):
            out.append(F("query_wrong", "soft", 20, f"Искал по другому запросу: «{q[:60]}» вместо «{kw}»"))
            break
    return out


_NOT_FOUND = re.compile(r"не\s+(отобраз|наш[её]л|нашл|находит|найден|выпал|выда[её]т)|нет\s+в\s+(поиске|выдаче|результатах)|"
                        r"(цена|стоимость)\s+(изменил|другая|не\s+совпад)|изменил(ась|ся)\s+ли\s+цена", re.I)


def rule_not_found(r, t, ctx, shots):
    """Исполнитель сам пишет, что товара нет в выдаче по фильтру цены. На живых отчётах (24.11): «ваш товар не
    отобразился в результатах поиска в указанном ценовом диапазоне. Изменилась ли цена?» — цена на площадке ушла
    из диапазона ТЗ, задание впустую жжёт лимит. Отчёт — человеку, а в сводке — предупреждение по заданию."""
    if _NOT_FOUND.search(r["text"] or ""):
        return [F("not_found_msg", "soft", 30, "Исполнитель пишет, что товар не находится по запросу или фильтру цены")]
    return []


RULES = [rule_blacklist, rule_screenshot, rule_control, rule_dup_image, rule_stages, rule_links, rule_not_found,
         rule_dup_text, rule_shared_ip, rule_geo, rule_too_fast, rule_concentration, rule_history]

RULE_NAMES = {
    "blacklist": "Чёрный список", "no_screen": "Нет скриншота", "control_missing": "Нет контрольного ответа",
    "control_wrong": "Неверный контрольный ответ", "dup_exact": "Чужой скриншот (копия)",
    "dup_near": "Похожий скриншот", "dup_text": "Одинаковый текст", "shared_ip": "Общий IP",
    "too_fast": "Слишком быстро", "concentration": "Много заданий у одного", "bad_history": "Плохая история",
    "no_control": "Нет контрольного вопроса", "vision_fail": "Скрин не подтверждает действие",
    "vision_ok": "Скрин подтверждён ИИ", "agent_ok": "Агент: скрин подтверждён",
    "agent_bad": "Агент: скрин не подтверждает", "agent_unsure": "Агент не уверен",
    "file_unavailable": "Скрин не скачался",
    "few_stages": "Не все этапы на скринах", "link_ok": "Ссылка на наш товар",
    "link_wrong": "Ссылка на чужой товар", "query_wrong": "Другой поисковый запрос",
    "not_found_msg": "Исполнитель не нашёл товар", "foreign_ip": "IP не из России", "many_tasks": "Много разных заданий за сутки",
}


def decide(findings: list[dict]) -> tuple[int, str]:
    score = min(100, max(0, 100 - sum(f["weight"] for f in findings)))
    hard = [f for f in findings if f["kind"] == "hard"]
    if hard:
        return score, "reject" if any(f["action"] == "reject" for f in hard) else "rework"
    if score >= config.AUTO_APPROVE_SCORE:
        return score, "approve"
    return score, "manual"


def comment_for(findings: list[dict]) -> str:
    """Текст исполнителю при возврате: что исправить, без внутренних деталей."""
    msgs = {"no_screen": "приложите скриншот, на котором видно выполненное действие",
            "control_missing": "ответьте на контрольный вопрос из задания",
            "control_wrong": "ответ на контрольный вопрос неверный — откройте товар и проверьте",
            "dup_exact": "скриншот не ваш — отчёт отклонён",
            "link_wrong": "в отчёте ссылка на другой товар — найдите товар с фото из задания",
            "blacklist": "доступ к заданиям закрыт"}
    parts = [msgs[f["code"]] for f in findings if f["code"] in msgs]
    return ("Пожалуйста, " + "; ".join(parts) + ".") if parts else "Отчёт не соответствует заданию."


def hash_report_images(platform, r: dict, ctx: Context) -> list[tuple[str, str, int | None]]:
    """(url, sha, dhash) только по успешно скачанным файлам — url едет вместе с хешем."""
    shots = []
    for url in json.loads(r["files"] or "[]"):
        row = db.one("SELECT sha, dhash FROM images WHERE report_id=? AND url=?", (r["id"], url))
        if row:
            shots.append((url, row["sha"], int(row["dhash"], 16) if row["dhash"] else None))
            continue
        try:
            data = images.cached_fetch(platform, url)
        except Exception as e:
            db.log_action("file_error", str(r["id"]), f"{url}: {e}", ok=False)
            continue
        sha, h = images.fingerprint(data)
        shots.append((url, sha, h))
    return shots


def remember_images(r: dict, shots, ctx: Context):
    for url, sha, h in shots:
        db.conn().execute("INSERT OR IGNORE INTO images(report_id, url, sha, dhash, worker_id) VALUES (?,?,?,?,?)",
                          (r["id"], url, sha, f"{h:064x}" if h is not None else None, r["worker_id"]))
        ctx.sha.setdefault(sha, (r["id"], r["worker_id"]))
        if h is not None:
            ctx.near.add(h, r["id"], r["worker_id"])


def check_report(platform, r: dict, t: dict, ctx: Context, vision=None) -> dict:
    shots = hash_report_images(platform, r, ctx)
    findings = []
    for rule in RULES:
        findings += rule(r, t, ctx, shots)
    learning.apply_overrides(findings, ctx.overrides)
    score, verdict = decide(findings)
    if verdict == "manual" and vision is not None and shots:
        extra = vision(r, t)
        if extra:
            findings.append(extra)
            score, verdict = decide(findings)
    remember_images(r, shots, ctx)
    key = strip_answers(r["text"], json.loads(t["control_expected"] or "[]"))
    if len(key) >= 12:
        ctx.texts[(r["task_id"], key)].add(r["worker_id"])
    return {"score": score, "verdict": verdict, "findings": findings}


def backfill_images(platform, hours: int = 48, limit: int = 3000, budget: int | None = None) -> int:
    """Отпечатки уже оплаченных отчётов тоже нужны: чужой скрин часто берут из старого
    принятого отчёта. Догоняем их порциями, чтобы не тормозить проверку новых."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = db.q("SELECT r.id, r.worker_id, r.files FROM reports r WHERE r.platform=? AND r.submitted_at>=? "
                "AND r.files<>'[]' AND r.status<>'pending' AND NOT EXISTS (SELECT 1 FROM images i WHERE i.report_id=r.id) "
                "ORDER BY r.submitted_at DESC LIMIT ?", (platform.name, since, limit))
    left = budget if budget is not None else (images.FETCH_BUDGET if platform.name != "demo" else 10 ** 9)
    for r in rows:
        need = [u for u in json.loads(r["files"]) if not images.is_cached(u)]
        if len(need) > left:              # лимит скачиваний на цикл исчерпан — остальное в следующий раз
            break
        left -= images.prefetch(platform, need)
        for url in json.loads(r["files"]):
            try:
                sha, h = images.fingerprint(images.cached_fetch(platform, url))
            except Exception:
                continue
            db.conn().execute("INSERT OR IGNORE INTO images(report_id, url, sha, dhash, worker_id) VALUES (?,?,?,?,?)",
                              (r["id"], url, sha, f"{h:064x}" if h is not None else None, r["worker_id"]))
    db.conn().commit()
    return len(rows)


def check_pending(platform, limit: int = 5000, vision=None) -> int:
    """Проверить все новые отчёты, ожидающие проверки. Старые — первыми:
    тогда «оригинал» уже в индексе, когда приходит копия."""
    rows = db.q(
        "SELECT r.*, t.need_screen, t.control_expected, t.min_seconds, t.kind, t.stages, t.keyword, p.sku "
        "FROM reports r JOIN tasks t ON t.id=r.task_id LEFT JOIN projects p ON p.id=t.project_id WHERE r.status='pending' AND r.checked_at IS NULL "
        "AND r.platform=? ORDER BY r.submitted_at LIMIT ?", (platform.name, limit))
    if not rows:
        return 0
    # Скрины качаем по одному и не больше лимита за цикл; отчёт проверяется, только когда все его файлы на месте.
    # Не успели — он останется непроверенным до следующего цикла (а не получит «скрин не скачался»).
    left, ready = (images.FETCH_BUDGET if platform.name != "demo" else 10 ** 9), []
    for r in rows:
        need = [u for u in json.loads(r["files"] or "[]") if not images.is_cached(u)]
        if len(need) > left:
            break
        left -= images.prefetch(platform, need)
        ready.append(r)
    rows = ready
    backfill_images(platform, budget=left)
    ctx = Context()
    for r in rows:
        res = check_report(platform, r, r, ctx, vision)
        db.conn().execute(
            "UPDATE reports SET score=?, verdict=?, reasons=?, checked_at=? WHERE id=?",
            (res["score"], res["verdict"], json.dumps(res["findings"], ensure_ascii=False), db.now(), r["id"]))
    db.conn().commit()
    return len(rows)
