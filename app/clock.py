"""时间工具：一律用**本地墙上时间**的 naive datetime，存库是 'YYYY-MM-DDTHH:mm:ss'。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
