"""并发与多进程边界：验证码原子消费、配额不穿透、锁回收、调度租约。"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app import auth, db
from app import config as cfg
from app.checkin import relogin
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.errors import RateLimitError
from app.locks import active_lock_count, lock_for


def inject_code(email: str, code: str) -> None:
    now = local_now(cfg.config.tz)
    db.insert_login_code(email, auth.hash_login_code(email, code),
                         to_local_iso(now + timedelta(minutes=10)), to_local_iso(now))


def run_concurrently(count: int, fn):
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(index: int) -> None:
        barrier.wait()                      # 尽量让它们真的同时开始
        results[index] = fn(index)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return results


def test_captcha_is_consumed_atomically():
    email = "atomic@example.com"
    inject_code(email, "424242")
    auth.limiters["verify_ip"].reset()

    results = run_concurrently(8, lambda _i: auth.verify_login_code(email, "424242", "127.0.0.1", "concurrent"))
    winners = [result for result in results if result.ok]
    assert len(winners) == 1, f"应当只有一个请求成功，实际 {len(winners)} 个"

    user = db.find_user_by_email(email)
    sessions = db._all("SELECT token_hash FROM sessions WHERE user_id = ?", (user["id"],))
    assert len(sessions) == 1, "并发成功一次也只该建一条会话"

    losers = [result for result in results if not result.ok]
    assert all(result.reason for result in losers)


def test_wrong_code_does_not_consume_and_counts_attempt():
    email = "wrong-code@example.com"
    inject_code(email, "111111")
    auth.limiters["verify_ip"].reset()

    result = auth.verify_login_code(email, "222222", "127.0.0.1", "t")
    assert not result.ok
    row = db.latest_login_code(email)
    assert row["used"] == 0 and row["attempts"] == 1     # 错码不消耗，只计一次尝试


def test_account_quota_cannot_be_exceeded_concurrently(monkeypatch):
    user = db.upsert_user("quota-race@example.com", to_local_iso(local_now(cfg.config.tz)))
    monkeypatch.setattr("app.config.config", cfg.config.model_copy(update={"max_accounts_per_user": 3}))
    monkeypatch.setattr("app.accounts.verify_login", lambda *_args, **_kw: {
        "location": {"canDk": True, "yxMc": "升华8栋"},
        "address": "升华8栋",
        "session": {"token": "t", "casual": "c", "cookies": "[]"},
        "window": ("20:00", "22:30"),
    })
    from app.accounts import create_or_update

    results = run_concurrently(6, lambda index: _try_add(create_or_update, user, f"9777{index:05d}"))
    saved = db.count_accounts(user["id"])
    assert saved == 3, f"上限 3 却存了 {saved} 个"
    rejected = [error for error in results if error]
    assert len(rejected) == 3 and all("最多只能托管" in str(error) for error in rejected)

    db._exec("DELETE FROM accounts WHERE user_id = ?", (user["id"],))
    db._exec("DELETE FROM users WHERE id = ?", (user["id"],))


def _try_add(create_or_update, user, username):
    try:
        create_or_update(user, {"csuUsername": username, "password": "x", "jd": 112.9, "wd": 28.1})
        return None
    except Exception as error:  # noqa: BLE001 - 收集被拒的原因
        return error


def test_lock_registry_is_recycled():
    assert active_lock_count() == 0
    for index in range(50):
        with lock_for(f"add:tmp-{index}"):
            assert active_lock_count() >= 1
    assert active_lock_count() == 0, "不同 key 的锁用完应当回收，不能只增不减"


def test_relogin_cooldown_lives_in_service_layer():
    now = to_local_iso(local_now(cfg.config.tz))
    user = db.upsert_user("relogin-cool@example.com", now)
    account = db.insert_account({
        "user_id": user["id"], "csu_username": "977800001", "password_enc": encrypt_secret("x"),
        "enabled": 1,
        "jd": 112.9, "wd": 28.1, "dkdz": "", "created_at": now, "updated_at": now,
    })
    first = relogin(db.get_account_by_id(account["id"]))
    assert first["ok"] is False

    with pytest.raises(RateLimitError):
        relogin(db.get_account_by_id(account["id"]))

    db.delete_account(user["id"], account["id"])
    db._exec("DELETE FROM users WHERE id = ?", (user["id"],))


def test_scheduler_lease_allows_only_one_owner():
    from app.scheduler import lease_stale_seconds

    now = local_now(cfg.config.tz)
    fresh = to_local_iso(now)
    stale = to_local_iso(now - timedelta(seconds=lease_stale_seconds() + 1))

    assert db.acquire_scheduler_lease("host:1", fresh, stale) is True
    assert db.acquire_scheduler_lease("host:2", fresh, stale) is False   # 已有持有者
    assert db.heartbeat_scheduler_lease("host:1", fresh) is True
    assert db.heartbeat_scheduler_lease("host:2", fresh) is False        # 非持有者续不了租

    later = to_local_iso(now + timedelta(seconds=lease_stale_seconds() + 10))
    assert db.acquire_scheduler_lease("host:2", later, to_local_iso(now + timedelta(seconds=1))) is True
    assert db.heartbeat_scheduler_lease("host:1", later) is False        # 原持有者已被顶掉

    db._exec("DELETE FROM scheduler_lease")


def test_schema_has_what_the_code_needs():
    structure = db._structure(db._conn)
    for table in ("users", "accounts", "records", "sessions", "login_codes", "scheduler_lease"):
        assert table in structure, f"缺表：{table}"
    for column in ("auth_error", "password_enc", "cookies", "token_at"):
        assert column in structure["accounts"], f"accounts 缺列：{column}"

    rows = db._all("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'records'")
    index = next(row for row in rows if row["name"] == "idx_records_account")
    assert "account_id" in index["sql"]


def test_lease_is_released_on_clean_shutdown():
    from app import scheduler as sched

    now = local_now(cfg.config.tz)
    assert db.acquire_scheduler_lease("host:9", to_local_iso(now), to_local_iso(now)) is True

    sched._lease_owner = "host:9"
    sched.stop_scheduler()

    assert db._all("SELECT owner FROM scheduler_lease") == []
    assert sched._lease_owner is None


def test_lease_stale_follows_scan_interval(monkeypatch):
    from app import scheduler as sched

    monkeypatch.setattr(sched.cfg.config, "scheduler_interval", 20)
    assert sched.lease_stale_seconds() == 120          # 下限兜底

    monkeypatch.setattr(sched.cfg.config, "scheduler_interval", 3600)
    assert sched.lease_stale_seconds() == 10800        # 3 个扫描周期


def test_structure_mismatch_is_reported():
    expected = {"accounts": {"id", "csu_username", "auth_error"}, "records": {"id"}}
    assert db.missing_structure(expected, expected) == []
    assert db.missing_structure(expected, {"accounts": {"id", "csu_username"}}) == [
        "records", "accounts.auth_error"]   # 先报缺表，再报缺列
    assert db.unexpected_structure(expected, {
        "accounts": {"id", "csu_username", "auth_error", "next_run_at"}, "records": {"id"},
    }) == ["accounts.next_run_at"]


def test_structure_is_checked_before_ddl_runs():
    import re
    import sqlite3
    import tempfile
    from pathlib import Path as PathClass

    from app import db
    old_schema = re.sub(r",?\s*auth_error\s+TEXT NOT NULL DEFAULT ''", "", db._SCHEMA)
    old_schema = re.sub(r"CREATE INDEX IF NOT EXISTS idx_accounts_auth_error[^;]*;", "", old_schema)
    assert "auth_error" not in old_schema and "auth_error" in db._SCHEMA

    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(PathClass(tmp) / "old.db"), isolation_level=None)
        try:
            conn.executescript(old_schema)
            with pytest.raises(RuntimeError) as error:
                db._ensure_schema(conn)
            message = str(error.value)
            assert "accounts.auth_error" in message
            assert "请删除旧数据库并重新启动" in message
        finally:
            conn.close()
