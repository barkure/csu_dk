"""日志级别与输出脱敏。"""
from __future__ import annotations

import io
import json

import pytest
import structlog

from app import log


@pytest.fixture()
def output(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(log, "_logger", structlog.wrap_logger(structlog.PrintLogger(stream)))
    return stream


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
def test_event_level_reaches_output(output, level):
    log.log_event("test.event", level=level, status=503, count=2)

    row = json.loads(output.getvalue())
    assert row["level"] == level
    assert row["event"] == "test.event"
    assert row["status"] == 503
    assert row["count"] == 2


def test_free_text_and_nested_fields_are_scrubbed(output):
    log.log_event("test.error", level="error", error="请求失败 token=secret-jwt user@example.com",
                  context={"password": "secret-password", "details": ["CASTGC=TGT-private"]},
                  url="https://ca.example.com/login?ticket=ST-private", upstream_status="331")

    text = output.getvalue()
    for value in ("secret-jwt", "user@example.com", "secret-password", "TGT-private", "ST-private"):
        assert value not in text
    row = json.loads(text)
    assert row["error"].startswith("请求失败")
    assert row["upstream_status"] == "331"
    assert row["context"]["password"] == "***"


def test_enabled_change_keeps_audit_fields_without_full_username(output):
    log.log_account_enabled_changed(user_id=4, account_id=12, username="8301210402",
                                    enabled=False, source="api")

    text = output.getvalue()
    row = json.loads(text)
    assert "8301210402" not in text
    assert {key: value for key, value in row.items() if key not in {"t", "level"}} == {
        "event": "account.enabled_changed", "user_id": 4, "account_id": 12,
        "csu_username_tail": "0402", "enabled": False, "source": "api",
    }
