"""端到端脚本失败时不留下数据，也不删除已有用户。"""
from __future__ import annotations

import pytest

from app import db
from tools import e2e


@pytest.fixture(autouse=True)
def reset_script(monkeypatch):
    monkeypatch.setattr(e2e, "EMAIL", "e2e-tool-test@example.com")
    monkeypatch.setattr(e2e, "CSU_USER", None)
    monkeypatch.setattr(e2e, "CSU_PASS", None)
    monkeypatch.setattr(e2e, "cookie", None)
    monkeypatch.setattr(e2e, "failures", 0)


def test_non_json_health_stops_before_writing(monkeypatch):
    monkeypatch.setattr(e2e, "call", lambda *_args, **_kwargs: (502, "Bad Gateway"))
    assert e2e.main() == 1
    assert db.find_user_by_email(e2e.EMAIL) is None
    assert db.latest_login_code(e2e.EMAIL) is None


def test_existing_user_is_never_deleted(monkeypatch):
    user = db.upsert_user(e2e.EMAIL, db.now_iso())
    monkeypatch.setattr(e2e, "call", lambda *_args, **_kwargs: (200, {"ok": True}))
    assert e2e.main() == 1
    assert db.find_user_by_email(e2e.EMAIL)["id"] == user["id"]
    assert db.latest_login_code(e2e.EMAIL) is None
    db.delete_user(e2e.EMAIL)


@pytest.mark.parametrize("failure_status", [400, 429])
def test_failed_login_cleans_code_and_user(monkeypatch, failure_status):
    def fake_call(path, *_args, **_kwargs):
        if path == "/api/health":
            return 200, {"ok": True}
        if failure_status == 429:
            db.upsert_user(e2e.EMAIL, db.now_iso())
        return failure_status, {"error": "验证失败"}

    monkeypatch.setattr(e2e, "call", fake_call)
    assert e2e.main() == 1
    assert db.find_user_by_email(e2e.EMAIL) is None
    assert db.latest_login_code(e2e.EMAIL) is None


def test_verify_rate_limit_is_failure_and_cleans_data(monkeypatch, capsys):
    def fake_call(path, _method="GET", body=None):
        if path == "/api/health":
            return 200, {"ok": True}
        if body["code"] == "424242":
            db.upsert_user(e2e.EMAIL, db.now_iso())
            e2e.cookie = "session=fake"
            return 200, {"ok": True}
        return 429, {"error": "尝试过于频繁"}

    monkeypatch.setattr(e2e, "call", fake_call)
    assert e2e.main() == 1
    assert "限流" in capsys.readouterr().out
    assert db.find_user_by_email(e2e.EMAIL) is None
    assert db.latest_login_code(e2e.EMAIL) is None
