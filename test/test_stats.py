"""公开统计与缓存。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import config as cfg
from app import db, stats
from app.main import app


@pytest.fixture()
def store(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    with monkeypatch.context() as patch:
        patch.setattr(db, "_conn", conn)
        patch.setattr(stats, "_cached", {})
        patch.setattr(stats, "_expires", 0)
        now = datetime.fromisoformat("2026-10-23T23:59:30")
        patch.setattr(stats, "local_now", lambda _: now)
        yield conn, now
    conn.close()


def add_account(conn, enabled=1):
    conn.execute("INSERT OR IGNORE INTO users(id, email, created_at) VALUES(1, 'stats@example.com', '2026-10-23')")
    return conn.execute("""
        INSERT INTO accounts(user_id, csu_username, password_enc, enabled, created_at, updated_at)
        VALUES(1, lower(hex(randomblob(8))), '', ?, '2026-10-23', '2026-10-23')
    """, (enabled,)).lastrowid


def record(conn, account_id, status, run_at="2026-10-23T20:00:00"):
    conn.execute("INSERT INTO records(account_id, status, run_at, trigger) VALUES(?, ?, ?, 'schedule')",
                 (account_id, status, run_at))


def test_public_stats_empty(store):
    response = TestClient(app).get("/api/stats")
    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-10-23", "updatedAt": "2026-10-23T23:59:30",
        "accounts": 0, "enabledAccounts": 0, "todaySuccess": 0,
        "enabledToday": {"success": 0, "notDue": 0, "noTask": 0, "failed": 0,
                         "verifying": 0, "pending": 0, "expired": 0},
    }


def test_success_counts_accounts_including_disabled(store):
    conn, _ = store
    first = add_account(conn)
    for status in ("failed", "success", "skipped", "failed"):
        record(conn, first, status)
    record(conn, add_account(conn, enabled=0), "skipped")
    record(conn, add_account(conn), "success", "2026-10-22T23:59:59")
    record(conn, add_account(conn), "success", "2026-10-24T00:00:00")
    for status in ("failed", "waiting", "no_task"):
        record(conn, add_account(conn), status)
    result = stats.snapshot()
    assert (result["accounts"], result["enabledAccounts"], result["todaySuccess"]) == (7, 6, 2)
    assert result["enabledToday"] == {
        "success": 1, "notDue": 0, "noTask": 1, "failed": 1, "verifying": 0, "pending": 3, "expired": 0,
    }


def test_enabled_categories_are_disjoint(store, monkeypatch):
    conn, _ = store
    monkeypatch.setattr(stats, "local_now", lambda _: datetime.fromisoformat("2026-10-23T13:00:00"))
    monkeypatch.setattr(cfg.config, "checkin_start", "12:00")
    for status in ("success", "no_task", "failed"):
        record(conn, add_account(conn), status, "2026-10-23T12:00:00")
    for start, end, not_before in (("20:00", "23:30", None), ("05:00", "12:00", None),
                                   ("05:00", "23:30", "14:00"), (None, None, None)):
        account_id = add_account(conn)
        conn.execute("UPDATE accounts SET checkin_start=?,checkin_end=?,not_before=? WHERE id=?",
                     (start, end, not_before, account_id))
    for date in ("2026-10-23", "2026-10-22"):
        account_id = add_account(conn)
        record(conn, account_id, "failed", date + "T12:00:00")
        db.upsert_verification(account_id, date + "T12:01:00", "manual", date + "T12:01:30", date)
    record(conn, add_account(conn, enabled=0), "success")

    result = stats.snapshot()

    assert result["enabledToday"] == {
        "success": 1, "notDue": 2, "noTask": 1, "failed": 1, "verifying": 1, "pending": 2, "expired": 1,
    }
    assert sum(result["enabledToday"].values()) == result["enabledAccounts"] == 9
    assert result["todaySuccess"] == 2


@pytest.mark.parametrize(("start", "end", "not_before", "time", "expected"), [
    ("05:00", "23:30", None, "11:59", "notDue"),
    ("05:00", "23:30", "14:00", "14:00", "pending"),
    ("05:00", "23:30", None, "23:30", "pending"),
    ("05:00", "23:30", None, "23:31", "expired"),
    ("23:00", "01:00", None, "22:00", "notDue"),
    ("23:00", "01:00", None, "23:00", "pending"),
    ("23:00", "01:00", None, "00:30", "notDue"),
    ("invalid", "23:30", None, "13:00", "pending"),
])
def test_category_respects_account_and_global_times(monkeypatch, start, end, not_before, time, expected):
    monkeypatch.setattr(cfg.config, "checkin_start", "12:00")
    account = {"success": False, "status": None, "verifying": False,
               "checkin_start": start, "checkin_end": end, "not_before": not_before}
    assert stats._category(account, time) == expected


def test_cache_expires_after_ten_seconds(store, monkeypatch):
    conn, _ = store
    monkeypatch.setattr(stats.time, "monotonic", lambda: 100)
    first = stats.snapshot()
    first["accounts"] = 999
    first["enabledToday"]["success"] = 999
    add_account(conn)
    monkeypatch.setattr(stats.time, "monotonic", lambda: 109)
    assert stats.snapshot()["accounts"] == 0
    assert stats.snapshot()["enabledToday"]["success"] == 0
    monkeypatch.setattr(stats.time, "monotonic", lambda: 110)
    assert stats.snapshot()["accounts"] == 1


def test_new_day_refreshes_before_cache_expires(store, monkeypatch):
    conn, now = store
    now = now.replace(second=55)
    monkeypatch.setattr(stats, "local_now", lambda _: now)
    monkeypatch.setattr(stats.time, "monotonic", lambda: 100)
    record(conn, add_account(conn), "success")
    assert stats.snapshot()["todaySuccess"] == 1
    monkeypatch.setattr(stats, "local_now", lambda _: now + timedelta(seconds=6))
    monkeypatch.setattr(stats.time, "monotonic", lambda: 106)
    result = stats.snapshot()
    assert result["date"] == "2026-10-24"
    assert result["todaySuccess"] == 0
    assert result["enabledToday"]["success"] == 0
    assert result["enabledToday"]["notDue"] == 1
