"""公开账号统计，缓存 60 秒。"""
from __future__ import annotations

import time
from datetime import timedelta
from threading import Lock

from . import config as cfg
from . import db
from .clock import local_now, to_local_iso
from .domain import CheckinStatus

_lock = Lock()
_cached: dict = {}
_expires = 0.0


def snapshot() -> dict:
    global _cached, _expires
    with _lock:
        now = local_now(cfg.config.tz)
        day = now.date().isoformat()
        if _cached.get("date") == day and time.monotonic() < _expires:
            return dict(_cached)
        end = (now.date() + timedelta(days=1)).isoformat()
        counts = db._one("""
            SELECT COUNT(*) AS accounts,
                   COALESCE(SUM(enabled = 1), 0) AS enabledAccounts,
                   COALESCE(SUM(EXISTS (
                       SELECT 1 FROM records r WHERE r.account_id = accounts.id
                       AND r.run_at >= ? AND r.run_at < ? AND r.status IN (?, ?)
                   )), 0) AS todaySuccess
            FROM accounts
        """, (day, end, CheckinStatus.SUCCESS, CheckinStatus.SKIPPED))
        _cached = {"date": day, "updatedAt": to_local_iso(now), **counts}
        _expires = time.monotonic() + 60
        return dict(_cached)
