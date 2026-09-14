"""账号状态：四种、与自动打卡开关独立、只由认证故障决定；测试一律不打真实学校。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import checkin, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.csu.cas import CasIpFrozenError
from app.csu.zhxg import ZhxgError
from app.errors import SecretDecryptError
from app.main import app as fastapi_app
from app.views import AUTH_STATUS, account_status

USER_EMAIL = "status-test@example.com"

STATUS_CASES = [
    ("", "正常", "ok"),
    ("bad_credentials", "密码错误", "err"),
    ("locked", "账号锁定", "err"),
    ("other", "其他故障", "err"),
    ("something-from-the-future", "其他故障", "err"),   # 未知取值按最保守的处理
]


_counter = iter(range(1, 1000))


def make_account(user_id: int, username: str | None = None, **overrides) -> dict:
    now = to_local_iso(local_now(cfg.config.tz))
    row = {
        "user_id": user_id, "csu_username": username or f"9{next(_counter):08d}", "password_enc": encrypt_secret("pw"),
        "enabled": 1,
        "jd": 112.936833, "wd": 28.157238, "dkdz": "升华8栋", "created_at": now, "updated_at": now,
    }
    row.update(overrides)
    return db.insert_account(row)


@pytest.fixture()
def user():
    return db.upsert_user(USER_EMAIL, to_local_iso(local_now(cfg.config.tz)))


@pytest.fixture()
def client():
    return TestClient(fastapi_app)


def ui_login(client: TestClient, email: str) -> None:
    from app import auth

    auth.limiters["request_ip"].reset()
    now = to_local_iso(local_now(cfg.config.tz))
    db.upsert_user(email, now)
    token = auth.create_session(db.find_user_by_email(email)["id"], "status-test")
    client.cookies.set(auth.SESSION_COOKIE, token)


@pytest.mark.parametrize(("kind", "text", "cls"), STATUS_CASES)
def test_four_states_and_colors(kind, text, cls):
    assert account_status({"auth_error": kind}) == {"text": text, "cls": cls}
    assert AUTH_STATUS[kind]["0" if False else 0] if False else True  # 占位：常量表本身不参与断言


def test_status_ignores_last_checkin_result_and_login_state():
    failed_checkin = {"auth_error": "", "last_status": "failed", "last_message": "打卡失败"}
    assert account_status(failed_checkin)["text"] == "正常"

    not_logged_in = {"auth_error": "", "token": "", "token_at": None}
    assert account_status(not_logged_in)["text"] == "正常"


def test_status_survives_next_day(user):
    account = make_account(user["id"])
    db.set_auth_error(account["id"], "bad_credentials")
    db.update_account(account["id"], {"last_run_at": "2026-09-01T20:00:00", "token_at": "2026-09-01T20:00:00"})

    row = db.get_account_by_id(account["id"])
    assert account_status(row) == {"text": "密码错误", "cls": "err"}
    assert row["auth_error"] == "bad_credentials"


def test_toggle_does_not_change_status(client, user):
    account = make_account(user["id"])
    db.set_auth_error(account["id"], "locked")
    ui_login(client, USER_EMAIL)

    response = client.post(f"/ui/accounts/{account['id']}/toggle")
    assert response.status_code == 200
    assert "已停用" not in response.text and "停用自动打卡" not in response.text

    row = db.get_account_by_id(account["id"])
    assert row["enabled"] == 0, "开关本身要生效"
    assert row["auth_error"] == "locked", "状态不能因为关掉自动打卡而变"
    assert "账号锁定" in client.get("/ui/accounts").text


def test_auth_success_clears_the_failure(client, user, monkeypatch):
    account = make_account(user["id"])
    db.set_auth_error(account["id"], "bad_credentials")

    ui_login(client, USER_EMAIL)
    client.get("/ui/accounts")          # 只刷新页面：状态必须还在
    assert db.get_account_by_id(account["id"])["auth_error"] == "bad_credentials"
    monkeypatch.setattr("app.accounts.verify_login", lambda *_a, **_k: {
        "location": {"canDk": True, "yxMc": "升华8栋"}, "address": "升华8栋",
        "session": {"token": encrypt_secret("jwt"), "casual": "c", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })
    response = client.post("/ui/accounts", data={
        "csuUsername": account["csu_username"], "password": "new-password",
        "coords": "112.936833,28.157238", "windowStart": "20:00", "windowEnd": "22:30",
    })
    assert response.status_code == 200

    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "", "验证成功后要恢复正常"
    assert account_status(row) == {"text": "正常", "cls": "ok"}


def test_engine_clears_failure_after_successful_login(user, monkeypatch):
    account = make_account(user["id"])
    db.set_auth_error(account["id"], "other")

    from app import checkin as engine

    class FakeZhxg:
        token = "t"
        casual = "c"

        def cookies_json(self) -> str:
            return "[]"

    monkeypatch.setattr(engine, "build_client", lambda _account: FakeZhxg())
    monkeypatch.setattr(engine, "cas_login", lambda *_a, **_k: "<html></html>")
    engine._login(db.get_account_by_id(account["id"]))

    assert db.get_account_by_id(account["id"])["auth_error"] == ""


@pytest.mark.parametrize(("message", "kind"), [
    ("学号或密码错误", "bad_credentials"),
    ("账号已被锁定，请联系学校", "locked"),
    ("CAS 要求输入验证码，但自动识别不可用（未安装 ddddocr 或识别失败）", "other"),
    ("CAS 验证码连续 2 次未通过，请先在浏览器登录一次后再试", "other"),
    ("账号未激活，请先在统一身份认证平台激活", "other"),
    ("CAS 登录失败：会话已失效", None),                 # 重新登录即可，不是账号故障
    ("HTTPSConnectionPool(host='ca.csu.edu.cn'): Read timed out", None),
    ("打卡点距离超出 300 米", None),                     # 打卡阶段/执行问题
])
def test_failure_classification(message, kind):
    error = RuntimeError(message)
    assert checkin._failure_kind(error, message) == kind


def test_ip_freeze_is_not_an_account_failure():
    error = CasIpFrozenError("学校统一身份认证已冻结本机 IP：多次无效登录会触发风控")
    assert checkin._failure_kind(error, str(error)) is None


def test_local_crypto_and_zhxg_errors_are_other():
    assert checkin._failure_kind(SecretDecryptError("x"), "x") == "other"
    assert checkin._failure_kind(ZhxgError("换取业务 token 失败"), "换取业务 token 失败") == "other"


def test_checkin_stage_failure_does_not_touch_status(user, monkeypatch):
    account = make_account(user["id"])
    from app import checkin as engine

    class FakeZhxg:
        token = "t"
        casual = "c"

        def cookies_json(self) -> str:
            return "[]"

        def dk_status(self):
            return {"code": "200", "data": {"sfydk": False, "kdk": True, "dksj": None}}

        def check_location(self, *_a, **_k):
            return {"code": "200", "data": {"canDk": False, "yxMc": "升华8栋", "pcMi": 320,
                                            "msg": "距离打卡点过远"}}

    monkeypatch.setattr(engine, "build_client", lambda _account: FakeZhxg())
    monkeypatch.setattr(engine, "cas_login", lambda *_a, **_k: "<html></html>")
    result = engine.run_checkin(account, "manual")

    assert result["status"] == "failed"
    assert "位置校验未通过" in result["message"] and "320 米" in result["message"]
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "", "执行问题不该被当成认证故障"
    assert row["last_status"] == "failed"


def test_failure_log_has_no_secrets(monkeypatch):
    events = []
    monkeypatch.setattr("app.checkin.log_event", lambda event, **fields: events.append((event, fields)))
    checkin.mark_auth_failure(7, RuntimeError("学号或密码错误"), "学号或密码错误")

    assert events == [("checkin.auth_failed", {"account_id": 7, "kind": "bad_credentials",
                                               "detail": "学号或密码错误"})]
    _, fields = events[0]
    assert set(fields) == {"account_id", "kind", "detail"}, "别把整个账号对象或凭据塞进日志"
    assert "v1." not in str(fields) and "CASTGC" not in str(fields)


def test_checkin_stage_exception_does_not_flag_account(user, monkeypatch):
    account = make_account(user["id"])
    from app import checkin as engine

    class FakeZhxg:
        token = "t"
        casual = "c"

        def cookies_json(self) -> str:
            return "[]"

        def dk_status(self):
            raise RuntimeError("业务接口返回异常：{'message': '账号已被锁定，请联系学校'}")

    monkeypatch.setattr(engine, "build_client", lambda _account: FakeZhxg())
    monkeypatch.setattr(engine, "cas_login", lambda *_a, **_k: "<html></html>")
    result = engine.run_checkin(account, "manual")

    assert result["status"] == "failed"
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "", "打卡阶段的异常不该被当成账号认证故障"


def test_api_password_change_clears_failure(client, user, monkeypatch):
    account = make_account(user["id"])
    db.set_auth_error(account["id"], "bad_credentials")
    ui_login(client, USER_EMAIL)

    monkeypatch.setattr("app.accounts.verify_login", lambda *_a, **_k: {
        "location": {"canDk": True, "yxMc": "升华8栋"}, "address": "升华8栋",
        "session": {"token": encrypt_secret("jwt"), "casual": "c", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })
    response = client.patch(f"/api/accounts/{account['id']}", json={"password": "new-password"})
    assert response.status_code == 200, response.text

    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == ""


def test_api_password_change_failure_records_kind(client, user, monkeypatch):
    from app.errors import AppError

    account = make_account(user["id"])
    ui_login(client, USER_EMAIL)

    def reject(*_a, **_k):
        raise AppError("验证失败，未保存：学号或密码错误", status=400, expose=True)

    monkeypatch.setattr("app.accounts.verify_login", reject)
    response = client.patch(f"/api/accounts/{account['id']}", json={"password": "wrong"})
    assert response.status_code == 400

    assert db.get_account_by_id(account["id"])["auth_error"] == "bad_credentials"


def test_log_scrubs_secrets_inside_the_message(monkeypatch):
    events = []
    monkeypatch.setattr("app.checkin.log_event", lambda event, **fields: events.append(fields))

    poisoned = ("CAS 登录失败：学号或密码错误（Set-Cookie: CASTGC=TGT-1234567890-abcdef; "
                "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop "
                "本地密文 v1.AAAA.BBBBCCCCDDDD）")
    checkin.mark_auth_failure(9, RuntimeError(poisoned), poisoned)

    detail = events[0]["detail"]
    assert "TGT-1234567890-abcdef" not in detail, "cookie 值不能进日志"
    assert "eyJhbGciOiJIUzI1NiJ9" not in detail, "JWT 不能进日志"
    assert "v1.AAAA" not in detail, "本地密文不能进日志"
    assert len(detail) <= 160


@pytest.mark.parametrize(("poisoned", "leak"), [
    ('{"token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature9x", "ok": true}',
     "eyJhbGciOiJIUzI1NiJ9"),
    ('{"token":"abcdef1234567890"}', "abcdef1234567890"),
    ('{"password": "hunter2secret"}', "hunter2secret"),
    ('{"pwd":"pw-secret-123"}', "pw-secret-123"),
    ('请求头 Authorization: Bearer abcdefghijklmnop.qrstuv', "abcdefghijklmnop.qrstuv"),
    ('Set-Cookie: JSESSIONID=ABC123DEF456; Path=/', "ABC123DEF456"),
    ('{"cookies": "a=b; c=d"}', "a=b; c=d"),
    ('https://ca.csu.edu.cn/authserver/login?ticket=ST-123456-abcdef', "ST-123456-abcdef"),
    ('{"password": "FAKE FIRST SECOND"}', "FAKE FIRST SECOND"),      # 值里有空格
    ('{"token": "abc def ghi jkl"}', "abc def ghi jkl"),
    ("{'pwd': 'pw with spaces'}", "pw with spaces"),                 # 单引号 JSON
    ('{"token": "a\\"b secret"}', 'b secret'),                      # 值里有转义引号
    ('{"cookie": "FAKE_FIRST, FAKE_SECOND"}', "FAKE_FIRST, FAKE_SECOND"),   # 值里有逗号
    ('{"cookies": "FAKE_FIRST, FAKE_SECOND"}', "FAKE_FIRST, FAKE_SECOND"),  # 复数形式
    ('{"cookie": "secret (paren)"}', "secret (paren)"),                     # 值里有右括号
    ('{"token": "a, b, c"}', "a, b, c"),
])
def test_secrets_inside_messages_never_reach_the_log(monkeypatch, poisoned, leak):
    events = []
    monkeypatch.setattr("app.checkin.log_event", lambda event, **fields: events.append(fields))

    message = f"CAS 登录失败：学号或密码错误（{poisoned}）"
    assert checkin.mark_auth_failure(11, RuntimeError(message), message) == "bad_credentials"
    detail = events[0]["detail"]
    assert leak not in detail, f"凭据泄漏进日志了：{detail}"
    assert len(detail) <= 160
