"""批量打卡与维护任务。"""
from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import config as cfg
from . import db
from .auth import limiters_sweep
from .checkin import has_fresh_login, login_paused_until, run_checkin, scrub_detail, sweep_login_state
from .clock import local_now, parse_local, to_local_iso
from .domain import CheckinStatus, Trigger
from .log import log_event
from .validate import to_minutes

MAX_ATTEMPTS_PER_DAY = 3
RETRY_INTERVAL_MINUTES = 10

_scheduler: BackgroundScheduler | None = None
_lease_owner: str | None = None


def lease_stale_seconds() -> int:
    """租约至少覆盖三个扫描周期。"""
    return max(120, cfg.config.scheduler_interval * 3)


def _done_in_window(account: dict, start: datetime, end: datetime) -> bool:
    if account.get("last_status") not in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED):
        return False
    last_run = account.get("last_run_at")
    return bool(last_run) and start <= parse_local(last_run) <= end


def _attempts_today(account: dict) -> int:
    """统计当前时间窗内的自动尝试次数。"""
    start, end = _window_bounds(local_now(cfg.config.tz))
    return len([
        record for record in db.list_records(account["id"], 10)
        if record["trigger"] == Trigger.SCHEDULE and start <= parse_local(record["run_at"]) <= end
    ])


def _window_bounds(now: datetime) -> tuple[datetime, datetime]:
    """返回包含当前日期的全局打卡窗口。"""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=to_minutes(cfg.config.checkin_window_start))
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=to_minutes(cfg.config.checkin_window_end))
    if end <= start:
        end += timedelta(days=1)
        if now < start:
            start -= timedelta(days=1)
            end -= timedelta(days=1)
    return start, end


def _inside_window(now: datetime) -> bool:
    start, end = _window_bounds(now)
    return start <= now <= end


def _ready(account: dict, now: datetime, force: bool = False) -> bool:
    if force:
        return True
    start, end = _window_bounds(now)
    if not start <= now <= end or _done_in_window(account, start, end):
        return False
    records = [record for record in db.list_records(account["id"], MAX_ATTEMPTS_PER_DAY)
               if record["trigger"] == Trigger.SCHEDULE and start <= parse_local(record["run_at"]) <= end]
    if len(records) >= MAX_ATTEMPTS_PER_DAY:
        return False
    if records and now - parse_local(records[0]["run_at"]) < timedelta(minutes=RETRY_INTERVAL_MINUTES):
        return False
    return not login_paused_until() or has_fresh_login(account)


def run_batch(trigger: Trigger | str = Trigger.SCHEDULE, force: bool = False,
              accounts: list[dict] | None = None) -> list[tuple[dict, dict]]:
    now = local_now(cfg.config.tz)
    results = []
    source = db.all_enabled_accounts() if accounts is None else accounts
    for account in sorted(source, key=lambda item: item["id"]):
        if not _ready(account, now, force):
            continue
        result = run_checkin(account, trigger)
        results.append((account, result))
        if result.get("paused_until"):
            continue
        log_event("checkin.batch", account_id=account["id"], status=result["status"],
                  message=scrub_detail(result["message"]))
    return results


def maintenance() -> None:
    now = local_now(cfg.config.tz)
    if _lease_owner and not _holds_lease(now):
        return

    try:
        codes = db.purge_codes_older_than(to_local_iso(now - timedelta(days=1)))
        sessions = db.purge_expired_sessions(to_local_iso(now))
        records = db.purge_old_records(to_local_iso(now - timedelta(days=cfg.config.record_retention_days)))
        limiters_sweep()
        sweep_login_state()
        if codes or sessions or records:
            log_event("maintenance.purged", codes=codes, sessions=sessions, records=records)
    except Exception as error:  # noqa: BLE001 - 清理失败只记日志，不能拖垮调度线程
        print(f"[maintenance] 清理失败：{error}", flush=True)


def _holds_lease(now: datetime, announce: bool = True) -> bool:
    """续租，或接管已过期的租约。"""
    now_iso = to_local_iso(now)
    if db.heartbeat_scheduler_lease(_lease_owner, now_iso):
        return True
    stale_before = to_local_iso(now - timedelta(seconds=lease_stale_seconds()))
    if db.acquire_scheduler_lease(_lease_owner, now_iso, stale_before):
        if announce:
            print("[scheduler] 已接管调度租约", flush=True)
        return True
    return False


def tick() -> None:
    now = local_now(cfg.config.tz)
    if _lease_owner and not _holds_lease(now):
        return
    run_batch()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler, _lease_owner
    _lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    if _holds_lease(local_now(cfg.config.tz), announce=False):
        print(f"[scheduler] 已获得调度租约（{_lease_owner}）", flush=True)
    else:
        print("[scheduler] 已有进程持有调度租约，本进程暂不执行打卡（租约失效后会自动接管）", flush=True)

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(tick, "interval", seconds=cfg.config.scheduler_interval,
                       id="tick", max_instances=1, coalesce=True)
    _scheduler.add_job(maintenance, "interval", seconds=cfg.config.maintenance_interval,
                       id="maintenance", max_instances=1, coalesce=True)
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler, _lease_owner
    if _lease_owner:
        db.release_scheduler_lease(_lease_owner)
        _lease_owner = None
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
