"""Правила, добавленные по реальным отчётам: страна по IP и «много разных заданий».
Запуск: python tests/test_live_rules.py  (страна проверяется, если собран data/ipcountry.csv)"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import checks, config, geo  # noqa: E402

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print(("OK  " if ok else "FAIL"), name, got)


ctx = SimpleNamespace(worker_day={}, project_day={}, worker_tasks={"a": config.WORKER_TASKS_DAY + 1, "b": config.WORKER_TASKS_DAY})
r = lambda w, ip="": {"worker_id": w, "project_id": 1, "ip": ip}
check("прошёл больше нормы разных заданий", [f["code"] for f in checks.rule_concentration(r("a"), {}, ctx, [])], ["many_tasks"])
check("ровно норма — молчим", [f["code"] for f in checks.rule_concentration(r("b"), {}, ctx, [])], [])
check("кривой IP — страны нет", geo.country("999.1.1.1"), None)
check("IPv6 — страны нет", geo.country("2a00:1450::1"), None)
if (config.ROOT / "data" / "ipcountry.csv").exists():
    check("Египет", geo.country("156.214.197.144"), "EG")
    check("Россия", geo.country("95.24.0.1"), "RU")
    check("IP из Египта — мягкий признак", [(f["code"], f["kind"]) for f in checks.rule_geo(r("a", "156.214.197.144"), {}, ctx, [])],
          [("foreign_ip", "soft")])
    check("IP из России — молчим", checks.rule_geo(r("a", "95.24.0.1"), {}, ctx, []), [])
else:
    print("SKIP страна: нет data/ipcountry.csv — запустите python tools/geo_update.py")
nf = lambda text: [f["code"] for f in checks.rule_not_found({"text": text}, {}, ctx, [])]
check("товар не отобразился в диапазоне цен", nf("Здравствуйте, ваш товар не отобразился в результатах поиска в указанном ценовом диапазоне. Изменилась ли цена?"), ["not_found_msg"])
check("не нашёл товар", nf("не нашел товар по фильтру"), ["not_found_msg"])
check("обычный текст — молчим", nf("Задание выполнено. Товар найден по запросу с фильтром цены, добавлен в корзину"), [])
check("пустой текст — молчим", nf(""), [])
check("нет IP — молчим", checks.rule_geo(r("a", ""), {}, ctx, []), [])
sys.exit(bad)
