"""公开统计与缓存。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

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


def test_cache_expires_after_sixty_seconds(store, monkeypatch):
    conn, _ = store
    monkeypatch.setattr(stats.time, "monotonic", lambda: 100)
    first = stats.snapshot()
    first["accounts"] = 999
    add_account(conn)
    monkeypatch.setattr(stats.time, "monotonic", lambda: 159)
    assert stats.snapshot()["accounts"] == 0
    monkeypatch.setattr(stats.time, "monotonic", lambda: 160)
    assert stats.snapshot()["accounts"] == 1


def test_new_day_refreshes_before_cache_expires(store, monkeypatch):
    conn, now = store
    monkeypatch.setattr(stats.time, "monotonic", lambda: 100)
    record(conn, add_account(conn), "success")
    assert stats.snapshot()["todaySuccess"] == 1
    monkeypatch.setattr(stats, "local_now", lambda _: now + timedelta(seconds=30))
    result = stats.snapshot()
    assert result["date"] == "2026-10-24"
    assert result["todaySuccess"] == 0
