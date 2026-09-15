"""请求体加密。"""
from __future__ import annotations

from app.csu.des import des_encrypt, generate_casual

KEY = "AbCdEf12GhIjKl34"


def test_matches_school_frontend_ascii():
    assert des_encrypt({"paramsData": {"dklb": "PA"}}, KEY) == (
        "e916c32361d768b4419e6210fb78f37c8ede7a4ae0044cb07885c83c2e235d5e"
    )


def test_matches_school_frontend_with_chinese():
    payload = {"jd": 112.936833, "wd": 28.157238, "dkbc": "校内住宿打卡", "dkdz": "升华8栋"}
    assert des_encrypt(payload, KEY) == (
        "b2d338abd6d415e401d7534aafe8d8988ce328368780f0a1fde99a39df036ce1"
        "9e81bf8953ca23f042b54c447d6a889b747d9e46c8d30a15e2854be503d1981"
        "070c4300b2f54259ec9324f9a46bc76375bdc6ec152d5c8c6"
    )


def test_key_only_uses_first_8_bytes():
    assert des_encrypt({"a": 1}, "12345678zzzzzzzz") == des_encrypt({"a": 1}, "12345678")


def test_casual_is_16_alnum():
    value = generate_casual()
    assert len(value) == 16
    assert value.isalnum()
    assert len(generate_casual(8)) == 8
