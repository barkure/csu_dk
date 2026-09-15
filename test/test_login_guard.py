from __future__ import annotations

import json

import pytest

from app import accounts, checkin, db, ratelimit
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.csu.cas import CasIpFrozenError
from app.errors import LoginPausedError, RateLimitError

USERNAME = "255000999"

_counter = iter(range(1, 500))


@pytest.fixture()
def user():
    return db.upsert_user("guard-test@example.com", to_local_iso(local_now(cfg.config.tz)))


@pytest.fixture()
def audit(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(checkin, "log_event",
                        lambda event, **fields: events.append({"event": event, **fields}))
    return events


class FakeLogin:
    token = None
    casual = None
    exit = ""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    def has_login_cookie(self):
        return False

    def login(self, username, password, before_password_login=None):
        if before_password_login:
            before_password_login()
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "html"

    def cookies_json(self):
        return "[]"


class FakeEngine(FakeLogin):
    def __init__(self, error: Exception | None = None):
        super().__init__(error)
        self.done = False

    def dk_status(self, dklb="PA"):
        return {"code": "200",
                "data": {"sfydk": 1 if self.done else 0, "kdk": True, "dkbc": "校内住宿打卡"}}

    def check_location(self, jd, wd, dklb="PA"):
        return {"code": "200", "data": {"canDk": True, "yxMc": "升华8栋"}}

    def submit_dk(self, **_kwargs):
        self.done = True
        return {"code": "200"}


def make_account(user_id: int, **overrides) -> dict:
    now = local_now(cfg.config.tz)
    row = {
        "user_id": user_id, "csu_username": f"9{next(_counter):08d}",
        "password_enc": encrypt_secret("whatever"), "enabled": 1, "dkdz": "",
        "created_at": to_local_iso(now), "updated_at": to_local_iso(now),
    }
    row.update(overrides)
    return db.insert_account(row)


def due_account(user_id: int, **overrides) -> dict:
    return make_account(
        user_id,



        **overrides,
    )


def test_credential_failures_trip_local_cooldown(real_login, audit):
    client = FakeLogin(RuntimeError("学号或密码错误"))

    for _ in range(cfg.config.cred_fail_max):
        with pytest.raises(RuntimeError):
            checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CREATE)
    assert client.calls == cfg.config.cred_fail_max

    with pytest.raises(RateLimitError):
        checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CREATE)
    assert client.calls == cfg.config.cred_fail_max, "冷却期内不该再访问学校"
    assert [event["result"] for event in audit] == ["bad_credentials"] * cfg.config.cred_fail_max + [
        "cred_cooldown"]


def test_success_clears_consecutive_failures(real_login):
    bad = FakeLogin(RuntimeError("学号或密码错误"))
    for _ in range(cfg.config.cred_fail_max - 1):
        with pytest.raises(RuntimeError):
            checkin.cas_login(bad, USERNAME, "pw", entry=checkin.ENTRY_CREATE)

    checkin.cas_login(FakeLogin(), USERNAME, "pw", entry=checkin.ENTRY_CREATE)
    assert checkin.credential_failure_state(USERNAME)[0] == 0

    for _ in range(cfg.config.cred_fail_max - 1):
        with pytest.raises(RuntimeError):
            checkin.cas_login(bad, USERNAME, "pw", entry=checkin.ENTRY_CREATE)
    assert checkin.credential_failure_state(USERNAME)[1] == 0, "成功之后计数从头开始，不该进冷却"


def test_upstream_error_and_ip_freeze_do_not_count(real_login, audit):
    for _ in range(cfg.config.cred_fail_max + 2):
        with pytest.raises(RuntimeError):
            checkin.cas_login(FakeLogin(RuntimeError("连接超时")), USERNAME, "pw",
                              entry=checkin.ENTRY_CREATE)
    assert checkin.credential_failure_state(USERNAME) == (0, 0.0), "网络异常不是用户的错"
    assert {event["result"] for event in audit} == {"upstream_error"}

    with pytest.raises(CasIpFrozenError):
        checkin.cas_login(FakeLogin(CasIpFrozenError("您的IP已被冻结")), f"{USERNAME}x", "pw",
                          entry=checkin.ENTRY_CHECKIN)
    assert checkin.credential_failure_state(f"{USERNAME}x") == (0, 0.0)
    assert checkin.login_pause_remaining() > 0, "冻结要触发全局暂停"
    assert audit[-1]["result"] == "ip_frozen"


