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
    "CSU_DK_CHECKIN_WINDOW_START": "19:30",
    "CSU_DK_CHECKIN_WINDOW_END": "23:00",
    "CSU_DK_COOKIE_SECURE": "true",            # 示例建议值与代码默认值不同
    "MAIL_FROM": "Changed <changed@example.com>",
    "TENCENT_SES_VERIFICATION_CODE_TEMPLATE_ID": "123456",
    "TENCENT_SES_CREDENTIAL_INVALID_TEMPLATE_ID": "123456",
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


def test_blank_template_id_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("TENCENT_SES_VERIFICATION_CODE_TEMPLATE_ID", "")
    monkeypatch.setenv("TENCENT_SES_CREDENTIAL_INVALID_TEMPLATE_ID", "")
    assert Settings().tencent_ses_verification_code_template_id == 0
    assert Settings().tencent_ses_credential_invalid_template_id == 0


SUGGESTIONS = {
    "MAIL_FROM", "TENCENT_SES_SECRET_ID", "TENCENT_SES_SECRET_KEY",
    "TENCENT_SES_VERIFICATION_CODE_TEMPLATE_ID",
    "TENCENT_SES_CREDENTIAL_INVALID_TEMPLATE_ID",
    "CSU_DK_ALLOWED_EMAILS", "CSU_DK_COOKIE_SECURE",
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
