"""FastAPI 应用：JSON 接口（给测试与程序化调用）+ 网页界面（Jinja2 + htmx）。"""
from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import accounts as accounts_service
from . import auth, db, netinfo, ui
from . import config as cfg
from .checkin import has_fresh_login, relogin, run_checkin
from .clock import local_now, to_local_iso
from .domain import Trigger
from .errors import AppError, RateLimitError, friendly_message
from .mailer import mailer_enabled
from .scheduler import (
    schedule_next,
    schedule_next_after,
    start_scheduler,
    stop_scheduler,
)
from .startup import run_startup_checks


# 请求体上限：只挡带 Content-Length 的请求；不带该头的分块请求拿不到提前拒绝，
# 真正对外时由前置反向代理限制请求体大小
class RequestCodeBody(BaseModel):
    email: str = ""


class VerifyBody(BaseModel):
    email: str = ""
    code: str = ""


class AccountBody(BaseModel):
    """新增/编辑账号的请求体（网页表单走另一条路径，这里只服务 JSON 接口）。"""

    csuUsername: str = ""
    password: str | None = None
    jd: float | None = None
    wd: float | None = None
    windowStart: str | None = None
    windowEnd: str | None = None
    enabled: bool | None = None
    runNow: bool = False


MAX_BODY_BYTES = 64 * 1024
APP_DIR = pathlib.Path(__file__).resolve().parent


class UnauthorizedError(AppError):
    status = 401
    expose = True
    code = "unauthorized"

    def __init__(self, message: str = "未登录"):
        super().__init__(message)


def _now_iso() -> str:
    return to_local_iso(local_now(cfg.config.tz))


def _current_user(request: Request) -> dict:
    user = auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE))
    if not user:
        raise UnauthorizedError()
    return user


def _public_account(account: dict) -> dict:
    return {
        "id": account["id"],
        "csuUsername": account["csu_username"],
        "enabled": bool(account["enabled"]),
        "needsReauth": bool(account["needs_reauth"]),
        "windowStart": account["window_start"],
        "windowEnd": account["window_end"],
        "jd": account["jd"],
        "wd": account["wd"],
        "dkdz": account["dkdz"] or "",
        "online": has_fresh_login(account),
        "nextRunAt": account["next_run_at"],
        "lastRunAt": account["last_run_at"],
        "lastStatus": account["last_status"],
        "lastMessage": account["last_message"],
    }


def _must_account(user_id: int, account_id: int) -> dict:
    account = db.get_account(user_id, account_id)
    if not account:
        raise AppError("账号不存在", status=404, expose=True)
    return account


@asynccontextmanager
async def lifespan(_app: FastAPI):
    report = run_startup_checks()
    print(f"[startup] 打卡账号 {len(db.all_accounts_raw())} 个"
          + (f"，待处理 {len(report['flagged'])} 个" if report["flagged"] else ""), flush=True)
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.include_router(ui.router)


@app.exception_handler(AppError)
async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after_sec)} if isinstance(exc, RateLimitError) else None
    return JSONResponse({"error": friendly_message(exc), "code": exc.code}, status_code=exc.status, headers=headers)


@app.exception_handler(RequestValidationError)
async def _bad_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"error": "请求参数不正确", "code": "bad_request"}, status_code=400)


@app.exception_handler(Exception)
async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
    print(f"[error] {exc!r}", flush=True)
    return JSONResponse({"error": "服务端内部错误"}, status_code=500)


@app.middleware("http")
async def _guards(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse({"error": "请求体过大（上限 64KB）", "code": "payload_too_large"}, status_code=413)

    response = await call_next(request)
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        # 页面与静态资源都不缓存：状态变了刷新即生效，不用人工维护 ?v=N
        response.headers["cache-control"] = "no-cache"
    return response


# ---------- JSON 接口 ----------

@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "time": _now_iso(),
        "mail": mailer_enabled(),
        "defaults": {"windowStart": cfg.config.default_window_start, "windowEnd": cfg.config.default_window_end},
    }


@app.post("/api/auth/request-code")
def request_code(request: Request, payload: RequestCodeBody | None = None) -> dict:
    result = auth.request_login_code((payload.email if payload else ""), netinfo.client_ip(request))
    response: dict = {"ok": True, "sent": result.get("sent", False)}
    if not result.get("sent") and result.get("dev_code") and netinfo.is_local_request(request):
        response["devCode"] = result["dev_code"]
    return response


@app.post("/api/auth/verify")
def verify_code(request: Request, response: Response, payload: VerifyBody | None = None) -> dict:
    result = auth.verify_login_code(payload.email if payload else "",
                                    payload.code if payload else "",
                                    netinfo.client_ip(request),
                                    request.headers.get("user-agent", ""))
    if not result.ok:
        return JSONResponse({"error": result.reason}, status_code=400)

    response.set_cookie(auth.SESSION_COOKIE, result.token,
                        **auth.session_cookie_options(request.headers.get("x-forwarded-proto")))
    return {"ok": True, "user": {"email": result.user["email"]}}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    user = _current_user(request)
    return {"user": {"email": user["email"]}, "mailEnabled": mailer_enabled()}


@app.get("/api/accounts")
def list_accounts(request: Request) -> dict:
    user = _current_user(request)
    return {"accounts": [_public_account(account) for account in db.list_accounts(user["id"])]}


@app.post("/api/accounts")
def create_account(request: Request, payload: AccountBody | None = None) -> dict:
    fields = payload.model_dump() if payload else {}
    user = _current_user(request)
    result = accounts_service.create_or_update(user, fields)
    account_id = result["account_id"]
    schedule_next(_must_account(user["id"], account_id))
    out: dict = {"account": _public_account(_must_account(user["id"], account_id)), "verify": result["verify"]}
    if fields.get("runNow"):
        out["run"] = run_checkin(_must_account(user["id"], account_id), Trigger.MANUAL)
        schedule_next_after(_must_account(user["id"], account_id), out["run"])
        out["account"] = _public_account(_must_account(user["id"], account_id))
    return out


@app.patch("/api/accounts/{account_id}")
def patch_account(request: Request, account_id: int, payload: AccountBody | None = None) -> dict:
    user = _current_user(request)
    accounts_service.update(user, account_id, payload.model_dump() if payload else {})
    schedule_next(_must_account(user["id"], account_id))
    return {"account": _public_account(_must_account(user["id"], account_id))}


@app.delete("/api/accounts/{account_id}")
def delete_account(request: Request, account_id: int) -> dict:
    user = _current_user(request)
    if not db.delete_account(user["id"], account_id):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/run")
def run_now(request: Request, account_id: int) -> dict:
    user = _current_user(request)
    account = _must_account(user["id"], account_id)
    result = run_checkin(account, Trigger.MANUAL)
    schedule_next_after(_must_account(user["id"], account_id), result)
    return {"result": result, "account": _public_account(_must_account(user["id"], account_id))}


@app.post("/api/accounts/{account_id}/relogin")
def do_relogin(request: Request, account_id: int) -> dict:
    user = _current_user(request)
    account = _must_account(user["id"], account_id)

    result = relogin(account)
    if result["ok"]:
        schedule_next(_must_account(user["id"], account_id))
    return {"result": result, "account": _public_account(_must_account(user["id"], account_id))}


@app.get("/api/accounts/{account_id}/records")
def records(request: Request, account_id: int, limit: int = 30) -> dict:
    user = _current_user(request)
    account = _must_account(user["id"], account_id)
    return {"records": db.list_records(account["id"], min(max(limit, 1), 200))}
