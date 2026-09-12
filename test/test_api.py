"""接口层测试：不联网、不碰学校（要真实登录的路径在 test_e2e.py）。"""
from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.main import app
from app.ratelimit import SlidingWindow


@pytest.fixture()
def client():
    for limiter in auth.limiters.values():
        limiter.reset()
    # 不用 with：不触发 lifespan，避免测试里把调度器跑起来（它会对学校发真实登录请求）
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
    assert body["defaults"] == {"windowStart": "20:00", "windowEnd": "22:30"}
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


def test_cooldown_rejection_does_not_consume_global_quota(client):
    email = "cooldown@example.com"
    inject_code(email, "424242")  # 有未用验证码 → 冷却生效

    blocked = client.post("/api/auth/request-code", json={"email": email})
    assert blocked.status_code == 429
    assert auth.limiters["request_global"].peek("global") == 0

    ok = client.post("/api/auth/request-code", json={"email": "fresh-email@example.com"})
    assert ok.status_code == 200
    assert auth.limiters["request_global"].peek("global") == 1


def test_oversized_body_rejected(client):
    response = client.post("/api/auth/verify", json={"email": "a@b.c", "code": "x" * 200_000})
    assert response.status_code == 413


def make_account(user_id: int, username: str, **overrides) -> dict:
    now = to_local_iso(local_now(cfg.config.tz))
    row = {
        "user_id": user_id, "csu_username": username, "password_enc": encrypt_secret("whatever"),
        "enabled": 1, "window_start": "20:00", "window_end": "22:30",
        "jd": 112.936833, "wd": 28.157238, "dkdz": "", "created_at": now, "updated_at": now,
    }
    row.update(overrides)
    return db.insert_account(row)


@pytest.fixture()
def owner(client):
    login(client, "owner@example.com")
    user = db.find_user_by_email("owner@example.com")
    # 每个用例一个独立学号：同一个库里互撞唯一约束、并会误撞"提交即验证"
    username = f"9{next(_usernames):08d}"
    account = make_account(user["id"], username)
    yield client, user, account
    # 收尾清掉：否则同一用户会累积到"每用户最多 5 个账号"的上限
    db.delete_account(user["id"], account["id"])


_usernames = itertools.count(1)


@pytest.fixture()
def taken_username(owner):
    """已存在的学号（用于验证"编辑已有账号"类目）"""
    return owner[2]["csu_username"]


def test_account_limit_per_user(owner, monkeypatch):
    client, _, _ = owner
    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(update={"max_accounts_per_user": 1}))
    response = client.post("/api/accounts", json={
        "csuUsername": "299999999", "password": "x", "jd": 112.9, "wd": 28.1,
    })
    assert response.status_code == 400
    assert "最多只能托管" in response.json()["error"]


def test_list_returns_own_accounts_and_schedules(owner):
    client, _, account = owner
    from app.scheduler import schedule_next

    schedule_next(db.get_account_by_id(account["id"]))
    accounts = client.get("/api/accounts").json()["accounts"]
    mine = [item for item in accounts if item["id"] == account["id"]]
    assert len(mine) == 1
    assert mine[0]["csuUsername"] == account["csu_username"]
    assert mine[0]["nextRunAt"]


def test_post_without_coordinates_is_400(owner):
    """新学号缺经纬度 → 400，且**不会**去登录学校。"""
    client, _, _ = owner
    response = client.post("/api/accounts", json={"csuUsername": "988888888", "password": "x"})
    assert response.status_code == 400
    assert "经纬度" in response.json()["error"]

    # 已存在账号改密码时缺经纬度会回落到库里的值，所以这里用全新学号
    assert db.get_account_by_username(db.find_user_by_email("owner@example.com")["id"], "988888888") is None


def test_post_existing_account_without_coordinates_uses_saved_ones(owner):
    """给已有账号重新提交密码时，经纬度沿用库里的值（因此会走到登录验证）——这里只验证它不会 400。"""
    client, _, account = owner
    response = client.post("/api/accounts", json={
        "csuUsername": account["csu_username"], "password": "whatever",
    })
    # 没有真实学校可登，所以必然是"验证失败"；关键是**不是**经纬度缺失的错误
    assert response.status_code == 400
    assert "经纬度" not in response.json()["error"]


def test_patch_nan_coordinates_rejected_and_not_saved(owner):
    client, _, account = owner
    response = client.patch(f"/api/accounts/{account['id']}", json={"jd": "abc"})
    assert response.status_code == 400
    assert response.json()["code"] in ("invalid_coord", "bad_request")
    assert db.get_account_by_id(account["id"])["jd"] == 112.936833


def test_patch_out_of_range_rejected(owner):
    client, _, account = owner
    response = client.patch(f"/api/accounts/{account['id']}", json={"wd": 91})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_coord"


def test_patch_bad_window_rejected(owner):
    client, _, account = owner
    for body in ({"windowStart": "99:00"}, {"windowEnd": "25:00"}, {"windowStart": "20:60"}):
        response = client.patch(f"/api/accounts/{account['id']}", json=body)
        assert response.status_code == 400, body
        assert response.json()["code"] == "invalid_time"
    saved = db.get_account_by_id(account["id"])
    assert (saved["window_start"], saved["window_end"]) == ("20:00", "22:30")


