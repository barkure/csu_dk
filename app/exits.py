"""学校流量出口：在校园网代理与服务器直连之间轮询，自动跳过冷却出口。"""
from __future__ import annotations

import time
from threading import Lock

from . import config as cfg
from .log import log_event

_frozen_until: dict[str, float] = {}
_next_index = 0
_lock = Lock()


def _exits() -> list[str]:
    """返回参与轮询的代理出口与服务器直连（空串）。"""
    return [*cfg.config.outbound_proxies, ""]


def label(exit_: str) -> str:
    return exit_ or "服务器直连"


def _is_healthy(candidate: str, now: float) -> bool:
    return _frozen_until.get(candidate, 0.0) <= now


def _select(candidates: list[str], now: float) -> str:
    """在已持锁时轮询选择健康出口。"""
    global _next_index

    for offset in range(len(candidates)):
        index = (_next_index + offset) % len(candidates)
        candidate = candidates[index]
        if _is_healthy(candidate, now):
            _next_index = (index + 1) % len(candidates)
            return candidate
    return candidates[0]


def current() -> str:
    """轮询选择健康出口；全在冷却时返回配置中的第一个出口。"""
    with _lock:
        return _select(_exits(), time.time())


def available() -> bool:
    with _lock:
        now = time.time()
        return any(_is_healthy(candidate, now) for candidate in _exits())


def _cool_down(candidate: str, now: float) -> str:
    """已持锁时标记出口冷却。"""
    candidates = _exits()
    if candidate not in candidates:
        candidate = _select(candidates, now)
    _frozen_until[candidate] = now + cfg.config.ip_freeze_cooldown_seconds
    return candidate


def mark_frozen(frozen: str, reason: str = "") -> str | None:
    """冷却被冻结的出口，返回下一个可用出口；无可用出口时返回 None。"""
    with _lock:
        now = time.time()
        frozen = _cool_down(frozen, now)
        candidates = _exits()
        switched = _select(candidates, now) if any(
            _is_healthy(item, now) for item in candidates) else None
    log_event("exit.frozen", level="warning", exit=label(frozen), reason=reason[:120],
              cooldown_seconds=cfg.config.ip_freeze_cooldown_seconds,
              switched_to=label(switched) if switched is not None else "")
    return switched


def mark_unreachable(broken: str) -> None:
    """冷却连接失败的出口，不推进轮询。"""
    with _lock:
        broken = _cool_down(broken, time.time())
    log_event("exit.unreachable", level="warning", exit=label(broken),
              cooldown_seconds=cfg.config.ip_freeze_cooldown_seconds)


def reset() -> None:
    global _next_index

    with _lock:
        _frozen_until.clear()
        _next_index = 0


def summary() -> dict[str, int]:
    """返回健康出口数和出口总数。"""
    with _lock:
        candidates = _exits()
        now = time.time()
        healthy = sum(_is_healthy(candidate, now) for candidate in candidates)
    return {"healthy": healthy, "total": len(candidates)}
