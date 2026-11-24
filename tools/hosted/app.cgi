#!/usr/bin/env python3
"""Панель на хостинге без постоянного процесса: каждый запрос к /api/... обслуживает этот CGI-скрипт.
Путь к Python (первая строка) и ROOT поправьте под свой сервер. Настройки — в .env рядом с run.py:
HOSTED=1 (без фоновых потоков, синхронизацию запускает cron) и READ_ONLY=1 (в UNU ничего не отправляется)."""
import os
import sys

ROOT = os.environ.get("UNU_PANEL_ROOT", "/path/to/panel")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wsgiref.handlers import CGIHandler  # noqa: E402

from a2wsgi import ASGIMiddleware  # noqa: E402

from app import db  # noqa: E402

db.init()
from app.api import app  # noqa: E402

CGIHandler().run(ASGIMiddleware(app))