def test_patch_valid_updates(owner):
    client, _, account = owner
    response = client.patch(f"/api/accounts/{account['id']}", json={"jd": 112.9, "wd": 28.1, "windowEnd": "22:00"})
    assert response.status_code == 200
    saved = db.get_account_by_id(account["id"])
    assert (saved["jd"], saved["window_end"]) == (112.9, "22:00")


def test_disable_clears_schedule(owner):
    client, _, account = owner
    client.patch(f"/api/accounts/{account['id']}", json={"enabled": True})
    assert db.get_account_by_id(account["id"])["next_run_at"]

    client.patch(f"/api/accounts/{account['id']}", json={"enabled": False})
    assert db.get_account_by_id(account["id"])["next_run_at"] is None


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


def test_changing_coordinates_clears_stale_building_name(owner):
    """楼栋名是学校按坐标返回的：坐标变了就不能再显示旧的（否则"新坐标+旧楼栋"很误导）。"""
    client, _, account = owner
    db.update_account(account["id"], {"dkdz": "升华8栋"})

    response = client.patch(f"/api/accounts/{account['id']}", json={"jd": 112.9, "wd": 28.1})
    assert response.status_code == 200
    assert db.get_account_by_id(account["id"])["dkdz"] == ""

    # 只改窗口时不该动楼栋名
    db.update_account(account["id"], {"dkdz": "升华8栋", "jd": 112.9, "wd": 28.1})
    assert client.patch(f"/api/accounts/{account['id']}", json={"windowEnd": "22:00"}).status_code == 200
    assert db.get_account_by_id(account["id"])["dkdz"] == "升华8栋"


def test_reprobe_on_save_refills_building_name(owner, monkeypatch):
    """带密码重新保存时会重新探测学校，楼栋名随之更新。"""
    client, _, account = owner
    db.update_account(account["id"], {"dkdz": "旧楼栋"})
    monkeypatch.setattr("app.accounts.probe_window", lambda *_args, **_kw: {
        "location": {"canDk": True, "yxMc": "升华26栋"},
        "address": "升华26栋",
        "session": {"token": "t", "casual": "c", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })

    response = client.patch(f"/api/accounts/{account['id']}", json={"password": "new", "jd": 112.9, "wd": 28.1})
    assert response.status_code == 200
    assert db.get_account_by_id(account["id"])["dkdz"] == "升华26栋"


def test_add_account_outside_window_leaves_address_empty(owner, monkeypatch):
    """窗口外添加：实时请求拿不到楼栋名 → 地址留空（界面只显示坐标），不拿历史凑。"""
    client, user, _ = owner
    monkeypatch.setattr("app.accounts.probe_window", lambda *_args, **_kw: {
        "location": {"canDk": False, "msg": "未到打卡时间"},   # 窗口外学校就是这么回的
        "address": None,
        "session": {"token": "t", "casual": "c", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })

    response = client.post("/api/accounts", json={
        "csuUsername": "977200001", "password": "x", "jd": 112.936833, "wd": 28.157238,
    })
    assert response.status_code == 200
    assert response.json()["account"]["dkdz"] == ""
    assert db.get_account_by_username(user["id"], "977200001")["dkdz"] == ""
def test_add_account_happy_path_stores_everything(owner, monkeypatch):
    """添加成功的完整路径：默认开启、窗口与地址来自探测、登录态一并落库。

    （这之前没有用例覆盖 → 漏掉了 enabled 在"新建"分支上的 TypeError。）
    """
    client, user, _ = owner
    monkeypatch.setattr("app.accounts.probe_window", lambda *_args, **_kw: {
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
    assert (account["window_start"], account["window_end"]) == ("20:00", "22:30")
    assert account["dkdz"] == "升华8栋"
    assert account["token"] == "tok"
    assert account["needs_reauth"] == 0
    assert account["next_run_at"] is not None
    db.delete_account(user["id"], account["id"])


def test_add_account_happy_path_via_ui_form(owner, monkeypatch):
    """网页表单那条路径（htmx POST）也要能添加成功。"""
    client, user, _ = owner
    monkeypatch.setattr("app.accounts.probe_window", lambda *_args, **_kw: {
        "location": {"canDk": True, "yxMc": "升华5栋"},
        "address": "升华5栋",
        "session": {"token": "tok2", "casual": "cas2", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })

    html = client.post("/ui/accounts", data={
        "csuUsername": "977300002", "password": "x", "coords": "112.936292,28.156628",
    }).text
    assert "验证通过，已保存" in html
    assert db.get_account_by_username(user["id"], "977300002")["dkdz"] == "升华5栋"
    db.delete_account(user["id"], db.get_account_by_username(user["id"], "977300002")["id"])


def test_captcha_consume_rolls_back_when_session_creation_fails(client, monkeypatch):
    """消费验证码与建会话必须在同一事务里。

    否则中途失败会留下"验证码已作废、用户还没登录"的残局，用户只能重新申请。
    """
    from app import auth

    inject_code("rollback@example.com", "777777")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟建会话失败")

    monkeypatch.setattr(auth, "_insert_session", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/auth/verify", json={"email": "rollback@example.com", "code": "777777"})

    after = db.latest_login_code("rollback@example.com")
    assert after["used"] == 0, "事务回滚后验证码应当还是可用的"
