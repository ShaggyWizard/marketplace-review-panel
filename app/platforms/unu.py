"""Клиент UNU (https://unu.ru/api-info).

Все вызовы: POST https://unu.im/api, поля api_key, action=<метод> и параметры метода.
Ответ — JSON с success (1/0) и errors.

Проверено на живом ключе 23.11.2026 (только чтение): get_balance, get_folders, get_tasks,
get_tariffs, get_reports, get_blacklist, get_expenses, get_countries; v2 balance, services.
Расхождения с документацией, уже учтены в коде:
- success приходит как true (bool), а не 1;
- в get_balance замороженные деньги — поле freeze, а не blocked_money;
- id в get_tariffs — строки; корневая папка «Мои задачи» имеет id 0;
- тарифа «корзина WB» в v1 нет: для действий с карточкой товара — тариф 74
  «Активность по карточке (лайк, избранное, клики)», от 5 ₽;
- цены услуг v2 отличаются от таблицы на сайте (WB cart by keyword — 10 ₽, а не 9) — берём из API.

API v2 (https://unu.im/api/v2, поле key) — готовые услуги в стиле SMM-панели: заказали
количество, UNU выполняет сам, отчётов нет. Здесь методы services / order_add / order_status.

Что ещё не проверено (нужны деньги на балансе и живые отчёты):
- формат дат в messages[].date — разбираем "YYYY-MM-DD HH:MM:SS", считаем время московским;
- ссылки в files могут быть относительными — дописываем домен;
- статусы отчёта кроме 1/2/3/6 не описаны — сохраняем как other:<код>;
- лимиты запросов не описаны — делаем паузу между вызовами и повтор при сетевой ошибке.
"""
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from .. import config
from .base import NewTask, Platform, PlatformError, ReportInfo, TaskInfo

TASK_STATUS = {1: "new", 2: "limit", 3: "paused", 4: "active", 5: "rejected", 6: "moderation"}
REPORT_STATUS = {1: "in_progress", 2: "pending", 3: "rework", 6: "paid"}
MSK = timezone(timedelta(hours=3))


def _iso(s) -> str | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK) \
            .astimezone(timezone.utc).isoformat()
    except ValueError:
        return str(s)


