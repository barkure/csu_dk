"""纯校验/解析函数：不碰数据库和网络，可直接单测。"""
from __future__ import annotations

import math
import re
from typing import NamedTuple

from .errors import BadRequestError, InvalidTimeError


class Window(NamedTuple):
    start: str
    end: str


_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WINDOW_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-~—–至到]\s*(\d{1,2}:\d{2})")


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


def format_hm(value: tuple[int, int]) -> str:
    return f"{value[0]:02d}:{value[1]:02d}"


def to_minutes(text: object) -> int:
    hours, minutes = parse_hm(text)
    return hours * 60 + minutes


def from_minutes(total: float) -> str:
    value = (round(total) % 1440 + 1440) % 1440
    return f"{value // 60:02d}:{value % 60:02d}"


def window_span_hours(start: object, end: object) -> float:
    """跨天窗口（23:00-01:00）按 +24h 计。"""
    span = to_minutes(end) - to_minutes(start)
    return (span + 1440 if span <= 0 else span) / 60


def validate_window(start: str, end: str, max_hours: float | None = None) -> tuple[str, str, float]:
    span = window_span_hours(start, end)
    if span <= 0:
        raise BadRequestError("打卡窗口无效：开始与结束时间相同", "invalid_window")
    if max_hours and span > max_hours:
        raise BadRequestError(f"打卡窗口跨度 {span:.1f} 小时过大（上限 {max_hours:g} 小时）", "window_too_wide")
    return start, end, span


def validate_coordinates(jd: object, wd: object) -> tuple[float, float]:
    if (isinstance(jd, bool) or isinstance(wd, bool)
            or not isinstance(jd, (int, float)) or not isinstance(wd, (int, float))):
        raise BadRequestError("经纬度必须是有效数字", "invalid_coord")
    jd, wd = float(jd), float(wd)
    if math.isnan(jd) or math.isnan(wd) or math.isinf(jd) or math.isinf(wd):
        raise BadRequestError("经纬度必须是有效数字", "invalid_coord")
    if not -180 <= jd <= 180:
        raise BadRequestError("经度必须在 -180–180 之间", "invalid_coord")
    if not -90 <= wd <= 90:
        raise BadRequestError("纬度必须在 -90–90 之间", "invalid_coord")
    return jd, wd


def parse_window_text(text: object) -> Window | None:
    """学校返回的窗口文本（外部数据），解析不出来返回 None 由调用方兜底。"""
    match = _WINDOW_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return Window(format_hm(parse_hm(match.group(1))), format_hm(parse_hm(match.group(2))))
    except InvalidTimeError:
        return None


def parse_coords_input(text: object) -> tuple[float, float] | None:
    """解析输入框里的经纬度：分隔符允许中英文逗号与空格；明显填反了就自动换回来。"""
    parts = [part for part in re.split(r"[,，\s]+", str(text or "").strip()) if part]
    if len(parts) != 2:
        return None
    try:
        jd, wd = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if abs(wd) > 90 and abs(jd) <= 90:
        jd, wd = wd, jd
    if abs(jd) > 180 or abs(wd) > 90:
        return None
    return jd, wd


def apply_window_margin(window: Window | tuple[str, str] | None, margin_minutes: float | None = None) -> Window | None:
    """把窗口尾部提前 margin_minutes（至少保留 30 分钟）。"""
    if not window or not margin_minutes:
        return window
    start, end = window
    start_minutes = to_minutes(start)
    span = to_minutes(end) - start_minutes
    if span <= 0:
        span += 1440
    span = max(30, span - margin_minutes)
    return Window(format_hm(parse_hm(start)), from_minutes(start_minutes + span))
