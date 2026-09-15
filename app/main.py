"""FastAPI 应用。"""
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
from .scheduler import start_scheduler, stop_scheduler
from .startup import run_startup_checks


class RequestCodeBody(BaseModel):
    email: str = ""


class VerifyBody(BaseModel):
    email: str = ""
    code: str = ""


class AccountBody(BaseModel):
    csuUsername: str = ""
    password: str | None = None
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
        "needsReauth": bool(account["auth_error"]),
        "dkdz": account["dkdz"] or "",
        "online": has_fresh_login(account),
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


def _json_error(request: Request, message: str, status: int, *, code: str = "",
                headers: dict | None = None) -> JSONResponse:
    """返回 JSON 错误，并阻止 htmx 替换页面。"""
    extra = dict(headers or {})
    if request.headers.get("hx-request"):
        extra["HX-Reswap"] = "none"
    payload = {"error": message} | ({"code": code} if code else {})
    return JSONResponse(payload, status_code=status, headers=extra or None)


@app.exception_handler(AppError)
async def _app_error(request: Request, exc: AppError) -> JSONResponse:
    extra = {"Retry-After": str(exc.retry_after_sec)} if isinstance(exc, RateLimitError) else None
    return _json_error(request, friendly_message(exc), exc.status, code=exc.code, headers=extra)


@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, _exc: RequestValidationError) -> JSONResponse:
    return _json_error(request, "请求参数不正确", 400, code="bad_request")


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    print(f"[error] {exc!r}", flush=True)
    return _json_error(request, "服务端内部错误", 500)


@app.middleware("http")
async def _guards(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return _json_error(request, "请求体过大（上限 64KB）", 413, code="payload_too_large")

    response = await call_next(request)
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["cache-control"] = "no-cache"
    return response


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "time": _now_iso(),
        "mail": mailer_enabled(),
        "checkinWindow": {"start": cfg.config.checkin_window_start, "end": cfg.config.checkin_window_end},
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
                        **auth.session_cookie_options(request))
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
    result = accounts_service.create_or_update(user, fields, ip=netinfo.client_ip(request))
    account_id = result["account_id"]
    out: dict = {"account": _public_account(_must_account(user["id"], account_id)), "verify": result["verify"]}
    if fields.get("runNow"):
        out["run"] = run_checkin(_must_account(user["id"], account_id), Trigger.MANUAL)
        out["account"] = _public_account(_must_account(user["id"], account_id))
    return out


@app.patch("/api/accounts/{account_id}")
def patch_account(request: Request, account_id: int, payload: AccountBody | None = None) -> dict:
    user = _current_user(request)
    accounts_service.update(user, account_id, payload.model_dump() if payload else {},
                            ip=netinfo.client_ip(request))
    return {"account": _public_account(_must_account(user["id"], account_id))}


@app.delete("/api/accounts/{account_id}")
def delete_account(request: Request, account_id: int) -> dict:
    user = _current_user(request)
    if not accounts_service.delete(user, account_id):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/run")
def run_now(request: Request, account_id: int) -> dict:
    user = _current_user(request)
    account = _must_account(user["id"], account_id)
    result = run_checkin(account, Trigger.MANUAL)
    return {"result": result, "account": _public_account(_must_account(user["id"], account_id))}


@app.post("/api/accounts/{account_id}/relogin")
def do_relogin(request: Request, account_id: int) -> dict:
    user = _current_user(request)
    account = _must_account(user["id"], account_id)

    result = relogin(account, ip=netinfo.client_ip(request))
    return {"result": result, "account": _public_account(_must_account(user["id"], account_id))}


@app.get("/api/accounts/{account_id}/records")
def records(request: Request, account_id: int, limit: int = 30) -> dict:
    user = _current_user(request)
    account = _must_account(user["id"], account_id)
    return {"records": db.list_records(account["id"], min(max(limit, 1), 200))}
