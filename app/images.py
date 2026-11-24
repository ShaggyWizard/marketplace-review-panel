"""Скриншоты: скачать один раз, положить в кеш, посчитать отпечатки.

sha256 — точная копия файла (скачал чужой скрин и приложил).
dHash (256 бит, сетка 16×16) — «почти та же картинка»: пересжатие, обрезка, другое время в статус-баре.
Для быстрого поиска похожих отпечаток режется на 16 кусков по 16 бит: если два хеша
отличаются не больше чем в 15 битах, хотя бы один кусок у них совпадает (принцип Дирихле) —
значит сравнивать надо только с теми, у кого совпал хоть один байт, а не со всеми.
"""
import hashlib
import os
import threading
import time
import io
from pathlib import Path

from PIL import Image

from . import config

CACHE = config.DB_PATH.parent / "files"
N = 16                                 # 16×16 = 256 бит


def dhash(data: bytes) -> int:
    img = Image.open(io.BytesIO(data)).convert("L").resize((N + 1, N), Image.LANCZOS)
    px = list(img.getdata())
    bits = 0
    for row in range(N):
        for col in range(N):
            bits = (bits << 1) | (px[row * (N + 1) + col] > px[row * (N + 1) + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def bands(h: int) -> list[tuple[int, int]]:
    return [(k, (h >> (16 * k)) & 0xFFFF) for k in range(16)]


def cached_fetch(platform, url: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / hashlib.sha1(url.encode()).hexdigest()
    if f.exists():
        return f.read_bytes()
    data = platform.fetch_file(url)
    tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.part")     # пишем рядом и подменяем разом
    tmp.write_bytes(data)
    os.replace(tmp, f)
    return data


FETCH_PAUSE = float(os.getenv("FETCH_PAUSE_SEC", "0.4"))       # пауза между скачиваниями скринов
FETCH_BUDGET = int(os.getenv("FETCH_PER_SYNC", "150"))          # не больше стольких новых файлов за один цикл


def is_cached(url: str) -> bool:
    return (CACHE / hashlib.sha1(url.encode()).hexdigest()).exists()


def prefetch(platform, urls, budget: int | None = None) -> int:
    """Скачать в кеш те файлы, которых ещё нет: по одному, с паузой, не больше budget штук. Возвращает, сколько скачано.
    Параллельно качать нельзя: 24.11.2026 загрузка в 6 потоков с хостинга кончилась тем, что UNU перестал отвечать
    адресу сервера. Большой кабинет поэтому догружается за несколько циклов, а не за один."""
    CACHE.mkdir(parents=True, exist_ok=True)
    n = 0
    for u in dict.fromkeys(urls):
        if is_cached(u):
            continue
        if budget is not None and n >= budget:
            break
        try:
            cached_fetch(platform, u)
        except Exception:
            pass
        n += 1
        if getattr(platform, "name", "") != "demo":      # демо-скрины рисуются на месте, сети там нет
            time.sleep(FETCH_PAUSE)
    return n


def fingerprint(data: bytes) -> tuple[str, int | None]:
    sha = hashlib.sha256(data).hexdigest()
    try:
        return sha, dhash(data)
    except Exception:                    # не картинка (pdf, видео) — только точное совпадение
        return sha, None


class NearIndex:
    """Индекс похожих картинок в памяти: ~30 дней скриншотов помещаются в десятки МБ."""

    def __init__(self):
        self.by_band: dict[tuple[int, int], list[tuple[int, int, str]]] = {}

    def add(self, h: int, report_id: int, worker_id: str):
        for b in bands(h):
            self.by_band.setdefault(b, []).append((h, report_id, worker_id))

    def find(self, h: int, max_dist: int, exclude_report: int):
        seen, out = set(), []
        for b in bands(h):
            for h2, rid, wid in self.by_band.get(b, ()):
                if rid == exclude_report or rid in seen:
                    continue
                seen.add(rid)
                dist = hamming(h, h2)
                if dist <= max_dist:
                    out.append((dist, rid, wid))
        return sorted(out)
