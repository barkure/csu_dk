"""自定义随机时段：按日期和服务端密钥抽签，重启不会改变当天计划。"""
from __future__ import annotations

import hashlib
import hmac
import random
from datetime import datetime, timedelta

from . import config as cfg
from .errors import BadRequestError, InvalidTimeError, MasterKeyInvalidError, MasterKeyMissingError
from .validate import account_checkin_window, parse_hm, to_minutes


def validate_range(start: str, end: str, account: dict) -> tuple[str | None, str | None]:
    if not start.strip() and not end.strip():
        return None, None
    try:
        first, last = parse_hm(start), parse_hm(end)
    except InvalidTimeError as error:
        raise BadRequestError("请完整填写开始和结束时间（HH:mm）") from error
    start, end = f"{first[0]:02d}:{first[1]:02d}", f"{last[0]:02d}:{last[1]:02d}"
    if to_minutes(start) >= to_minutes(end):
        raise BadRequestError("结束时间须晚于开始时间；请设置同一天内的时段")
    if to_minutes(start) < to_minutes(cfg.config.checkin_start):
        raise BadRequestError(f"开始时间不能早于自动调度起始时间 {cfg.config.checkin_start}")
    window = account_checkin_window(account)
    if window:
        school_start, school_end = map(to_minutes, window)
        a, b = to_minutes(start), to_minutes(end)
        if school_start == school_end:
            inside = True
        elif school_start < school_end:
            inside = school_start <= a < b <= school_end
        else:
            inside = a >= school_start or b <= school_end
        if not inside:
            raise BadRequestError(f"随机时段须在学校允许时段 {'–'.join(window)} 内")
    return start, end


def daily_plan(account: dict, now: datetime) -> tuple[datetime, datetime, datetime] | None:
    """返回开始、结束、抽中的执行时间；不创建任务，也不触发学校请求。"""
    start, end = account.get("random_start"), account.get("random_end")
    if not start and not end:
        return None
    try:
        start, end = validate_range(start or "", end or "", account)
    except BadRequestError:
        return None
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first = midnight + timedelta(minutes=to_minutes(start))
    last = midnight + timedelta(minutes=to_minutes(end))
    # 留一个扫描周期，避免最后一秒的计划被下一轮扫描错过。
    seconds = max(1, int((last - first).total_seconds()) - cfg.config.scheduler_interval)
    message = f"csu-dk:random-checkin:v1|{account['id']}|{now.date()}|{start}|{end}".encode()
    try:
        key = cfg.read_master_key()
    except (MasterKeyInvalidError, MasterKeyMissingError):
        return None
    seed = hmac.new(key.encode(), message, hashlib.sha256).digest()
    target = first + timedelta(seconds=random.Random(seed).randrange(seconds))
    return first, last, target


def is_due(account: dict, now: datetime) -> bool:
    if not account.get("random_start") and not account.get("random_end"):
        return True
    plan = daily_plan(account, now)
    return bool(plan and plan[2] <= now <= plan[1])
