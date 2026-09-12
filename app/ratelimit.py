"""限流：包一层 limits 库（MovingWindowRateLimiter + 内存存储）。重启清零，多实例各算各的。"""
from __future__ import annotations

import time
from dataclasses import dataclass

from limits import parse as parse_rate
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter


@dataclass(frozen=True)
class Verdict:
    ok: bool
    remaining: int = 0
    retry_after_sec: int = 0


class SlidingWindow:
    def __init__(self, window_ms: int, max_: int):
        seconds = max(1, round(window_ms / 1000))
        self._item = parse_rate(f"{max_}/{seconds} seconds")
        self._storage = MemoryStorage()
        self._limiter = MovingWindowRateLimiter(self._storage)

    def take(self, key: str) -> Verdict:
        if self._limiter.hit(self._item, key):
            stats = self._limiter.get_window_stats(self._item, key)
            return Verdict(True, stats.remaining, 0)
        stats = self._limiter.get_window_stats(self._item, key)
        return Verdict(False, 0, max(1, round(stats.reset_time - time.time())))

    def peek(self, key: str) -> int:
        stats = self._limiter.get_window_stats(self._item, key)
        return self._item.amount - stats.remaining

    def reset(self) -> None:
        self._storage.reset()

    def sweep(self) -> int:
        # 内存存储自己按窗口淘汰计数，这里只是给维护任务一个统一入口
        return 0

    def __len__(self) -> int:
        return 0
