"""启动自检：密码解不开的账号一律保持暂停，重启不解除任何故障。"""
from __future__ import annotations

import pytest

from app import config as cfg
from app import db
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.startup import run_startup_checks


@pytest.fixture()
def user():
    return db.upsert_user("startup-test@example.com", to_local_iso(local_now(cfg.config.tz)))


@pytest.fixture(autouse=True)
def _clean_accounts():
    for row in db.all_accounts_raw():
        db._exec("DELETE FROM accounts WHERE id = ?", (row["id"],))
    yield
    for row in db.all_accounts_raw():
        db._exec("DELETE FROM accounts WHERE id = ?", (row["id"],))


def make_account(user_id: int, username: str, *, password_enc: str, schedule: str | None) -> dict:
    now = to_local_iso(local_now(cfg.config.tz))
    account = db.insert_account({
        "user_id": user_id, "csu_username": username, "password_enc": password_enc,
        "enabled": 1, "dkdz": "升华8栋",
        "token": encrypt_secret("jwt"), "token_at": now,
        "created_at": now, "updated_at": now,
    })
    return account


BROKEN = "v1.broken.cipher"


def test_unreadable_password_is_paused_and_flagged(user):
    account = make_account(user["id"], "255000001", password_enc=BROKEN, schedule="2026-09-13T20:00:00")

    report = run_startup_checks()

    assert report["flagged"] == ["255000001"]
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "other"


def test_restart_keeps_an_existing_failure_and_pause(user):
    account = make_account(user["id"], "255000002", password_enc=BROKEN, schedule=None)
    db.set_auth_error(account["id"], "locked")

    report = run_startup_checks()

    assert report["flagged"] == ["255000002"]
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "locked", "具体原因不能被覆盖成笼统的其他故障"


def test_restart_never_unpauses_even_with_a_live_session(user):
    account = make_account(user["id"], "255000003", password_enc=BROKEN, schedule=None)
    db.set_auth_error(account["id"], "bad_credentials")
    db.update_account(account["id"], {
        "cookies": encrypt_secret('[{"name": "CASTGC", "value": "TGT-1", "domain": "ca.csu.edu.cn"}]'),
    })

    run_startup_checks()

    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "bad_credentials"


def test_healthy_account_is_left_alone(user):
    schedule = "2026-09-13T20:05:00"
    account = make_account(user["id"], "255000004", password_enc=encrypt_secret("pw"), schedule=schedule)

    report = run_startup_checks()

    assert report["flagged"] == []
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == ""


def test_missing_master_key_pauses_everything(monkeypatch, user):
    make_account(user["id"], "255000005", password_enc=encrypt_secret("pw"), schedule="2026-09-13T20:00:00")
    monkeypatch.setattr(cfg, "MASTER_KEY_PATH", cfg.MASTER_KEY_PATH.with_name("no-such-key"))
    monkeypatch.setattr(cfg, "_cached_key", None)

    report = run_startup_checks()

    assert report["flagged"] == ["255000005"]
    assert db.get_account_by_id(db.all_accounts_raw()[0]["id"])["auth_error"] == "other"
