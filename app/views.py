"""界面状态与文案。"""
from __future__ import annotations

from . import config as cfg
from .clock import local_now, to_local_iso
from .domain import AuthError, CheckinStatus

STATUS_TEXT = {
    CheckinStatus.SUCCESS: "打卡成功", CheckinStatus.SKIPPED: "已打过卡",
    CheckinStatus.WAITING: "未到时间", CheckinStatus.FAILED: "打卡失败",
}
STATUS_CLASS = {
    CheckinStatus.SUCCESS: "ok", CheckinStatus.SKIPPED: "",
    CheckinStatus.WAITING: "", CheckinStatus.FAILED: "err",
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


def window_passed(_account: dict) -> bool:
    """跨天窗口永远算"今天稍后还有机会"，不判漏打。"""
    start, end = cfg.config.checkin_window_start, cfg.config.checkin_window_end
    if not start or not end or start > end:
        return False
    return _now()[1] > end


def today_result(account: dict) -> dict:
    """卡片只有三种结果；今天没记录时，过了窗口算失败，否则算未到时间。"""
    today = _now()[0][:10]
    fresh = bool(account.get("last_status")) and str(account.get("last_run_at") or "")[:10] == today
    if not fresh:
        if not account.get("enabled"):
            return {"label": "未到时间", "cls": ""}
        return {"label": "打卡失败", "cls": "err"} if window_passed(account) else {"label": "未到时间", "cls": ""}
    if account["last_status"] in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED):
        return {"label": "打卡成功", "cls": "ok"}
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
