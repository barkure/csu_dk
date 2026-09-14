"""接口层测试：不联网、不碰学校（要真实登录的路径在 test_e2e.py）。"""
from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, checkin, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.main import app
from app.ratelimit import SlidingWindow


@pytest.fixture()
def client():
    for limiter in auth.limiters.values():
        limiter.reset()
    return TestClient(app)


def inject_code(email: str, code: str, minutes: int = 10) -> None:
    now = local_now(cfg.config.tz)
    db.insert_login_code(
        email,
        auth.hash_login_code(email, code),
        to_local_iso(now + timedelta(minutes=minutes)),   # 有效期要真的在未来
        to_local_iso(now),
    )


def login(client: TestClient, email: str) -> None:
    inject_code(email, "424242")
    response = client.post("/api/auth/verify", json={"email": email, "code": "424242"})
    assert response.status_code == 200, response.text


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["checkinWindow"] == {"start": "20:00", "end": "23:30"}
    assert body["mail"] is False  # 测试环境没配腾讯云邮件推送


def test_requires_login(client):
    assert client.get("/api/accounts").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_invalid_email_is_400_not_500(client):
    response = client.post("/api/auth/verify", json={"email": "not-an-email", "code": "123456"})
    assert response.status_code == 400
    assert "邮箱" in response.json()["error"]
    assert client.post("/api/auth/request-code", json={"email": "nope"}).status_code == 400


def test_login_flow_sets_session(client):
    login(client, "student-a@example.com")
    body = client.get("/api/auth/me").json()
    assert body["user"]["email"] == "student-a@example.com"


def test_wrong_code_rejected(client):
    inject_code("student-bad@example.com", "111111")
    response = client.post("/api/auth/verify", json={"email": "student-bad@example.com", "code": "222222"})
    assert response.status_code == 400
    assert "不正确" in response.json()["error"]


def test_logout_invalidates_session(client):
    login(client, "student-c@example.com")
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_ip_rate_limit_returns_429_with_retry_after(client):
    auth.limiters["request_ip"] = SlidingWindow(600_000, 5)
    last = None
    for index in range(6):
        last = client.post("/api/auth/request-code", json={"email": f"limit-{index}@example.com"})
    assert last.status_code == 429
    assert last.json()["code"] == "rate_limited"
    assert int(last.headers["retry-after"]) > 0


def test_oversized_body_rejected(client):
    response = client.post("/api/auth/verify", json={"email": "a@b.c", "code": "x" * 200_000})
    assert response.status_code == 413


def make_account(user_id: int, username: str, **overrides) -> dict:
    now = to_local_iso(local_now(cfg.config.tz))
    row = {
        "user_id": user_id, "csu_username": username, "password_enc": encrypt_secret("whatever"),
        "enabled": 1,
        "jd": 112.936833, "wd": 28.157238, "dkdz": "", "created_at": now, "updated_at": now,
    }
    row.update(overrides)
    return db.insert_account(row)


@pytest.fixture()
def owner(client):
    login(client, "owner@example.com")
    user = db.find_user_by_email("owner@example.com")
    username = f"9{next(_usernames):08d}"
    account = make_account(user["id"], username)
    yield client, user, account
    db.delete_account(user["id"], account["id"])


_usernames = itertools.count(1)


@pytest.fixture()
def taken_username(owner):
    return owner[2]["csu_username"]


def test_add_account_during_login_pause_returns_503(owner, real_login):
    client, user, _ = owner
    checkin.pause_logins("测试：模拟学校冻结")
    username = "977400001"

    response = client.post("/api/accounts", json={
        "csuUsername": username, "password": "x", "jd": 112.936833, "wd": 28.157238,
    })

    assert response.status_code == 503, response.text
    assert "风控" in response.json()["error"]
    assert db.get_account_by_username(user["id"], username) is None


def test_account_limit_per_user(owner, monkeypatch):
    client, _, _ = owner
    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(update={"max_accounts_per_user": 1}))
    response = client.post("/api/accounts", json={
        "csuUsername": "299999999", "password": "x", "jd": 112.9, "wd": 28.1,
    })
    assert response.status_code == 400
    assert "最多只能托管" in response.json()["error"]


def test_list_returns_own_accounts(owner):
    client, _, account = owner
    accounts = client.get("/api/accounts").json()["accounts"]
    mine = [item for item in accounts if item["id"] == account["id"]]
    assert len(mine) == 1
    assert mine[0]["csuUsername"] == account["csu_username"]


