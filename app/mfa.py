"""Continue the school's SMS MFA in its original session; no check-in submission."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import db
from .csu.cas import CAS_BASE, UA, CasMfaRequired
from .csu.zhxg import CAS_CALLBACK, ZhxgClient
from .errors import AppError, BadRequestError, UpstreamError
from .locks import lock_for

TTL = 600
MAX_ATTEMPTS = 3


@dataclass
class Pending:
    client: ZhxgClient
    challenge: CasMfaRequired
    expires: float
    identity: tuple[int, str, str]
    sent_at: float = 0
    attempts: int = 0


_pending: dict[int, Pending] = {}


def _identity(account_id: int) -> tuple[int, str, str]:
    account = db.get_account_by_id(account_id)
    if not account:
        raise AppError("账号不存在", status=404, expose=True)
    return account["user_id"], account["csu_username"], account["password_enc"]


def remember(account_id: int, client: ZhxgClient, challenge: CasMfaRequired) -> None:
    now = time.monotonic()
    for key in list(_pending):
        if _pending[key].expires <= now:
            _pending.pop(key, None)
    _pending[account_id] = Pending(client, challenge, now + TTL, _identity(account_id))


def _get(account_id: int) -> Pending:
    pending = _pending.get(account_id)
    if pending is None or pending.expires <= time.monotonic():
        _pending.pop(account_id, None)
        raise BadRequestError("认证会话未准备好或已过期，请先点击「准备学校认证」")
    if pending.identity != _identity(account_id):
        _pending.pop(account_id, None)
        raise BadRequestError("账号信息已变化，请重新准备认证")
    params = pending.challenge.params
    if params.get("service") != CAS_CALLBACK or str(params.get("reAuthType")) != "3":
        raise BadRequestError("学校本次要求的认证方式不是短信，请在学校官方页面完成认证")
    if not params.get("reAuthUserId") or str(params.get("isMultifactor")).lower() not in ("true", "1"):
        raise BadRequestError("学校认证参数不完整，请重新准备认证")
    return pending


def ready(account_id: int) -> bool:
    try:
        _get(account_id)
        return True
    except BadRequestError:
        return False


def begin(account: dict, *, ip: str | None = None) -> dict:
    from .checkin import relogin

    with lock_for(f"account:{account['id']}"):
        if ready(account["id"]):
            return {"ok": True, "message": "认证会话已就绪，可发送短信验证码"}
    # relogin acquires the same non-reentrant account lock itself.
    result = relogin(account, ip=ip)
    if result["ok"]:
        return result
    if ready(account["id"]):
        return {"ok": True, "message": "密码验证通过，请点击发送短信验证码"}
    return result


def _post(pending: Pending, path: str, payload: dict) -> dict:
    response = pending.client.session.post(
        CAS_BASE + path, data=payload,
        headers={"user-agent": UA, "referer": pending.challenge.url,
                 "origin": "https://ca.csu.edu.cn", "X-Requested-With": "XMLHttpRequest"},
        timeout=20, allow_redirects=False,
    )
    if response.status_code != 200:
        raise UpstreamError("学校认证会话已失效或接口暂不可用，请重新准备认证")
    try:
        data = response.json()
    except ValueError as error:
        raise UpstreamError("学校返回了非预期页面，请重新准备认证") from error
    if not isinstance(data, dict):
        raise UpstreamError("学校认证响应格式异常")
    return data


def send_code(account: dict) -> dict:
    with lock_for(f"account:{account['id']}"):
        pending = _get(account["id"])
        now = time.monotonic()
        if pending.sent_at and now - pending.sent_at < 120:
            raise BadRequestError("短信已经请求，请等两分钟后再发送")
        # Count even a timed-out request: the school may already have sent the SMS.
        pending.sent_at = now
        data = _post(pending, "/dynamicCode/getDynamicCodeByReauth.do", {
            "userName": pending.challenge.params["reAuthUserId"],
            "authCodeTypeName": "reAuthDynamicCodeType",
        })
        if data.get("res") != "success":
            raise UpstreamError("学校未确认短信发送成功，请稍后重试或检查学校绑定手机号")
        return {"ok": True, "message": "学校已发送短信到绑定手机号，请在下方输入验证码"}


def finish(account: dict, code: str, trust: bool = False) -> dict:
    from .checkin import BUSINESS_CODES, OK_CODE, persist_state

    code = code.strip()
    if not re.fullmatch(r"[0-9]{4,8}", code):
        raise BadRequestError("请输入短信中的4至8位数字验证码")
    with lock_for(f"account:{account['id']}"):
        pending = _get(account["id"])
        if pending.attempts >= MAX_ATTEMPTS:
            _pending.pop(account["id"], None)
            raise BadRequestError("本次认证尝试次数已达上限，请重新准备认证")
        pending.attempts += 1
        params = pending.challenge.params
        payload = {
            "service": CAS_CALLBACK, "reAuthType": "3", "isMultifactor": params["isMultifactor"],
            "password": "", "dynamicCode": code, "uuid": "", "answer1": "", "answer2": "", "otpCode": "",
        }
        if params.get("isSleepAccount") == "0":
            # Exact choice exposed by the official trust-device dialog; opt-in only.
            payload["skipTmpReAuth"] = "true" if trust else "false"
        result = _post(pending, "/reAuthCheck/reAuthSubmit.do", payload)
        if result.get("code") in ("reAuth_failed", "reAuth_unauthorized"):
            raise BadRequestError("学校未通过验证码校验，请检查验证码或重新准备认证")
        response = pending.client.session.get(
            CAS_BASE + "/login", params={"service": CAS_CALLBACK},
            headers={"user-agent": UA}, timeout=20,
        )
        if urlsplit(response.url).hostname != "zhxg.csu.edu.cn" or response.status_code != 200:
            _pending.pop(account["id"], None)
            raise UpstreamError("学校尚未完成认证，请重新准备认证或在学校官方页面检查")
        pending.client._exchange_callback(response.text)
        status = pending.client.dk_status()
        business_code = str(status.get("code") or "")
        if (business_code not in BUSINESS_CODES
                or (business_code == OK_CODE and not isinstance(status.get("data"), dict))):
            raise UpstreamError("二次认证后仍未取得有效的学校业务状态，请稍后重试")
        persist_state(account["id"], pending.client)
        _pending.pop(account["id"], None)
        return {"ok": True, "message": "学校认证完成，登录态已加密保存，原有自动打卡设置已保留；未提交新的打卡"}
