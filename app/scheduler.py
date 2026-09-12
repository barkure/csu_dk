"""调度：每天在时间窗内随机时刻执行打卡，外加维护任务。"""
from __future__ import annotations

import os
import random
import socket
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import config as cfg
from . import db
from .auth import limiters_sweep
from .checkin import fill_address, run_checkin, scrub_detail
from .clock import local_now, parse_local, pick_next_run, to_local_iso
from .domain import CheckinStatus, Trigger
from .log import log_event
from .validate import to_minutes

MAX_ATTEMPTS_PER_DAY = 3
RETRY_MIN_MINUTES = 5
RETRY_MAX_MINUTES = 20
RETRY_MIN_REMAINING_MINUTES = 10

_scheduler: BackgroundScheduler | None = None
_lease_owner: str | None = None
_bootstrapped = False


def lease_stale_seconds() -> int:
    """多久没心跳才算租约失效。

    必须跟扫描间隔挂钩：间隔能配到 3600 秒，若固定成两分钟，正常持有者还没轮到下一次
    心跳就被别人判成"过期"，两个进程会轮流接管。
    """
    return max(120, cfg.config.scheduler_interval * 3)


def schedule_next(account: dict, tomorrow: bool = False) -> str | None:
    """停用或待重新提交凭据的账号不排期，否则界面会显示一个永远不会执行的下次时刻。"""
    if not account.get("enabled") or account.get("needs_reauth"):
        db.update_account(account["id"], {"next_run_at": None})
        return None

    if tomorrow:
        reference = local_now(cfg.config.tz).replace(hour=0, minute=0, second=1, microsecond=0) + timedelta(days=1)
    else:
        reference = local_now(cfg.config.tz)

    iso = to_local_iso(pick_next_run(account["window_start"], account["window_end"], cfg.config.tz, reference))
    db.update_account(account["id"], {"next_run_at": iso})
    return iso


def schedule_next_after(account: dict, result: dict) -> str | None:
    done = result.get("status") in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED)
    return schedule_next(account, tomorrow=done)


def _done_today(account: dict, today: str) -> bool:
    if account.get("last_status") not in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED):
        return False
    return str(account.get("last_run_at") or "")[:10] == today


def bootstrap() -> None:
    """启动时校准排期，并纠正"今天已打完但排期还停在今天"的脏数据（否则当晚反复空跑）。"""
    today = to_local_iso(local_now(cfg.config.tz))[:10]
    for account in db.all_enabled_accounts():
        scheduled_today = str(account.get("next_run_at") or "")[:10] == today
        if account.get("next_run_at") and _done_today(account, today) and scheduled_today:
            schedule_next(account, tomorrow=True)
            continue
        if not account.get("next_run_at"):
            schedule_next(account)


def _attempts_today(account: dict) -> int:
    """只数自动重试的次数：手动点"立即打卡"不该吃掉当晚的重试额度。"""
    today = to_local_iso(local_now(cfg.config.tz))[:10]
    return len([
        record for record in db.list_records(account["id"], 10)
        if record["trigger"] == Trigger.SCHEDULE and str(record["run_at"])[:10] == today
    ])


def _window_bounds(account: dict, now: datetime) -> tuple[datetime, datetime]:
    """包含 now 的那个打卡时段的起止。

    跨午夜窗口（23:00-01:00）在 00:30 时，要返回"昨晚 23:00 → 今天 01:00"，
    否则会把结束时间算成"今天 01:00"（已过去），重试和补地址都会失效。
    """
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=to_minutes(account["window_start"]))
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=to_minutes(account["window_end"]))
    if end <= start:                     # 跨天窗口
        end += timedelta(days=1)
        if now < start:                  # 还在"昨晚开窗"的那一段里
            start -= timedelta(days=1)
            end -= timedelta(days=1)
    return start, end


def _schedule_retry(account: dict) -> str | None:
    now = local_now(cfg.config.tz)
    at = now + timedelta(minutes=random.uniform(RETRY_MIN_MINUTES, RETRY_MAX_MINUTES))
    if (_window_bounds(account, now)[1] - at).total_seconds() < RETRY_MIN_REMAINING_MINUTES * 60:
        return None
    iso = to_local_iso(at)
    db.update_account(account["id"], {"next_run_at": iso})
    return iso


def _inside_window(account: dict, now: datetime) -> bool:
    start, end = _window_bounds(account, now)
    return start <= now <= end


