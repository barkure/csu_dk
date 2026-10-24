"""时间与校验：严格 HH:mm、跨天窗口、脱敏。"""
from __future__ import annotations

import pytest

from app.errors import InvalidTimeError
from app.log import redact
from app.validate import parse_hm, parse_window


def test_parse_hm_strict():
    assert parse_hm("20:00") == (20, 0)
    assert parse_hm("7:05") == (7, 5)
    for bad in ["99:00", "25:00", "20:60", "24:00", "20", "abc", "", "  ", "20:0"]:
        with pytest.raises(InvalidTimeError):
            parse_hm(bad)


def test_parse_window_accepts_school_format():
    assert parse_window("20:00-23:30") == ("20:00", "23:30")
    assert parse_window(" 5:00 ~ 23:30 ") == ("05:00", "23:30")
    assert parse_window("05:00至23:30") == ("05:00", "23:30")
    for bad in [None, "", "20:00", "20:00-", "25:00-26:00", "abc", "20:00-23:30,"]:
        assert parse_window(bad) is None, bad


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