class UnuPlatform(Platform):
    name = "unu"
    MIN_INTERVAL = 0.35   # сек между запросами — бережём площадку, лимиты не описаны

    def __init__(self, api_key: str = config.UNU_API_KEY, url: str = config.UNU_API_URL):
        if not api_key:
            raise PlatformError("Нет UNU_API_KEY")
        self.key, self.url = api_key, url
        self.s = requests.Session()
        self._last = 0.0
        self._tariff_cache: list | None = None

    # --- низкий уровень ---
    def call(self, action: str, _timeout: int = 60, _tries: int = 3, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if config.READ_ONLY and not action.startswith("get_"):     # вторая защита, под обёрткой ReadOnly
            raise PlatformError(f"Режим «только чтение»: {action} не отправляется")
        for attempt in range(_tries):
            wait = self.MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.s.post(self.url, data={"api_key": self.key, "action": action, **params},
                                timeout=_timeout)
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                # повторяем только чтение: запрос, который создаёт или тратит, мог уже пройти
                if attempt == _tries - 1 or not action.startswith("get_"):
                    raise PlatformError(f"{action}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if str(data.get("success")) not in ("1", "True", "true"):
                raise PlatformError(f"{action}: {data.get('errors') or data}")
            return data
        raise PlatformError(action)

    # --- методы панели ---
    def balance(self, quick: bool = False) -> dict:
        d = self.call("get_balance", _timeout=8, _tries=1) if quick else self.call("get_balance")
        return {"balance": float(d.get("balance", 0)),
                "blocked": float(d.get("freeze", d.get("blocked_money", 0)))}

    def find_folder(self, name: str) -> str | None:
        for f in self.call("get_folders").get("folders", []):
            if f.get("name") == name:
                return str(f["id"])
        return None

    def ensure_folder(self, name: str) -> str:
        return self.find_folder(name) or str(self.call("create_folder", name=name)["folder_id"])

    def move_task(self, ext_id: str, folder_ext_id: str) -> None:
        self.call("move_task", task_id=ext_id, folder_id=folder_ext_id)

    def get_tasks(self) -> list[TaskInfo]:
        d = self.call("get_tasks")
        items = d.get("tasks") if "tasks" in d else next((v for v in d.values() if isinstance(v, list)), [])
        return [TaskInfo(
            ext_id=str(t["id"]), name=t.get("name", ""), price=float(t.get("price_rub", 0)),
            status=TASK_STATUS.get(int(t.get("status", 0)), f"other:{t.get('status')}"),
            folder_ext_id=str(t.get("folder_id")) if t.get("folder_id") else None,
            limit_total=int(t.get("limit_total", 0)), done_total=int(t.get("done_total", 0)),
            check_total=int(t.get("check_total", 0)),
        ) for t in items]

    def get_reports(self, task_ext_ids: list[str]) -> list[ReportInfo]:
        out: list[ReportInfo] = []
        for i in range(0, len(task_ext_ids), 20):          # несколько заданий за один вызов
            chunk = ",".join(task_ext_ids[i:i + 20])
            offset = 0
            while True:
                d = self.call("get_reports", task_id=chunk, offset=offset)
                items = d["reports"] if "reports" in d else \
                    next((v for v in d.values() if isinstance(v, list)), [])
                out += [self._report(r) for r in items]
                if len(items) < 1000:
                    break
                offset += len(items)
        return out

    def _report(self, r: dict) -> ReportInfo:
        wid = str(r.get("worker_id"))
        texts, files, last = [], [], None
        msgs = r.get("messages") or []
        # после доработки важна только последняя сдача: сообщения исполнителя после нашего последнего ответа
        ours = [i for i, m in enumerate(msgs) if str(m.get("from_id")) != wid]
        if ours and ours[-1] < len(msgs) - 1:
            msgs = msgs[ours[-1] + 1:]
        for m in msgs:
            if str(m.get("from_id")) != wid:
                continue                                   # наши ответы исполнителю не считаем
            if m.get("text"):
                texts.append(str(m["text"]))
            for f in m.get("files") or []:
                files.append(f if str(f).startswith("http") else "https://unu.im" + str(f))
            last = m.get("date") or last
        st = int(r.get("status", 0))
        return ReportInfo(
            ext_id=str(r["id"]), task_ext_id=str(r["task_id"]), worker_id=wid,
            ip=str(r.get("IP") or r.get("ip") or ""), price=float(r.get("price_rub", 0)),
            status=REPORT_STATUS.get(st, f"other:{st}"), text="\n".join(texts), files=files,
            submitted_at=_iso(last),
        )

    # тариф под тип ТЗ: всё, что делается в карточке товара, — «Активность по карточке»
    KIND_TARIFF = {"card_boost": 74, "search_cart": 74, "favorite": 74, "review_like": 74}

    def _tariff(self, price: float, kind: str | None = None) -> int:
        if kind in self.KIND_TARIFF:
            return self.KIND_TARIFF[kind]
        if self._tariff_cache is None:
            self._tariff_cache = self.call("get_tariffs").get("tariffs", [])
        ok = [t for t in self._tariff_cache if float(t.get("min_price_rub", 0)) <= price]
        if not ok:
            raise PlatformError("Цена ниже минимальной для всех тарифов")
        return int(max(ok, key=lambda t: float(t["min_price_rub"]))["id"])

    def create_task(self, t: NewTask) -> str:
        d = self.call(
            "add_task_start", name=t.name, descr=t.descr, link=t.link or None,
            need_for_report=t.need_for_report, price=t.price,
            tarif_id=t.tarif_id or self._tariff(t.price, t.kind), folder_id=t.folder_ext_id,
            need_screen=1 if t.need_screen else 0, time_for_check=t.time_for_check,
            limit_per_user=t.limit_per_user, limit_per_ip=t.limit_per_ip,
            limit_per_user_folder=t.limit_per_user_folder if t.folder_ext_id else None,
            limit_per_day=t.limit_per_day, add_to_limit=t.limit,
        )
        return str(d["task_id"])

    def pause_task(self, ext_id: str) -> None:
        self.call("task_pause", task_id=ext_id)

    def play_task(self, ext_id: str) -> None:
        self.call("task_play", task_id=ext_id)

    def approve(self, report_ext_id: str) -> None:
        self.call("approve_report", report_id=report_ext_id)

    def reject(self, report_ext_id: str, comment: str, rework: bool) -> None:
        self.call("reject_report", report_id=report_ext_id, comment=comment,
                  reject_type=1 if rework else 2)

    def blacklist(self, worker_id: str) -> None:
        self.call("add_blacklist", add_blacklist_id=worker_id)

    FILE_HOSTS = ("unu.im", "unu.ru")
    MAX_FILE = 15 * 1024 * 1024

    def fetch_file(self, url: str) -> bytes:
        """Скачиваем только файлы UNU по https и не больше 15 МБ: ссылку в отчёт пишет исполнитель."""
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in self.FILE_HOSTS):
            raise PlatformError(f"Файл не с домена UNU: {host}")
        with self.s.get(url, timeout=30, stream=True, allow_redirects=False) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > self.MAX_FILE:
                    raise PlatformError("Файл больше 15 МБ")
        return data

    # --- API v2: готовые услуги ---
    def v2(self, action: str, **params):
        if config.READ_ONLY and action not in ("services", "status", "balance"):
            raise PlatformError(f"Режим «только чтение»: заказ услуги не отправляется")
        wait =self.MIN_INTERVAL - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        r = self.s.post(self.url.rstrip("/") + "/v2", data={"key": self.key, "action": action, **params}, timeout=60)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise PlatformError(f"v2 {action}: {data['error']}")
        return data

    def services(self) -> list[dict]:
        return [{"id": s["service"], "name": s["name"], "category": s["category"],
                 "price": float(s["rate"]) / 1000, "min": int(s["min"]), "max": int(s["max"])}
                for s in self.v2("services")]

    def order_add(self, service: int, quantity: int, link: str) -> str:
        return str(self.v2("add", service=service, quantity=quantity, link=link)["order"])

    def order_status(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            d = self.v2("status", orders=",".join(chunk)) if len(chunk) > 1 else {chunk[0]: self.v2("status", order=chunk[0])}
            for k, v in d.items():
                if isinstance(v, dict) and "status" in v:
                    try:
                        out[str(k)] = {"status": v["status"], "remains": int(v.get("remains") or 0),
                                       "charge": float(v.get("charge") or 0)}
                    except (TypeError, ValueError):
                        continue                   # один кривой заказ не ломает остальные
        return out
