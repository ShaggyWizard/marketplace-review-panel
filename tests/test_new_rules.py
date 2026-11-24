"""Правила по видео-инструкции заказчика: скрины этапов и ссылки в тексте отчёта.
Запуск: python tests/test_new_rules.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import checks  # noqa: E402

T = {"stages": 4, "sku": "1928879673", "keyword": "магнитола автомобильная с блютуз"}
SEARCH = ("https://www.wildberries.ru/catalog/0/search.aspx?search=%D0%BC%D0%B0%D0%B3%D0%BD%D0%B8%D1%82%D0%BE%D0%BB%D0%B0"
          "+%D0%B0%D0%B2%D1%82%D0%BE%D0%BC%D0%BE%D0%B1%D0%B8%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F+%D1%81+%D0%B1%D0%BB%D1%8E%D1%82%D1%83%D0%B7")


def rep(text="", n=4):
    return {"text": text, "files": json.dumps([f"https://unu.im/f{i}.png" for i in range(n)])}


def codes(r):
    return [f["code"] for f in checks.rule_stages(r, T, None, []) + checks.rule_links(r, T, None, [])]


cases = [
    ("4 скрина, без ссылок", rep(), []),
    ("Done и 2 скрина", rep("Done", 2), ["few_stages"]),
    ("ссылка на нашу карточку и поиск", rep(f"{SEARCH}\nhttps://www.wildberries.ru/catalog/1928879673/detail.aspx"), ["link_ok"]),
    ("ссылка на чужую карточку", rep("https://www.wildberries.ru/catalog/123456789/detail.aspx"), ["link_wrong"]),
    ("другой запрос", rep("https://www.wildberries.ru/catalog/0/search.aspx?search=%D0%BC%D0%B0%D0%B3%D0%BD%D0%B8%D1%82%D0%BE%D0%BB%D0%B0"),
     ["query_wrong"]),
    ("без этапов в ТЗ", None, []),
]
bad = 0
for name, r, want in cases:
    if r is None:
        got = [f["code"] for f in checks.rule_stages(rep("", 1), {"stages": 0}, None, [])]
    else:
        got = codes(r)
    ok = got == want
    bad += not ok
    print(("OK  " if ok else "FAIL"), name, got)
sys.exit(bad)
