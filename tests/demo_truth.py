"""«Правда» демо-генератора: какой отчёт честный, а какой брак. Нужна замеру точности
(eval_demo.py) и симуляции решений человека (tools/demo_learning.py)."""
import re

from app.platforms import demo

anchor = demo._anchor()
kinds = {w["id"]: w["kind"] for w in demo.WORKERS}


def truth(i: int) -> str:
    """bad — отчёт нельзя оплачивать; ok — честный."""
    r = demo.gen_report(i, anchor)
    _, ans = demo.control_for(r["task"])
    if not r["files"]:
        return "bad"
    if r["files"][0].split("?")[0] != f"demo://shot/{i}" or "?v=" in r["files"][0]:
        return "bad"                                    # чужой скриншот
    if "p=" in r["files"][0] or "noact" in r["files"][0]:
        return "bad"                                    # скрин не того товара / без действия
    if ans[0] not in r["text"]:
        # цена с отличием в пределах личной скидки (СПП) — человек товар открывал, это не брак
        if r["task"][2] == "search_cart" and any(0.6 * int(ans[0]) <= int(n) <= 1.05 * int(ans[0])
                                                 for n in re.findall(r"\d+", r["text"])):
            return "ok"
        return "bad"                                    # нет или неверный контрольный ответ
    return "ok"


def stolen(i: int) -> bool:
    f = demo.gen_report(i, anchor)["files"]
    return bool(f) and (f[0].split("?")[0] != f"demo://shot/{i}" or "?v=" in f[0])


def agent_sees(i: int) -> str:
    """Что ответил бы агент по картинке: видит не тот товар и пустое действие, но не видит,
    что скрин украден (на нём честный товар), и не проверяет контрольный ответ."""
    f = demo.gen_report(i, anchor)["files"]
    if not f:
        return "bad"
    if "p=" in f[0] or "noact" in f[0]:
        return "bad"
    return "ok"
