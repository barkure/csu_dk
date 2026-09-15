"""端到端检查：对**运行中**的服务跑一遍真实流程（会真的登录学校、可能真的打卡）。

用法：
    CSU_USERNAME=学号 CSU_PASSWORD=密码 uv run python test/e2e.py [baseUrl]

注意这里不是 pytest 用例：它要用项目里真实的数据目录往库里注入验证码，
而 pytest 有自己的临时数据目录（test/conftest.py）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, db
from app import config as cfg
from app.clock import local_now, to_local_iso

BASE = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{cfg.config.port}"
EMAIL = "e2e-test@example.com"
CSU_USER = os.environ.get("CSU_USERNAME")
CSU_PASS = os.environ.get("CSU_PASSWORD")

failures = 0
cookie: str | None = None


def ok(label: str, passed: bool, extra: str = "") -> None:
    global failures
    if not passed:
        failures += 1
    print(f"{'✅' if passed else '❌'} {label}{'  ' + extra if extra else ''}")


def call(path: str, method: str = "GET", body: dict | None = None):
    global cookie
    request = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json", **({"cookie": cookie} if cookie else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, text, headers = response.status, response.read().decode(), response.headers
    except urllib.error.HTTPError as error:
        status, text, headers = error.code, error.read().decode(), error.headers

    if cookie is None and headers.get("set-cookie"):
        cookie = headers["set-cookie"].split(";")[0]
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def inject_login_code(email: str, code: str) -> None:
    now = local_now(cfg.config.tz)
    db.insert_login_code(email, auth.hash_login_code(email, code), to_local_iso(now), to_local_iso(now))


def main() -> int:
    status, health = call("/api/health")
    ok("健康检查", status == 200 and health.get("ok") is True, json.dumps(health, ensure_ascii=False)[:80])

    code = "424242"
    inject_login_code(EMAIL, code)
    status, body = call("/api/auth/verify", "POST", {"email": EMAIL, "code": code})
    ok("邮箱验证码登录", status == 200 and bool(cookie), str(body)[:80])

    status, body = call("/api/auth/verify", "POST", {"email": EMAIL, "code": "000000"})
    ok("错误验证码被拒", status == 400, str(body)[:80])

    status, _ = call("/api/accounts")
    ok("会话有效", status == 200)

    if not CSU_USER or not CSU_PASS:
        print("\n⏭  未提供 CSU_USERNAME / CSU_PASSWORD，跳过打卡相关步骤")
        print(f"{'❌' if failures else '✅'} 已完成的部分：{failures} 项失败")
        return 1 if failures else 0

    status, body = call("/api/accounts", "POST", {
        "csuUsername": CSU_USER, "password": CSU_PASS, "runNow": True,
    })
    ok("添加账号（提交即验证）", status == 200, str(body)[:120])
    if status != 200:
        print(f"\n❌ 添加失败：{body}")
        return 1

    account_id = body["account"]["id"]
    ok("添加后即可用（有登录态）", body["account"].get("online") is True)
    ok("打卡窗口已自动获取", bool(body["account"].get("windowEnd")), json.dumps({
        "window": f"{body['account']['windowStart']}-{body['account']['windowEnd']}",
        "run": body.get("run", {}).get("message", ""),
    }, ensure_ascii=False))

    status, body = call(f"/api/accounts/{account_id}", "PATCH", {"windowEnd": "22:00"})
    ok("改窗口", status == 200 and body["account"]["windowEnd"] == "22:00", str(body)[:80])

    status, _ = call(f"/api/accounts/{account_id}", "PATCH", {"enabled": False})
    _, listed = call("/api/accounts")
    stopped = next(item for item in listed["accounts"] if item["id"] == account_id)
    ok("停用后清空排期", status == 200 and stopped["nextRunAt"] is None)

    status, _ = call(f"/api/accounts/{account_id}", "PATCH", {"enabled": True})
    ok("重新启用后重新排期", status == 200)

    status, body = call(f"/api/accounts/{account_id}/records?limit=5")
    ok("执行记录", status == 200 and len(body["records"]) > 0, str(body["records"][:1])[:120])

    status, _ = call(f"/api/accounts/{account_id}", "DELETE")
    ok("删除账号", status == 200)

    call("/api/auth/logout", "POST")
    status, _ = call("/api/accounts")
    ok("登出后会话失效", status == 401)

    db.delete_user(EMAIL)
    print(f"\n{'❌ ' + str(failures) + ' 项失败' if failures else '✅ 全部通过'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
