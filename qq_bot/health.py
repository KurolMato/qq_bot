from __future__ import annotations

import time
from dataclasses import dataclass, replace


STARTED_AT = time.monotonic()


@dataclass(frozen=True)
class ComponentHealth:
    state: str = "starting"
    detail: str = "正在启动"
    last_success: float | None = None
    failures: int = 0


_components: dict[str, ComponentHealth] = {}


def mark_success(name: str, detail: str = "正常") -> None:
    _components[name] = ComponentHealth("ok", detail, time.monotonic(), 0)


def mark_failure(name: str, detail: str) -> None:
    previous = _components.get(name, ComponentHealth())
    _components[name] = replace(
        previous,
        state="error",
        detail=detail,
        failures=previous.failures + 1,
    )


def mark_disabled(name: str, detail: str = "未启用") -> None:
    _components[name] = ComponentHealth("disabled", detail, None, 0)


def snapshot(name: str) -> ComponentHealth:
    return _components.get(name, ComponentHealth())


def uptime_text() -> str:
    total = int(time.monotonic() - STARTED_AT)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"
