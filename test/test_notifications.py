"""账号异常通知的持久化重试。"""
from __future__ import annotations

import itertools
import subprocess
import sys
from datetime import timedelta
from unittest.mock import Mock

import pytest

from app import checkin, db, notifications
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret

_ids = itertools.count()


@pytest.fixture()
def account():
    now = to_local_iso(local_now(cfg.config.tz))
    user = db.upsert_user(f"notice-{next(_ids)}@example.com", now)
    row = db.insert_account({
        "user_id": user["id"], "csu_username": f"notice-{next(_ids)}",
        "password_enc": encrypt_secret("test"), "enabled": 1,
        "created_at": now, "updated_at": now,
    })
    yield row
    db.delete_user(user["email"])


def reject(account):
    error = RuntimeError("学号或密码错误")
    checkin.mark_auth_failure(account["id"], error, str(error))


def test_failed_notice_waits_then_retries(account, monkeypatch):
    now = local_now(cfg.config.tz)
    monkeypatch.setattr(notifications, "local_now", lambda _tz: now)
    sender = Mock(side_effect=[RuntimeError("邮件暂不可用"), {"sent": True}])
    monkeypatch.setattr(notifications, "send_credential_invalid_notice", sender)

    reject(account)
    task = db.get_credential_notice(account["id"])
    assert task["attempts"] == 1
    assert task["next_at"] == to_local_iso(now + timedelta(seconds=notifications.NOTICE_RETRY_SECONDS))
    assert db.get_account_by_id(account["id"])["enabled"] == 0

    notifications.retry_credential_notices()
    assert sender.call_count == 1
    now += timedelta(seconds=notifications.NOTICE_RETRY_SECONDS)
    notifications.retry_credential_notices()
    assert sender.call_count == 2
    assert db.get_credential_notice(account["id"]) is None


@pytest.mark.parametrize("result", [RuntimeError("发送失败"), {"sent": False}])
def test_notice_attempts_are_bounded(account, monkeypatch, result):
    now = local_now(cfg.config.tz)
    monkeypatch.setattr(notifications, "local_now", lambda _tz: now)
    sender = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(notifications, "send_credential_invalid_notice", sender)

    reject(account)
    for _ in range(notifications.MAX_NOTICE_ATTEMPTS + 1):
        now += timedelta(seconds=notifications.NOTICE_RETRY_SECONDS)
        notifications.retry_credential_notices()

    assert sender.call_count == notifications.MAX_NOTICE_ATTEMPTS
    assert db.get_credential_notice(account["id"])["attempts"] == notifications.MAX_NOTICE_ATTEMPTS
    reject(account)
    assert sender.call_count == notifications.MAX_NOTICE_ATTEMPTS


@pytest.mark.parametrize("recovery", ["update", "clear", "delete"])
def test_recovery_or_deletion_cancels_pending_notice(account, monkeypatch, recovery):
    sender = Mock(side_effect=RuntimeError("发送失败"))
    monkeypatch.setattr(notifications, "send_credential_invalid_notice", sender)
    reject(account)
    if recovery == "update":
        db.update_account(account["id"], {"auth_error": "", "enabled": 1})
    elif recovery == "clear":
        db.set_auth_error(account["id"], "")
    else:
        db.delete_account(account["user_id"], account["id"])

    notifications.retry_credential_notices()
    assert sender.call_count == 1
    assert db.get_credential_notice(account["id"]) is None


def test_notification_resumes_in_a_new_process(account, monkeypatch):
    monkeypatch.setattr(notifications, "send_credential_invalid_notice", Mock(side_effect=RuntimeError("发送失败")))
    reject(account)
    db._exec("UPDATE credential_notices SET next_at = ? WHERE account_id = ?",
             ("2000-01-01T00:00:00", account["id"]))
    script = """
from app import notifications
def send(*args):
    print('simulated-mail')
    return {'sent': True}
notifications.send_credential_invalid_notice = send
notifications.retry_credential_notices()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("simulated-mail") == 1
    assert db.get_credential_notice(account["id"]) is None


def test_auth_error_and_notice_are_committed_together(account, monkeypatch):
    def fail(*_args):
        raise RuntimeError("写入任务失败")

    monkeypatch.setattr(db, "queue_credential_notice", fail)
    with pytest.raises(RuntimeError, match="写入任务失败"):
        reject(account)

    saved = db.get_account_by_id(account["id"])
    assert saved["auth_error"] == ""
    assert saved["enabled"] == 1


def test_tick_processes_notices(monkeypatch):
    from app import scheduler

    retry = Mock()
    monkeypatch.setattr(scheduler, "_lease_owner", None)
    monkeypatch.setattr(scheduler, "run_verifications", lambda: [])
    monkeypatch.setattr(scheduler, "run_batch", lambda: [])
    monkeypatch.setattr(scheduler, "refresh_logins", lambda: [])
    monkeypatch.setattr(scheduler, "retry_credential_notices", retry)

    scheduler.tick()

    retry.assert_called_once_with()
