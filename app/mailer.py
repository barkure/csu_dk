"""Plunk 事务邮件；未配置时提供本地调试验证码。"""
from __future__ import annotations

from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config as cfg
from .errors import UpstreamError
from .log import log_event

VERIFICATION_CODE_SUBJECT = "【妙妙道具】登录验证码"
CREDENTIAL_INVALID_SUBJECT = "【妙妙道具】账号登录异常"
TIMEOUT_SECONDS = 15
_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def mailer_enabled() -> bool:
    return bool(cfg.config.plunk_secret_key and cfg.config.mail_from)


def credential_invalid_notice_enabled() -> bool:
    return mailer_enabled()


def _render(name: str, **context: object) -> str:
    return _ENV.get_template(f"mail/{name}.html").render(**context)


def _post(payload: dict) -> str:
    try:
        response = requests.post(
            f"{cfg.config.plunk_api_url}/v1/send",
            json=payload,
            headers={"Authorization": f"Bearer {cfg.config.plunk_secret_key}"},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as error:
        raise UpstreamError("邮件发送失败，请稍后重试") from error
    if response.status_code != 200:
        raise UpstreamError("邮件发送失败，请稍后重试")
    try:
        body = response.json()
    except ValueError as error:
        raise UpstreamError("邮件发送失败，请稍后重试") from error
    data = body.get("data") if isinstance(body, dict) and body.get("success") is True else None
    emails = data.get("emails") if isinstance(data, dict) else None
    email_id = emails[0].get("email") if isinstance(emails, list) and emails and isinstance(emails[0], dict) else None
    if not isinstance(email_id, str) or not email_id:
        raise UpstreamError("邮件发送失败，请稍后重试")
    return email_id


def _send(email: str, subject: str, body: str) -> dict:
    # 省略 data 字段，避免将验证码和学号尾号写入联系人资料。
    return {"sent": True, "id": _post({
        "to": email,
        "subject": subject,
        "body": body,
        "from": cfg.parse_mail_from(cfg.config.mail_from),
    })}


def send_login_code(email: str, code: str) -> dict:
    if not mailer_enabled():
        log_event("mail.verification_code_debug", email=email)
        print(f"[mailer] 本地调试验证码（邮箱已隐藏）：{code}", flush=True)
        return {"sent": False, "dev_code": code}

    result = _send(email, VERIFICATION_CODE_SUBJECT, _render(
        "login_code", code=code, minutes=cfg.config.code_minutes))
    log_event("mail.verification_code_sent", email=email, message_id=result["id"])
    return result


def send_credential_invalid_notice(email: str, username: str) -> dict:
    """通知用户重新提交学校账号密码。"""
    if not credential_invalid_notice_enabled():
        log_event("mail.credential_invalid_skipped", email=email, reason="未配置账号异常邮件")
        return {"sent": False}

    result = _send(email, CREDENTIAL_INVALID_SUBJECT, _render(
        "credential_invalid", username_tail=username[-4:] if username else ""))
    log_event("mail.credential_invalid_sent", email=email, message_id=result["id"])
    return result
