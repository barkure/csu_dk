"""界面状态与文案。"""
from __future__ import annotations

from . import config as cfg
from .clock import local_now, to_local_iso
from .domain import AuthError, CheckinStatus
from .validate import to_minutes

STATUS_TEXT = {
    CheckinStatus.SUCCESS: "打卡成功", CheckinStatus.SKIPPED: "已打过卡",
    CheckinStatus.NO_TASK: "不用打卡", CheckinStatus.WAITING: "未到时间",
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


def window_passed() -> bool:
    """跨天窗口永远算"今天稍后还有机会"，不判漏打。"""
    start, end = cfg.config.checkin_window_start, cfg.config.checkin_window_end
    if not start or not end:
        return False
    start_minutes, end_minutes = to_minutes(start), to_minutes(end)
    if start_minutes > end_minutes:  # 跨午夜窗口
        return False
    return to_minutes(_now()[1]) > end_minutes


def today_result(account: dict) -> dict:
    """返回账号的今日结果。"""
    today = _now()[0][:10]
    fresh = bool(account.get("last_status")) and str(account.get("last_run_at") or "")[:10] == today
    if not fresh:
        if not account.get("enabled"):
            if account.get("last_status") == CheckinStatus.NO_TASK:
                return {"label": "不用打卡", "cls": ""}
            return {"label": "已暂停", "cls": ""}
        return {"label": "打卡失败", "cls": "err"} if window_passed() else {"label": "未到时间", "cls": ""}
    if account["last_status"] in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED):
        return {"label": "打卡成功", "cls": "ok"}
    if account["last_status"] == CheckinStatus.NO_TASK:
        return {"label": "不用打卡", "cls": ""}
    if account["last_status"] == CheckinStatus.FAILED:
        return {"label": "打卡失败", "cls": "err"}
    return {"label": "未到时间", "cls": ""}


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


def account_view(account: dict) -> dict:
    return {
        "raw": account,
        "status": account_status(account),
        "today": today_result(account),
    }
