"""纯校验/解析函数：不碰数据库和网络，可直接单测。"""
from __future__ import annotations

import re

from .errors import InvalidTimeError

_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WINDOW_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*[-–~至]\s*(\d{1,2}:\d{2})\s*$")


def parse_hm(text: object) -> tuple[int, int]:
    """严格 HH:mm；'99:00' 会被日期计算静默算成 4 天后，必须在这一层拦住。"""
    raw = str(text or "").strip()
    match = _HM_RE.match(raw)
    if not match:
        raise InvalidTimeError(f"时间格式必须是 HH:mm，收到 {text!r}")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23:
        raise InvalidTimeError(f"小时必须在 00–23 之间，收到 {raw}")
    if minutes > 59:
        raise InvalidTimeError(f"分钟必须在 00–59 之间，收到 {raw}")
    return hours, minutes


def to_minutes(text: object) -> int:
    hours, minutes = parse_hm(text)
    return hours * 60 + minutes


def parse_window(text: object) -> tuple[str, str] | None:
    """解析学校返回的打卡时段（如 '20:00-23:30'）；格式不对返回 None。"""
    match = _WINDOW_RE.match(str(text or ""))
    if not match:
        return None
    try:
        start = parse_hm(match.group(1))
        end = parse_hm(match.group(2))
    except InvalidTimeError:
        return None
    return f"{start[0]:02d}:{start[1]:02d}", f"{end[0]:02d}:{end[1]:02d}"


def account_checkin_window(account: dict) -> tuple[str, str] | None:
    """返回完整且有效的账号时段。"""
    start, end = account.get("checkin_start"), account.get("checkin_end")
    return parse_window(f"{start}-{end}") if start and end else None


def before_account_start(account: dict, time: str) -> bool:
    value = account.get("not_before")
    if not value:
        return False
    try:
        return to_minutes(time) < to_minutes(value)
    except InvalidTimeError:
        return False


def not_before_error(account: dict) -> str:
    window = account_checkin_window(account)
    value = account.get("not_before")
    if not window or not value:
        return ""
    try:
        start, end = map(to_minutes, window)
        time = to_minutes(value)
        if (start < end and time < start) or (start > end and end < time < start):
            return f"不能早于学校打卡开始时间 {window[0]}"
        if start < end and time > end:
            return f"不能晚于学校打卡结束时间 {window[1]}"
    except InvalidTimeError:
        return "请重新设置打卡时间"
    return ""
