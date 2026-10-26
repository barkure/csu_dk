"""公开账号统计，缓存 10 秒。"""
from __future__ import annotations

import time
from copy import deepcopy
from datetime import timedelta
from threading import Lock

from . import config as cfg
from . import db
from .clock import local_now, to_local_iso
from .domain import CheckinStatus
from .validate import account_window_state

_lock = Lock()
_cached: dict = {}
_expires = 0.0


def _category(account: dict, time: str) -> str:
    if account["success"]:
        return "success"
    if account["status"] == CheckinStatus.NO_TASK:
        return "noTask"
    if account["verifying"]:
        return "verifying"
    if account["status"] == CheckinStatus.FAILED:
        return "failed"
    state = account_window_state(account, time, cfg.config.checkin_start)
    return {"before": "notDue", "inside": "pending", "after": "expired"}[state]


def snapshot() -> dict:
    global _cached, _expires
    with _lock:
        now = local_now(cfg.config.tz)
        day = now.date().isoformat()
        if _cached.get("date") == day and time.monotonic() < _expires:
            return deepcopy(_cached)
        end = (now.date() + timedelta(days=1)).isoformat()
        accounts = db._all("""
            SELECT enabled, checkin_start, checkin_end, not_before,
                   EXISTS (
                       SELECT 1 FROM records r WHERE r.account_id = accounts.id
                       AND r.run_at >= ? AND r.run_at < ? AND r.status IN (?, ?)
                   ) AS success,
                   (SELECT status FROM records r WHERE r.account_id = accounts.id
                    AND r.run_at >= ? AND r.run_at < ? ORDER BY run_at DESC, id DESC LIMIT 1) AS status,
                   EXISTS (SELECT 1 FROM verifications v WHERE v.account_id = accounts.id
                           AND v.run_at >= ? AND v.run_at < ?) AS verifying
            FROM accounts
        """, (day, end, CheckinStatus.SUCCESS, CheckinStatus.SKIPPED, day, end, day, end))
        enabled_today = dict.fromkeys(("success", "notDue", "noTask", "failed", "verifying", "pending", "expired"), 0)
        for account in accounts:
            if account["enabled"]:
                enabled_today[_category(account, now.strftime("%H:%M"))] += 1
        _cached = {
            "date": day, "updatedAt": to_local_iso(now), "accounts": len(accounts),
            "enabledAccounts": sum(enabled_today.values()),
            "todaySuccess": sum(account["success"] for account in accounts),
            "enabledToday": enabled_today,
        }
        _expires = time.monotonic() + 10
        return deepcopy(_cached)
