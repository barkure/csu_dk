"""静态资源的健壮性：JS 必须能被解析。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
OUR_SCRIPTS = ["geo.js", "login.js"]

# 内置 htmx 的版本（升级时同时改这里和 README）
HTMX_VERSION = "4.0.0"


def test_vendored_htmx_version_is_pinned():
    found = re.search(r'version="([0-9.]+)"', (STATIC / "htmx.min.js").read_text())
    assert found, "内置 htmx 里找不到 version 标记"
    assert found.group(1) == HTMX_VERSION, f"内置 htmx 是 {found.group(1)}，这里钉的是 {HTMX_VERSION}"


def test_script_htmx_event_names_exist_in_vendored_htmx():
    """脚本里引用的 htmx 事件名必须在内置 htmx 里存在：写错不报错，只是静默不触发。"""
    vendor = (STATIC / "htmx.min.js").read_text()
    used = set()
    for name in OUR_SCRIPTS:
        used |= set(re.findall(r"['\"](htmx:[A-Za-z:]+)['\"]", (STATIC / name).read_text()))
    missing = sorted(e for e in used if f'"{e}"' not in vendor and f"'{e}'" not in vendor)
    assert not missing, f"这些 htmx 事件名在内置 htmx 里不存在（版本改名了？）：{missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 做语法检查")
@pytest.mark.parametrize("name", OUR_SCRIPTS)
def test_static_script_parses(name: str):
    result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
    assert result.returncode == 0, f"{name} 语法错误：{result.stderr}"


@pytest.mark.parametrize("name", [*OUR_SCRIPTS, "app.css"])
def test_comments_are_balanced(name: str):
    text = (STATIC / name).read_text()
    assert text.count("/*") == text.count("*/"), f"{name} 的块注释不配平（多半是被删注释的脚本删坏了）"


def test_templates_reference_existing_assets():
    templates = Path(__file__).resolve().parent.parent / "app" / "templates"
    referenced = set()
    for path in templates.rglob("*.html"):
        for chunk in path.read_text().split('src="')[1:]:
            if chunk.startswith("/static/"):
                referenced.add(chunk.split('"')[0].split("/")[-1])
    for asset in referenced:
        assert (STATIC / asset).exists(), f"模板引用了不存在的静态文件：{asset}"


def test_css_id_selectors_exist_in_templates():
    """CSS 里按 id 定位的元素必须在模板里真的存在。"""
    css = (STATIC / "app.css").read_text()
    ids = {
        match for match in __import__("re").findall(r"#([a-zA-Z][\w-]*)", css)
        if not __import__("re").fullmatch(r"[0-9a-fA-F]{3,8}", match)  # 排掉 #fff / #c62828 这类颜色
    }
    templates = Path(__file__).resolve().parent.parent / "app" / "templates"
    html = "\n".join(path.read_text() for path in templates.rglob("*.html"))
    missing = sorted(item for item in ids if f'id="{item}"' not in html)
    assert not missing, f"CSS 里有用到但在模板中不存在的 id：{missing}"


def test_mobile_records_drop_the_year_only():
    """移动端不显示年份：窄屏仍是三列列表，只是时间省掉年份。"""
    css = (STATIC / "app.css").read_text()
    wide, mobile = css.split("@media (max-width: 720px)")

    # 默认规则必须在媒体查询之前：同权重下写后面会盖掉窄屏的 display: inline
    assert ".ts-short { display: none; }" in wide, "默认藏起短格式，且要写在媒体查询之前"
    assert "#records th, #records td { white-space: normal; }" in mobile, "保持原来的列表样式"
    assert "#records td:nth-child(1) { white-space: normal; width: 1%; }" in mobile, \
        "时间要收缩成两行（月-日 / 时:分），给说明列腾地方"
    assert ".ts-full { display: none; }" in mobile
    assert ".ts-short { display: inline; }" in mobile
