"""Проверка скриншотов агентом — так, чтобы не смотреть каждую картинку.

Воронка:
  1. Правила (checks.py) бесплатно решают ~90% отчётов: чистые — принять,
     доказанный брак (чужой скрин, неверный контрольный ответ, нет скрина) — вернуть.
  2. Агенту уходит только серая зона + 3% случайных «принятых» для выборочного контроля
     (так видно, не пропускают ли правила брак).
  3. Скриншоты склеиваются в листы 4×3: агент читает 12 отчётов за одну картинку,
     а не 12 картинок. Это в ~6 раз меньше токенов на изображения.
  4. Одинаковые скрины (по отпечатку) агенту не отправляются — их уже решили правила.
  5. Человеку остаётся только то, где агент написал «не уверен».

Агент — любой: субагент Claude Code (.claude/agents/proverka-skrinov.md), Codex или вызов
API. Обмен через папку: панель кладёт batch-*/sheet-NN.png + tasks.json, агент пишет
verdicts.json, панель забирает результат.
"""
import io
import json
import re
from collections import Counter, defaultdict
import os
import random
from datetime import datetime, timezone

from PIL import Image, ImageDraw

from . import checks, config, db, images, learning, platforms

# У каждой базы свои пачки: иначе после ввода ключа в списке висят пачки из демо
AGENT_DIR = config.DB_PATH.parent / ("agent" if config.DB_PATH.stem == "demo" else f"agent-{config.DB_PATH.stem}")
BIG_TILE = (1500, 1400)     # лист «один отчёт»: для отчётов из нескольких полноэкранных скринов
# Размер листа. 4×3 проверено на демо; для мелкого текста реальных скринов можно
# уменьшить до 3×2 (крупнее плитки, дороже) — переменные AGENT_SHEET_COLS / AGENT_SHEET_ROWS.
COLS = int(os.getenv("AGENT_SHEET_COLS", "4"))
ROWS = int(os.getenv("AGENT_SHEET_ROWS", "3"))
TILE_W, TILE_H = (270, 480) if COLS * ROWS >= 9 else (400, 710)
# Живые отчёты — 3–5 полноэкранных скринов с ПК: в плитке 270 px запрос и цену не прочитать.
# Для них: AGENT_SHEET_COLS=1 AGENT_SHEET_ROWS=1 AGENT_TILE=1500x1400 (один отчёт на лист).
if os.getenv("AGENT_TILE"):
    TILE_W, TILE_H = (int(x) for x in os.getenv("AGENT_TILE").lower().split("x"))
PER_SHEET = COLS * ROWS
SPOT_SHARE = 0.03
# Сомнения, которые снимает хороший скрин: только видимое на картинке. Скорость, одинаковый
# текст, концентрацию, копию скрина, ферму на IP и плохую историю картинка не опровергает —
# такие отчёты агенту не шлём, их смотрит человек (замечание ревью Codex 23.11).
AGENT_CAN_RESOLVE = learning.BASE_AGENT    # + причины, отданные агенту по разбору (learning.agent_resolvable)
SHADOW_SHARE = 0.1     # доля пачки — спорные отчёты «в тени»: решает человек, ответ агента только сравниваем
VERDICTS = {"ok", "bad", "unsure"}
# Отчёты заданий без контрольного ответа не оплачиваются, пока скрины не посмотрел агент или человек
BLIND_TO_AGENT = os.getenv("BLIND_TO_AGENT", "1") == "1"
NEW_WORKER_CHECKS = 3      # столько первых отчётов исполнителя агент смотрит всегда


