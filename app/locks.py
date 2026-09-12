"""进程内串行锁：同 key 排队，不同 key 并行。只对单进程有效。

锁用完就回收：按学号/用户的锁会随请求不断产生新 key，
只增不删的表会一直涨（而且每个 key 都留着一把 Lock 对象）。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_registry = threading.Lock()
_locks: dict[str, dict] = {}          # key -> {"lock": Lock, "users": 持有者+等待者}


@contextmanager
def lock_for(key: str):
    with _registry:
        entry = _locks.setdefault(key, {"lock": threading.Lock(), "users": 0})
        entry["users"] += 1           # 先登记，避免最后一个持有者把 entry 删掉
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
    """当前登记的锁数量（测试用：应当回落到 0）。"""
    with _registry:
        return len(_locks)
