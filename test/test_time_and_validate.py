"""时间与校验：严格 HH:mm、跨天窗口、脱敏。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.clock import pick_next_run, to_local_iso
from app.errors import BadRequestError, InvalidTimeError
from app.log import redact
from app.validate import (
    apply_window_margin,
    parse_hm,
    parse_window_text,
    validate_coordinates,
    validate_window,
    window_span_hours,
)


def test_parse_hm_strict():
    assert parse_hm("20:00") == (20, 0)
    assert parse_hm("7:05") == (7, 5)
    for bad in ["99:00", "25:00", "20:60", "24:00", "20", "abc", "", "  ", "20:0"]:
        with pytest.raises(InvalidTimeError):
            parse_hm(bad)


def test_window_span_cross_day():
    assert window_span_hours("20:00", "22:30") == 2.5
    assert window_span_hours("23:00", "01:00") == 2


def test_validate_window_rejects_bad_input():
    with pytest.raises(InvalidTimeError):
        validate_window("99:00", "99:30", 6)
    with pytest.raises(BadRequestError):
        validate_window("20:00", "23:30", 2)
    validate_window("20:00", "22:30", 6)


def test_validate_coordinates():
    assert validate_coordinates(112.9, 28.1) == (112.9, 28.1)
    for jd, wd in [(float("nan"), 28), (112, float("nan")), (float("inf"), 28), (181, 28), (112, -91), ("x", 28)]:
        with pytest.raises(BadRequestError):
            validate_coordinates(jd, wd)


def test_parse_window_text():
    assert parse_window_text("20:00-23:30") == ("20:00", "23:30")
    assert parse_window_text("时段 20:00 ~ 23:30 内") == ("20:00", "23:30")
    assert parse_window_text("99:00-99:30") is None
    assert parse_window_text("暂无") is None


def test_apply_window_margin():
    assert apply_window_margin(("20:00", "23:30"), 60) == ("20:00", "22:30")
    assert apply_window_margin(("23:00", "01:00"), 30) == ("23:00", "00:30")
    assert apply_window_margin(("20:00", "20:10"), 60) == ("20:00", "20:30")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_pick_next_run_inside_window_uses_remaining_time():
    for _ in range(50):
        result = to_local_iso(pick_next_run("20:00", "22:30", "Asia/Shanghai", at("2026-09-12T21:00:00")))
        assert "2026-09-12T21:00:00" < result < "2026-09-12T22:30:00"


def test_pick_next_run_before_window():
    result = to_local_iso(pick_next_run("20:00", "22:30", "Asia/Shanghai", at("2026-09-12T19:00:00")))
    assert result.startswith("2026-09-12T")


def test_pick_next_run_after_window_goes_tomorrow():
    result = to_local_iso(pick_next_run("20:00", "22:30", "Asia/Shanghai", at("2026-09-12T23:00:00")))
    assert result.startswith("2026-09-13T")


def test_pick_next_run_cross_midnight_stays_in_current_window():
    """23:00-01:00 在 00:30 仍在昨晚开的窗口里，不能排到今晚。"""
    for _ in range(50):
        result = to_local_iso(pick_next_run("23:00", "01:00", "Asia/Shanghai", at("2026-09-13T00:30:00")))
        assert "2026-09-13T00:30:00" < result < "2026-09-13T01:00:00"


def test_pick_next_run_cross_midnight_after_window():
    result = to_local_iso(pick_next_run("23:00", "01:00", "Asia/Shanghai", at("2026-09-13T01:30:00")))
    assert "2026-09-13T23:00:00" < result < "2026-09-14T01:00:00"


def test_pick_next_run_rejects_bad_window():
    with pytest.raises(InvalidTimeError):
        pick_next_run("99:00", "99:30", "Asia/Shanghai", at("2026-09-12T21:00:00"))


def test_redact_hides_credentials_keeps_counters():
    out = redact({
        "password": "p", "code": "123456", "dev_code": "123456", "code_hash": "abc",
        "casual": "AbCdEf12GhIjKl34", "token": "jwt", "cookies": "[{}]", "email": "a@b.c",
        "csu_username_tail": "2034", "codes": 3, "sessions": 0, "status": "success",
    })
    for key in ["password", "code", "dev_code", "code_hash", "casual", "token", "cookies", "email"]:
        assert out[key] == "***", key
    assert out["codes"] == 3
    assert out["csu_username_tail"] == "2034"
    assert out["status"] == "success"


def test_redact_hides_ciphertext_like_values():
    out = redact({"note": "v1.aaa.bbb", "key": "re_abcdefgh"})
    assert out["note"] == "***"
    assert out["key"] == "***"
