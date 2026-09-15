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
        "password_enc": encrypt_secret("whatever"), "enabled": 1, "dkdz": "",
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


def test_batch_randomly_picks_two_ready_accounts(user, monkeypatch):
    from app import scheduler

    accounts = [make_account(user["id"]) for _ in range(5)]
    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    calls = []
    monkeypatch.setattr(scheduler, "run_checkin", lambda account, trigger: (
        calls.append(account["id"]) or {"status": "success", "message": "ok"}
    ))
    monkeypatch.setattr(scheduler.random, "sample", lambda population, k: list(population)[:k])

    results = scheduler.run_batch(accounts=accounts)

    assert calls == [accounts[0]["id"], accounts[1]["id"]]
    assert [account["id"] for account, _result in results] == calls


def test_batch_force_runs_all_ready_accounts(user, monkeypatch):
    from app import scheduler

    accounts = [make_account(user["id"]) for _ in range(4)]
    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(scheduler, "run_checkin",
                        lambda account, trigger: {"status": "success", "message": "ok"})
    monkeypatch.setattr(scheduler.random, "sample", lambda population, k: list(population)[:k])

    results = scheduler.run_batch(force=True, accounts=accounts)
    assert len(results) == 4


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


def test_checkin_and_refresh_ticks_use_separate_windows(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(scheduler, "_lease_owner", None)
    called = []
    monkeypatch.setattr(scheduler, "refresh_logins", lambda: called.append("refresh"))
    monkeypatch.setattr(scheduler, "run_batch", lambda: called.append("batch"))

    monkeypatch.setattr(scheduler, "local_now", lambda _tz: datetime(2026, 9, 12, 10, 0))  # noqa: DTZ001
    scheduler.tick()
    scheduler.refresh_tick()
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: datetime(2026, 9, 12, 21, 0))  # noqa: DTZ001
    scheduler.tick()
    scheduler.refresh_tick()
    assert called == ["refresh", "batch"]


def test_scheduler_registers_separate_intervals(monkeypatch):
    from app import scheduler

    jobs = []

    class FakeScheduler:
        def add_job(self, fn, _kind, **kwargs):
            jobs.append((fn.__name__, kwargs["seconds"]))

        def start(self):
            pass

    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **_kwargs: FakeScheduler())
    monkeypatch.setattr(scheduler, "_holds_lease", lambda *_args, **_kwargs: True)
    scheduler.start_scheduler()
    assert ("tick", cfg.config.scheduler_interval) in jobs
    assert ("refresh_tick", cfg.config.refresh_interval) in jobs
    scheduler._scheduler = None
    scheduler._lease_owner = None


def test_refresh_picks_one_stale_account(user, monkeypatch):
    from app import scheduler

    now = datetime(2026, 9, 12, 10, 0)  # noqa: DTZ001
    today = to_local_iso(now)
    yesterday = to_local_iso(now - timedelta(days=1))
    fresh = make_account(user["id"], token="t", cookies="[]", token_at=today)
    broken = make_account(user["id"], token="t", cookies="[]", token_at=yesterday,
                          auth_error="bad_credentials")
    stale = make_account(user["id"], token="t", cookies="[]", token_at=yesterday)
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(checkin, "local_now", lambda _tz: now)
    calls = []
    monkeypatch.setattr(scheduler, "refresh_login", lambda account: (
        calls.append(account["id"]) or {"ok": True, "message": "已刷新登录态"}
    ))
    monkeypatch.setattr(scheduler.random, "sample", lambda population, k: list(population)[:k])

    results = scheduler.refresh_logins(accounts=[fresh, broken, stale])
    assert calls == [stale["id"]]
    assert [account["id"] for account, _result in results] == [stale["id"]]


def test_refresh_stops_when_all_have_today_token(user, monkeypatch):
    from app import scheduler

    now = datetime(2026, 9, 12, 10, 0)  # noqa: DTZ001
    today = to_local_iso(now)
    first = make_account(user["id"], token="t", cookies="[]", token_at=today)
    second = make_account(user["id"], token="t", cookies="[]", token_at=today)
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(checkin, "local_now", lambda _tz: now)
    calls = []
    monkeypatch.setattr(scheduler, "refresh_login", lambda account: (
        calls.append(account["id"]) or {"ok": True, "message": "登录态有效"}
    ))
    results = scheduler.refresh_logins(accounts=[first, second])
    assert calls == []
    assert results == []


