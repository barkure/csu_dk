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


def mark_frozen(frozen: str, reason: str = "") -> str | None:
    """将指定出口标记为冷却；返回下一个可用出口，没有可用出口时返回 None。"""
    with _lock:
        candidates = _exits()
        now = time.time()
        if frozen not in candidates:
            frozen = _select(candidates, now)
        _frozen_until[frozen] = now + cfg.config.ip_freeze_cooldown_seconds
        switched = _select(candidates, now) if any(
            _is_healthy(candidate, now) for candidate in candidates) else None
    log_event("exit.frozen", level="warning", exit=label(frozen), reason=reason[:120],
              cooldown_seconds=cfg.config.ip_freeze_cooldown_seconds,
              switched_to=label(switched) if switched is not None else "")
    return switched


def reset() -> None:
    global _next_index

    with _lock:
        _frozen_until.clear()
        _next_index = 0


def snapshot() -> dict[str, int]:
    """返回各出口的剩余冷却秒数。"""
    with _lock:
        now = time.time()
        return {label(candidate): max(0, round(_frozen_until.get(candidate, 0.0) - now))
                for candidate in _exits()}


def summary() -> dict[str, int]:
    """返回健康出口数和出口总数。"""
    with _lock:
        candidates = _exits()
        now = time.time()
        healthy = sum(_is_healthy(candidate, now) for candidate in candidates)
    return {"healthy": healthy, "total": len(candidates)}
