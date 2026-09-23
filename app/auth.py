"""账号体系：邮箱验证码登录、会话、白名单、限流。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config as cfg
from . import db
from .clock import local_now, to_local_iso
from .errors import BadRequestError, ForbiddenError, RateLimitError
from .mailer import send_login_code
from .ratelimit import SlidingWindow

SESSION_COOKIE = "csu_dk_session"
MAX_CODE_ATTEMPTS = 5

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def hash_login_code(email: str, code: str) -> str:
    """HMAC(master.key)：6 位码只有 100 万种可能，光加盐挡不住离线爆破。"""
    key = base64.b64decode(cfg.read_master_key())
    return hmac.new(key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


limiters = {
    "request_ip": SlidingWindow(cfg.config.request_ip.window_ms, cfg.config.request_ip.max),
    "verify_ip": SlidingWindow(cfg.config.verify_ip.window_ms, cfg.config.verify_ip.max),
}


def normalize_email(email: object) -> str:
    value = str(email or "").strip().lower()
    if not _EMAIL_RE.match(value):
        raise BadRequestError("邮箱格式不正确")
    if len(value) > 254:
        raise BadRequestError("邮箱过长")
    return value


def assert_email_allowed(email: str) -> None:
    """没有独立注册步骤（验证通过即建用户），所以白名单是唯一准入门。"""
    allowed = cfg.config.allowed_emails
    if allowed and email not in allowed:
        raise ForbiddenError("该邮箱不在允许名单内，无法登录")


def allowlist_enabled() -> bool:
    return bool(cfg.config.allowed_emails)


def _enforce(limiter: SlidingWindow, key: str, message: str) -> None:
    verdict = limiter.take(key)
    if not verdict.ok:
        raise RateLimitError(f"{message}，请 {verdict.retry_after_sec} 秒后再试", verdict.retry_after_sec)


def request_login_code(email: object, ip: str = "unknown") -> dict:
    normalized = normalize_email(email)
    now = local_now(cfg.config.tz)

    assert_email_allowed(normalized)
    _enforce(limiters["request_ip"], ip, "请求过于频繁")

    previous = db.latest_login_code(normalized)
    if previous:
        elapsed = (now - datetime.fromisoformat(previous["created_at"])).total_seconds()
        if elapsed < cfg.config.code_cooldown_seconds:
            wait = max(1, round(cfg.config.code_cooldown_seconds - elapsed))
            raise RateLimitError(f"请求过于频繁，请 {wait} 秒后再试", wait)

    day_ago = to_local_iso(now - timedelta(days=1))
    if db.count_recent_codes(normalized, day_ago) >= cfg.config.login_code_daily_max:
        raise RateLimitError(f"该邮箱 24 小时内最多请求 {cfg.config.login_code_daily_max} 次验证码，请稍后再试", 3600)

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_id = db.insert_login_code(
        normalized,
        hash_login_code(normalized, code),
        to_local_iso(now + timedelta(minutes=cfg.config.code_minutes)),
        to_local_iso(now),
    )

    try:
        return send_login_code(normalized, code)
    except Exception:
        db.delete_login_code(code_id)  # 发信失败就撤销，否则用户白等一个冷却周期
        raise


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    user: dict | None = None
    token: str | None = None      # 会话令牌：与消费验证码在同一个事务里创建
    reason: str = ""


def verify_login_code(email: object, code: object, ip: str = "unknown",
                      user_agent: str | None = None) -> VerifyResult:
    normalized = normalize_email(email)
    assert_email_allowed(normalized)
    _enforce(limiters["verify_ip"], ip, "尝试过于频繁")

    row = db.latest_login_code(normalized)
    if not row:
        return VerifyResult(False, reason="请先获取验证码")

    now_iso = to_local_iso(local_now(cfg.config.tz))
    if now_iso > row["expires_at"]:
        return VerifyResult(False, reason="验证码已过期，请重新获取")
    if row["attempts"] >= MAX_CODE_ATTEMPTS:
        return VerifyResult(False, reason="尝试次数过多，请重新获取验证码")

    provided = str(code or "").strip()
    actual = hash_login_code(normalized, provided) if re.fullmatch(r"\d{6}", provided) else ""
    if not hmac.compare_digest(actual, row["code_hash"]):
        db.bump_code_attempts(row["id"])
        return VerifyResult(False, reason="验证码不正确")

    now = local_now(cfg.config.tz)
    # 原子完成验证码消费、用户创建和会话创建
    with db.transaction():
        if not db.consume_login_code(row["id"], MAX_CODE_ATTEMPTS):
            return VerifyResult(False, reason="验证码已被使用，请重新获取")
        user = db.upsert_user(normalized, now_iso)
        token = _insert_session(user["id"], now, None)
    return VerifyResult(True, user=user, token=token)


def _insert_session(user_id: int, now, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    db.insert_session(
        _sha256(token),
        user_id,
        to_local_iso(now),
        to_local_iso(now + timedelta(days=cfg.config.session_days)),
        (user_agent or "")[:200],
    )
    db.touch_user_login(user_id, to_local_iso(now))
    db.trim_sessions(user_id, cfg.config.max_sessions_per_user)
    return token


def create_session(user_id: int, user_agent: str = "") -> str:
    return _insert_session(user_id, local_now(cfg.config.tz), user_agent)


def resolve_session(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.find_session(_sha256(token))
    if not row:
        return None
    if to_local_iso(local_now(cfg.config.tz)) > row["expires_at"]:
        db.delete_session(row["token_hash"])
        return None
    return db.find_user_by_id(row["user_id"])


def destroy_session(token: str | None) -> None:
    if token:
        db.delete_session(_sha256(token))


def session_cookie_options() -> dict:
    """Cookie 是否仅通过 HTTPS 发送由部署配置决定。"""
    return {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": cfg.config.cookie_secure,
        "max_age": cfg.config.session_days * 86_400,
    }
