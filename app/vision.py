"""Проверка скриншота моделью Claude — только для «серой зоны».

Чистые и явно бракованные отчёты решаются правилами бесплатно. Модель смотрит лишь
отчёты, которые иначе ушли бы человеку (обычно 5–15% потока), и отвечает:
видно ли на скрине нужную площадку, товар и выполненное действие.
Включается, если задан ANTHROPIC_API_KEY и установлен пакет anthropic.
"""
import base64
import json

from . import config, images

PROMPT = """Ты проверяешь отчёт исполнителя на бирже заданий.
Задание: {task}
Товар: {product}, артикул {sku}, площадка {mp}.
Контрольные ответы: {expected}.
Текст отчёта: {text}

Посмотри на скриншот и ответь строго JSON без пояснений вокруг:
{{"shows_marketplace": true/false, "shows_product": true/false, "shows_action": true/false,
  "looks_edited": true/false, "reason": "одна короткая фраза по-русски"}}"""


def make_checker(platform):
    if not config.VISION_ENABLED:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    from . import db

    def check(r: dict, t: dict):
        url = json.loads(r["files"])[0]
        try:
            data = images.cached_fetch(platform, url)
            p = db.one("SELECT p.* FROM projects p JOIN tasks t ON t.project_id=p.id WHERE t.id=?", (r["task_id"],)) or {}
            resp = client.beta.messages.create(
                model=config.VISION_MODEL,
                max_tokens=2000,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.standard_b64encode(data).decode()}},
                    {"type": "text", "text": PROMPT.format(
                        task=t.get("name") or "", product=p.get("name", ""), sku=p.get("sku", ""),
                        mp=p.get("marketplace", ""), expected=t.get("control_expected"), text=r["text"])},
                ]}],
            )
            if resp.stop_reason == "refusal":
                return None
            raw = "".join(b.text for b in resp.content if b.type == "text")
            v = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as e:                       # модель недоступна — решает человек
            db.log_action("vision_error", str(r["id"]), str(e), ok=False)
            return None
        if v.get("looks_edited") or not (v.get("shows_product") and v.get("shows_action")):
            return {"code": "vision_fail", "kind": "soft", "weight": 30,
                    "text": "ИИ: " + v.get("reason", "скрин не подтверждает действие"), "action": None}
        return {"code": "vision_ok", "kind": "soft", "weight": -25,
                "text": "ИИ: " + v.get("reason", "на скрине товар и действие"), "action": None}

    return check
