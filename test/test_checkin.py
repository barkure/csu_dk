"""打卡引擎与调度：登录态新鲜度、楼栋名补全、排期规则。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import checkin, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.scheduler import bootstrap, schedule_next, schedule_next_after

_counter = iter(range(1, 1000))


def make_account(user_id: int, **overrides) -> dict:
    now = local_now(cfg.config.tz)
    row = {
        "user_id": user_id, "csu_username": f"9{next(_counter):08d}",
        "password_enc": encrypt_secret("whatever"), "enabled": 1,
        "window_start": "20:00", "window_end": "22:30",
        "jd": 112.936833, "wd": 28.157238, "dkdz": "",
        "created_at": to_local_iso(now), "updated_at": to_local_iso(now),
    }
    row.update(overrides)
    return db.insert_account(row)


@pytest.fixture()
def user():
    return db.upsert_user("checkin-test@example.com", to_local_iso(local_now(cfg.config.tz)))


class FakeClient:
    def __init__(self, address="升华26栋"):
        self.address = address
        self.calls = 0

    def check_location(self, jd, wd, dklb="PA"):
        self.calls += 1
        return {"code": "200", "data": {"canDk": True, "yxMc": self.address}}


def test_fill_address_uses_existing_login(user, monkeypatch):
    account = make_account(user["id"], token="t", casual="c", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz)))
    fake = FakeClient()
    monkeypatch.setattr("app.checkin.build_client", lambda _account: fake)

    assert checkin.fill_address(account) == "升华26栋"
    assert db.get_account_by_id(account["id"])["dkdz"] == "升华26栋"
    assert fake.calls == 1


def test_fill_address_skips_when_already_known(user, monkeypatch):
    account = make_account(user["id"], dkdz="升华8栋", token="t", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz)))
    fake = FakeClient()
    monkeypatch.setattr("app.checkin.build_client", lambda _account: fake)

    assert checkin.fill_address(account) is None
    assert fake.calls == 0


def test_fill_address_skips_without_fresh_login(user, monkeypatch):
    """没有当天登录态时不为这一件事单独登录学校（登录是有风控代价的）。"""
    stale = to_local_iso(local_now(cfg.config.tz) - timedelta(days=1))
    account = make_account(user["id"], token="t", cookies="[]", token_at=stale)
    fake = FakeClient()
    monkeypatch.setattr("app.checkin.build_client", lambda _account: fake)

    assert checkin.fill_address(account) is None
    assert fake.calls == 0


def test_has_fresh_login_rules(user):
    now = local_now(cfg.config.tz)
    today = db.get_account_by_id(make_account(user["id"], token="t", cookies="[]",
                                              token_at=to_local_iso(now))["id"])
    assert checkin.has_fresh_login(today) is True

    yesterday = db.get_account_by_id(make_account(user["id"], token="t", cookies="[]",
                                                  token_at=to_local_iso(now - timedelta(days=1)))["id"])
    assert checkin.has_fresh_login(yesterday) is False  # 跨自然日：JWT 已失效

    future = db.get_account_by_id(make_account(user["id"], token="t", cookies="[]",
                                               token_at=to_local_iso(now + timedelta(hours=1)))["id"])
    assert checkin.has_fresh_login(future) is False  # 时间戳在未来 → 保守重登

    assert checkin.has_fresh_login({"token": None, "token_at": None}) is False


def test_schedule_next_tomorrow_lands_in_tomorrow_window(user):
    account = make_account(user["id"])
    iso = schedule_next(db.get_account_by_id(account["id"]), tomorrow=True)
    tomorrow = to_local_iso(local_now(cfg.config.tz) + timedelta(days=1))[:10]
    assert iso.startswith(tomorrow)
    assert "20:00:00" < iso[11:] < "22:30:00"


def test_schedule_next_after_success_goes_tomorrow(user):
    account = make_account(user["id"])
    iso = schedule_next_after(db.get_account_by_id(account["id"]), {"status": "success"})
    assert iso[:10] != to_local_iso(local_now(cfg.config.tz))[:10]

    still_today = schedule_next_after(db.get_account_by_id(account["id"]), {"status": "waiting"})
    hour = local_now(cfg.config.tz).hour
    if hour < 20:                      # 窗口还没开始才会排到今天
        assert still_today[:10] == to_local_iso(local_now(cfg.config.tz))[:10]


def test_needs_reauth_account_is_not_scheduled(user):
    account = make_account(user["id"])
    db.set_auth_error(account["id"], "other")
    assert schedule_next(db.get_account_by_id(account["id"])) is None
    assert db.get_account_by_id(account["id"])["next_run_at"] is None


def test_bootstrap_fixes_done_today_but_scheduled_today(user):
    """今天已经打过了、排期却还停在今天 → 启动时应该推到明天（否则当晚反复空跑）。"""
    now = local_now(cfg.config.tz)
    account = make_account(user["id"])
    # insert_account 的列清单里没有 last_*/next_run_at（排期与结果都由后续写入），所以显式造状态
    db.update_account(account["id"], {
        "last_status": "skipped", "last_run_at": to_local_iso(now),
        "next_run_at": to_local_iso(now + timedelta(minutes=1)),
    })
    bootstrap()
    assert db.get_account_by_id(account["id"])["next_run_at"][:10] != to_local_iso(now)[:10]


def test_bootstrap_schedules_accounts_without_plan(user):
    now = local_now(cfg.config.tz)
    account = make_account(user["id"], next_run_at=None)
    bootstrap()
    assert db.get_account_by_id(account["id"])["next_run_at"] is not None
    assert to_local_iso(now)  # 用到了 now，避免 linter 抱怨未使用


def test_attempts_today_counts_only_scheduled(user):
    from app.scheduler import _attempts_today

    now = local_now(cfg.config.tz)
    account = make_account(user["id"])
    db.add_record(account["id"], to_local_iso(now), "manual", "waiting", "手动")
    assert _attempts_today(account) == 0

    db.add_record(account["id"], to_local_iso(now), "schedule", "waiting", "自动")
    assert _attempts_today(account) == 1

    db.add_record(account["id"], to_local_iso(now - timedelta(days=1)), "schedule", "failed", "昨天")
    assert _attempts_today(account) == 1


def test_inside_window():
    from app.scheduler import _inside_window

    account = {"window_start": "20:00", "window_end": "22:30"}
    # 本地墙上时间本来就是 naive（存库也是本地时间串），这里的 DTZ001 不适用
    assert _inside_window(account, datetime(2026, 9, 12, 21, 0)) is True       # noqa: DTZ001
    assert _inside_window(account, datetime(2026, 9, 12, 19, 59)) is False     # noqa: DTZ001
    assert _inside_window(account, datetime(2026, 9, 12, 22, 31)) is False     # noqa: DTZ001


def test_address_only_comes_from_live_request(user, monkeypatch):
    """拿不到实时楼栋名就不写地址（不用历史记录凑数）。"""
    account = make_account(user["id"], token="t", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz)))

    class NoAddress:
        def check_location(self, *_args, **_kw):
            return {"code": "200", "data": {"canDk": False, "msg": "未到打卡时间"}}

    monkeypatch.setattr("app.checkin.build_client", lambda _account: NoAddress())
    assert checkin.fill_address(account) is None
    assert db.get_account_by_id(account["id"])["dkdz"] == ""


def test_address_written_when_live_request_returns_it(user, monkeypatch):
    account = make_account(user["id"], token="t", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz)))

    class WithAddress:
        def check_location(self, *_args, **_kw):
            return {"code": "200", "data": {"canDk": True, "yxMc": "升华8栋"}}

    monkeypatch.setattr("app.checkin.build_client", lambda _account: WithAddress())
    assert checkin.fill_address(account) == "升华8栋"
    assert db.get_account_by_id(account["id"])["dkdz"] == "升华8栋"


class EngineClient:
    """打卡引擎的假客户端：覆盖 run_checkin 的每条分支。"""

    def __init__(self, status, location=None, submit=None, after=None):
        self.casual = "casual1234567890"
        self.token = "tok"
        self._status = status
        self._location = location
        self._submit = submit
        self._after = after
        self.submitted = None

    def dk_status(self, dklb="PA"):
        # 第一次返回 _status，提交后（复核）返回 _after
        return {"code": "200", "data": self._after if self.submitted and self._after else self._status}

    def check_location(self, jd, wd, dklb="PA"):
        return {"code": "200", "data": self._location or {}}

    def submit_dk(self, **kwargs):
        self.submitted = kwargs
        return self._submit or {"code": "200"}

    def cookies_json(self):
        return "[]"


def engine_account(user, monkeypatch, client, **overrides):
    account = make_account(user["id"], token="t", casual="c", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz)), **overrides)
    monkeypatch.setattr("app.checkin.build_client", lambda _account: client)
    return db.get_account_by_id(account["id"])


def engine_status(account):
    return checkin.run_checkin(account, "schedule")


def test_engine_skipped_when_already_checked_in(user, monkeypatch):
    account = engine_account(user, monkeypatch, EngineClient({"sfydk": 1, "dksj": "2026-09-12 20:18:43"}))
    result = engine_status(account)
    assert result["status"] == "skipped"
    assert "今日已打卡" in result["message"]
    assert db.list_records(account["id"], 1)[0]["status"] == "skipped"


def test_engine_waiting_before_window(user, monkeypatch):
    account = engine_account(user, monkeypatch, EngineClient({"sfydk": 0, "kdk": False, "bkyy": "未到打卡时间"}))
    result = engine_status(account)
    assert result["status"] == "waiting"       # 未到时间不算失败
    assert "未到打卡时间" in result["message"]


def test_engine_failed_after_window(user, monkeypatch):
    account = engine_account(user, monkeypatch, EngineClient({"sfydk": 0, "kdk": False, "bkyy": "已过打卡时间"}))
    result = engine_status(account)
    assert result["status"] == "failed"


def test_engine_failed_when_location_rejected(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": False, "msg": "不在考勤范围", "yxMc": "升华5栋", "pcMi": 700},
    )
    account = engine_account(user, monkeypatch, client)
    result = engine_status(account)
    assert result["status"] == "failed"
    assert "位置校验未通过" in result["message"] and "升华5栋" in result["message"]
    assert client.submitted is None            # 位置不过就不该提交


def test_engine_success_stores_address(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": True, "yxMc": "升华8栋"},
        after={"sfydk": 1, "dksj": "2026-09-12 21:02:00"},
    )
    account = engine_account(user, monkeypatch, client)
    result = engine_status(account)

    assert result["status"] == "success"
    assert result["dksj"] == "2026-09-12 21:02:00"
    assert client.submitted["dkdz"] == "升华8栋"       # 提交的地址来自学校按坐标返回的楼栋名
    assert client.submitted["dkbc"] == "校内住宿打卡"
    assert db.get_account_by_id(account["id"])["dkdz"] == "升华8栋"


def test_engine_failed_when_submit_rejected(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True},
        location={"canDk": True, "yxMc": "升华8栋"},
        submit={"code": "500", "message": "服务异常"},
    )
    account = engine_account(user, monkeypatch, client)
    result = engine_status(account)
    assert result["status"] == "failed"
    assert "提交失败" in result["message"]


def test_engine_marks_needs_reauth_on_bad_credentials(user, monkeypatch):
    account = make_account(user["id"])
    monkeypatch.setattr("app.checkin.build_client", lambda _account: EngineClient({}))

    def boom(*_args, **_kw):
        raise RuntimeError("学号或密码错误")

    monkeypatch.setattr("app.checkin.cas_login", boom)
    result = checkin.run_checkin(account, "schedule")

    assert result["status"] == "failed"
    saved = db.get_account_by_id(account["id"])
    assert saved["auth_error"] == "bad_credentials"   # 状态记成"密码错误"
    assert saved["needs_reauth"] == 1                 # 派生标记：停下来等用户，不再每天撞学校
    assert saved["next_run_at"] is None


def test_engine_failed_when_account_disabled(user, monkeypatch):
    account = make_account(user["id"], enabled=0)
    monkeypatch.setattr("app.checkin.build_client", lambda _account: EngineClient({}))
    result = checkin.run_checkin(account, "schedule")
    assert result["status"] == "failed"
    assert "停用" in result["message"]


def test_inside_window_handles_cross_midnight():
    """跨午夜窗口：23:00-01:00 在 00:30 仍算在窗口内（字符串比较做不到这件事）。"""
    from app.scheduler import _inside_window, _window_bounds

    account = {"window_start": "23:00", "window_end": "01:00"}
    # 本地墙上时间本来就是 naive，DTZ001 不适用
    assert _inside_window(account, datetime(2026, 9, 12, 23, 30)) is True     # noqa: DTZ001
    assert _inside_window(account, datetime(2026, 9, 13, 0, 30)) is True      # noqa: DTZ001
    assert _inside_window(account, datetime(2026, 9, 13, 2, 0)) is False      # noqa: DTZ001
    assert _inside_window(account, datetime(2026, 9, 13, 22, 0)) is False     # noqa: DTZ001

    start, end = _window_bounds(account, datetime(2026, 9, 13, 0, 30))        # noqa: DTZ001
    assert (start.day, start.hour) == (12, 23), "00:30 时窗口起点应是昨晚 23:00"
    assert (end.day, end.hour) == (13, 1), "00:30 时窗口终点应是今天 01:00"


def test_retry_is_scheduled_inside_cross_midnight_window(user, monkeypatch):
    """23:30 时跨午夜窗口还剩 90 分钟，应当安排当晚重试，而不是以为窗口已过。"""
    from app.scheduler import _schedule_retry

    account = make_account(user["id"], window_start="23:00", window_end="01:00")
    monkeypatch.setattr("app.scheduler.local_now", lambda _tz: datetime(2026, 9, 12, 23, 30))  # noqa: DTZ001

    iso = _schedule_retry(db.get_account_by_id(account["id"]))
    assert iso is not None, "窗口还没结束，不该放弃重试"
    assert iso.startswith("2026-09-12T23:") or iso.startswith("2026-09-13T00:"), iso


def test_login_survives_undecryptable_password_when_session_is_alive(monkeypatch):
    """本地密码解不开（换过密钥/数据坏了）时，别当场判死 —— 交给 CAS 用会话。"""
    from app import checkin
    from app.errors import SecretDecryptError

    seen = {}

    def boom(_account):
        raise SecretDecryptError("密码解不开")

    monkeypatch.setattr(checkin, "_decrypt_password", boom)
    monkeypatch.setattr(checkin, "build_client", lambda _account: "client")
    monkeypatch.setattr(checkin, "cas_login", lambda c, u, p: seen.update(password=p) or "ok")
    monkeypatch.setattr(checkin, "persist_state", lambda _account_id, _client: None)

    assert checkin._login({"id": 1, "csu_username": "255000001", "password_enc": "x"}) == "client"
    assert seen["password"] is None, "应当把 None 交给 CAS，让它在真需要密码时才报错"
