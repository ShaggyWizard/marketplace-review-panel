"""Демо петли обучения: «человек» разбирает очередь, агент часть спорных смотрит в тени.

Решения человека берутся из правды генератора (честный — принять, украденный скрин — отклонить,
остальной брак — на доработку). Агент в тени видит картинку: ловит не тот товар и скрин без
действия, но не видит кражу скрина. После этого learning.summary() показывает, что предлагает
разбор, а --apply применяет все безопасные предложения и перепроверяет очередь.

    DEMO=1 python tools/demo_learning.py [--reviews=400] [--apply]
Пишет в базу демо (data/demo.sqlite или DB_PATH). Живой аккаунт не трогает: без DEMO=1 не запускается.
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)

from app import config, db, learning, platforms, sync  # noqa: E402

if not config.DEMO:
    sys.exit("Только для демо: запустите с DEMO=1")
from demo_truth import agent_sees, stolen, truth  # noqa: E402

args = dict(a.lstrip("-").split("=", 1) if "=" in a else (a.lstrip("-"), "1") for a in sys.argv[1:])
N = int(args.get("reviews", 400))
rnd = random.Random(7)

db.init()
queue = db.q("SELECT id, ext_id FROM reports WHERE status='pending' AND decision IS NULL AND verdict='manual' "
             "ORDER BY submitted_at")
rnd.shuffle(queue)
take = queue[:N]
# агент в тени — на половине разбираемых
for r in take[: N // 2]:
    i = int(r["ext_id"][1:])
    v = "unsure" if rnd.random() < 0.04 else agent_sees(i)
    db.run("UPDATE reports SET agent_shadow=? WHERE id=?", (v, r["id"]))
done = 0
for r in take:
    i = int(r["ext_id"][1:])
    t = truth(i)
    action = "approve" if t == "ok" else ("reject" if stolen(i) else "rework")
    comment = None if action == "approve" else ("Скриншот чужой" if action == "reject" else "Не соответствует заданию")
    try:
        sync.apply_decision(r["id"], action, "человек", comment)
        done += 1
    except Exception as e:
        print("пропуск", r["id"], e)
print(f"Человек разобрал {done} отчётов из очереди {len(queue)}")

s = learning.summary()
print(f"Разборов всего: {s['reviews']}; агент в тени совпал с человеком: {s['agent']['agree']} из {s['agent']['n']}")
print("Причина: разобрано / принято / брак")
for x in s["rules"]:
    print(f"  {x['name']:32} {x['n']:>4} / {x['approve']:>4} / {x['rework'] + x['reject']:>4}   агент {x['agent_agree']}/{x['agent_n']}")
print("Предложения:")
for g in s["suggestions"]:
    print(f"  {g['text']}  → на {g['of']} свежих: без человека ещё {g['gained']}, брака в оплату {g['new_pay']}, "
          f"лишних отказов {g['new_reject']}")
q = s["queue"]
print(f"Очередь человеку сейчас {q['now']}; после всех предложений: человеку {q['human']}, агенту {q['agent']}, "
      f"правилам {q['auto']}")

if "apply" in args:
    for g in s["suggestions"]:
        learning.apply(g["code"], g["change"])
    sync.run_once()
    print("Применено, очередь перепроверена:",
          db.one("SELECT COUNT(*) n FROM reports WHERE status='pending' AND decision IS NULL AND verdict='manual'")["n"])
