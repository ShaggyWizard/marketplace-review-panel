"""Запуск панели.

    python run.py           — с ключом UNU из .env: живой аккаунт, http://127.0.0.1:8800
    python run.py --demo    — демо-данные (2 400 отчётов в сутки), даже если ключ есть

Без UNU_API_KEY панель всегда стартует на демо-данных.
"""
import os
import sys

import uvicorn

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    sys.path.insert(0, here)
    if "--demo" in sys.argv:
        os.environ["DEMO"] = "1"
    port = int(os.getenv("PORT", "8800"))
    for a in sys.argv:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    print(f"Панель: http://127.0.0.1:{port}")
    uvicorn.run("app.api:app", host=os.getenv("HOST", "127.0.0.1"), port=port)
