"""限流：包一层 limits 库（MovingWindowRateLimiter + 内存存储）。重启清零，多实例各算各的。"""
from __future__ import annotations

import threading
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

    def reset(self) -> None:
        self._storage.reset()


class FailureCooldown:
    """带过期时间的连续失败计数器。"""

    def __init__(self, max_failures: int, cooldown_seconds: int, fail_ttl_seconds: int | None = None):
        self._max = max(1, max_failures)
        self._cooldown = max(1, cooldown_seconds)
        self._ttl = max(1, cooldown_seconds if fail_ttl_seconds is None else fail_ttl_seconds)
        self._lock = threading.Lock()
        self._fails: dict[str, tuple[int, float]] = {}
        self._until: dict[str, float] = {}

    def _live(self, key: str, now: float) -> int:
        count, last = self._fails.get(key, (0, 0.0))
        if count and now - last > self._ttl:
            self._fails.pop(key, None)
            return 0
        return count

    def blocked_for(self, key: str) -> int:
        with self._lock:
            left = self._until.get(key, 0.0) - time.time()
            if left <= 0:
                self._until.pop(key, None)
                return 0
            return int(left) + 1

    def record_failure(self, key: str) -> int:
        with self._lock:
            now = time.time()
            count = self._live(key, now) + 1
            if count >= self._max:
                self._until[key] = now + self._cooldown
                self._fails.pop(key, None)
            else:
                self._fails[key] = (count, now)
            return count

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._until.pop(key, None)

    def state(self, key: str) -> tuple[int, float]:
        with self._lock:
            return self._live(key, time.time()), self._until.get(key, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._until.clear()

    def sweep(self) -> int:
        now = time.time()
        with self._lock:
            stale = [key for key, (_count, last) in self._fails.items() if now - last > self._ttl]
            for key in stale:
                self._fails.pop(key, None)
            expired = [key for key, until in self._until.items() if until <= now]
            for key in expired:
                self._until.pop(key, None)
            return len(stale) + len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._fails) + len(self._until)
