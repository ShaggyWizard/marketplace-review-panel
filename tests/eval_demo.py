"""Насколько точно проверка ловит брак на демо-данных.

В демо известна «правда»: какой отчёт честный, а какой нет. Скрипт сравнивает
с вердиктами панели. Запуск после первой синхронизации: python tests/eval_demo.py
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from app import db  # noqa: E402
from demo_truth import kinds, truth  # noqa: E402

rows = db.q("SELECT ext_id, worker_id, verdict FROM reports WHERE verdict IS NOT NULL")
m = Counter()
by_kind = Counter()
for r in rows:
    t = truth(int(r["ext_id"][1:]))
    v = r["verdict"]
    v = "auto_ok" if v == "approve" else "auto_bad" if v in ("reject", "rework") else "manual"
    m[(t, v)] += 1
    by_kind[(kinds[r["worker_id"]], v)] += 1

ok_total = sum(n for (t, _), n in m.items() if t == "ok")
bad_total = sum(n for (t, _), n in m.items() if t == "bad")
print(f"Проверено отчётов: {len(rows)}  (честных {ok_total}, брака {bad_total})\n")
print(f"{'':10}{'принять':>10}{'человеку':>10}{'брак':>10}")
for t in ("ok", "bad"):
    print(f"{('честные' if t == 'ok' else 'брак'):10}" + "".join(f"{m[(t, v)]:>10}" for v in ("auto_ok", "manual", "auto_bad")))
print()
print(f"Брак, который ушёл бы в оплату автоматически: {m[('bad', 'auto_ok')]} из {bad_total} "
      f"({m[('bad', 'auto_ok')] * 100 / max(bad_total, 1):.1f}%)")
print(f"Честные, забракованные автоматически: {m[('ok', 'auto_bad')]} из {ok_total} "
      f"({m[('ok', 'auto_bad')] * 100 / max(ok_total, 1):.1f}%)")
print(f"Доля решённых без человека: {(len(rows) - m[('ok', 'manual')] - m[('bad', 'manual')]) * 100 / max(len(rows), 1):.0f}%\n")
print("По типам исполнителей (принять / человеку / брак):")
for k in ("honest", "grinder", "farm", "stealer", "lazy", "faker"):
    print(f"  {k:8}", " / ".join(str(by_kind[(k, v)]) for v in ("auto_ok", "manual", "auto_bad")))

# --- агент: насколько верно он оценил скриншоты ---
ag = db.q("SELECT ext_id, agent_verdict FROM reports WHERE agent_verdict IS NOT NULL")
if ag:
    a = Counter((truth(int(r["ext_id"][1:])), r["agent_verdict"]) for r in ag)
    print(f"\nАгент проверил {len(ag)} отчётов (правда / ответ агента):")
    for t in ("ok", "bad"):
        print(f"  {('честные' if t == 'ok' else 'брак'):8}  ok {a[(t, 'ok')]:>4}   bad {a[(t, 'bad')]:>4}   не уверен {a[(t, 'unsure')]:>4}")
    print(f"  Брак, который агент пропустил: {a[('bad', 'ok')]}; честные, которые он забраковал: {a[('ok', 'bad')]}")
