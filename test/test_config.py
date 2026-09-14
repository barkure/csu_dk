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
    "MAIL_FROM": "Changed <changed@example.com>",
    "TENCENT_SES_TEMPLATE_ID": "123456",
}


def mutated(name: str, value: str) -> str:
    if name in SPECIAL:
        return SPECIAL[name]
    if value.isdigit():
        return str(int(value) + 1)
    return f"{value}x"


@pytest.mark.parametrize("name", sorted(documented()))
def test_documented_variable_is_actually_read(name: str, monkeypatch):
    baseline = Settings()
    monkeypatch.setenv(name, mutated(name, documented()[name]))
    assert Settings() != baseline, f"{name} 改了但配置没变 —— 这个变量没有被读到"


def test_data_dir_defaults_into_project(monkeypatch):
    monkeypatch.delenv("CSU_DK_TEST_DATA_DIR", raising=False)
    assert Settings().data_dir == PROJECT_ROOT / "data"
    assert "CSU_DK_HOME" not in EXAMPLE.read_text()
    assert "CSU_DK_TEST_DATA_DIR" not in EXAMPLE.read_text()   # 仅供测试，不写进部署文档


def test_blank_template_id_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("TENCENT_SES_TEMPLATE_ID", "")
    assert Settings().tencent_ses_template_id == 0
SUGGESTIONS = {
    "MAIL_FROM", "TENCENT_SES_SECRET_ID", "TENCENT_SES_SECRET_KEY", "TENCENT_SES_TEMPLATE_ID",
    "CSU_DK_ALLOWED_EMAILS",
}


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
        if str(default) != value:
            mismatches.append(f"{var}: 示例 {value!r} ≠ 代码默认 {default!r}")
    assert not mismatches, "示例与代码默认值不一致：\n  " + "\n  ".join(mismatches)
