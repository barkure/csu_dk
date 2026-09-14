"""网页界面：Jinja2 模板 + htmx 局部替换。所有逻辑都走服务层，这里只负责渲染。"""
from __future__ import annotations

import pathlib

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import accounts as accounts_service
from . import auth, db, netinfo
from . import config as cfg
from .checkin import relogin, run_checkin
from .clock import local_now, to_local_iso
from .domain import CheckinStatus, Trigger
from .errors import AppError, RateLimitError
from .scheduler import schedule_next, schedule_next_after
from .validate import parse_coords_input
from .views import STATUS_CLASS, account_view, fmt, fmt_full, status_label

templates = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))
templates.env.globals.update(
    status_class=lambda status: STATUS_CLASS.get(status or "", ""),
    status_label=status_label,
    fmt=fmt,
    fmt_full=fmt_full,
)

router = APIRouter()


def _user(request: Request) -> dict | None:
    return auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE))


def _to_login(request: Request) -> Response:
    """htmx 请求要 HX-Redirect，普通请求才 303。"""
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": "/"})
    return RedirectResponse("/", status_code=303)


def _context(user: dict, **extra) -> dict:
    context = {
        "email": user["email"],
        "rows": [account_view(account) for account in db.list_accounts(user["id"])],
        "open_id": None,
        "editing": None,
        "messages": {},
        "msg": "",
        "msg_kind": "",
        "account": None,
        "records": [],
    }
    context.update(extra)
    return context


def _render(request: Request, template: str, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context)


def _oob(html: str) -> str:
    """把片段标记成 htmx 的带外替换（按元素 id 匹配）。"""
    return html.replace(' id="accounts-card"', ' id="accounts-card" hx-swap-oob="true"', 1)


# ---------- 页面 ----------

@router.get("/", response_class=HTMLResponse)
def page_login(request: Request):
    if _user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _render(request, "login.html",
                   {"sent": False, "msg": "", "msg_kind": "", "email": "", "code": "", "cooldown": 0})


