"""纯校验/解析函数：不碰数据库和网络，可直接单测。"""
from __future__ import annotations

import re

from .errors import InvalidTimeError

_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


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
