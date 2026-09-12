"""静态资源的健壮性：JS 必须能被解析（曾经有一版脚本把块注释首行删了，页面按钮全失效）。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
OUR_SCRIPTS = ["geo.js", "login.js"]


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
    """CSS 里按 id 定位的元素（如右上角的叉）必须在模板里真的存在 —— 移植时最容易丢的就是 id。"""
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
    """窄屏的历史记录仍是三列列表，只是时间省掉年份。

    之前等宽时间在 390px 下会被折成四行、年份折散；与其改排法，不如窄屏就不显示年份。
    """
    css = (STATIC / "app.css").read_text()
    wide, mobile = css.split("@media (max-width: 720px)")

    # 默认规则必须在媒体查询之前：同权重下写后面会盖掉窄屏的 display: inline
    assert ".ts-short { display: none; }" in wide, "默认藏起短格式，且要写在媒体查询之前"
    assert "#records th, #records td { white-space: normal; }" in mobile, "保持原来的列表样式"
    assert "#records td:nth-child(1) { white-space: normal; width: 1%; }" in mobile, \
        "时间要收缩成两行（月-日 / 时:分），给说明列腾地方"
    assert ".ts-full { display: none; }" in mobile
    assert ".ts-short { display: inline; }" in mobile