def fill_missing_addresses(now) -> None:
    """打卡时段内把缺的楼栋名补上（不用等打卡成功；学校只在时段内给这个名字）。"""
    for account in db.all_enabled_accounts():
        if account.get("dkdz") or not _inside_window(account, now):
            continue
        try:
            address = fill_address(account)
        except Exception as error:  # noqa: BLE001 - 补地址失败不该影响其它账号
            print(f"[address] 补楼栋名失败：{error}", flush=True)
            continue
        if address:
            log_event("address.filled", account_id=account["id"], address=address)


def maintenance() -> None:
    now = local_now(cfg.config.tz)
    # 和 tick 一样先看租约：这台进程不持有租约时，补楼栋名会真的去登录学校
    if _lease_owner and not _holds_lease(now):
        return

    fill_missing_addresses(now)
    try:
        # 验证码保留 24 小时：每邮箱每日上限要靠历史行计数
        codes = db.purge_codes_older_than(to_local_iso(now - timedelta(days=1)))
        sessions = db.purge_expired_sessions(to_local_iso(now))
        records = db.purge_old_records(to_local_iso(now - timedelta(days=cfg.config.record_retention_days)))
        limiters_sweep()
        if codes or sessions or records:
            log_event("maintenance.purged", codes=codes, sessions=sessions, records=records)
    except Exception as error:  # noqa: BLE001 - 清理失败只记日志，不能拖垮调度线程
        print(f"[maintenance] 清理失败：{error}", flush=True)


def _holds_lease(now: datetime, announce: bool = True) -> bool:
    """确保本进程持有调度租约；没有就尝试接管（上一个进程可能刚被 kill）。

    必须每轮都试：只在启动时抢一次的话，遇到"被 kill 的进程留下的租约还没过期"，
    新进程就会一直拒绝，结果变成**没有任何进程在调度**。
    """
    now_iso = to_local_iso(now)
    if db.heartbeat_scheduler_lease(_lease_owner, now_iso):
        _bootstrap_once()
        return True
    stale_before = to_local_iso(now - timedelta(seconds=lease_stale_seconds()))
    if db.acquire_scheduler_lease(_lease_owner, now_iso, stale_before):
        if announce:
            # 只有运行中接管才值得报；启动时由 start_scheduler 统一报"获得/未获得"
            print("[scheduler] 已接管调度租约", flush=True)
        return True
    return False


def _bootstrap_once() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    bootstrap()
    _bootstrapped = True


def tick() -> None:
    now = local_now(cfg.config.tz)
    # 只有正常启动过调度器的进程才受租约约束（直接调用 tick 的场景，例如测试，不受影响）
    if _lease_owner and not _holds_lease(now):
        return
    for account in db.due_accounts(to_local_iso(now)):
        if account.get("next_run_at"):
            overdue = (now - parse_local(account["next_run_at"])).total_seconds() / 60
            if overdue > cfg.config.catchup_minutes:
                db.add_record(account["id"], to_local_iso(now), Trigger.SCHEDULE,
                              CheckinStatus.FAILED, f"服务离线，错过打卡窗口 {overdue:.0f} 分钟")
                db.update_account(account["id"], {
                    "last_run_at": to_local_iso(now), "last_status": CheckinStatus.FAILED,
                    "last_message": "错过打卡窗口",
                })
                schedule_next(account, tomorrow=True)
                continue

        result = run_checkin(account, Trigger.SCHEDULE)
        log_event(
            "checkin.scheduled",
            account_id=account["id"],
            csu_username_tail=str(account["csu_username"])[-4:],
            status=result["status"],
            # 错误文案来自学校，可能夹着凭据：按形态擦一遍再进日志
            message=scrub_detail(result["message"]),
        )

        if (result["status"] not in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED)
                and _attempts_today(account) < MAX_ATTEMPTS_PER_DAY):
            retry_at = _schedule_retry(account)
            if retry_at:
                print(f"[scheduler] {account['csu_username']} 未完成，{retry_at} 再试一次", flush=True)
                continue
        schedule_next_after(account, result)


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler, _lease_owner
    # 多进程（uvicorn --workers > 1）时只让一个进程跑调度：否则同一账号会被重复执行。
    # 抢到租约才 bootstrap（修正排期），否则多进程启动时每个进程都会去改调度状态。
    _lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    if _holds_lease(local_now(cfg.config.tz), announce=False):
        print(f"[scheduler] 已获得调度租约（{_lease_owner}）", flush=True)
    else:
        # 定时器照常起：每轮 tick 都会再试，租约一过期就自动接管
        print("[scheduler] 已有进程持有调度租约，本进程暂不执行打卡（租约失效后会自动接管）", flush=True)

    _scheduler = BackgroundScheduler(daemon=True)
    # max_instances=1 + coalesce：上一轮没跑完就跳过，不叠加
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
