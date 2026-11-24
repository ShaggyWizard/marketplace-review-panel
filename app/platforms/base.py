"""Общий интерфейс площадки с заданиями.

Каждая площадка (UNU, другие биржи заданий) приводит свои ответы к одному виду,
а вся панель — синхронизация, проверка, аналитика — работает только с этим видом.
Чтобы подключить новую площадку, достаточно написать класс-наследник Platform
и зарегистрировать его в platforms/__init__.py.
"""
from dataclasses import dataclass, field


@dataclass
class TaskInfo:
    ext_id: str
    name: str
    price: float
    status: str            # active / paused / limit / moderation / rejected / new
    folder_ext_id: str | None
    limit_total: int
    done_total: int
    check_total: int


@dataclass
class ReportInfo:
    ext_id: str
    task_ext_id: str
    worker_id: str
    ip: str
    status: str            # in_progress / pending / rework / paid / rejected
    price: float
    text: str
    files: list[str] = field(default_factory=list)
    submitted_at: str | None = None
    taken_at: str | None = None


@dataclass
class NewTask:
    name: str
    descr: str
    need_for_report: str
    price: float
    limit: int
    link: str = ""
    need_screen: bool = True
    limit_per_user: int = 1          # один человек — одно выполнение: главный рычаг против «одного на всех»
    # один человек — одно задание из папки товара (у UNU: limit_per_user_folder). Папка = товар, поэтому
    # исполнитель не пройдёт по одному артикулу несколько заданий с разными запросами (менеджер заказчика, 24.11)
    limit_per_user_folder: int | None = 1
    limit_per_ip: int = 1
    limit_per_day: int | None = None
    time_for_check: int = 48         # часов на проверку
    folder_ext_id: str | None = None
    tarif_id: int | None = None
    kind: str | None = None          # тип ТЗ — по нему выбирается тариф


class PlatformError(Exception):
    pass


class Platform:
    name = "base"

    def balance(self) -> dict: ...
    def ensure_folder(self, name: str) -> str: ...
    def find_folder(self, name: str) -> str | None: return None      # найти папку, ничего не создавая
    def move_task(self, ext_id: str, folder_ext_id: str) -> None: raise PlatformError("Не поддерживается")
    def get_tasks(self) -> list[TaskInfo]: ...
    def get_reports(self, task_ext_ids: list[str]) -> list[ReportInfo]: ...
    def create_task(self, t: NewTask) -> str: ...
    def pause_task(self, ext_id: str) -> None: ...
    def play_task(self, ext_id: str) -> None: ...
    def approve(self, report_ext_id: str) -> None: ...
    def reject(self, report_ext_id: str, comment: str, rework: bool) -> None: ...
    def blacklist(self, worker_id: str) -> None: ...
    def fetch_file(self, url: str) -> bytes: ...
    # готовые услуги (заказ количества без отчётов), если площадка их умеет
    def services(self) -> list[dict]: return []
    def order_add(self, service: int, quantity: int, link: str) -> str: raise PlatformError("Не поддерживается")
    def order_status(self, ids: list[str]) -> dict[str, dict]: return {}
