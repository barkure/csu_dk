"""配置映射：.env.example 里写的每个变量，代码都必须真的读它。

这条测试的由来：改用 pydantic-settings 时漏了 5 个映射（CSU_DK_MAX_ACCOUNTS 等），
名字对不上，又因为默认值恰好相同，改了 .env 毫无效果却一直没被发现。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = PROJECT_ROOT / ".env.example"


def documented() -> dict[str, str]:
    return dict(re.findall(r"^([A-Z_]+)=(.*)$", EXAMPLE.read_text(), re.M))


SPECIAL = {
    "CSU_DK_TZ": "UTC",                          # 必须是合法时区
    "CSU_DK_ALLOWED_EMAILS": "someone@example.com",  # 必须是合法邮箱
    "CSU_DK_HOST": "0.0.0.0",
    "CSU_DK_CHECKIN_START": "19:30",
    "CSU_DK_COOKIE_SECURE": "true",            # 示例建议值与代码默认值不同
    "MAIL_FROM": "Changed <changed@example.com>",
    "PLUNK_API_URL": "https://mail.example.com",
}


def mutated(name: str, value: str) -> str:
    if name in SPECIAL:
        return SPECIAL[name]
    if value.lower() in {"true", "false"}:
        return "false" if value.lower() == "true" else "true"
    if value.isdigit():
        return str(int(value) + 1)
    return f"{value}x"


@pytest.mark.parametrize("name", sorted(documented()))
def test_documented_variable_is_actually_read(name: str, monkeypatch):
    baseline = Settings()
    monkeypatch.setenv(name, mutated(name, documented()[name]))
    assert Settings() != baseline, f"{name} 改了但配置没变 —— 这个变量没有被读到"


def test_data_dir_defaults_into_project(monkeypatch):
    monkeypatch.delenv("CSU_DK_DATA_DIR", raising=False)
    monkeypatch.delenv("CSU_DK_TEST_DATA_DIR", raising=False)
    assert Settings().data_dir == PROJECT_ROOT / "data"
    assert "CSU_DK_HOME" not in EXAMPLE.read_text()
    # 测试别名仅供 test/conftest.py 使用：.env.example 里可以提及，但不许启用赋值
    assert not re.search(r"^CSU_DK_TEST_DATA_DIR=", EXAMPLE.read_text(), re.M)


def test_blank_plunk_settings_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("PLUNK_API_URL", "")
    monkeypatch.setenv("PLUNK_SECRET_KEY", "")
    settings = Settings()
    assert settings.plunk_api_url == "https://next-api.useplunk.com"
    assert settings.plunk_secret_key == ""


@pytest.mark.parametrize("value", [
    "ftp://mail.example.com",
    "https://user:pass@mail.example.com",
    "mail.example.com",
])
def test_plunk_api_url_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("PLUNK_API_URL", value)
    with pytest.raises(ValueError, match="PLUNK_API_URL"):
        Settings()


def test_mail_from_must_be_a_valid_sender(monkeypatch):
    monkeypatch.setenv("MAIL_FROM", "not-an-email")
    with pytest.raises(ValueError, match="MAIL_FROM"):
        Settings()


SUGGESTIONS = {
    "MAIL_FROM", "CSU_DK_ALLOWED_EMAILS", "CSU_DK_COOKIE_SECURE",
}


def test_outbound_proxies_are_split_and_deduplicated(monkeypatch):
    monkeypatch.setenv(
        "CSU_DK_PROXIES",
        "http://127.0.0.1:1091, http://127.0.0.1:1092,http://127.0.0.1:1091",
    )
    assert Settings().outbound_proxies == (
        "http://127.0.0.1:1091",
        "http://127.0.0.1:1092",
    )


@pytest.mark.parametrize("value", [
    "127.0.0.1:1091",
    "ftp://127.0.0.1:1091",
    "socks5://127.0.0.1:1091",      # 没装 PySocks，装了也用不了，直接拒绝
    "socks5h://127.0.0.1:1091",
    "http://127.0.0.1",
    "http://127.0.0.1:not-a-port",
])
def test_outbound_proxies_reject_invalid_urls(monkeypatch, value):
    monkeypatch.setenv("CSU_DK_PROXIES", value)
    with pytest.raises(ValueError, match="CSU_DK_PROXIES 里有非法代理地址"):
        Settings()


def test_documented_values_match_code_defaults():
    aliases = {}
    for field_name, field in Settings.model_fields.items():
        alias = field.validation_alias
        aliases[str(alias) if alias else f"CSU_DK_{field_name.upper()}"] = field_name

    mismatches = []
    for var, value in documented().items():
        if var in SUGGESTIONS:
            continue
        field_name = aliases.get(var)
        assert field_name, f"{var} 写在 .env.example 里，配置类却没有对应字段"
        default = getattr(Settings(), field_name)
        documented_default = str(default).lower() if isinstance(default, bool) else str(default)
        if documented_default != value:
            mismatches.append(f"{var}: 示例 {value!r} ≠ 代码默认 {default!r}")
    assert not mismatches, "示例与代码默认值不一致：\n  " + "\n  ".join(mismatches)