def _tile(datas: list, n: int, TILE_W: int = TILE_W, TILE_H: int = TILE_H) -> Image.Image:
    """Плитка одного отчёта. Один скрин — во всю плитку; 2–4 скрина этапов — сеткой 2×2 с номерами этапов,
    чтобы агент видел все доказательства, а не только первый файл (ревью Codex 24.11)."""
    tile = Image.new("RGB", (TILE_W, TILE_H + 34), "white")
    from .platforms.demo import F
    d = ImageDraw.Draw(tile)
    shown = 0
    # на живых отчётах 5 скринов у каждого пятого (24.11) — поэтому до 6: сетка 3×2
    cols = 1 if len(datas) <= 1 else 2 if len(datas) <= 4 else 3
    rws = 1 if len(datas) <= 1 else 2
    cells = [((i % cols) * TILE_W // cols, (i // cols) * TILE_H // rws, TILE_W // cols, TILE_H // rws)
             for i in range(cols * rws)]
    for k, (data, (x, y, w, h)) in enumerate(zip(datas[:6], cells)):
        if not data:
            continue
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((w - 4, h - 4))
            tile.paste(im, (x + (w - im.width) // 2, 34 + y + (h - im.height) // 2))
            shown += 1
            if len(datas) > 1:
                d.text((x + 4, 36 + y), str(k + 1), font=F(16), fill=(220, 0, 0))
        except Exception:              # не картинка (pdf, битый файл) — пусть смотрит человек
            pass
    d.rectangle([0, 0, TILE_W, 32], fill=(20, 20, 20))
    d.text((10, 4), f"№ {n}" + (f"  ({len(datas)} скр.)" if len(datas) > 1 else ""), font=F(22), fill=(255, 220, 0))
    if not shown:
        d.text((20, 200), "нет скриншота", font=F(18), fill=(200, 0, 0))
    return tile


def needs_eyes(reasons) -> bool:
    """Отчёт, который правила одобрили «вслепую»: контрольного ответа у задания нет."""
    if isinstance(reasons, str):
        reasons = json.loads(reasons or "[]")
    return BLIND_TO_AGENT and any(f.get("code") == "no_control" for f in reasons)


def make_batch(limit: int = 240) -> dict | None:
    """Собрать пачку для агента. Три источника, по убыванию важности:
    1) серая зона — правила не смогли решить;
    1а) «слепые» — у задания нет контрольного ответа, всё доказательство на скринах: правилам
        там не за что зацепиться (на живых отчётах 24.11 они одобрили 443 из 449), поэтому такие
        отчёты агент смотрит все, а не выборочно;
    2) новички — первые NEW_WORKER_CHECKS отчётов каждого исполнителя, даже если правила
       их приняли: доверие к человеку зарабатывается, дальше его смотрим только выборочно;
    3) случайные 3% принятых — контроль того, что правила не пропускают брак."""
    can = learning.agent_resolvable()
    manual = db.q("SELECT r.id, r.reasons FROM reports r WHERE r.status='pending' AND r.verdict='manual' "
                  "AND r.agent_batch IS NULL AND r.agent_shadow IS NULL AND r.files<>'[]' ORDER BY r.submitted_at")
    gray, other = [], []
    for r in manual:
        # «слепой» отчёт (у задания нет контрольного ответа) без взгляда на скрины не решить вообще —
        # его агент смотрит по-настоящему, а не «в тени», какие бы ещё мягкие причины на нём ни висели
        (gray if {f["code"] for f in json.loads(r["reasons"]) if f["weight"] > 0} <= can or needs_eyes(r["reasons"])
         else other).append(r)
    gray = gray[:int(limit * 0.6)]
    # «тень»: часть спорных, которые агенту пока не доверены. Решает человек, а по совпадению
    # ответов learning.py предлагает отдать такую причину агенту — так агент становится самостоятельнее.
    shadow = random.sample(other, min(len(other), int(limit * SHADOW_SHARE)))
    limit -= len(shadow)
    approved = db.q("SELECT r.id, r.worker_id, r.reasons FROM reports r WHERE r.status='pending' AND r.verdict='approve' "
                    "AND r.decision IS NULL AND r.agent_batch IS NULL AND r.files<>'[]' ORDER BY r.submitted_at")
    blind = [r for r in approved if needs_eyes(r["reasons"])][:max(0, limit - len(gray))]
    blind_ids = {r["id"] for r in blind}
    approved = [r for r in approved if r["id"] not in blind_ids]
    checked = trusted_counts()
    newbies, seen = [], set()
    room = limit - len(gray) - len(blind)
    for r in approved:
        if len(newbies) >= int(room * 0.7):
            break
        if checked.get(r["worker_id"], 0) < NEW_WORKER_CHECKS and r["worker_id"] not in seen:
            newbies.append(r)
            seen.add(r["worker_id"])
    rest = [r for r in approved if r["worker_id"] not in seen]
    k = min(len(rest), max(1, int(len(approved) * SPOT_SHARE)), room - len(newbies))
    spot = newbies + random.sample(rest, max(0, k))
    ids = [r["id"] for r in gray] + [r["id"] for r in blind] + [r["id"] for r in spot] + [r["id"] for r in shadow]
    if not ids:
        return None
    return write_batch(ids, {r["id"] for r in spot}, {r["id"] for r in newbies},
                       {"gray": len(gray), "blind": len(blind), "new_workers": len(newbies), "spot": len(spot) - len(newbies),
                        "shadow": len(shadow)}, shadow_ids={r["id"] for r in shadow})


def write_batch(ids: list[int], spot_ids: set, new_ids: set, meta: dict, prefix: str = "batch-",
                shadow_ids: set = frozenset()) -> dict:
    """Склеить скрины в листы 4×3 и положить описание каждого номера в tasks.json."""
    name = prefix + datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = AGENT_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    # Сначала занимаем отчёты за пачкой, потом клеим листы: иначе автооплата успевает пройти, пока пачка
    # собирается, а две пачки берут один отчёт (ревью Codex 24.11). Берём только ещё свободные.
    c = db.conn()
    for i in ids:
        if i in shadow_ids:
            c.execute("UPDATE reports SET agent_shadow=? WHERE id=? AND agent_shadow IS NULL", ("wait:" + name, i))
        else:
            c.execute("UPDATE reports SET agent_batch=? WHERE id=? AND agent_batch IS NULL AND decision IS NULL "
                      "AND status='pending'", (name, i))
    c.commit()
    ids = [r["id"] for r in db.q("SELECT id FROM reports WHERE agent_batch=? OR agent_shadow=? ORDER BY submitted_at, id",
                                 (name, "wait:" + name))]
    if not ids:
        folder.rmdir()
        return None
    p = platforms.get(platforms.default_name())
    rows = db.q(f"SELECT r.*, t.name task_name, t.need_for_report, t.control_expected, t.keyword, t.descr scenario, "
                f"p.name product, p.sku, "
                f"p.marketplace FROM reports r JOIN tasks t ON t.id=r.task_id JOIN projects p ON p.id=r.project_id "
                f"WHERE r.id IN ({','.join('?' * len(ids))})", ids)
    items = []
    # Раскладку выбираем по самим отчётам: один скрин на отчёт — 12 на лист; несколько полноэкранных скринов этапов
    # (так выглядят живые отчёты) — один отчёт на большой лист, иначе запрос и цену не прочитать.
    nfiles = [len(json.loads(r["files"] or "[]")) for r in rows]
    big = bool(rows) and not os.getenv("AGENT_TILE") and sum(nfiles) / len(rows) > 1.5
    COLS, ROWS = (1, 1) if big else (globals()["COLS"], globals()["ROWS"])
    TILE_W, TILE_H = BIG_TILE if big else (globals()["TILE_W"], globals()["TILE_H"])
    PER_SHEET = COLS * ROWS
    for s in range(0, len(rows), PER_SHEET):
        chunk = rows[s:s + PER_SHEET]
        sheet = Image.new("RGB", (COLS * (TILE_W + 6), ROWS * (TILE_H + 40)), (90, 90, 90))
        for k, r in enumerate(chunk):
            n = s + k + 1
            files = json.loads(r["files"])
            datas = []
            for f in files[:6]:
                try:
                    datas.append(images.cached_fetch(p, f))
                except Exception:
                    datas.append(None)
            sheet.paste(_tile(datas, n, TILE_W, TILE_H), ((k % COLS) * (TILE_W + 6), (k // COLS) * (TILE_H + 40)))
            items.append({
                "n": n, "report_id": r["id"], "sheet": f"sheet-{s // PER_SHEET + 1:02d}.png",
                "product": r["product"], "sku": r["sku"], "marketplace": r["marketplace"],
                "task": r["task_name"], "need_for_report": r["need_for_report"],
                # сколько скринов в отчёте и сколько из них реально попало на лист
                "files": len(files), "seen": sum(1 for d in datas if d),
                # сценарий и запрос: без них агент бракует честный отчёт «через карточку конкурента» (живые данные 24.11)
                "keyword": r["keyword"] or "", "scenario": (r["scenario"] or "")[:1500],
                "expected_answer": json.loads(r["control_expected"] or "[]"),
                "worker_text": r["text"],
                "rules_found": [f["text"] for f in json.loads(r["reasons"] or "[]") if f["weight"] > 0],
                "spot_check": r["id"] in spot_ids,
                "shadow": r["id"] in shadow_ids,
                "why_sent": "новый исполнитель" if r["id"] in new_ids else
                            "выборочный контроль" if r["id"] in spot_ids else
                            "спорный: решает человек, оцени как обычно" if r["id"] in shadow_ids else "правила не уверены",
            })
        sheet.save(folder / f"sheet-{s // PER_SHEET + 1:02d}.png", optimize=not big)
    (folder / "tasks.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    # случаи, где агент раньше разошёлся с человеком: агент читает их перед пачкой
    (folder / "examples.json").write_text(json.dumps(learning.agent_examples(), ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    res = {"name": name, "folder": str(folder), "reports": len(ids), **meta,
           "sheets": (len(rows) + PER_SHEET - 1) // PER_SHEET}
    db.log_action("agent_batch", name, res)
    return res


def import_results() -> dict:
    """Забрать verdicts.json из всех пачек, которые агент уже обработал."""
    done = {"batches": 0, "ok": 0, "bad": 0, "unsure": 0, "spot_bad": 0}
    if not AGENT_DIR.exists():
        return done
    for folder in sorted(AGENT_DIR.glob("*batch-*")):
        vf = folder / "verdicts.json"
        if not vf.exists() or (folder / ".imported").exists():
            continue
        items = {i["n"]: i for i in json.loads((folder / "tasks.json").read_text(encoding="utf-8"))}
        try:
            answers = json.loads(vf.read_text(encoding="utf-8"))
            assert isinstance(answers, list)
            answers = {int(v["n"]): v for v in answers}
            bad_rows = [n for n, v in answers.items() if n not in items or v.get("verdict") not in VERDICTS]
            assert not bad_rows, f"неизвестные номера или вердикты: {bad_rows[:5]}"
        except Exception as e:         # файл ещё пишется или испорчен — не трогаем, ждём исправления
            db.log_action("agent_bad_file", folder.name, str(e), ok=False)
            continue
        missing = [it["report_id"] for n, it in items.items() if n not in answers]
        if missing:                    # пропущенные агентом номера вернуть в очередь (и в тень тоже)
            db.conn().executemany("UPDATE reports SET agent_batch=NULL WHERE id=? AND agent_batch=?",
                                  [(i, folder.name) for i in missing])
            db.conn().executemany("UPDATE reports SET agent_shadow=NULL WHERE id=? AND agent_shadow=?",
                                  [(i, "wait:" + folder.name) for i in missing])
        for n, v in answers.items():
            it = items[n]
            verdict, reason = v["verdict"], str(v.get("reason", ""))[:300]
            # агент видел не все скрины (больше 6 или файл не открылся) — его «ok» не подтверждает невиденное
            if verdict == "ok" and it.get("files", 0) > it.get("seen", it.get("files", 0)):
                verdict, reason = "unsure", f"агент видел {it['seen']} из {it['files']} скринов; " + reason
            # сценария задания у агента не было — «не тот порядок действий» может быть нормой: решает человек
            if verdict == "bad" and "scenario" in it and not it["scenario"] and not it.get("expected_answer"):
                verdict, reason = "unsure", "сценарий задания не задан; агент: " + reason
            r = db.one("SELECT * FROM reports WHERE id=?", (it["report_id"],))
            if it.get("shadow"):           # в тени: на решение не влияет, только запоминаем для сравнения
                if r and r["agent_shadow"] == "wait:" + folder.name:
                    db.conn().execute("UPDATE reports SET agent_shadow=? WHERE id=?", (verdict, r["id"]))
                    db.conn().execute("UPDATE reviews SET agent_verdict=? WHERE report_id=? AND agent_verdict IS NULL",
                                      (verdict, r["id"]))       # человек мог решить раньше агента
                continue
            if not r or r["agent_batch"] != folder.name:
                continue               # отчёт пересдали после доработки — старый ответ агента не годится
            findings = json.loads(r["reasons"] or "[]")
            findings = [f for f in findings if not f["code"].startswith("agent_")]
            if verdict == "ok":
                # хороший скрин снимает только «картиночные» сомнения, не копию и не ферму
                lift = sum(f["weight"] for f in findings if f["code"] in learning.agent_resolvable())
                findings.append({"code": "agent_ok", "kind": "soft", "weight": -lift,
                                 "text": "Агент: " + (reason or "на скрине товар и действие"), "action": None})
            elif verdict == "bad":
                findings.append({"code": "agent_bad", "kind": "hard", "weight": 50,
                                 "text": "Агент: " + (reason or "скрин не подтверждает выполнение"), "action": "rework"})
                if it["spot_check"]:
                    done["spot_bad"] += 1
            else:
                findings.append({"code": "agent_unsure", "kind": "soft", "weight": 0,
                                 "text": "Агент не уверен: " + reason, "action": None})
            score, new_verdict = checks.decide(findings)
            if verdict == "unsure":
                new_verdict = "manual"
            if r["status"] == "pending" and r["decision"] is None:
                db.conn().execute("UPDATE reports SET reasons=?, score=?, verdict=?, agent_verdict=? WHERE id=?",
                                  (json.dumps(findings, ensure_ascii=False), score, new_verdict, verdict, r["id"]))
            else:   # уже оплачен — только запоминаем для статистики выборочного контроля
                db.conn().execute("UPDATE reports SET agent_verdict=? WHERE id=?", (verdict, r["id"]))
            done[verdict if verdict in ("ok", "bad") else "unsure"] += 1
        db.conn().commit()
        try:
            done["products"] = done.get("products", 0) + learn_products(items, answers)
        except Exception as e:             # подпись товаров — удобство, вердикты из-за неё не теряем
            db.log_action("learn_products_error", folder.name, str(e)[:300], ok=False)
        (folder / ".imported").write_text(datetime.now(timezone.utc).isoformat())
        done["batches"] += 1
        db.log_action("agent_import", folder.name, done)
    return done


def _top(values, need: int = 2, share: float = 0.5):
    """Самое частое значение, если его назвали хотя бы need раз и это не меньше доли share от всех ответов."""
    c = Counter(v for v in values if v)
    if not c:
        return None
    v, n = c.most_common(1)[0]
    return v if n >= need and n >= share * sum(c.values()) else None


def learn_products(items: dict, answers: dict) -> int:
    """Подписать черновики товаров по тому, что агент прочитал со скринов (артикул, запрос, название).
    Прочитанное сохраняется у отчёта (reports.agent_read), а решение принимается по всем отчётам задания, а не по
    одной пачке: в пачке на задание приходится два-три отчёта, и большинства там не набрать.
    Только для товаров без артикула — то есть заведённых автоматически; то, что ввёл человек, не трогаем.
    Артикул принимается, если его назвали минимум в двух отчётах задания и это большинство: одного отчёта мало,
    на скрине может оказаться чужой товар. Прочитанное агентом — данные с картинок исполнителей: в базу идут
    только цифры артикула и короткий текст."""
    from . import marketplaces
    tids = set()
    for n, v in answers.items():
        it = items.get(n)
        if not it:
            continue
        sku = re.sub(r"\D", "", str(v.get("sku") or ""))
        read = {"sku": sku if 6 <= len(sku) <= 12 else "",
                "query": re.sub(r"\s+", " ", str(v.get("query") or "")).strip().lower()[:80],
                "product": re.sub(r"\s+", " ", str(v.get("product") or "")).strip()[:80]}
        if any(read.values()):
            db.run("UPDATE reports SET agent_read=? WHERE id=?", (json.dumps(read, ensure_ascii=False), it["report_id"]))
        r = db.one("SELECT task_id FROM reports WHERE id=?", (it["report_id"],))
        if r:
            tids.add(r["task_id"])
    changed = 0
    for tid in tids:
        got = {"sku": [], "query": [], "product": []}
        for r in db.q("SELECT agent_read FROM reports WHERE task_id=? AND agent_read IS NOT NULL", (tid,)):
            try:
                read = json.loads(r["agent_read"])
            except ValueError:
                continue
            for k in got:
                got[k].append(read.get(k) or "")
        t = db.one("SELECT t.id, t.project_id, t.keyword, p.sku, p.keywords, p.marketplace FROM tasks t "
                   "JOIN projects p ON p.id=t.project_id WHERE t.id=?", (tid,))
        if not t:
            continue
        query = _top(got["query"])
        if query and not t["keyword"]:
            db.run("UPDATE tasks SET keyword=? WHERE id=?", (query, tid))
        if t["sku"]:                        # товар задан человеком или уже определён
            if query and query not in json.loads(t["keywords"] or "[]"):
                db.run("UPDATE projects SET keywords=? WHERE id=?",
                       (json.dumps(json.loads(t["keywords"] or "[]") + [query], ensure_ascii=False), t["project_id"]))
            continue
        sku = _top(got["sku"])
        if not sku:
            continue
        same = db.one("SELECT id, keywords FROM projects WHERE sku=? AND id<>?", (sku, t["project_id"]))
        if same:                            # товар уже есть — задание уходит к нему, пустой черновик удаляем
            db.run("UPDATE tasks SET project_id=? WHERE id=?", (same["id"], tid))
            db.run("UPDATE reports SET project_id=? WHERE task_id=?", (same["id"], tid))
            if not db.one("SELECT 1 FROM tasks WHERE project_id=?", (t["project_id"],)):
                db.run("DELETE FROM projects WHERE id=?", (t["project_id"],))
            kws = json.loads(same["keywords"] or "[]")
            if query and query not in kws:
                db.run("UPDATE projects SET keywords=? WHERE id=?", (json.dumps(kws + [query], ensure_ascii=False), same["id"]))
        else:
            name = marketplaces.wb_name(sku) or _top(got["product"], need=1, share=0) or f"Товар {sku}"
            db.run("UPDATE projects SET sku=?, name=?, product_url=?, keywords=? WHERE id=?",
                   (sku, name, f"https://www.wildberries.ru/catalog/{sku}/detail.aspx",
                    json.dumps([query] if query else [], ensure_ascii=False), t["project_id"]))
        db.log_action("product_found", sku, {"task": tid, "query": query})
        changed += 1
    return changed


def batches() -> list[dict]:
    out = []
    if not AGENT_DIR.exists():
        return out
    for folder in sorted(AGENT_DIR.glob("*batch-*"), reverse=True)[:10]:
        try:
            n = len(json.loads((folder / "tasks.json").read_text(encoding="utf-8")))
        except Exception:
            continue
        state = "забран" if (folder / ".imported").exists() else \
            "агент ответил" if (folder / "verdicts.json").exists() else "ждёт агента"
        out.append({"name": folder.name, "reports": n, "sheets": len(list(folder.glob("sheet-*.png"))),
                    "state": state, "folder": str(folder)})
    return out


def spot_stats() -> dict:
    """Выборочный контроль: сколько брака агент нашёл среди отчётов, которые правила приняли."""
    n = bad = 0
    for folder in AGENT_DIR.glob("batch-*") if AGENT_DIR.exists() else []:   # контрольные testbatch не считаем
        vf = folder / "verdicts.json"
        if not vf.exists():
            continue
        try:
            spot = {i["n"] for i in json.loads((folder / "tasks.json").read_text(encoding="utf-8")) if i["spot_check"]}
            answers = json.loads(vf.read_text(encoding="utf-8"))
        except Exception:
            continue                  # файл агента ещё пишется или испорчен
        for v in answers if isinstance(answers, list) else []:
            if isinstance(v, dict) and str(v.get("n", "")).isdigit() and int(v["n"]) in spot:
                n += 1
                bad += v.get("verdict") == "bad"
    return {"checked": n, "bad": bad}


def trusted_counts() -> dict[str, int]:
    """Сколько отчётов исполнителя агент уже реально проверил и принял (а не просто получил в пачку)."""
    return {r["worker_id"]: r["n"] for r in db.q(
        "SELECT worker_id, COUNT(*) n FROM reports WHERE agent_verdict='ok' GROUP BY worker_id")}
