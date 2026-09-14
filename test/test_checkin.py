"""打卡引擎与调度。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import checkin, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret

_counter = iter(range(1, 1000))


def make_account(user_id: int, **overrides) -> dict:
    now = local_now(cfg.config.tz)
    row = {
        "user_id": user_id, "csu_username": f"9{next(_counter):08d}",
        "password_enc": encrypt_secret("whatever"), "enabled": 1,

        "jd": 112.936833, "wd": 28.157238, "dkdz": "",
        "created_at": to_local_iso(now), "updated_at": to_local_iso(now),
    }
    row.update(overrides)
    return db.insert_account(row)


@pytest.fixture()
def user():
    return db.upsert_user("checkin-test@example.com", to_local_iso(local_now(cfg.config.tz)))


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


def test_attempts_today_counts_only_scheduled(user, monkeypatch):
    from app.scheduler import _attempts_today

    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr("app.scheduler.local_now", lambda _tz: now)
    account = make_account(user["id"])
    db.add_record(account["id"], to_local_iso(now), "manual", "waiting", "手动")
    assert _attempts_today(account) == 0

    db.add_record(account["id"], to_local_iso(now), "schedule", "waiting", "自动")
    assert _attempts_today(account) == 1

    db.add_record(account["id"], to_local_iso(now - timedelta(days=1)), "schedule", "failed", "昨天")
    assert _attempts_today(account) == 1


def test_batch_runs_all_ready_accounts_in_id_order(user, monkeypatch):
    from app import scheduler

    first = make_account(user["id"])
    second = make_account(user["id"])
    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    calls = []
    monkeypatch.setattr(scheduler, "run_checkin", lambda account, trigger: (
        calls.append((account["id"], trigger)) or {"status": "success", "message": "ok"}
    ))

    results = scheduler.run_batch(accounts=[second, first])

    assert [account["id"] for account, _result in results] == [first["id"], second["id"]]
    assert [account_id for account_id, _trigger in calls] == [first["id"], second["id"]]


def test_pause_skips_accounts_that_need_login_without_spending_retry(user, monkeypatch):
    from app import scheduler

    account = make_account(user["id"], token=None, token_at=None)
    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(scheduler, "login_paused_until", lambda: "2026-09-12T22:00:00")
    monkeypatch.setattr(scheduler, "run_checkin", lambda *_args: pytest.fail("暂停时不应登录"))

    assert scheduler.run_batch(accounts=[account]) == []
    assert db.list_records(account["id"]) == []


def test_batch_retries_after_pause_ends(user, monkeypatch):
    from app import scheduler

    account = make_account(user["id"], token=None, token_at=None)
    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(scheduler, "login_paused_until", lambda: None)
    monkeypatch.setattr(scheduler, "run_checkin", lambda *_args: {"status": "success", "message": "ok"})

    assert len(scheduler.run_batch(accounts=[account])) == 1

def test_inside_window():
    from app.scheduler import _inside_window

    assert _inside_window(datetime(2026, 9, 12, 21, 0)) is True       # noqa: DTZ001
    assert _inside_window(datetime(2026, 9, 12, 19, 59)) is False     # noqa: DTZ001
    assert _inside_window(datetime(2026, 9, 12, 23, 31)) is False     # noqa: DTZ001


class EngineClient:

    def __init__(self, status, location=None, submit=None, after=None):
        self.casual = "casual1234567890"
        self.token = "tok"
        self._status = status
        self._location = location
        self._submit = submit
        self._after = after
        self.submitted = None

    def dk_status(self, dklb="PA"):
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
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.936833, 28.157238), "升华5栋", {"canDk": False, "pcMi": 700, "yxMc": "升华5栋"},
        "located",
    ))
    result = engine_status(account)
    assert result["status"] == "failed"
    assert "位置校验未通过" in result["message"] and "升华5栋" in result["message"]
    assert client.submitted is None            # 位置不过就不该提交


def test_engine_determines_location_when_unknown(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        after={"sfydk": 1, "dksj": "2026-09-12 20:18:43"},
    )
    account = engine_account(user, monkeypatch, client, jd=None, wd=None)
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.936237, 28.158935), "升华24栋", {"canDk": True, "pcMi": 2, "yxMc": "升华24栋"},
        "located",
    ))
    result = engine_status(account)
    assert result["status"] == "success"
    saved = db.get_account_by_id(account["id"])
    assert (saved["jd"], saved["wd"], saved["dkdz"]) == (112.936237, 28.158935, "升华24栋")
    assert client.submitted["jd"] == 112.936237


def test_engine_relocates_when_position_rejected(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": False, "msg": "不在考勤范围", "yxMc": "升华5栋", "pcMi": 700},
        after={"sfydk": 1, "dksj": "2026-09-12 20:18:43"},
    )
    account = engine_account(user, monkeypatch, client)
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.935978, 28.158930), "升华24栋", {"canDk": True, "pcMi": 3, "yxMc": "升华24栋"},
        "located",
    ))
    result = engine_status(account)
    assert result["status"] == "success"
    saved = db.get_account_by_id(account["id"])
    assert (saved["jd"], saved["wd"], saved["dkdz"]) == (112.935978, 28.158930, "升华24栋")
    assert client.submitted["jd"] == 112.935978, "提交要用重测后的坐标"


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


def test_engine_marks_auth_error_on_bad_credentials(user, monkeypatch):
    account = make_account(user["id"])
    monkeypatch.setattr("app.checkin.build_client", lambda _account: EngineClient({}))

    def boom(*_args, **_kw):
        raise RuntimeError("学号或密码错误")

    monkeypatch.setattr("app.checkin.cas_login", boom)
    result = checkin.run_checkin(account, "schedule")

    assert result["status"] == "failed"
    saved = db.get_account_by_id(account["id"])
    assert saved["auth_error"] == "bad_credentials"   # 状态记成"密码错误"


def test_engine_failed_when_account_disabled(user, monkeypatch):
    account = make_account(user["id"], enabled=0)
    monkeypatch.setattr("app.checkin.build_client", lambda _account: EngineClient({}))
    result = checkin.run_checkin(account, "schedule")
    assert result["status"] == "failed"
    assert "停用" in result["message"]


def test_inside_window_handles_cross_midnight(monkeypatch):
    from app.scheduler import _inside_window, _window_bounds

    monkeypatch.setattr(cfg.config, "checkin_window_start", "23:00")
    monkeypatch.setattr(cfg.config, "checkin_window_end", "01:00")
    assert _inside_window(datetime(2026, 9, 12, 23, 30)) is True     # noqa: DTZ001
    assert _inside_window(datetime(2026, 9, 13, 0, 30)) is True      # noqa: DTZ001
    assert _inside_window(datetime(2026, 9, 13, 2, 0)) is False      # noqa: DTZ001
    assert _inside_window(datetime(2026, 9, 13, 22, 0)) is False     # noqa: DTZ001

    start, end = _window_bounds(datetime(2026, 9, 13, 0, 30))        # noqa: DTZ001
    assert (start.day, start.hour) == (12, 23), "00:30 时窗口起点应是昨晚 23:00"
    assert (end.day, end.hour) == (13, 1), "00:30 时窗口终点应是今天 01:00"


def test_cross_midnight_window_does_not_run_twice(user, monkeypatch):
    from app import scheduler

    monkeypatch.setattr(cfg.config, "checkin_window_start", "23:00")
    monkeypatch.setattr(cfg.config, "checkin_window_end", "01:00")
    now = datetime(2026, 9, 13, 0, 30)  # noqa: DTZ001
    account = make_account(user["id"])
    db.update_account(account["id"], {
        "last_status": "success", "last_run_at": "2026-09-12T23:30:00",
    })
    account = db.get_account_by_id(account["id"])
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(scheduler, "run_checkin", lambda *_args: pytest.fail("同一时间窗不应重复打卡"))

    assert scheduler.run_batch(accounts=[account]) == []


def test_login_survives_undecryptable_password_when_session_is_alive(monkeypatch):
    from app import checkin
    from app.errors import SecretDecryptError

    seen = {}

    def boom(_account):
        raise SecretDecryptError("密码解不开")

    monkeypatch.setattr(checkin, "_decrypt_password", boom)
    monkeypatch.setattr(checkin, "build_client", lambda _account: "client")
    monkeypatch.setattr(checkin, "cas_login", lambda c, u, p, **_kw: seen.update(password=p) or "ok")
    monkeypatch.setattr(checkin, "persist_state", lambda _account_id, _client: None)

    assert checkin._login({"id": 1, "csu_username": "255000001", "password_enc": "x"}) == "client"
    assert seen["password"] is None, "应当把 None 交给 CAS，让它在真需要密码时才报错"