def test_post_without_coordinates_only_verifies_login(owner, monkeypatch):
    client, user, _ = owner
    seen = {}

    def fake_verify(username, password, **kwargs):
        seen.update({"username": username, **kwargs})
        return {"location": None, "address": None, "jd": None, "wd": None, "session": {},
                "window": ("20:00", "22:30")}

    monkeypatch.setattr("app.accounts.verify_login", fake_verify)
    response = client.post("/api/accounts", json={"csuUsername": "988888888", "password": "x"})
    assert response.status_code == 200, response.text
    assert seen["username"] == "988888888"
    account = db.get_account_by_username(user["id"], "988888888")
    assert account["jd"] is None and account["wd"] is None and account["dkdz"] == ""
    db.delete_account(user["id"], account["id"])


def test_post_existing_account_without_coordinates_uses_saved_ones(owner):
    client, _, account = owner
    response = client.post("/api/accounts", json={
        "csuUsername": account["csu_username"], "password": "whatever",
    })
    assert response.status_code == 400
    assert "经纬度" not in response.json()["error"]


def test_disable_account(owner):
    client, _, account = owner
    client.patch(f"/api/accounts/{account['id']}", json={"enabled": False})
    assert db.get_account_by_id(account["id"])["enabled"] == 0


def test_accounts_are_isolated_per_user(owner):
    _, _, account = owner
    other = TestClient(app)
    login(other, "outsider@example.com")

    assert other.get("/api/accounts").json()["accounts"] == []
    assert other.patch(f"/api/accounts/{account['id']}", json={"jd": 1, "wd": 1}).status_code == 404
    assert other.delete(f"/api/accounts/{account['id']}").status_code == 404
    assert other.get(f"/api/accounts/{account['id']}/records").status_code == 404
    assert other.post(f"/api/accounts/{account['id']}/run").status_code == 404
    assert db.get_account_by_id(account["id"])["jd"] == 112.936833


def test_static_pages_not_cached(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "妙妙道具" in response.text
    assert response.headers["cache-control"] == "no-cache"

    asset = client.get("/static/app.css")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-cache"


def test_dashboard_requires_login(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_dashboard_renders_accounts_after_login(client):
    login(client, "ui@example.com")
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "账号列表" in response.text
    assert "ui@example.com" in response.text


def test_add_account_happy_path_stores_everything(owner, monkeypatch):
    client, user, _ = owner
    monkeypatch.setattr("app.accounts.verify_login", lambda *_args, **_kw: {
        "location": {"canDk": True, "yxMc": "升华8栋"},
        "address": "升华8栋",
        "session": {"token": "tok", "casual": "cas", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })

    response = client.post("/api/accounts", json={
        "csuUsername": "977300001", "password": "x", "jd": 112.936833, "wd": 28.157238,
    })
    assert response.status_code == 200, response.text
    account = db.get_account_by_username(user["id"], "977300001")
    assert account["enabled"] == 1
    assert account["dkdz"] == ""
    assert account["token"] == "tok"
    assert account["auth_error"] == ""
    db.delete_account(user["id"], account["id"])


def test_add_account_happy_path_via_ui_form(owner, monkeypatch):
    client, user, _ = owner
    seen = {}

    def fake_verify(username, password, **kwargs):
        seen.update({"username": username, **kwargs})
        return {"location": None, "address": None, "jd": None, "wd": None,
                "session": {"token": "tok2", "casual": "cas2", "cookies": "[]"},
                "window": ("20:00", "22:30")}

    monkeypatch.setattr("app.accounts.verify_login", fake_verify)

    html = client.post("/ui/accounts", data={"csuUsername": "977300002", "password": "x"}).text
    assert "验证通过，已保存" in html
    account = db.get_account_by_username(user["id"], "977300002")
    assert (account["jd"], account["wd"]) == (None, None), "定位留到首次打卡"
    assert account["token"] == "tok2"
    db.delete_account(user["id"], account["id"])


def test_captcha_consume_rolls_back_when_session_creation_fails(client, monkeypatch):
    from app import auth

    inject_code("rollback@example.com", "777777")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟建会话失败")

    monkeypatch.setattr(auth, "_insert_session", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/auth/verify", json={"email": "rollback@example.com", "code": "777777"})

    after = db.latest_login_code("rollback@example.com")
    assert after["used"] == 0, "事务回滚后验证码应当还是可用的"


def test_edit_account_without_coordinates_keeps_them_empty(owner, monkeypatch):
    client, user, _ = owner
    monkeypatch.setattr("app.accounts.verify_login", lambda *_a, **_kw: {
        "location": None, "address": None, "jd": None, "wd": None, "session": {}, "window": (None, None),
    })
    created = client.post("/api/accounts", json={"csuUsername": "977400001", "password": "x"}).json()
    account = db.get_account_by_username(user["id"], "977400001")
    assert created["account"]["csuUsername"] == "977400001"
    assert account["jd"] is None and account["wd"] is None

    response = client.post("/api/accounts", json={"csuUsername": "977400001", "password": "new"})
    assert response.status_code == 200, response.text
    saved = db.get_account_by_id(account["id"])
    assert (saved["jd"], saved["wd"]) == (None, None)
    db.delete_account(user["id"], account["id"])
