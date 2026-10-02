"""账号编辑、启停与删除的并发边界。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from app import accounts, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.errors import AppError
from app.locks import lock_for


@pytest.fixture()
def saved_account(monkeypatch):
    now = to_local_iso(local_now(cfg.config.tz))
    user = db.upsert_user("mutation-test@example.com", now)
    account = db.insert_account({
        "user_id": user["id"], "csu_username": "mutation-test", "password_enc": encrypt_secret("old"),
        "enabled": 1, "dkdz": "旧楼栋", "jd": 112.9, "wd": 28.1, "created_at": now, "updated_at": now,
    })
    monkeypatch.setattr(accounts, "verify_login", lambda *_args, **_kwargs: {
        "session": {"token": "new-token", "casual": "", "cookies": "[]"},
    })
    yield user, account
    db.delete_user(user["email"])


def watch_account_lock(monkeypatch):
    waiting = threading.Event()

    @contextmanager
    def observed_lock(key):
        if key.startswith("account:"):
            waiting.set()
        with lock_for(key):
            yield

    monkeypatch.setattr(accounts, "lock_for", observed_lock)
    return waiting


def test_edit_preserves_state_changed_while_waiting(saved_account, monkeypatch):
    user, account = saved_account
    waiting = watch_account_lock(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock_for(f"account:{account['id']}"):
            future = pool.submit(accounts.create_or_update, user, {
                "csuUsername": account["csu_username"], "password": "new",
            })
            assert waiting.wait(2)
            db.update_account(account["id"], {"enabled": 0, "dkdz": "新楼栋", "jd": 113.1, "wd": 28.2})
        future.result(timeout=2)

    saved = db.get_account_by_id(account["id"])
    assert (saved["enabled"], saved["dkdz"], saved["jd"], saved["wd"]) == (0, "新楼栋", 113.1, 28.2)
    assert saved["token"] == "new-token"


def test_deleted_account_is_not_recreated_by_waiting_edit(saved_account, monkeypatch):
    user, account = saved_account
    waiting = watch_account_lock(monkeypatch)
    probes = []
    monkeypatch.setattr(accounts, "verify_login", lambda *_args, **_kwargs: probes.append(True))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock_for(f"account:{account['id']}"):
            future = pool.submit(accounts.create_or_update, user, {
                "csuUsername": account["csu_username"], "password": "new",
            })
            assert waiting.wait(2)
            db.delete_account(user["id"], account["id"])
        with pytest.raises(AppError, match="账号不存在"):
            future.result(timeout=2)

    assert probes == []
    assert db.get_account_by_username(user["id"], account["csu_username"]) is None


@pytest.mark.parametrize("operation", ["patch", "set", "toggle", "delete"])
def test_mutation_waits_for_account_task(saved_account, monkeypatch, operation):
    user, account = saved_account
    waiting = watch_account_lock(monkeypatch)
    calls = {
        "patch": lambda: accounts.update(user, account["id"], {"enabled": False}),
        "set": lambda: accounts.set_enabled(user, account["id"], False, source="api"),
        "toggle": lambda: accounts.toggle_enabled(user, account["id"], source="ui"),
        "delete": lambda: accounts.delete(user, account["id"]),
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock_for(f"account:{account['id']}"):
            future = pool.submit(calls[operation])
            assert waiting.wait(2)
            assert not future.done()
            assert db.get_account_by_id(account["id"])["enabled"] == 1
        future.result(timeout=2)

    saved = db.get_account_by_id(account["id"])
    assert saved is None if operation == "delete" else saved["enabled"] == 0


@pytest.mark.parametrize("source", ["api", "ui"])
def test_password_recovery_emits_one_enable_event(saved_account, monkeypatch, source):
    user, account = saved_account
    db.update_account(account["id"], {"enabled": 0, "auth_error": "bad_credentials"})
    events = []
    monkeypatch.setattr("app.log.log_event", lambda event, **fields: events.append((event, fields)))
    payload = {"csuUsername": account["csu_username"], "password": "new"}

    accounts.create_or_update(user, payload, source=source)
    accounts.create_or_update(user, payload, source=source)

    assert db.get_account_by_id(account["id"])["enabled"] == 1
    assert events == [("account.enabled_changed", {
        "account_id": account["id"], "user_id": user["id"], "csu_username_tail": "test",
        "enabled": True, "source": source,
    })]


def test_post_existing_account_audits_explicit_disable(saved_account, monkeypatch):
    user, account = saved_account
    events = []
    monkeypatch.setattr("app.log.log_event", lambda event, **fields: events.append((event, fields)))
    accounts.create_or_update(user, {"csuUsername": account["csu_username"], "enabled": False})
    assert db.get_account_by_id(account["id"])["enabled"] == 0
    assert events == [("account.enabled_changed", {
        "account_id": account["id"], "user_id": user["id"], "csu_username_tail": "test",
        "enabled": False, "source": "api",
    })]


def test_two_toggles_restore_original_state(saved_account, monkeypatch):
    user, account = saved_account
    waiting = watch_account_lock(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with lock_for(f"account:{account['id']}"):
            futures = [pool.submit(accounts.toggle_enabled, user, account["id"], source="ui") for _ in range(2)]
            assert waiting.wait(2)
        assert all(future.result(timeout=2) for future in futures)
    assert db.get_account_by_id(account["id"])["enabled"] == 1
