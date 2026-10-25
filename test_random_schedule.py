"""随机调度、配置与权限回归测试；不调用学校网络。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, db, scheduler, views
from app import config as cfg
from app.crypto import encrypt_secret
from app.errors import AppError, BadRequestError
from app.main import app
from app.random_schedule import daily_plan, is_due, validate_range

NOW = datetime(2026, 10, 25, 21, 0)  # noqa: DTZ001


@pytest.fixture()
def account():
    user = db.upsert_user("random-owner@example.com", "2026-10-25T18:00:00")
    old = db.get_account_by_username(user["id"], "random-schedule-test")
    if old:
        db.delete_account(user["id"], old["id"])
    row = db.insert_account({
        "user_id": user["id"], "csu_username": "random-schedule-test",
        "password_enc": encrypt_secret("test"), "enabled": 1,
        "checkin_start": "20:00", "checkin_end": "23:30",
        "created_at": "2026-10-25T18:00:00", "updated_at": "2026-10-25T18:00:00",
    })
    accounts.set_random_window(user, row["id"], "21:00", "22:00")
    yield db.get_account_by_id(row["id"])
    db.delete_account(user["id"], row["id"])


def test_stable_after_key_cache_reset_and_new_day(account):
    first, last, target = daily_plan(account, NOW)
    assert first <= target < last
    cfg.reset_master_key_cache()
    assert daily_plan(db.get_account_by_id(account["id"]), NOW) == (first, last, target)
    tomorrow = daily_plan(account, NOW + timedelta(days=1))
    assert tomorrow[0] <= tomorrow[2] < tomorrow[1]
    assert tomorrow[2].date() == (NOW + timedelta(days=1)).date()


def test_due_boundaries_and_missed_window(account):
    first, last, target = daily_plan(account, NOW)
    assert not is_due(account, first - timedelta(seconds=1))
    assert not is_due(account, target - timedelta(seconds=1))
    assert is_due(account, target)
    assert is_due(account, last)
    assert not is_due(account, last + timedelta(seconds=1))


def test_scheduler_success_retry_limit_and_no_late_retry(account):
    target = daily_plan(account, NOW)[2]
    assert not scheduler._ready(account, target - timedelta(seconds=1))
    assert scheduler._ready(account, target)
    for offset in (0, 10, 20):
        db.add_record(account["id"], (target + timedelta(minutes=offset)).isoformat(),
                      "schedule", "failed", "test")
    assert not scheduler._ready(account, target + timedelta(minutes=30))
    successful = {**account, "last_run_at": target.isoformat(), "last_status": "success"}
    assert not scheduler._ready(successful, target + timedelta(seconds=1))
    assert not scheduler._ready(account, NOW.replace(hour=22, second=1))


@pytest.mark.parametrize(("start", "end"), [
    ("", "22:00"), ("25:00", "22:00"), ("22:00", "21:00"),
    ("21:00", "21:00"), ("19:00", "21:00"), ("23:00", "23:45"),
])
def test_invalid_or_outside_school_range(account, start, end):
    with pytest.raises(BadRequestError):
        validate_range(start, end, account)


def test_school_window_change_pauses_schedule(account):
    account["checkin_end"] = "21:30"
    assert daily_plan(account, NOW) is None
    assert not is_due(account, NOW.replace(minute=20))


def test_settings_require_owner_and_support_reset(account):
    owner = db.find_user_by_id(account["user_id"])
    other = db.upsert_user("random-other@example.com", "2026-10-25T18:00:00")
    with pytest.raises(AppError) as caught:
        accounts.set_random_window(other, account["id"], "20:30", "21:30")
    assert caught.value.status == 404
    accounts.set_random_window(owner, account["id"], "", "")
    saved = db.get_account_by_id(account["id"])
    assert saved["random_start"] is None and saved["random_end"] is None
    assert is_due(saved, NOW)


def test_ui_authenticated_save_and_anonymous_redirect(account):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    url = f"/ui/accounts/{account['id']}/schedule"
    assert client.post(url, data={"start": "20:30", "end": "22:00"},
                       follow_redirects=False).status_code == 303
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(account["user_id"], "random-test"))
    response = client.post(url, data={"start": "20:30", "end": "22:00"})
    assert response.status_code == 200
    assert "已保存" in response.text and 'value="20:30"' in response.text
    assert db.get_account_by_id(account["id"])["random_start"] == "20:30"
    assert "今日随机时间" in response.text


def test_old_database_adds_columns_without_losing_accounts():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(db._SCHEMA)
        connection.execute("ALTER TABLE accounts DROP COLUMN random_start")
        connection.execute("ALTER TABLE accounts DROP COLUMN random_end")
        connection.execute("INSERT INTO users (email,created_at) VALUES ('keep@example.com','now')")
        connection.commit()
        db._ensure_schema(connection)
        assert connection.execute("SELECT email FROM users").fetchone()[0] == "keep@example.com"
        assert {"random_start", "random_end"} <= db._structure(connection)["accounts"]
    finally:
        connection.close()


def owner_client(account):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(account["user_id"], "random-api-test"))
    return client


def test_api_update_preserves_omitted_schedule_and_clears_explicit_pair(account):
    client = owner_client(account)
    url = f"/api/accounts/{account['id']}"
    response = client.patch(url, json={"randomStart": "20:30", "randomEnd": "22:30"})
    assert response.status_code == 200, response.text
    saved = response.json()["account"]
    assert (saved["randomStart"], saved["randomEnd"]) == ("20:30", "22:30")
    assert saved["randomCheckinAt"] is not None
    response = client.patch(url, json={"enabled": False})
    assert response.json()["account"]["randomStart"] == "20:30"
    assert not response.json()["account"]["enabled"]
    response = client.patch(url, json={"randomStart": None, "randomEnd": None})
    cleared = response.json()["account"]
    assert cleared["randomStart"] is None and cleared["randomEnd"] is None
    assert cleared["randomCheckinAt"] is None


@pytest.mark.parametrize("payload", [
    {"randomStart": "21:30"}, {"randomEnd": "22:00"},
    {"randomStart": "", "randomEnd": "22:00"},
    {"randomStart": "19:00", "randomEnd": "22:00"},
])
def test_api_rejects_partial_or_invalid_ranges_without_changing_saved_value(account, payload):
    response = owner_client(account).patch(f"/api/accounts/{account['id']}", json=payload)
    assert response.status_code == 400
    assert db.get_account_by_id(account["id"])["random_start"] == "21:00"


def test_api_other_user_cannot_change_schedule(account):
    other = db.upsert_user("random-api-other@example.com", "2026-10-25T18:00:00")
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(other["id"], "other-test"))
    response = client.patch(f"/api/accounts/{account['id']}",
                            json={"randomStart": "20:30", "randomEnd": "22:30"})
    assert response.status_code == 404
    assert db.get_account_by_id(account["id"])["random_start"] == "21:00"


@pytest.mark.parametrize("channel", ["api", "ui"])
def test_create_saves_schedule_atomically_before_account_is_enabled(account, monkeypatch, channel):
    monkeypatch.setattr(accounts, "verify_login", lambda *_args, **_kwargs: {
        "session": {"token": "test-token", "casual": "test", "cookies": "[]"},
        "data": {"dksjfw": "20:00-23:30"},
    })
    client = owner_client(account)
    payload = {"csuUsername": f"random-create-{channel}", "password": "test",
               "randomStart": "21:15", "randomEnd": "22:15"}
    try:
        if channel == "api":
            response = client.post("/api/accounts", json=payload)
        else:
            response = client.post("/ui/accounts", data=payload)
        assert response.status_code == 200, response.text
        saved = db.get_account_by_username(account["user_id"], payload["csuUsername"])
        assert (saved["random_start"], saved["random_end"]) == ("21:15", "22:15")
        assert saved["enabled"] == 1
        assert not scheduler._ready(saved, NOW)
    finally:
        saved = db.get_account_by_username(account["user_id"], payload["csuUsername"])
        if saved:
            db.delete_account(account["user_id"], saved["id"])


def test_api_add_without_schedule_keeps_default_behavior(account, monkeypatch):
    monkeypatch.setattr(accounts, "verify_login", lambda *_args, **_kwargs: {
        "session": {"token": "test-token", "casual": "test", "cookies": "[]"},
        "data": {"dksjfw": "20:00-23:30"},
    })
    try:
        response = owner_client(account).post("/api/accounts", json={
            "csuUsername": "random-default-create", "password": "test",
        })
        assert response.status_code == 200, response.text
        saved = db.get_account_by_id(response.json()["account"]["id"])
        assert saved["random_start"] is None and saved["random_end"] is None
        assert scheduler._ready(saved, NOW)
    finally:
        saved = db.get_account_by_username(account["user_id"], "random-default-create")
        if saved:
            db.delete_account(account["user_id"], saved["id"])


def test_waiting_retry_respects_interval_and_window_end(account):
    first, last, target = daily_plan(account, NOW)
    assert first <= target < last
    db.add_record(account["id"], target.isoformat(), "schedule", "waiting", "test")
    assert not scheduler._ready(account, target + timedelta(minutes=9))
    if target + timedelta(minutes=10) <= last:
        assert scheduler._ready(account, target + timedelta(minutes=10))
    assert not scheduler._ready(account, last + timedelta(seconds=1))


def test_view_waits_for_target_and_displays_expired_window(account, monkeypatch):
    first, last, target = daily_plan(account, NOW)
    monkeypatch.setattr(views, "local_now", lambda _tz: first - timedelta(seconds=1))
    assert views.today_result(account)["label"] == f"等待 {target:%H:%M:%S}"
    monkeypatch.setattr(views, "local_now", lambda _tz: last + timedelta(seconds=1))
    assert views.today_result(account)["label"] == "随机时段已结束"


def test_unconfigured_account_never_reads_master_key(monkeypatch):
    monkeypatch.setattr(cfg, "read_master_key", lambda: pytest.fail("默认调度不应读取随机计划密钥"))
    assert daily_plan({"id": 1}, NOW) is None
    assert is_due({"id": 1}, NOW)


def test_missing_master_key_stops_random_plan_without_crashing(account, monkeypatch):
    from app.errors import MasterKeyMissingError

    def missing():
        raise MasterKeyMissingError(cfg.MASTER_KEY_PATH)

    monkeypatch.setattr(cfg, "read_master_key", missing)
    assert daily_plan(account, NOW) is None
    assert not is_due(account, NOW)


def test_ui_other_user_cannot_change_schedule(account):
    other = db.upsert_user("random-ui-other@example.com", "2026-10-25T18:00:00")
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(other["id"], "other-ui-test"))
    response = client.post(f"/ui/accounts/{account['id']}/schedule",
                           data={"start": "20:30", "end": "22:30"})
    assert response.status_code == 200
    assert 'value="20:30"' not in response.text
    assert db.get_account_by_id(account["id"])["random_start"] == "21:00"


def test_ui_default_form_leaves_random_range_blank(account):
    from bs4 import BeautifulSoup

    response = owner_client(account).get("/ui/form")
    form = BeautifulSoup(response.text, "html.parser")
    assert form.select_one('input[name="randomStart"]').get("value") == ""
    assert form.select_one('input[name="randomEnd"]').get("value") == ""


def test_batch_rechecks_deadline_before_each_request(account, monkeypatch):
    other = {**account, "id": account["id"] + 100_000}
    just_before_end = NOW.replace(hour=21, minute=59, second=59)
    after_end = NOW.replace(hour=22, minute=0, second=1)
    clock = iter([just_before_end, just_before_end, after_end])
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: next(clock))
    monkeypatch.setattr(scheduler.random, "sample", lambda population, k: list(population)[:k])
    calls = []
    monkeypatch.setattr(scheduler, "run_checkin", lambda selected, trigger: (
        calls.append(selected["id"]) or {"status": "success", "message": "test"}
    ))
    result = scheduler.run_batch(accounts=[account, other])
    assert calls == [account["id"]]
    assert len(result) == 1


def test_ui_edit_preserves_legacy_omission_and_accepts_explicit_empty_fields(account, monkeypatch):
    monkeypatch.setattr(accounts, "verify_login", lambda *_args, **_kwargs: {
        "session": {"token": "test-token", "casual": "test", "cookies": "[]"},
        "data": {"dksjfw": "20:00-23:30"},
    })
    client = owner_client(account)
    response = client.post("/ui/accounts", data={"csuUsername": account["csu_username"]})
    assert response.status_code == 200
    assert db.get_account_by_id(account["id"])["random_start"] == "21:00"
    response = client.post("/ui/accounts", data={
        "csuUsername": account["csu_username"], "randomRange": "true", "randomStart": "", "randomEnd": "",
    })
    assert response.status_code == 200
    cleared = db.get_account_by_id(account["id"])
    assert cleared["random_start"] is None and cleared["random_end"] is None
