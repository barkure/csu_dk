"""失败信息脱敏：落库、对外返回与读取展示三条边界。"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app import auth, checkin, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.main import app

SECRET = ("token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh "
          "CASTGC=TGT-1234567890-abcdef "
          "https://ca.csu.edu.cn/authserver/login?ticket=ST-123456-abcdef")
LEAKS = ("eyJhbGciOiJIUzI1NiJ9", "TGT-1234567890-abcdef", "ST-123456-abcdef")

_counter = iter(range(1, 1000))


def assert_no_leak(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, f"敏感内容泄漏：{text}"


def make_account(user_id: int, **overrides) -> dict:
    now = local_now(cfg.config.tz)
    last_message = overrides.pop("last_message", None)
    row = {
        "user_id": user_id, "csu_username": f"94{next(_counter):07d}",
        "password_enc": encrypt_secret("pw"), "enabled": 1, "dkdz": "",
        "created_at": to_local_iso(now), "updated_at": to_local_iso(now),
    }
    row.update(overrides)
    account = db.insert_account(row)
    if last_message is not None:  # insert_account 不插 last_message 列，建完补写
        db.update_account(account["id"], {"last_message": last_message})
    return db.get_account_by_id(account["id"])


def user() -> dict:
    """每测试独立用户，避免账号数上限互相干扰。"""
    return db.upsert_user(f"scrub-{next(_counter)}@example.com",
                          to_local_iso(local_now(cfg.config.tz)))


def api_client(email: str, **client_options) -> TestClient:
    for limiter in auth.limiters.values():
        limiter.reset()
    now = local_now(cfg.config.tz)
    db.insert_login_code(email, auth.hash_login_code(email, "424242"),
                         to_local_iso(now + timedelta(minutes=10)), to_local_iso(now))
    client = TestClient(app, **client_options)
    response = client.post("/api/auth/verify", json={"email": email, "code": "424242"})
    assert response.status_code == 200, response.text
    return client


class FakeClient:
    token = "t"
    casual = "c"

    def __init__(self, status=None, location=None):
        self._status = status or {}
        self._location = location
        self.submitted = None

    def dk_status(self, dklb="PA"):
        return self._status

    def check_location(self, jd, wd, dklb="PA"):
        return {"code": "200", "data": self._location or {}}

    def submit_dk(self, **kwargs):
        self.submitted = kwargs
        return {"code": "200"}

    def cookies_json(self):
        return "[]"


def attach(monkeypatch, client, **overrides) -> dict:
    account = make_account(user()["id"], token="t", casual="c", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz)), **overrides)
    monkeypatch.setattr("app.checkin.build_client", lambda _account: client)
    return db.get_account_by_id(account["id"])


def test_upstream_error_text_scrubbed_in_record_and_result(monkeypatch):
    client = FakeClient({"code": "500", "message": SECRET, "data": None})
    account = attach(monkeypatch, client)
    monkeypatch.setattr("app.checkin.cas_login", lambda *_a, **_k: None)  # 强制重登轮不真连网

    result = checkin.run_checkin(account, "schedule")

    assert result["status"] == "failed"
    assert "业务接口返回异常" in result["message"]
    assert_no_leak(result["message"])
    assert_no_leak(db.list_records(account["id"], 1)[0]["message"])
    assert_no_leak(db.get_account_by_id(account["id"])["last_message"] or "")


def test_bkyy_reason_scrubbed_without_exception(monkeypatch):
    client = FakeClient({"code": "200", "data": {"sfydk": 0, "kdk": False, "bkyy": SECRET}})
    account = attach(monkeypatch, client)

    result = checkin.run_checkin(account, "schedule")

    assert result["status"] == "failed"
    assert "当前不可打卡" in result["message"]
    assert_no_leak(result["message"])
    assert_no_leak(db.list_records(account["id"], 1)[0]["message"])


def test_location_rejection_scrubbed(monkeypatch):
    client = FakeClient({"code": "200", "data": {"sfydk": 0, "kdk": True, "dkbc": "x"}},
                        location={"canDk": False, "msg": SECRET, "reason": SECRET,
                                  "yxMc": "升华5栋", "pcMi": 700})
    account = attach(monkeypatch, client, dkdz="升华5栋")
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.936833, 28.157238), "升华5栋", {"canDk": False, "pcMi": 700, "yxMc": "升华5栋"},
        "located"))

    result = checkin.run_checkin(account, "schedule")

    assert result["status"] == "failed"
    assert "位置校验未通过" in result["message"]
    assert_no_leak(result["message"])
    assert_no_leak(db.list_records(account["id"], 1)[0]["message"])


def test_relogin_failure_scrubbed_but_classified_on_raw(monkeypatch):
    account = attach(monkeypatch, FakeClient())
    # 分类关键字藏在会被脱敏的形态里：只有用原文分类才能认出密码错误
    message = '认证失败 {"pwd": "密码错误"}'

    def boom(_account, **_kw):
        raise RuntimeError(message)

    monkeypatch.setattr("app.checkin._login", boom)
    result = checkin.relogin(account)

    assert result["ok"] is False
    assert "密码错误" not in result["message"], "展示层只见脱敏文本"
    assert "***" in result["message"]
    assert db.get_account_by_id(account["id"])["auth_error"] == "bad_credentials"


def test_refresh_login_failure_scrubbed(monkeypatch):
    account = attach(monkeypatch, FakeClient())

    def boom(_account, **_kw):
        raise RuntimeError(f"学号或密码有误（{SECRET}）")

    monkeypatch.setattr("app.checkin._login", boom)
    result = checkin.refresh_login(account)

    assert result["ok"] is False
    assert_no_leak(result["message"])


def test_api_probe_failure_response_is_scrubbed(monkeypatch):
    def reject(*_a, **_k):
        raise RuntimeError(f"学号或密码有误（{SECRET}）")

    monkeypatch.setattr("app.accounts.verify_login", reject)
    owner = user()
    client = api_client(owner["email"])
    response = client.post("/api/accounts", json={"csuUsername": "977600001", "password": "x"})

    assert response.status_code == 400
    assert "验证失败，未保存" in response.json()["error"]
    assert_no_leak(response.text)


def test_classification_uses_raw_text_while_response_is_scrubbed(monkeypatch):
    owner = user()
    account = make_account(owner["id"])

    def reject(*_a, **_k):
        raise RuntimeError('认证失败 {"pwd": "密码错误"}')

    monkeypatch.setattr("app.accounts.verify_login", reject)
    client = api_client(owner["email"])
    response = client.post("/api/accounts", json={"csuUsername": account["csu_username"]})

    assert response.status_code == 400
    assert "密码错误" not in response.json()["error"], "展示层只见脱敏文本"
    assert db.get_account_by_id(account["id"])["auth_error"] == "bad_credentials"


def test_read_side_scrubs_old_records_and_last_message():
    owner = user()
    now = to_local_iso(local_now(cfg.config.tz))
    account = make_account(owner["id"], last_message=f"打卡失败：{SECRET}")
    db.add_record(account["id"], now, "schedule", "failed", f"提交失败：{SECRET}")
    client = api_client(owner["email"])

    mine = next(item for item in client.get("/api/accounts").json()["accounts"]
                if item["id"] == account["id"])
    assert "打卡失败" in mine["lastMessage"]
    assert_no_leak(json.dumps(mine, ensure_ascii=False))

    body = client.get(f"/api/accounts/{account['id']}/records").json()
    assert "提交失败" in body["records"][0]["message"]
    assert_no_leak(json.dumps(body, ensure_ascii=False))

    html = client.get(f"/ui/accounts/{account['id']}/records").text
    assert "提交失败" in html
    assert_no_leak(html)


def test_ui_context_rows_scrubbed():
    from app import ui

    owner = user()
    account = make_account(owner["id"], last_message=f"打卡失败：{SECRET}")
    context = ui._context(owner)
    row = next(item for item in context["rows"] if item["raw"]["id"] == account["id"])
    assert "打卡失败" in row["raw"]["last_message"]
    assert_no_leak(json.dumps(row, ensure_ascii=False))


def test_unexpected_error_log_is_scrubbed(monkeypatch):
    events = []
    monkeypatch.setattr("app.main.log_event", lambda event, **fields: events.append((event, fields)))

    def boom(*_a, **_k):
        raise RuntimeError(f"内部故障 {SECRET}")

    monkeypatch.setattr("app.main.db.list_accounts", boom)
    client = api_client(user()["email"], raise_server_exceptions=False)
    response = client.get("/api/accounts")

    assert response.status_code == 500
    assert response.json() == {"error": "服务端内部错误"}
    event, fields = events[-1]
    assert event == "http.unexpected_error"
    assert_no_leak(json.dumps(fields, ensure_ascii=False))
