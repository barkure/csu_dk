"""界面状态与文案。"""
from __future__ import annotations

from . import config as cfg
from .clock import local_now, to_local_iso
from .domain import AuthError, CheckinStatus
from .validate import account_checkin_window, account_window_state, to_minutes

STATUS_TEXT = {
    CheckinStatus.SUCCESS: "打卡成功", CheckinStatus.SKIPPED: "已打过卡",
    CheckinStatus.NO_TASK: "无需打卡", CheckinStatus.WAITING: "未到时间",
    CheckinStatus.FAILED: "打卡失败",
}
BADGE_CLASS = {
    CheckinStatus.SUCCESS: "ok", CheckinStatus.SKIPPED: "",
    CheckinStatus.NO_TASK: "", CheckinStatus.WAITING: "", CheckinStatus.FAILED: "err",
}


def status_label(status: str | None) -> str:
    return STATUS_TEXT.get(status or "", status or "")


def fmt(iso: str | None) -> str:
    return iso.replace("T", " ")[5:16] if iso else "—"


def fmt_full(iso: str | None) -> str:
    return iso.replace("T", " ")[:16] if iso else "—"


def _now() -> tuple[str, str]:
    now = local_now(cfg.config.tz)
    return to_local_iso(now), to_local_iso(now)[11:16]


def window_state(start: str, end: str) -> str:
    """返回当前时段状态：before / inside / after。"""
    start_minutes, end_minutes = to_minutes(start), to_minutes(end)
    now_minutes = to_minutes(_now()[1])
    if start_minutes > end_minutes:
        return "inside" if now_minutes >= start_minutes or now_minutes <= end_minutes else "before"
    if now_minutes > end_minutes:
        return "after"
    return "inside" if now_minutes >= start_minutes else "before"


def today_result(account: dict, *, verifying: bool = False) -> dict:
    """返回账号的今日结果。"""
    if verifying:
        return {"label": "等待确认", "cls": ""}
    today = _now()[0][:10]
    fresh = bool(account.get("last_status")) and str(account.get("last_run_at") or "")[:10] == today
    if fresh and account["last_status"] in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED):
        return {"label": "打卡成功", "cls": "ok"}
    if fresh and account["last_status"] == CheckinStatus.NO_TASK:
        return {"label": "无需打卡", "cls": ""}
    if fresh and account["last_status"] == CheckinStatus.FAILED:
        return {"label": "打卡失败", "cls": "err"}
    if not fresh and not account.get("enabled"):
        return {"label": "已暂停", "cls": ""}
    state = _account_window_state(account)
    if state == "after":
        return {"label": "时段已结束", "cls": ""}
    return {"label": "排队打卡中", "cls": ""} if state == "inside" else {"label": "未到时间", "cls": ""}


def _account_window_state(account: dict) -> str:
    return account_window_state(account, _now()[1], cfg.config.checkin_start)


AUTH_STATUS = {
    AuthError.NONE: ("正常", "ok"),
    AuthError.BAD_CREDENTIALS: ("密码错误", "err"),
    AuthError.LOCKED: ("账号锁定", "err"),
    AuthError.OTHER: ("其他故障", "err"),
}


def account_status(account: dict) -> dict:
    raw = str(account.get("auth_error") or "")
    text, cls = AUTH_STATUS.get(AuthError(raw) if raw in set(AuthError) else AuthError.OTHER,
                                AUTH_STATUS[AuthError.OTHER])
    return {"text": text, "cls": cls}


def account_window(account: dict) -> str:
    """返回账号打卡时段，未知时返回空字符串。"""
    window = account_checkin_window(account)
    return "–".join(window) if window else ""


def account_view(account: dict, *, verifying: bool = False) -> dict:
    return {
        "raw": account,
        "status": account_status(account),
        "today": today_result(account, verifying=verifying),
        "window": account_window(account),
    }
