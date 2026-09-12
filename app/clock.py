"""时间工具：一律用**本地墙上时间**的 naive datetime，存库是 'YYYY-MM-DDTHH:mm:ss'。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .validate import parse_hm

_FALLBACK_OFFSETS = {"Asia/Shanghai": 8, "Asia/Chongqing": 8, "UTC": 0}


def local_now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz)).replace(tzinfo=None, microsecond=0)
    except Exception:  # noqa: BLE001 - 时区名非法时的兜底，不该让服务起不来
        offset = _FALLBACK_OFFSETS.get(tz, 8)
        return (datetime.now(UTC) + timedelta(hours=offset)).replace(tzinfo=None, microsecond=0)


def to_local_iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def parse_local(iso: str) -> datetime:
    return datetime.fromisoformat(str(iso).replace("Z", ""))


def pick_next_run(
    window_start: str,
    window_end: str,
    tz: str,
    reference: datetime | None = None,
    min_lead_seconds: int = 60,
) -> datetime:
    """窗口内随机取时刻。

    候选窗口向前看一天：跨天窗口（23:00-01:00）在 00:30 仍算在窗口内；
    窗口内启动就在剩余时间里抽；窗口都过去了才顺延到明天。
    """
    start = parse_hm(window_start)
    end = parse_hm(window_end)
    reference = reference or local_now(tz)
    base = reference.replace(hour=start[0], minute=start[1], second=0, microsecond=0)

    span = base.replace(hour=end[0], minute=end[1]) - base
    if span.total_seconds() <= 0:
        span += timedelta(days=1)

    earliest = reference + timedelta(seconds=min_lead_seconds)
    for offset_days in (-1, 0, 1):
        begin = base + timedelta(days=offset_days)
        finish = begin + span
        low = max(begin, earliest)
        if finish - low > timedelta(0):
            import random

            return low + timedelta(seconds=random.random() * (finish - low).total_seconds())
    raise ValueError(f"无法为窗口 {window_start}-{window_end} 计算下次执行时刻")


def today_local(tz: str) -> str:
    return to_local_iso(local_now(tz))[:10]