@router.get("/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    return _render(request, "dashboard.html", _context(user))


# ---------- 登录 ----------

@router.post("/ui/code", response_class=HTMLResponse)
def ui_code(request: Request, email: str = Form("")):
    context = {"sent": False, "msg": "", "msg_kind": "", "email": email, "code": "", "cooldown": 0}
    try:
        result = auth.request_login_code(email, netinfo.client_ip(request))
    except RateLimitError as error:
        # 带上剩余秒数让按钮继续倒计时；否则冷却期内按钮可反复点，消息会越堆越多
        return _render(request, "partials/login_form.html",
                       {**context, "sent": True, "msg": error.message, "msg_kind": "err",
                        "cooldown": error.retry_after_sec})
    except AppError as error:
        return _render(request, "partials/login_form.html", {**context, "msg": error.message, "msg_kind": "err"})

    context["sent"] = True
    context["cooldown"] = cfg.config.code_cooldown_seconds
    if result.get("sent"):
        context["msg"] = "验证码已发送，请查收邮件"
    else:
        # 未配 Resend 的本地调试模式：直接把验证码填上
        context.update(msg=f"本地调试模式，验证码：{result.get('dev_code', '')}", code=result.get("dev_code", ""))
    return _render(request, "partials/login_form.html", context)


@router.post("/ui/login")
def ui_login(request: Request, email: str = Form(""), code: str = Form("")):
    context = {"sent": True, "msg": "", "msg_kind": "err", "email": email, "code": ""}
    try:
        result = auth.verify_login_code(email, code, netinfo.client_ip(request),
                                        request.headers.get("user-agent", ""))
    except AppError as error:
        return _render(request, "partials/login_form.html", {**context, "msg": error.message})
    if not result.ok:
        return _render(request, "partials/login_form.html", {**context, "msg": result.reason})

    response = Response(status_code=204, headers={"HX-Redirect": "/dashboard"})
    response.set_cookie(auth.SESSION_COOKIE, result.token,
                        **auth.session_cookie_options(request))
    return response


@router.post("/ui/logout")
def ui_logout(request: Request):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    response = Response(status_code=204, headers={"HX-Redirect": "/"})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------- 账号 ----------

@router.get("/ui/accounts", response_class=HTMLResponse)
def ui_accounts(request: Request, open: str = ""):
    user = _user(request)
    if not user:
        return _to_login(request)
    return _render(request, "partials/accounts.html", _context(user, open_id=int(open) if open.isdigit() else None))


@router.get("/ui/form", response_class=HTMLResponse)
def ui_form(request: Request, edit: str = ""):
    user = _user(request)
    if not user:
        return _to_login(request)
    editing = db.get_account(user["id"], int(edit)) if edit.isdigit() else None
    return _render(request, "partials/form.html", _context(user, editing=editing))


@router.post("/ui/accounts", response_class=HTMLResponse)
def ui_create(request: Request, csu_username: str = Form("", alias="csuUsername"),
              password: str = Form(""), coords: str = Form("")):
    user = _user(request)
    if not user:
        return _to_login(request)

    payload: dict = {"csuUsername": csu_username}
    if password:
        payload["password"] = password
    parsed = parse_coords_input(coords)
    if parsed:
        payload["jd"], payload["wd"] = parsed

    try:
        result = accounts_service.create_or_update(user, payload)
    except AppError as error:
        return _render(request, "partials/form.html", _context(user, msg=error.message, msg_kind="err"))

    schedule_next(db.get_account_by_id(result["account_id"]))
    context = _context(user, open_id=result["account_id"], msg="验证通过，已保存", msg_kind="ok")
    form_html = _render(request, "partials/form.html", context).body.decode()
    accounts_html = _oob(_render(request, "partials/accounts.html", context).body.decode())
    return HTMLResponse(form_html + accounts_html)


@router.post("/ui/accounts/{account_id}/toggle", response_class=HTMLResponse)
def ui_toggle(request: Request, account_id: int):
    user = _user(request)
    if not user:
        return _to_login(request)
    account = db.get_account(user["id"], account_id)
    if not account:
        return _render(request, "partials/accounts.html", _context(user))

    db.update_account(account_id, {
        "enabled": 0 if account["enabled"] else 1,
        "updated_at": to_local_iso(local_now(cfg.config.tz)),
    })
    schedule_next(db.get_account_by_id(account_id))
    return _render(request, "partials/accounts.html", _context(user, open_id=account_id))


@router.post("/ui/accounts/{account_id}/run", response_class=HTMLResponse)
def ui_run(request: Request, account_id: int):
    user = _user(request)
    if not user:
        return _to_login(request)
    account = db.get_account(user["id"], account_id)
    if not account:
        return _render(request, "partials/accounts.html", _context(user))

    result = run_checkin(account, Trigger.MANUAL)
    schedule_next_after(db.get_account_by_id(account_id), result)
    messages = {
        account_id: {
            "text": result["message"],
            "kind": "ok" if result["status"] in (CheckinStatus.SUCCESS, CheckinStatus.SKIPPED) else "err",
        },
    }
    return _render(request, "partials/accounts.html", _context(user, open_id=account_id, messages=messages))


@router.post("/ui/accounts/{account_id}/relogin", response_class=HTMLResponse)
def ui_relogin(request: Request, account_id: int):
    user = _user(request)
    if not user:
        return _to_login(request)
    account = db.get_account(user["id"], account_id)
    if not account:
        return _render(request, "partials/accounts.html", _context(user))

    try:
        result = relogin(account)          # 限流与登录都在服务层，两处入口一致
    except AppError as error:
        messages = {account_id: {"text": error.message, "kind": "err"}}
        return _render(request, "partials/accounts.html", _context(user, open_id=account_id, messages=messages))

    if result["ok"]:
        schedule_next(db.get_account_by_id(account_id))
    messages = {account_id: {"text": result["message"], "kind": "ok" if result["ok"] else "err"}}
    return _render(request, "partials/accounts.html", _context(user, open_id=account_id, messages=messages))


@router.post("/ui/accounts/{account_id}/delete", response_class=HTMLResponse)
def ui_delete(request: Request, account_id: int):
    user = _user(request)
    if not user:
        return _to_login(request)
    db.delete_account(user["id"], account_id)
    return _render(request, "partials/accounts.html", _context(user))


@router.get("/ui/accounts/{account_id}/records", response_class=HTMLResponse)
def ui_records(request: Request, account_id: int):
    user = _user(request)
    if not user:
        return _to_login(request)
    account = db.get_account(user["id"], account_id)
    if not account:
        return _render(request, "partials/records.html", _context(user))
    return _render(request, "partials/records.html",
                   _context(user, account=account, records=db.list_records(account_id, 30)))


@router.get("/ui/records/close", response_class=HTMLResponse)
def ui_records_close(request: Request):
    user = _user(request)
    if not user:
        return _to_login(request)
    return _render(request, "partials/records.html", _context(user))
