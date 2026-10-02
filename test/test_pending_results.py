"""待复核任务的展示与结算。"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, checkin, db, scheduler
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.errors import AppError
from app.main import app

_ids = itertools.count()


@pytest.fixture()
def account():
    now = to_local_iso(local_now(cfg.config.tz))
    user = db.upsert_user(f"pending-{next(_ids)}@example.com", now)
    row = db.insert_account({
        "user_id": user["id"], "csu_username": f"pending-{next(_ids)}",
        "password_enc": encrypt_secret("test"), "enabled": 0,
        "token": "test-token", "casual": "test-casual", "cookies": "[]", "token_at": now,
        "created_at": now, "updated_at": now,
    })
    db.update_account(row["id"], {"last_status": "failed", "last_run_at": now})
    db.upsert_verification(row["id"], now, "manual", now, now)
    yield row
    db.delete_user(user["email"])


def test_disabled_account_finishes_manual_verification(account, monkeypatch):
    school = SimpleNamespace(token="test-token", dk_status=lambda: {
        "code": "200", "data": {"sfydk": 1, "dksj": "2026-10-02 20:00:00"},
    })
    monkeypatch.setattr(checkin, "build_client", lambda _account: school)

    results = scheduler.run_verifications()

    assert [result["status"] for _, result in results] == ["success"]
    assert db.get_verification(account["id"]) is None
    assert db.get_account_by_id(account["id"])["enabled"] == 0
    assert db.list_records(account["id"])[0]["trigger"] == "manual"


def test_pending_state_survives_page_refresh(account):
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(account["user_id"]))

    for path in ("/dashboard", "/ui/accounts"):
        response = client.get(path)
        assert response.status_code == 200
        assert "等待确认" in response.text
        assert "打卡失败" not in response.text
    public = client.get("/api/accounts").json()["accounts"][0]
    assert public["verifying"] is True
    assert public["lastStatus"] == "failed"
    db.delete_verification(account["id"])
    assert client.get("/api/accounts").json()["accounts"][0]["verifying"] is False


@pytest.mark.parametrize("operation", ["checkin", "refresh", "verification"])
def test_scheduler_skips_accounts_deleted_after_selection(account, monkeypatch, operation):
    def removed(*_args, **_kwargs):
        raise AppError("账号不存在", status=404, expose=True)

    if operation == "checkin":
        monkeypatch.setattr(scheduler, "run_checkin", removed)
        db.delete_verification(account["id"])
        assert scheduler.run_batch(accounts=[account], force=True) == []
    elif operation == "refresh":
        monkeypatch.setattr(scheduler, "refresh_login", removed)
        monkeypatch.setattr(scheduler, "has_fresh_login", lambda _account: False)
        assert scheduler.refresh_logins(accounts=[dict(account, enabled=1)]) == []
    else:
        monkeypatch.setattr(scheduler, "resolve_verification", removed)
        assert scheduler.run_verifications() == []
