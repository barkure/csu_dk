"""检查运行中的服务；提供校园账号时会实际登录并可能打卡。

用法：
    CSU_USERNAME=学号 CSU_PASSWORD=密码 uv run python tools/e2e.py [baseUrl]

脚本会在当前数据目录创建并清理测试用户；测试邮箱可通过 CSU_DK_E2E_EMAIL 指定。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, db
from app import config as cfg
from app.clock import local_now, to_local_iso

BASE = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{cfg.config.port}"
EMAIL = os.environ.get("CSU_DK_E2E_EMAIL", "e2e-test@example.com")
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


def inject_login_code(email: str, code: str) -> int:
    now = local_now(cfg.config.tz)
    return db.insert_login_code(email, auth.hash_login_code(email, code),
                                to_local_iso(now + timedelta(minutes=cfg.config.code_minutes)), to_local_iso(now))


def main() -> int:
    status, health = call("/api/health")
    healthy = status == 200 and isinstance(health, dict) and health.get("ok") is True
    ok("健康检查", healthy, str(health)[:80])
    if not healthy:
        return 1
    if db.find_user_by_email(EMAIL):
        print(f"❌ 测试邮箱 {EMAIL} 已有用户，请换一个邮箱；不会删除现有数据")
        return 1

    code = "424242"
    code_id = inject_login_code(EMAIL, code)
    try:
        return run_checks(code)
    finally:
        db.delete_login_code(code_id)
        db.delete_user(EMAIL)


def run_checks(code: str) -> int:
    status, body = call("/api/auth/verify", "POST", {"email": EMAIL, "code": code})
    logged_in = status == 200 and bool(cookie)
    ok("邮箱验证码登录", logged_in, str(body)[:80])
    if not logged_in:
        return 1

    status, body = call("/api/auth/verify", "POST", {"email": EMAIL, "code": "000000"})
    ok("错误验证码被拒", status == 400, str(body)[:80])
    if status != 400:
        if status == 429:
            print("验证码校验已被限流，请等限流窗口结束后再测")
        return 1

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
    ok("账号验证通过", body.get("verify") == {"ok": True})
    ok("立即打卡已执行", "run" in body, str(body.get("run", {}))[:120])

    status, body = call(f"/api/accounts/{account_id}", "PATCH", {"enabled": False})
    _, listed = call("/api/accounts")
    stopped = next(item for item in listed["accounts"] if item["id"] == account_id)
    ok("停用自动打卡", status == 200 and body["account"]["enabled"] is False
       and stopped["enabled"] is False)

    status, body = call(f"/api/accounts/{account_id}", "PATCH", {"enabled": True})
    ok("重新启用自动打卡", status == 200 and body["account"]["enabled"] is True)

    status, body = call(f"/api/accounts/{account_id}/records?limit=5")
    ok("执行记录可查询", status == 200 and isinstance(body.get("records"), list),
       str(body.get("records", [])[:1])[:120])

    status, _ = call(f"/api/accounts/{account_id}", "DELETE")
    ok("删除账号", status == 200)

    call("/api/auth/logout", "POST")
    status, _ = call("/api/accounts")
    ok("登出后会话失效", status == 401)

    print(f"\n{'❌ ' + str(failures) + ' 项失败' if failures else '✅ 全部通过'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
