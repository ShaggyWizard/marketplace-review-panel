"""SQLite: одна база на панель. На 2–3 тыс. отчётов в день хватает с запасом
(за год ~1 млн строк отчётов, все горячие выборки по индексам)."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    marketplace TEXT NOT NULL,          -- wb / ozon / ym ...
    sku TEXT,
    product_url TEXT,
    keywords TEXT DEFAULT '[]',         -- json: ключевые запросы для позиций
    platform TEXT NOT NULL,             -- unu / demo / ...
    folder_ext_id TEXT,                 -- папка на площадке = проект
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    platform TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    name TEXT,
    kind TEXT DEFAULT 'custom',
    descr TEXT,
    need_for_report TEXT,
    price REAL,
    need_screen INTEGER DEFAULT 1,
    limit_total INTEGER DEFAULT 0,
    done_total INTEGER DEFAULT 0,
    check_total INTEGER DEFAULT 0,
    status TEXT,
    control_expected TEXT DEFAULT '[]', -- json: допустимые ответы на контрольный вопрос
    min_seconds INTEGER DEFAULT 0,      -- быстрее этого честно не сделать
    synced_at TEXT,
    UNIQUE(platform, ext_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    task_id INTEGER REFERENCES tasks(id),
    project_id INTEGER,
    worker_id TEXT,
    ip TEXT,
    status TEXT,                        -- in_progress / pending / rework / paid / rejected
    price REAL,
    text TEXT,
    files TEXT DEFAULT '[]',
    taken_at TEXT,
    submitted_at TEXT,
    first_seen_at TEXT,
    verdict TEXT,                       -- approve / rework / reject / manual
    score INTEGER,
    reasons TEXT DEFAULT '[]',
    checked_at TEXT,
    decision TEXT,                      -- approved / rework / rejected (что реально отправили)
    decided_at TEXT,
    decided_by TEXT,                    -- auto / человек
    UNIQUE(platform, ext_id)
);
CREATE INDEX IF NOT EXISTS ix_reports_status ON reports(status, verdict);
CREATE INDEX IF NOT EXISTS ix_reports_worker ON reports(worker_id, submitted_at);
CREATE INDEX IF NOT EXISTS ix_reports_task ON reports(task_id, submitted_at);
CREATE INDEX IF NOT EXISTS ix_reports_project ON reports(project_id, submitted_at);
CREATE INDEX IF NOT EXISTS ix_reports_ip ON reports(ip);

CREATE TABLE IF NOT EXISTS images (
    report_id INTEGER,
    url TEXT,
    sha TEXT,
    dhash TEXT,
    worker_id TEXT,
    PRIMARY KEY (report_id, url)
);
CREATE INDEX IF NOT EXISTS ix_images_sha ON images(sha);

CREATE TABLE IF NOT EXISTS workers (
    platform TEXT,
    worker_id TEXT,
    blacklisted INTEGER DEFAULT 0,
    note TEXT,
    PRIMARY KEY (platform, worker_id)
);

CREATE TABLE IF NOT EXISTS positions (
    project_id INTEGER,
    keyword TEXT,
    date TEXT,
    position INTEGER,                   -- NULL = не нашли в первых страницах
    PRIMARY KEY (project_id, keyword, date)
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    kind TEXT,
    ref TEXT,
    detail TEXT,
    ok INTEGER
);

-- заказы готовых услуг (UNU API v2): панель дробит ТЗ на заказы по дням
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    ext_id TEXT,
    date TEXT,
    quantity INTEGER,
    status TEXT,
    remains INTEGER,
    charge REAL,
    UNIQUE(task_id, date)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- разбор: каждое решение человека вместе с тем, что сказали правила и агент (learning.py)
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    report_id INTEGER,
    worker_id TEXT,
    task_id INTEGER,
    ts TEXT,
    score INTEGER,
    system_verdict TEXT,                -- что предлагала проверка: approve / rework / reject / manual
    findings TEXT,                      -- {код причины: вес} на момент решения
    agent_verdict TEXT,                 -- ответ агента, если отчёт был у него (в том числе «в тени»)
    human TEXT,                         -- approve / rework / reject
    comment TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_report ON reviews(report_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


MIGRATIONS = [
    ("reports", "agent_batch", "TEXT"),      # в какую пачку агенту ушёл отчёт
    ("reports", "agent_verdict", "TEXT"),    # ok / bad / unsure
    ("tasks", "keyword", "TEXT"),            # ключевой запрос ТЗ
    ("tasks", "per_day", "INTEGER"),         # план выполнений в сутки
    ("tasks", "service_id", "INTEGER"),      # готовая услуга UNU v2 (если ТЗ — заказ, а не своё задание)
    ("tasks", "days", "INTEGER"),
    ("tasks", "stages", "INTEGER DEFAULT 0"),
    ("reports", "agent_shadow", "TEXT"),
    ("reviews", "text", "TEXT"),               # снимок отчёта на момент решения человека
    ("reviews", "files", "TEXT"),       # ответ агента «в тени»: решает человек, ответ только сравниваем  # сколько скринов этапов ждём в отчёте (0 — не проверяем)
    ("positions", "source", "TEXT"),         # mpstats / agent / wb_public / demo
    ("reports", "agent_read", "TEXT"),       # что агент прочитал со скрина: {"sku","query","product"} — данные, не решение
    ("positions", "depth", "INTEGER"),       # сколько первых мест просмотрено: «не найден» значит «ниже depth»
    ("positions", "city", "TEXT"),           # город выдачи (у агента — адрес доставки в профиле)
]


def init() -> None:
    conn().executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in q(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn().execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn().commit()


def q(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql: str, args=()) -> int:
    cur = conn().execute(sql, args)
    conn().commit()
    return cur.lastrowid


def log_action(kind: str, ref: str, detail, ok: bool = True) -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    run("INSERT INTO actions(ts, kind, ref, detail, ok) VALUES (?,?,?,?,?)",
        (now(), kind, ref, detail, int(ok)))


def get_setting(key: str, default: str) -> str:
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    run("INSERT INTO settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
