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
    # 只看未注释的赋值行
    return dict(re.findall(r"^([A-Z_]+)=(.*)$", EXAMPLE.read_text(), re.M))


SPECIAL = {
    "CSU_DK_TZ": "UTC",                          # 必须是合法时区
    "CSU_DK_ALLOWED_EMAILS": "someone@example.com",  # 必须是合法邮箱
    "CSU_DK_HOST": "0.0.0.0",
    "MAIL_FROM": "Changed <changed@example.com>",
    # 模板 ID 在 .env.example 里故意留空（表示必须自己填），测试自己给一个数字
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
    """数据目录默认在项目内，不对外暴露"挪走它"的选项（测试环境里那个覆盖是给用例用的）。"""
    monkeypatch.delenv("CSU_DK_TEST_DATA_DIR", raising=False)
    assert Settings().data_dir == PROJECT_ROOT / "data"
    assert "CSU_DK_HOME" not in EXAMPLE.read_text()
    assert "CSU_DK_TEST_DATA_DIR" not in EXAMPLE.read_text()   # 仅供测试，不写进部署文档


def test_blank_template_id_is_treated_as_unset(monkeypatch):
    """模板 ID 留空（模板还没审核通过）时按“没配”处理，不能让配置加载失败。"""
    monkeypatch.setenv("TENCENT_SES_TEMPLATE_ID", "")
    assert Settings().tencent_ses_template_id == 0


# .env.example 里写的是"建议填的值"，代码默认是空，天然对不上
SUGGESTIONS = {
    "MAIL_FROM", "TENCENT_SES_SECRET_ID", "TENCENT_SES_SECRET_KEY", "TENCENT_SES_TEMPLATE_ID",
    "CSU_DK_ALLOWED_EMAILS",
}


def test_documented_values_match_code_defaults():
    """示例里写的默认值必须和代码默认值一致。

    这类漂移不会被任何测试抓住：照抄示例的人拿到的是另一套策略（校验限流曾出现示例
    1800 秒 15 次、代码 600 秒 30 次），而"变量能被读到"的测试照样全绿。
    """
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
