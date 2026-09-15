"""学校流量出口：首选校园网代理，被冻时临时切到服务器直连。"""
from __future__ import annotations

import time

from . import config as cfg
from .log import log_event

_frozen_until: dict[str, float] = {}


def _exits() -> list[str]:
    """按优先级返回出口：校园网代理在前，服务器直连（空串）兜底。"""
    proxy = (cfg.config.outbound_proxy or "").strip()
    return [proxy, ""] if proxy else [""]


def label(exit_: str) -> str:
    return exit_ or "服务器直连"


def current() -> str:
    """当前该用的出口：优先首选，冷却中的跳过；全在冷却则返回首选。"""
    now = time.time()
    for candidate in _exits():
        if _frozen_until.get(candidate, 0.0) <= now:
            return candidate
    return _exits()[0]


def available() -> bool:
    now = time.time()
    return any(_frozen_until.get(candidate, 0.0) <= now for candidate in _exits())


def mark_frozen(frozen: str, reason: str = "") -> str | None:
    """将指定出口标记为冷却；返回下一个可用出口，没有可用出口时返回 None。"""
    if frozen not in _exits():
        frozen = current()
    _frozen_until[frozen] = time.time() + cfg.config.ip_freeze_cooldown_seconds
    switched = current() if available() else None
    log_event("exit.frozen", level="warning", exit=label(frozen), reason=reason[:120],
              cooldown_seconds=cfg.config.ip_freeze_cooldown_seconds,
              switched_to=label(switched) if switched is not None else "")
    return switched


def reset() -> None:
    _frozen_until.clear()


def snapshot() -> dict[str, int]:
    """各出口剩余冷却秒数，供日志/查看用。"""
    now = time.time()
    return {label(candidate): max(0, round(_frozen_until.get(candidate, 0.0) - now))
            for candidate in _exits()}