def test_save_during_pause_neither_talks_to_school_nor_writes_db(user, real_login, monkeypatch):
    checkin.pause_logins("测试：模拟学校冻结")
    client = FakeLogin()
    monkeypatch.setattr(checkin, "ZhxgClient", lambda *_args, **_kwargs: client)
    before = db.list_accounts(user["id"])

    with pytest.raises(LoginPausedError):
        accounts.create_or_update(user, {"csuUsername": USERNAME, "password": "pw"}, ip="1.2.3.4")

    assert client.calls == 0, "暂停期间不该请求学校认证"
    assert db.list_accounts(user["id"]) == before, "验证失败不能改动数据库里的账号"


def test_relogin_during_pause_does_not_talk_to_school(user, real_login, monkeypatch):
    account = make_account(user["id"])
    checkin.pause_logins("测试：模拟学校冻结")
    client = FakeLogin()
    monkeypatch.setattr(checkin, "build_client", lambda _account: client)

    with pytest.raises(LoginPausedError):
        checkin.relogin(account, ip="1.2.3.4")
    assert client.calls == 0


def test_switching_username_does_not_bypass_the_user_limit(real_login):
    user_id = 4242
    for index in range(cfg.config.cred_fail_max_user):
        with pytest.raises(RuntimeError):
            checkin.cas_login(FakeLogin(RuntimeError("学号或密码错误")), f"2550000{index:02d}",
                              "pw", entry=checkin.ENTRY_CREATE, user_id=user_id)
    assert checkin.credential_failure_state("255000000")[1] == 0, "学号层不该到阈值"

    with pytest.raises(RateLimitError):
        checkin.cas_login(FakeLogin(RuntimeError("学号或密码错误")), "255000999", "pw",
                          entry=checkin.ENTRY_CREATE, user_id=user_id)


def test_switching_account_does_not_bypass_the_ip_limit(real_login):
    ip = "203.0.113.9"
    for index in range(cfg.config.cred_fail_max_ip):
        with pytest.raises(RuntimeError):
            checkin.cas_login(FakeLogin(RuntimeError("学号或密码错误")), f"2551000{index:02d}",
                              "pw", entry=checkin.ENTRY_CREATE,
                              user_id=10_000 + index, ip=ip)

    with pytest.raises(RateLimitError):
        checkin.cas_login(FakeLogin(RuntimeError("学号或密码错误")), "255100999", "pw",
                          entry=checkin.ENTRY_CREATE, user_id=99_999, ip=ip)


def test_sub_threshold_failures_expire_and_get_swept(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(ratelimit.time, "time", lambda: clock["now"])
    guard = ratelimit.FailureCooldown(3, 900, fail_ttl_seconds=60)

    for index in range(5):
        guard.record_failure(f"u:{index}")
    assert len(guard) == 5

    clock["now"] += 61
    assert guard.state("u:0")[0] == 0, "读的时候就要过期"
    assert guard.sweep() == 4, "剩下几个由维护任务收走"
    assert len(guard) == 0


def test_audit_log_records_no_secrets(real_login, audit):
    password = "Sup3rSecret!pwd"
    with pytest.raises(RuntimeError):
        checkin.cas_login(FakeLogin(RuntimeError("学号或密码错误")), USERNAME, password,
                          entry=checkin.ENTRY_RELOGIN, user_id=7, account_id=9, ip="1.2.3.4")

    blob = json.dumps(audit, ensure_ascii=False)
    assert password not in blob
    assert USERNAME not in blob, "不记完整学号，只留尾号"
    assert "cookie" not in blob.lower() and "token" not in blob.lower()

    event = audit[-1]
    assert event["event"] == "checkin.login"
    assert event["entry"] == checkin.ENTRY_RELOGIN
    assert event["result"] == "bad_credentials"
    assert event["user_id"] == 7
    assert event["account_id"] == 9
    assert event["ip"] == "1.2.3.4"
    assert event["fail_count"] == 1
    assert event["csu_username_tail"] == USERNAME[-4:]


def test_retry_storm_is_blocked_without_touching_school(real_login, audit, monkeypatch):
    from app.ratelimit import SlidingWindow

    monkeypatch.setattr(checkin, "_global_attempts", None)
    monkeypatch.setattr(checkin, "_attempt_gap", SlidingWindow(60_000, 1))
    client = FakeLogin(RuntimeError("连接超时"))

    with pytest.raises(RuntimeError):
        checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CREATE)
    with pytest.raises(RateLimitError):
        checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CREATE)

    assert client.calls == 1, "被挡住的那次不该访问学校"
    assert [event["result"] for event in audit] == ["upstream_error", "login_too_soon"]


