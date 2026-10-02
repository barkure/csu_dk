"""账号异常通知及失败重试。"""
from __future__ import annotations

from datetime import timedelta

from . import config as cfg
from . import db
from .clock import local_now, to_local_iso
from .domain import AuthError
from .locks import lock_for
from .log import log_event
from .mailer import send_credential_invalid_notice
from .redaction import scrub_detail

MAX_NOTICE_ATTEMPTS = 3
NOTICE_RETRY_SECONDS = 300


def send_credential_notice(account_id: int) -> None:
    """发送到期通知，失败时保留重试任务。"""
    with lock_for(f"notice:{account_id}"):
        task = db.get_credential_notice(account_id)
        if not task:
            return
        target = db.get_account_notification_target(account_id)
        if not target or target["auth_error"] != AuthError.BAD_CREDENTIALS:
            db.delete_credential_notice(account_id)
            return
        now = local_now(cfg.config.tz)
        if task["attempts"] >= MAX_NOTICE_ATTEMPTS or task["next_at"] > to_local_iso(now):
            return
        # 先记录尝试，进程重启后也受次数上限约束。
        db.attempt_credential_notice(account_id, to_local_iso(now + timedelta(seconds=NOTICE_RETRY_SECONDS)))
        try:
            result = send_credential_invalid_notice(target["email"], target["csu_username"])
        except Exception as error:  # noqa: BLE001 - 邮件失败不影响账号状态
            log_event("account.credential_invalid_notify_failed", level="warning",
                      account_id=account_id, attempt=task["attempts"] + 1,
                      error=scrub_detail(str(error)))
            return
        if result["sent"]:
            db.delete_credential_notice(account_id)
        log_event("account.credential_invalid_notified", account_id=account_id,
                  sent=result["sent"], attempt=task["attempts"] + 1)


def retry_credential_notices(limit: int = 1) -> None:
    now = to_local_iso(local_now(cfg.config.tz))
    for task in db.due_credential_notices(now, MAX_NOTICE_ATTEMPTS, limit):
        with lock_for(f"account:{task['account_id']}"):
            send_credential_notice(task["account_id"])
