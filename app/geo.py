"""Страна по IP исполнителя — по таблице data/ipcountry.csv (собирает tools/geo_update.py).

На живых отчётах заказчика (24.11.2026) около половины IP — не из России, при том что в шапке
маркетплейса у них стоит российский регион доставки. Засчитывает ли маркетплейс такие действия,
решает заказчик: правило мягкое, а доля по странам видна во вкладке «Исполнители».
Нет файла — страна не определяется, правило молчит.
"""
import bisect

from . import config

NAMES = {"RU": "Россия", "BY": "Беларусь", "KZ": "Казахстан", "EG": "Египет", "UA": "Украина",
         "UZ": "Узбекистан", "KG": "Киргизия", "AM": "Армения", "TR": "Турция", "IN": "Индия",
         "PK": "Пакистан", "BD": "Бангладеш", "NG": "Нигерия", "SY": "Сирия", "CZ": "Чехия", "RO": "Румыния", "GE": "Грузия", "FR": "Франция",
         "NO": "Норвегия", "AZ": "Азербайджан", "TJ": "Таджикистан", "MD": "Молдавия", "PL": "Польша", "DZ": "Алжир", "MA": "Марокко",
         "VN": "Вьетнам", "ID": "Индонезия", "DE": "Германия", "NL": "Нидерланды", "US": "США"}
_starts: list[int] = []
_rows: list[tuple[int, str]] = []
_mtime: float | None = -1.0        # время файла, с которого загружена таблица; None — файла не было


def _load():
    """Читаем таблицу при первом обращении и заново — когда файл появился или обновился (ревью Codex 24.11)."""
    global _starts, _rows, _mtime
    f = config.ROOT / "data" / "ipcountry.csv"
    m = f.stat().st_mtime if f.exists() else None
    if m == _mtime:
        return
    starts, rows = [], []
    if m is not None:
        for line in f.read_text(encoding="ascii").splitlines():
            s, e, cc = line.split(",")
            starts.append(int(s))
            rows.append((int(e), cc))
    _starts, _rows, _mtime = starts, rows, m


def country(ip: str) -> str | None:
    """Код страны ('RU', 'EG') или None, если IP не IPv4 или таблицы нет.
    В демо IP сгенерированы случайно и стран не моделируют — там страна не определяется."""
    if config.DEMO:
        return None
    _load()
    try:
        a = [int(x) for x in (ip or "").split(".")]
        if len(a) != 4 or any(not 0 <= x <= 255 for x in a):
            return None
    except ValueError:
        return None
    n = (a[0] << 24) | (a[1] << 16) | (a[2] << 8) | a[3]
    i = bisect.bisect_right(_starts, n) - 1
    if i >= 0 and n <= _rows[i][0]:
        return _rows[i][1]
    return None


def name(cc: str | None) -> str:
    return NAMES.get(cc or "", cc or "не определена")
