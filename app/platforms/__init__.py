"""Реестр площадок. Новая площадка = новый класс + строка здесь."""
from .. import config
from .base import Platform, PlatformError

_cache: dict[str, Platform] = {}
WRITES = ("create_task", "pause_task", "play_task", "approve", "reject", "blacklist", "ensure_folder",
          "move_task", "order_add")


class ReadOnly:
    """Обёртка «только чтение»: всё, что меняет что-то на площадке или тратит деньги, не выполняется."""

    def __init__(self, p: Platform):
        self._p, self.name = p, p.name

    def __getattr__(self, k):
        if k in WRITES:
            def refuse(*a, **kw):
                raise PlatformError("Режим «только чтение»: на площадку ничего не отправляется")
            return refuse
        return getattr(self._p, k)


def get(name: str) -> Platform:
    if name not in _cache:
        if name == "demo":
            from .demo import DemoPlatform
            _cache[name] = DemoPlatform()
        elif name == "unu":
            from .unu import UnuPlatform
            _cache[name] = ReadOnly(UnuPlatform()) if config.READ_ONLY else UnuPlatform()
        else:
            raise KeyError(f"Неизвестная площадка: {name}")
    return _cache[name]


def default_name() -> str:
    return "demo" if config.DEMO else "unu"


AVAILABLE = ["unu", "demo"]
