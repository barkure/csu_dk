"""进程内串行锁。"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_registry = threading.Lock()
_locks: dict[str, dict] = {}


@contextmanager
def lock_for(key: str):
    with _registry:
        entry = _locks.setdefault(key, {"lock": threading.Lock(), "users": 0})
        entry["users"] += 1
    lock = entry["lock"]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _registry:
            entry["users"] -= 1
            if entry["users"] <= 0 and _locks.get(key) is entry:
                del _locks[key]


def active_lock_count() -> int:
    """返回当前锁数量。"""
    with _registry:
        return len(_locks)