def test_refresh_login_updates_token_without_checkin_record(user, monkeypatch):
    account = make_account(user["id"], token="old", cookies="[]",
                           token_at=to_local_iso(local_now(cfg.config.tz) - timedelta(days=1)))

    class Client:
        token = "new"
        casual = "cas"
        def cookies_json(self):
            return "[]"

    monkeypatch.setattr("app.checkin.build_client", lambda _account: Client())
    monkeypatch.setattr("app.checkin.cas_login", lambda *_args, **_kw: None)

    result = checkin.refresh_login(db.get_account_by_id(account["id"]))
    saved = db.get_account_by_id(account["id"])
    assert result["ok"] is True
    assert saved["token"] == "new"
    assert checkin.has_fresh_login(saved) is True
    assert db.list_records(account["id"]) == []


def test_refresh_login_defers_rate_limit(user, monkeypatch):
    from app.errors import RateLimitError

    account = make_account(user["id"])
    monkeypatch.setattr("app.checkin.build_client", lambda _account: EngineClient({}))

    def boom(*_args, **_kw):
        raise RateLimitError("登录请求太密集，请 12 秒后再试", 12)

    monkeypatch.setattr("app.checkin.cas_login", boom)
    result = checkin.refresh_login(account)
    assert result["deferred"] is True
    assert db.get_account_by_id(account["id"])["auth_error"] == ""
    assert db.list_records(account["id"]) == []


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


def test_engine_relogs_when_session_rejected(user, monkeypatch):
    client = EngineClient({"sfydk": 1, "dksj": "2026-09-12 20:18:43"})
    account = engine_account(user, monkeypatch, client)
    calls = {"status": 0, "login": 0}
    original_status = client.dk_status

    def dk_status(dklb="PA"):
        calls["status"] += 1
        if calls["status"] == 1:
            return {"code": "401", "message": "未登录"}
        return original_status()

    def fake_login(_account, **_kw):
        calls["login"] += 1
        return client

    client.dk_status = dk_status
    monkeypatch.setattr("app.checkin._login", fake_login)
    result = engine_status(account)
    assert result["status"] == "skipped"
    assert calls == {"status": 2, "login": 1}


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
    account = engine_account(user, monkeypatch, client, dkdz="升华5栋")
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
    account = engine_account(user, monkeypatch, client)
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.936237, 28.158935), "升华24栋", {"canDk": True, "pcMi": 2, "yxMc": "升华24栋"},
        "located",
    ))
    result = engine_status(account)
    assert result["status"] == "success"
    saved = db.get_account_by_id(account["id"])
    assert saved["dkdz"] == "升华24栋"
    assert saved["jd"] is None and saved["wd"] is None
    assert client.submitted["jd"] == 112.936237


def test_engine_relocates_when_position_rejected(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": False, "msg": "不在考勤范围", "yxMc": "升华5栋", "pcMi": 700},
        after={"sfydk": 1, "dksj": "2026-09-12 20:18:43"},
    )
    account = engine_account(user, monkeypatch, client, dkdz="升华5栋")
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.935978, 28.158930), "升华24栋", {"canDk": True, "pcMi": 3, "yxMc": "升华24栋"},
        "located",
    ))
    result = engine_status(account)
    assert result["status"] == "success"
    saved = db.get_account_by_id(account["id"])
    assert saved["dkdz"] == "升华24栋"
    assert saved["jd"] is None
    assert client.submitted["jd"] == 112.935978, "提交要用重测后的坐标"


def test_engine_stores_private_coords_for_rental(user, monkeypatch):
    from app import buildings

    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        after={"sfydk": 1, "dksj": "2026-09-12 20:18:43"},
    )
    account = engine_account(user, monkeypatch, client)
    monkeypatch.setattr("app.checkin.buildings.for_student", lambda _client, name="": (
        (112.927056, 28.175114), "你申报的租房地址",
        {"canDk": True, "pcMi": 2, "yxMc": "你申报的租房地址"}, "located",
    ))
    result = engine_status(account)
    assert result["status"] == "success"
    saved = db.get_account_by_id(account["id"])
    assert saved["dkdz"] == "你申报的租房地址"
    assert (saved["jd"], saved["wd"]) == (112.927056, 28.175114)
    assert buildings.resolve("你申报的租房地址") is None