def test_scheduled_relogin_is_not_throttled_by_the_gap(real_login, audit, monkeypatch):
    from app.ratelimit import SlidingWindow

    monkeypatch.setattr(checkin, "_global_attempts", None)
    monkeypatch.setattr(checkin, "_attempt_gap", SlidingWindow(60_000, 1))
    client = FakeLogin()

    checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CHECKIN)
    checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CHECKIN)
    assert client.calls == 2


def test_global_login_burst_is_capped(real_login, audit, monkeypatch):
    from app.errors import RateLimitError as LimitError
    from app.ratelimit import SlidingWindow

    monkeypatch.setattr(checkin, "_attempt_gap", None)
    monkeypatch.setattr(checkin, "_global_attempts", SlidingWindow(60_000, 2))
    client = FakeLogin()

    checkin.cas_login(client, USERNAME, "pw", entry=checkin.ENTRY_CREATE)
    checkin.cas_login(client, "255100002", "pw", entry=checkin.ENTRY_CREATE)
    with pytest.raises(LimitError):
        checkin.cas_login(client, "255100003", "pw", entry=checkin.ENTRY_CREATE)
    assert client.calls == 2
    assert audit[-1]["result"] == "login_rate_limited"


def test_frozen_exit_switches_to_fallback(monkeypatch):
    """首选出口被冻结后切换到备选出口，冷却结束后恢复首选出口。"""
    import time

    from app import exits

    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(
        update={"outbound_proxy": "http://127.0.0.1:1091"}))
    exits.reset()
    assert exits.current() == "http://127.0.0.1:1091"

    assert exits.mark_frozen("http://127.0.0.1:1091", "学校冻结") == ""
    assert exits.current() == ""
    assert exits.available() is True

    exits._frozen_until["http://127.0.0.1:1091"] = time.time() - 1      # 冷却过期
    assert exits.current() == "http://127.0.0.1:1091"


def test_both_exits_frozen_then_pauses(real_login, audit, monkeypatch):
    """所有出口都被冻结后暂停密码登录。"""
    from app import exits

    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(
        update={"outbound_proxy": "http://127.0.0.1:1091"}))
    exits.reset()
    frozen = CasIpFrozenError("您的IP已被冻结")
    primary = FakeLogin(frozen)
    primary.exit = "http://127.0.0.1:1091"

    with pytest.raises(CasIpFrozenError):
        checkin.cas_login(primary, USERNAME, "pw", entry=checkin.ENTRY_CHECKIN)
    assert audit[-1]["result"] == "ip_frozen"
    assert checkin.login_pause_remaining() == 0  # 还有备选出口，不暂停
    assert exits.current() == ""

    fallback = FakeLogin(frozen)
    fallback.exit = ""
    with pytest.raises(CasIpFrozenError):
        checkin.cas_login(fallback, USERNAME, "pw", entry=checkin.ENTRY_CHECKIN)
    assert checkin.login_pause_remaining() > 0  # 所有出口都在冷却，暂停密码登录
