"""Петля обучения проверки: решения человека → поправки к правилам и агенту.

1. Каждое решение человека пишется в таблицу reviews вместе с тем, что сказали правила
   (причины и их веса) и агент.
2. По каждой причине считается, как часто человек с ней соглашался. Если по причине
   «слишком быстро» человек принял 47 из 50 — вес у неё лишний; если «похожий скрин»
   отклонён 30 из 30 — такие отчёты можно браковать без человека.
3. Агент часть спорных отчётов смотрит «в тени»: решает всё равно человек, а ответ агента
   только сравнивается. Если агент совпал с человеком почти всегда — причину можно отдать агенту.
4. Каждое предложение сначала проигрывается на истории решений: сколько отчётов ушло бы
   от человека и в скольких из них новое правило ошиблось бы. Опасные не предлагаются.
5. Применяет поправку человек кнопкой, не система сама: исполнители подстраиваются,
   и самообучающуюся проверку можно было бы приучить пропускать брак. Любую поправку
   можно откатить; всё пишется в журнал.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db

MIN_N = int(os.getenv("LEARN_MIN_N", "20"))     # меньше разборов — выводов не делаем
AGREE = 0.9                                     # с такой долей человек согласен — повод менять правило
AGENT_AGREE = 0.95
MAX_ERR = 0.05                                  # на истории поправка ошибается не чаще
BASE_AGENT = {"no_control", "file_unavailable"}
# Агенту можно отдать только сомнение, которое снимает картинка: нет контрольного вопроса, скрин не
# скачался, скринов меньше этапов, другой запрос, шаблонный текст. Скорость, «много у одного», ферму
# на IP, историю и копию скрина снимок не опровергает — по ним обучение предлагает только вес (ревью Codex 24.11).
AGENT_ELIGIBLE = {"no_control", "file_unavailable", "few_stages", "query_wrong", "dup_text"}
HOLDOUT = 0.3          # свежие 30% решений — проверка предложения на том, на чём оно не подбиралось
NAMES_HUMAN = {"approve": "принял", "rework": "вернул", "reject": "отклонил"}


# ---------- запись разбора ----------
def record(r: dict, action: str, comment: str | None) -> None:
    # исходные веса правил (до поправок) и без ответов агента — их replay добавляет сам по agent_verdict
    findings = {f["code"]: [f["kind"] if "base_weight" not in f else "soft", f.get("base_weight", f["weight"])]
                for f in json.loads(r["reasons"] or "[]") if not f["code"].startswith(("agent_", "vision_"))}
    agent_v = r.get("agent_shadow") if (r.get("agent_shadow") or "") in ("ok", "bad", "unsure") else r.get("agent_verdict")
    # текст и файлы — снимком: после пересдачи в reports будет уже другая версия отчёта
    db.run("INSERT INTO reviews(report_id, worker_id, task_id, ts, score, system_verdict, findings, agent_verdict, "
           "human, comment, text, files) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (r["id"], r["worker_id"], r["task_id"], db.now(), r["score"], r["verdict"],
            json.dumps(findings, ensure_ascii=False), agent_v, action, comment, r["text"], r["files"]))


# ---------- поправки ----------
def overrides() -> dict:
    try:
        return json.loads(db.get_setting("rule_overrides", "{}") or "{}")
    except ValueError:
        return {}


def apply_overrides(findings: list[dict], ov: dict) -> list[dict]:
    for f in findings:
        o = ov.get(f["code"])
        if not o or f["kind"] != "soft" or f["weight"] <= 0:
            continue
        f.setdefault("base_weight", f["weight"])   # исходный вес — чтобы обучение не снижало его повторно
        if "weight" in o:
            f["weight"] = o["weight"]
        if o.get("hard"):
            f["kind"], f["action"] = "hard", o["hard"]
    return findings


def agent_resolvable(ov: dict | None = None) -> set[str]:
    ov = overrides() if ov is None else ov
    return BASE_AGENT | {c for c, o in ov.items() if o.get("agent")}


# ---------- статистика ----------
def _reviews():
    out = []
    for r in db.q("SELECT * FROM reviews ORDER BY id"):
        # ответы агента/vision внутри причин не учитываем: replay добавит их сам по agent_verdict (ревью Sonnet)
        r["findings"] = {c: v for c, v in json.loads(r["findings"] or "{}").items()
                         if not c.startswith(("agent_", "vision_"))}
        out.append(r)
    return out


PER_WORKER = 2         # не больше 2 разборов одного исполнителя на причину: ферма не «научит» проверку
MIN_WORKERS = 10       # и вывод — только по 10+ разным исполнителям
AGENT_BAD_RECALL = 0.9  # агенту отдаём, только если он ловил брак, а не просто «всегда ок» при 95% честных


def stats(rows=None) -> list[dict]:
    rows = _reviews() if rows is None else rows
    s = defaultdict(lambda: {"n": 0, "approve": 0, "rework": 0, "reject": 0, "weights": [], "workers": set(),
                             "agent_n": 0, "agent_agree": 0, "agent_badn": 0, "agent_caught": 0,
                             "alone_n": 0, "alone_bad": 0})
    seen = defaultdict(int)
    for r in rows:
        positive = [c for c, (k, w) in r["findings"].items() if k == "soft" and w > 0]
        for code in positive:
            seen[(code, r["worker_id"])] += 1
            if seen[(code, r["worker_id"])] > PER_WORKER:
                continue
            x = s[code]
            x["n"] += 1
            x[r["human"]] += 1
            x["weights"].append(r["findings"][code][1])
            x["workers"].add(r["worker_id"])
            human_ok = r["human"] == "approve"
            if len(positive) == 1:      # причина — единственная: только так видно, что брак именно из-за неё
                x["alone_n"] += 1
                x["alone_bad"] += not human_ok
            if r["agent_verdict"] in ("ok", "bad"):
                x["agent_n"] += 1
                x["agent_agree"] += (r["agent_verdict"] == "ok") == human_ok
                if not human_ok:
                    x["agent_badn"] += 1
                    x["agent_caught"] += r["agent_verdict"] == "bad"
    out = []
    for code, x in s.items():
        ws = sorted(x.pop("weights"))
        out.append({"code": code, "weight": ws[len(ws) // 2], "n_workers": len(x.pop("workers")), **x})
    return sorted(out, key=lambda x: -x["n"])


def _verdict(fmap: dict, ov: dict, agent_v: str | None) -> str:
    """Что решила бы проверка по сохранённым причинам при поправках ov."""
    from .checks import decide
    fs = [{"code": c, "kind": k, "weight": w, "action": "rework" if k == "hard" else None}
          for c, (k, w) in fmap.items()]
    fs = apply_overrides(fs, ov)
    if agent_v == "ok":
        lift = sum(f["weight"] for f in fs if f["code"] in agent_resolvable(ov) and f["kind"] == "soft")
        fs.append({"code": "agent_ok", "kind": "soft", "weight": -lift, "action": None})
    elif agent_v == "bad" and any(f["code"] in agent_resolvable(ov) for f in fs):
        fs.append({"code": "agent_bad", "kind": "hard", "weight": 50, "action": "rework"})
    return decide(fs)[1]


def _route(fmap: dict, ov: dict) -> str:
    """Куда ушёл бы отчёт без ответа агента: auto (решают правила), agent или human."""
    if _verdict(fmap, ov, None) != "manual":
        return "auto"
    codes = {c for c, (k, w) in fmap.items() if k == "soft" and w > 0}
    return "agent" if codes and codes <= agent_resolvable(ov) else "human"


def replay(rows: list[dict], base_ov: dict, ov: dict) -> dict:
    """Проиграть решения людей при старых (base_ov) и новых (ov) правилах и сравнить построчно.
    Новые ошибки не взаимозачитываются со старыми: отдельно лишние оплаты брака и лишние отказы честным.
    Отчёт, отданный агенту, решается по ответу агента в тени; если агент его не видел — остаётся человеку."""
    gained = new_pay = new_reject = 0
    for r in rows:
        b = _verdict(r["findings"], base_ov, r["agent_verdict"])
        v = _verdict(r["findings"], ov, r["agent_verdict"])
        if v == b:
            continue
        if b == "manual" and v != "manual":
            gained += 1
        human_ok = r["human"] == "approve"
        if v == "approve" and not human_ok:
            new_pay += 1                    # брак ушёл бы в оплату — главная ошибка, допускаем ноль
        elif v in ("reject", "rework") and human_ok:
            new_reject += 1
        elif v == "reject" and r["human"] == "rework":
            new_reject += 1                 # отклонили безвозвратно то, что человек дал бы исправить
    return {"gained": gained, "new_pay": new_pay, "new_reject": new_reject}


def suggestions() -> list[dict]:
    """Подбор по старым решениям, проверка — на свежих, которых подбор не видел."""
    from .checks import RULE_NAMES
    rows, ov = _reviews(), overrides()
    cut = int(len(rows) * (1 - HOLDOUT))
    fit, check = rows[:cut], rows[cut:]
    if len(check) < 10:
        return []
    out = []
    for x in stats(fit):
        code, n, cur = x["code"], x["n"], ov.get(x["code"], {})
        name = RULE_NAMES.get(code, code)
        ok, bad = x["approve"], x["rework"] + x["reject"]
        cands = []
        if x["n_workers"] < MIN_WORKERS:
            continue
        # Отдать агенту можно только сомнение, видимое на картинке, и только если агент ловил брак.
        recall_ok = x["agent_badn"] >= 5 and x["agent_caught"] / x["agent_badn"] >= AGENT_BAD_RECALL
        if code in AGENT_ELIGIBLE and code not in agent_resolvable(ov) and x["agent_n"] >= max(10, MIN_N // 2) \
                and x["agent_agree"] / x["agent_n"] >= AGENT_AGREE and recall_ok:
            cands.append(({"agent": True},
                          f"«{name}»: агент совпал с человеком в {x['agent_agree']} из {x['agent_n']}, "
                          f"брак поймал в {x['agent_caught']} из {x['agent_badn']}. Отдавать такие отчёты агенту, а не человеку"))
        elif n >= MIN_N and ok / n >= AGREE and cur.get("weight", x["weight"]) > max(1, round(x["weight"] * 0.4)):
            new_w = max(1, round(x["weight"] * 0.4))     # от исходного веса и не до нуля: в сочетании он ещё нужен
            cands.append(({"weight": new_w},
                          f"«{name}»: человек принял {ok} из {n} у {x['n_workers']} исполнителей ({ok * 100 // n}%). "
                          f"Снизить вес {x['weight']} → {new_w}"))
        # браковать сразу — только если причина бракует и в одиночку, без соседних признаков; и только «вернуть»
        if x["alone_n"] >= MIN_N // 2 and x["alone_bad"] / x["alone_n"] >= AGREE and not cur.get("hard"):
            cands.append(({"hard": "rework"},
                          f"«{name}»: когда это единственное сомнение, человек забраковал {x['alone_bad']} из {x['alone_n']}. "
                          f"Сразу возвращать на доработку, без человека"))
        for change, text in cands:
            r = replay(check, ov, {**ov, code: {**cur, **change}})
            if r["gained"] <= 0 or r["new_pay"] > 0 or r["new_reject"] > MAX_ERR * r["gained"]:
                continue
            out.append({"code": code, "change": change, "text": text, **r, "of": len(check)})
    return out


def apply(code: str, change: dict) -> None:
    ov = overrides()
    before = ov.get(code, {})
    ov[code] = {**before, **{k: v for k, v in change.items() if k in ("weight", "hard", "agent")}}
    db.set_setting("rule_overrides", json.dumps(ov, ensure_ascii=False))
    db.log_action("rule_change", code, {"before": before, "after": ov[code]})
    _recheck(code)


def rollback(code: str) -> None:
    ov = overrides()
    if code not in ov:
        raise ValueError("Такой поправки нет")
    before = ov.pop(code)
    db.set_setting("rule_overrides", json.dumps(ov, ensure_ascii=False))
    db.log_action("rule_rollback", code, {"before": before})
    _recheck(code)


def _recheck(code: str) -> None:
    """Перепроверить в ближайшую синхронизацию только неразобранные отчёты с этой причиной."""
    db.run("UPDATE reports SET checked_at=NULL WHERE status='pending' AND decision IS NULL AND agent_batch IS NULL "
           "AND reasons LIKE ?", (f'%"code": "{code}"%',))


# ---------- примеры для агента ----------
def agent_examples(limit: int = 30) -> list[dict]:
    """Где агент ошибся по мнению человека — эти случаи агент читает перед каждой пачкой."""
    rows = db.q("SELECT v.agent_verdict, v.human, v.comment, v.text, t.need_for_report, t.control_expected, "
                "p.name product FROM reviews v JOIN tasks t ON t.id=v.task_id "
                "JOIN projects p ON p.id=t.project_id WHERE v.agent_verdict IN ('ok','bad') AND "
                "((v.agent_verdict='ok') <> (v.human='approve')) ORDER BY v.id DESC LIMIT ?", (limit,))
    return [{"product": r["product"], "need_for_report": r["need_for_report"],
             "expected_answer": json.loads(r["control_expected"] or "[]"), "worker_text": r["text"],
             "agent_said": r["agent_verdict"], "human_decided": NAMES_HUMAN.get(r["human"], r["human"]),
             "human_comment": r["comment"] or ""} for r in rows]


# ---------- сводка для панели ----------
def summary() -> dict:
    from .checks import RULE_NAMES
    rows, ov = _reviews(), overrides()
    st = stats(rows)
    agent_rows = [r for r in rows if r["agent_verdict"] in ("ok", "bad")]
    agree = sum((r["agent_verdict"] == "ok") == (r["human"] == "approve") for r in agent_rows)
    queue = db.q("SELECT reasons, agent_verdict, agent_shadow FROM reports "
                 "WHERE status='pending' AND decision IS NULL AND verdict='manual'")
    sugg = suggestions()
    all_ov = dict(ov)
    for s in sugg:
        all_ov[s["code"]] = {**all_ov.get(s["code"], {}), **s["change"]}
    routes = defaultdict(int)
    for q in queue:
        fmap = {f["code"]: [f["kind"], f["weight"]] for f in json.loads(q["reasons"] or "[]")
                if not f["code"].startswith("agent_")}
        routes[_route(fmap, all_ov)] += 1
    since7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    days = db.q("SELECT substr(r.submitted_at,1,10) d, COUNT(*) n, SUM(r.decided_by='человек') human "
                "FROM reports r WHERE r.submitted_at>=? GROUP BY d ORDER BY d", (since7,))
    return {
        "reviews": len(rows),
        "rules": [{**x, "name": RULE_NAMES.get(x["code"], x["code"]), "override": ov.get(x["code"])} for x in st],
        "suggestions": sugg,
        "overrides": [{"code": c, "name": RULE_NAMES.get(c, c), **o} for c, o in ov.items()],
        "agent": {"n": len(agent_rows), "agree": agree, "examples": len(agent_examples())},
        "queue": {"now": len(queue), "human": routes["human"], "agent": routes["agent"], "auto": routes["auto"]},
        "days": days,
        "min_n": MIN_N,
    }