def test_engine_reuses_account_coords_for_rental(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": True, "yxMc": "你申报的租房地址"},
        after={"sfydk": 1, "dksj": "2026-09-12 20:18:43"},
    )
    account = engine_account(user, monkeypatch, client, dkdz="你申报的租房地址",
                             jd=112.927056, wd=28.175114)
    monkeypatch.setattr("app.checkin.buildings.for_student",
                        lambda *_a, **_k: pytest.fail("租房已有坐标不该重测"))
    result = engine_status(account)
    assert result["status"] == "success"
    assert client.submitted["jd"] == 112.927056


def test_engine_uses_cached_building_coords(user, monkeypatch):
    from app import buildings

    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": True, "yxMc": "升华8栋"},
        after={"sfydk": 1, "dksj": "2026-09-12 21:02:00"},
    )
    account = engine_account(user, monkeypatch, client, dkdz="升华8栋")
    monkeypatch.setattr("app.checkin.buildings.for_student",
                        lambda *_a, **_k: pytest.fail("有楼栋缓存不该重测"))
    result = engine_status(account)
    assert result["status"] == "success"
    assert client.submitted["jd"] == buildings.resolve("升华8栋")[0]


def test_engine_success_stores_address(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True, "dkbc": "校内住宿打卡"},
        location={"canDk": True, "yxMc": "升华8栋"},
        after={"sfydk": 1, "dksj": "2026-09-12 21:02:00"},
    )
    account = engine_account(user, monkeypatch, client, dkdz="升华8栋")
    result = engine_status(account)

    assert result["status"] == "success"
    assert result["dksj"] == "2026-09-12 21:02:00"
    assert client.submitted["dkdz"] == "升华8栋"
    assert client.submitted["dkbc"] == "校内住宿打卡"
    assert db.get_account_by_id(account["id"])["dkdz"] == "升华8栋"


def test_engine_failed_when_submit_rejected(user, monkeypatch):
    client = EngineClient(
        {"sfydk": 0, "kdk": True},
        location={"canDk": True, "yxMc": "升华8栋"},
        submit={"code": "500", "message": "服务异常"},
    )
    account = engine_account(user, monkeypatch, client, dkdz="升华8栋")
    result = engine_status(account)
    assert result["status"] == "failed"
    assert "提交失败" in result["message"]


def test_engine_schedule_defers_login_rate_limit(user, monkeypatch):
    from app.errors import RateLimitError

    account = make_account(user["id"])
    monkeypatch.setattr("app.checkin.build_client", lambda _account: EngineClient({}))

    def boom(*_args, **_kw):
        raise RateLimitError("登录请求太密集，请 12 秒后再试", 12)

    monkeypatch.setattr("app.checkin.cas_login", boom)
    result = checkin.run_checkin(account, "schedule")

    assert result["deferred"] is True
    assert db.list_records(account["id"]) == []
    assert db.get_account_by_id(account["id"])["last_status"] is None


def test_batch_skips_deferred_rate_limit(user, monkeypatch):
    from app import scheduler

    account = make_account(user["id"])
    now = datetime(2026, 9, 12, 21, 0)  # noqa: DTZ001
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    monkeypatch.setattr(scheduler, "run_checkin", lambda *_args: {
        "status": "waiting", "message": "登录请求太密集", "deferred": True,
    })

    assert scheduler.run_batch(accounts=[account]) == []
    assert db.list_records(account["id"]) == []


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


def test_engine_disables_account_when_school_has_no_task(user, monkeypatch):
    client = EngineClient({})
    account = engine_account(user, monkeypatch, client)
    client.dk_status = lambda dklb="PA": {"code": "331", "message": "当前没有打卡事项", "data": None}
    logins = []
    monkeypatch.setattr("app.checkin.cas_login", lambda *_a, **_k: logins.append("login"))

    result = engine_status(account)

    assert result == {"status": "no_task", "message": "无打卡事项", "dksj": None}
    assert logins == []
    assert [record["status"] for record in db.list_records(account["id"])] == ["no_task"]
    saved = db.get_account_by_id(account["id"])
    assert saved["enabled"] == 0
    assert saved["auth_error"] == ""


def test_engine_still_fails_on_unknown_business_code(user, monkeypatch):
    client = EngineClient({})
    account = engine_account(user, monkeypatch, client)
    client.dk_status = lambda dklb="PA": {"code": "500", "message": "服务异常", "data": None}
    monkeypatch.setattr("app.checkin.cas_login", lambda *_a, **_k: None)

    result = engine_status(account)

    assert result["status"] == "failed"
    assert "业务接口返回异常" in result["message"]
