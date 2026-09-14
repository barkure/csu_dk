"""网页界面：Jinja2 模板 + htmx 片段。走的是和浏览器一样的表单流程。"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app import config as cfg
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret
from app.main import app


@pytest.fixture()
def client():
    for limiter in auth.limiters.values():
        limiter.reset()
    return TestClient(app)


def dev_code_from(html: str) -> str:
    match = re.search(r"验证码：(\d{6})", html)
    assert match, html
    return match.group(1)


def ui_login(client: TestClient, email: str) -> None:
    fragment = client.post("/ui/code", data={"email": email})
    assert fragment.status_code == 200
    response = client.post("/ui/login", data={"email": email, "code": dev_code_from(fragment.text)})
    assert response.status_code == 204, response.text
    assert response.headers["hx-redirect"] == "/dashboard"


def make_account(user_id: int, username: str, **overrides) -> dict:
    now = to_local_iso(local_now(cfg.config.tz))
    row = {
        "user_id": user_id, "csu_username": username, "password_enc": encrypt_secret("whatever"),
        "enabled": 1, "window_start": "20:00", "window_end": "22:30",
        "jd": 112.936833, "wd": 28.157238, "dkdz": "升华8栋", "created_at": now, "updated_at": now,
    }
    row.update(overrides)
    return db.insert_account(row)


def test_login_page_has_disabled_code_field(client):
    html = client.get("/").text
    assert "妙妙道具" in html
    assert "disabled" in html  # 还没获取验证码，输入框不可用


def test_code_fragment_rejects_bad_email(client):
    html = client.post("/ui/code", data={"email": "nope"}).text
    assert "邮箱格式不正确" in html


def test_code_fragment_echoes_dev_code(client):
    html = client.post("/ui/code", data={"email": "ui-a@example.com"}).text
    assert "本地调试模式，验证码：" in html
    assert dev_code_from(html)


def test_code_fragment_starts_cooldown_countdown(client):
    html = client.post("/ui/code", data={"email": "ui-cool@example.com"}).text
    assert f'data-cooldown="{cfg.config.code_cooldown_seconds}"' in html
    assert "login.js" not in html  # 脚本由登录页引入，片段里不该重复

    # 冷却期内再点：服务端要把真实剩余秒数顶回去，按钮才会继续倒计时
    again = client.post("/ui/code", data={"email": "ui-cool@example.com"}).text
    assert "请求过于频繁" in again
    cooldown = int(re.search(r'id="send-code"[^>]*data-cooldown="(\d+)"', again).group(1))
    assert 0 < cooldown <= cfg.config.code_cooldown_seconds


def test_message_lives_inside_the_swapped_panel(client):
    """消息必须在被替换的容器里，否则旧消息清不掉、会越堆越多。"""
    from bs4 import BeautifulSoup

    html = client.post("/ui/code", data={"email": "ui-panel@example.com"}).text
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one("#login-panel")
    assert panel is not None, "片段根节点应该是 #login-panel"
    assert panel.select_one("form#login-form") is not None
    assert panel.select_one("p.msg") is not None, "消息得在容器内部，否则替换时清不掉"
    assert len(soup.select("p.msg")) == 1, "一个片段只能有一条消息"
    assert 'hx-target="#login-panel"' in html, "换的是整个容器，不是表单"
    assert 'hx-target="#login-form"' not in html


def test_login_flow_and_dashboard(client):
    ui_login(client, "ui-b@example.com")
    html = client.get("/dashboard").text
    assert "ui-b@example.com" in html
    assert "账号列表" in html
    assert "还没有打卡账号" in html


def test_logout_clears_session(client):
    ui_login(client, "ui-c@example.com")
    assert client.post("/ui/logout").status_code == 204
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_session_cookie_secure_follows_the_request_scheme(client):
    """直连 TLS（不经过反代）也算 HTTPS，否则 Cookie 会少一个 Secure。"""
    def set_cookie(base_url: str, email: str) -> str:
        fresh = TestClient(app, base_url=base_url)
        code = dev_code_from(fresh.post("/ui/code", data={"email": email}).text)
        return fresh.post("/ui/login", data={"email": email, "code": code}).headers["set-cookie"]

    assert "Secure" not in set_cookie("http://example.com", "ui-http@example.com")
    assert "Secure" in set_cookie("https://example.com", "ui-tls@example.com")


def test_htmx_error_body_is_not_swapped(client):
    """htmx 也会 swap 4xx/5xx，出错响应得声明“这不是可替换的内容”，否则 JSON 会顶掉卡片。"""
    path = "/ui/accounts/abc/records"          # account_id 不是整数 → 400
    assert client.get(path, headers={"hx-request": "true"}).headers["hx-reswap"] == "none"
    assert "hx-reswap" not in client.get(path).headers


def test_fragments_require_session(client):
    # htmx 请求 → HX-Redirect（否则它会把登录页塞进片段里）
    for path in ["/ui/accounts", "/ui/form"]:
        response = client.get(path, headers={"hx-request": "true"})
        assert response.status_code == 204, path
        assert response.headers["hx-redirect"] == "/"

    # 普通请求 → 303 回登录页（不跟随跳转，否则会拿到登录页的 200）
    response = client.get("/ui/accounts", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_accounts_fragment_lists_rows_with_manage_panel(client):
    ui_login(client, "ui-d@example.com")
    user = db.find_user_by_email("ui-d@example.com")
    account = make_account(user["id"], "977000001")

    html = client.get(f"/ui/accounts?open={account['id']}").text
    assert "977000001" in html
    assert "升华8栋" in html          # 地址来自学校返回的楼栋名
    assert "立即打卡" in html          # 展开的管理面板
    assert "收起" in html

    collapsed = client.get("/ui/accounts").text
    assert "立即打卡" not in collapsed


def test_add_account_form_rejects_bad_coords(client):
    ui_login(client, "ui-e@example.com")
    html = client.post("/ui/accounts", data={"csuUsername": "977000002", "password": "x", "coords": "abc"}).text
    assert "经纬度" in html


def test_add_account_form_rejects_missing_password(client):
    ui_login(client, "ui-f@example.com")
    html = client.post("/ui/accounts", data={"csuUsername": "977000003", "coords": "112.9,28.1"}).text
    assert "密码" in html


def test_toggle_flips_enabled_and_clears_schedule(client):
    ui_login(client, "ui-g@example.com")
    user = db.find_user_by_email("ui-g@example.com")
    account = make_account(user["id"], "977000004")
    from app.scheduler import schedule_next

    schedule_next(db.get_account_by_id(account["id"]))

    client.post(f"/ui/accounts/{account['id']}/toggle")
    assert db.get_account_by_id(account["id"])["enabled"] == 0
    assert db.get_account_by_id(account["id"])["next_run_at"] is None

    client.post(f"/ui/accounts/{account['id']}/toggle")
    assert db.get_account_by_id(account["id"])["enabled"] == 1
    assert db.get_account_by_id(account["id"])["next_run_at"]


def test_edit_form_prefills_account(client):
    ui_login(client, "ui-h@example.com")
    user = db.find_user_by_email("ui-h@example.com")
    account = make_account(user["id"], "977000005")

    html = client.get(f"/ui/form?edit={account['id']}").text
    assert "编辑打卡账号" in html
    assert "977000005" in html
    assert "112.936833,28.157238" in html
    assert "取消编辑" in html


def test_records_fragment_shows_history(client):
    ui_login(client, "ui-i@example.com")
    user = db.find_user_by_email("ui-i@example.com")
    account = make_account(user["id"], "977000006")
    db.add_record(account["id"], to_local_iso(local_now(cfg.config.tz)), "schedule", "waiting",
                  "当前不可打卡：未到打卡时间")

    html = client.get(f"/ui/accounts/{account['id']}/records").text
    assert "977000006" in html
    assert "未到时间" in html
    assert "当前不可打卡" in html

    closed = client.get("/ui/records/close").text
    assert "hidden" in closed


def test_delete_removes_account(client):
    ui_login(client, "ui-j@example.com")
    user = db.find_user_by_email("ui-j@example.com")
    account = make_account(user["id"], "977000007")

    client.post(f"/ui/accounts/{account['id']}/delete")
    assert db.get_account_by_id(account["id"]) is None


def test_manage_button_toggles_between_expand_and_collapse(client):
    """展开与收起必须是两个不同的 URL，否则收不起来。"""
    ui_login(client, "ui-toggle@example.com")
    user = db.find_user_by_email("ui-toggle@example.com")
    account = make_account(user["id"], "977100001")

    import re

    collapsed = client.get("/ui/accounts").text
    assert f'hx-get="/ui/accounts?open={account["id"]}"' in collapsed
    assert re.search(r">\s*管理\s*<", collapsed)

    expanded = client.get(f"/ui/accounts?open={account['id']}").text
    assert 'hx-get="/ui/accounts?open="' in expanded   # 收起 → 请求不带 open
    assert re.search(r">\s*收起\s*<", expanded)


def test_address_has_its_own_column(client):
    """楼栋名单独一列：坐标列里只剩坐标。"""
    ui_login(client, "ui-cols@example.com")
    user = db.find_user_by_email("ui-cols@example.com")
    account = make_account(user["id"], "977500001")

    account = db.list_accounts(user["id"])[0]
    html = client.get(f"/ui/accounts?open={account['id']}").text   # 管理面板展开后才会有这两个按钮
    header = re.search(r"<thead>(.*?)</thead>", html, re.DOTALL).group(1)
    assert header.count("<th>") == 8                      # 学号/状态/自动打卡/下次执行时刻/今日结果/坐标/楼栋/管理
    assert "<th>楼栋</th>" in header
    assert header.index("<th>坐标</th>") < header.index("<th>楼栋</th>")

    row = next(r for r in re.findall(r'<tr class="account[^"]*">(.*?)</tr>', html, re.DOTALL)
               if account["csu_username"] in r)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
    assert cells[5].strip().startswith("112.936833,28.157238")   # 坐标列
    assert "升华8栋" not in cells[5]                              # 不再挤在坐标列里
    assert cells[6].strip() == "升华8栋"                          # 楼栋列

    # 没有楼栋名时那一列显示占位符，而不是把坐标挤过去
    empty = make_account(user["id"], "977500002", dkdz="")
    empty_html = client.get(f"/ui/accounts?open={empty['id']}").text
    empty_row = next(row for row in re.findall(r'<tr class="account[^"]*">(.*?)</tr>', empty_html, re.DOTALL)
                     if empty["csu_username"] in row)
    empty_cells = re.findall(r"<td[^>]*>(.*?)</td>", empty_row, re.DOTALL)
    assert empty_cells[5].strip().startswith("112.936833")
    assert empty_cells[6].strip() == "—"


def test_expanded_row_gets_is_open_class(client):
    """展开状态由服务端渲染（is-open 类），窄屏靠它把粗线挪到面板下方。"""
    ui_login(client, "ui-open@example.com")
    user = db.find_user_by_email("ui-open@example.com")
    account = make_account(user["id"], "977600001")

    collapsed = client.get("/ui/accounts").text
    assert '<tr class="account">' in collapsed
    assert "account is-open" not in collapsed

    expanded = client.get(f"/ui/accounts?open={account['id']}").text
    assert expanded.count('class="account is-open"') == 1
    assert expanded.index("account is-open") < expanded.index("actions-row")


def test_edit_and_records_jump_to_their_cards(client):
    """手机上一屏放不下：点「编辑」要滚到编辑区，点「历史记录」要滚到记录区。"""
    ui_login(client, "ui-jump@example.com")
    user = db.find_user_by_email("ui-jump@example.com")
    make_account(user["id"], "255000123")

    account = db.list_accounts(user["id"])[0]
    html = client.get(f"/ui/accounts?open={account['id']}").text   # 管理面板展开后才会有这两个按钮
    # 页顶用 body 而不是 window：窗口对象没有 scrollIntoView，showTarget:window 会静默不滚。
    assert 'hx-swap="outerHTML show:top showTarget:body"' in html
    assert 'hx-swap="outerHTML show:top showTarget:#records-card"' in html
    assert "show:window:top" not in html and "show:#records-card:top" not in html


def test_record_row_carries_both_timestamp_forms(client):
    """一条记录同时带「带年份」和「不带年份」两种时间，宽窄屏各显示一个。"""
    ui_login(client, "ui-ts@example.com")
    user = db.find_user_by_email("ui-ts@example.com")
    account = make_account(user["id"], "255000456")
    db.add_record(account["id"], "2026-09-12T20:18:47", "schedule", "success", "打卡成功", "2026-09-12 20:18:47")

    html = client.get(f"/ui/accounts/{account['id']}/records").text
    assert '<span class="ts-full">2026-09-12 20:18</span>' in html   # 宽屏：带年份
    assert '<span class="ts-short">09-12 20:18</span>' in html       # 窄屏：省掉年份


def test_coords_hint_icon_and_link(client):
    """经纬度右侧那个 ⓘ：默认收起，里面给出高德的坐标拾取器地址。"""
    ui_login(client, "ui-hint@example.com")
    html = client.get("/ui/form").text

    assert 'id="coords-hint-toggle"' in html
    assert 'aria-controls="coords-hint"' in html
    assert 'class="hint hidden" id="coords-hint"' in html, "默认要收起，点了才展开"
    assert "https://lbs.amap.com/tools/picker" in html
    assert "高德坐标拾取器" in html


def test_coords_buttons_are_kept_out_of_the_label(client):
    """经纬度那两个按钮不能放进 <label>。

    Chrome 会把 label 内任意位置的 hover 一起算到这个 label 下面的所有控件上，
    于是"点右侧的 ⓘ"会让旁边的「点击使用当前位置」也进 hover 态（用户实测到的问题）。
    标签改用 for= 关联输入框，点文字照样能聚焦。
    """
    from bs4 import BeautifulSoup

    ui_login(client, "ui-label@example.com")
    soup = BeautifulSoup(client.get("/ui/form").text, "html.parser")

    for button_id in ("pick-here", "coords-hint-toggle"):
        button = soup.select_one(f"#{button_id}")
        assert button is not None, f"表单里应当有 {button_id}"
        assert button.find_parent("label") is None, f"{button_id} 不能放在 <label> 里"

    label = soup.select_one(".label-text > label")
    assert label is not None and label.get("for") == "coords", "标签要用 for= 关联输入框"
    assert soup.select_one("#coords") is not None
