"""Контрольная пачка для агента: известные подделки вперемешку с честными отчётами.

Нужна, чтобы измерить самого агента: ловит ли он скрин не того товара и скрин без
действия — то, что правила не видят. Запуск: python tests/agent_testset.py [N]
Потом агент (субагент proverka-skrinov) проверяет пачку, а tests/eval_demo.py считает итог.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import agent, db  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
fake = db.q("SELECT id FROM reports WHERE status='pending' AND agent_batch IS NULL "
            "AND (files LIKE '%?p=%' OR files LIKE '%noact%') LIMIT ?", (n,))
honest = db.q("SELECT id FROM reports WHERE status='pending' AND verdict='approve' AND agent_batch IS NULL "
              "AND files NOT LIKE '%?%' AND files<>'[]' ORDER BY RANDOM() LIMIT ?", (n,))
ids = [r["id"] for r in fake + honest]
random.shuffle(ids)
print(agent.write_batch(ids, set(ids), set(), {"test_fake": len(fake), "test_honest": len(honest)}, prefix="testbatch-"))
