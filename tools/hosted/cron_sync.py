"""Один цикл синхронизации отдельным процессом: его запускают cron (раз в 5 минут) и кнопка «Обновить».

    */5 * * * * /путь/venv/bin/python /путь/panel/tools/hosted/cron_sync.py

От двойного запуска защищает файл-замок рядом с базой (app/sync.py).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app import config, db, sync  # noqa: E402

db.init()
if config.DEMO:
    sync.seed_demo()
print(sync.run_once())
