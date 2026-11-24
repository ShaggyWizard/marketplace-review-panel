"""Настройки берутся из переменных окружения (или файла .env рядом с run.py)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# Ключ можно задать в .env или ввести в самой панели (кнопка «Ключ UNU»): тогда он лежит в data/key.json.
# data/ не попадает ни в архив, ни в веб-папку; панель ключ обратно не показывает.
KEY_FILE = ROOT / "data" / "key.json"


def saved_key() -> str:
    try:
        import json
        return str(json.loads(KEY_FILE.read_text(encoding="utf-8")).get("unu_api_key") or "")
    except (OSError, ValueError):
        return ""


_env_key = os.getenv("UNU_API_KEY", "")
UNU_API_KEY = _env_key or saved_key()
KEY_SOURCE = "env" if _env_key else ("panel" if UNU_API_KEY else "")
# Задания, заведённые в кабинете руками, сами получают черновик товара (см. sync.auto_attach)
AUTO_ATTACH = os.getenv("AUTO_ATTACH", "1") == "1"
# Только чтение: панель синхронизирует и проверяет, но ничего не отправляет на площадку —
# ни решений, ни заданий, ни папок. Так запущена копия на сайте.
READ_ONLY = os.getenv("READ_ONLY", "0") == "1"
# На хостинге без постоянного процесса (CGI): фоновых потоков нет, синхронизацию запускает cron.
HOSTED = os.getenv("HOSTED", "0") == "1"
UNU_API_URL = os.getenv("UNU_API_URL", "https://unu.im/api")
# Без ключа работаем на демо-площадке со сгенерированными данными.
DEMO = os.getenv("DEMO", "1" if not UNU_API_KEY else "0") == "1"

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / ("demo.sqlite" if DEMO else "panel.sqlite")))
SYNC_INTERVAL_SEC = int(os.getenv("SYNC_INTERVAL_SEC", "300"))

# Режим решений: "advise" — только рекомендации, человек жмёт кнопку;
# "auto" — уверенные решения сразу уходят в площадку (approve/reject).
DECISION_MODE = os.getenv("DECISION_MODE", "advise")

# Пороги проверки
AUTO_APPROVE_SCORE = int(os.getenv("AUTO_APPROVE_SCORE", "80"))
NEAR_DUP_HAMMING = int(os.getenv("NEAR_DUP_HAMMING", "12"))
# Один исполнитель не должен делать больше этой доли отчётов проекта за сутки
WORKER_DAY_SHARE = float(os.getenv("WORKER_DAY_SHARE", "0.01"))
WORKER_DAY_MAX = int(os.getenv("WORKER_DAY_MAX", "5"))
# Один исполнитель проходит подряд много разных заданий кабинета (на живых данных — до 10 из 11)
WORKER_TASKS_DAY = int(os.getenv("WORKER_TASKS_DAY", "4"))
# Страны, из которых ждём исполнителей; остальные — мягкий признак (нужен data/ipcountry.csv)
GEO_HOME = {c.strip().upper() for c in os.getenv("GEO_HOME", "RU,BY,KZ").split(",") if c.strip()}
# Вес небольшой: IP не из России бывает и у живого покупателя с VPN (замечание ревью Gemini 24.11).
# Важнее город доставки на скрине — его читает агент. Сам по себе признак отчёт человеку не отправляет.
GEO_WEIGHT = int(os.getenv("GEO_WEIGHT", "5"))

# Проверка скриншота моделью Claude — только для серой зоны, если задан ключ
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "claude-opus-5-5")
VISION_ENABLED = bool(ANTHROPIC_API_KEY) and os.getenv("VISION_ENABLED", "1") == "1"
