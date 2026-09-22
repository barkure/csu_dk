"""账号管理。"""
from __future__ import annotations

import sqlite3

from . import config as cfg
from . import db
from .checkin import ENTRY_CREATE, ENTRY_UPDATE, mark_auth_failure, verify_login
from .clock import local_now, to_local_iso
from .crypto import decrypt_secret, encrypt_secret
from .domain import AuthError
from .errors import AppError, BadRequestError
from .locks import lock_for
from .log import EnabledChangeSource, log_account_enabled_changed, log_event

_USERNAME_BOUND_MESSAGE = "该学号已绑定至其他用户"
_USERNAME_BOUND_DB_ERROR = "accounts.csu_username already bound"


def _now_iso() -> str:
    return to_local_iso(local_now(cfg.config.tz))


def _probe(username: str, password: str | None, existing: dict | None, *,
           entry: str, user_id: int, ip: str | None = None) -> dict | None:
    candidate = password or (decrypt_secret(existing["password_enc"]) if existing else None)
    if not candidate:
        return None
    try:
        return verify_login(username, candidate, entry=entry, user_id=user_id,
                            account_id=(existing or {}).get("id"), ip=ip)
    except AppError:
        raise
    except Exception as error:
        raise AppError(f"验证失败，未保存：{error}", status=400, expose=True) from error


def _recovering_from_bad_credentials(existing: dict | None, payload: dict) -> bool:
    return (existing is not None and payload.get("enabled") is None
            and existing.get("auth_error") == AuthError.BAD_CREDENTIALS)


def _session_fields(probe: dict | None) -> dict:
    session = (probe or {}).get("session") or {}
    if not session.get("token"):
        return {}
    return {
        "token": session["token"],
        "casual": session["casual"],
        "cookies": session["cookies"],
        "token_at": _now_iso(),
        "auth_error": "",
    }


def _insert_account(row: dict) -> dict:
    try:
        return db.insert_account(row)
    except sqlite3.IntegrityError as error:
        if _USERNAME_BOUND_DB_ERROR in str(error):
            raise AppError(_USERNAME_BOUND_MESSAGE, status=409, expose=True) from error
        if "UNIQUE constraint failed" in str(error):
            raise AppError("该学号已经添加过了", status=409, expose=True) from error
        raise


def create_or_update(user: dict, payload: dict, *, ip: str | None = None) -> dict:
    """按用户串行新增或编辑账号。"""
    with lock_for(f"add:{user['id']}"):
        return _create_or_update_locked(user, payload, ip=ip)


def _create_or_update_locked(user: dict, payload: dict, *, ip: str | None = None) -> dict:
    csu_username = str(payload.get("csuUsername") or "").strip()
    if not csu_username:
        raise BadRequestError("缺少学号")

    existing = db.get_account_by_username(user["id"], csu_username)
    password = str(payload["password"]) if payload.get("password") else None
    if not password and not existing:
        raise BadRequestError("请填写密码（服务端不接受明文）")
    if not existing and db.count_accounts(user["id"]) >= cfg.config.max_accounts_per_user:
        raise BadRequestError(f"最多只能托管 {cfg.config.max_accounts_per_user} 个账号，请先删除不用的")

    try:
        probe = _probe(csu_username, password, existing,
                       entry=ENTRY_UPDATE if existing else ENTRY_CREATE,
                       user_id=user["id"], ip=ip)
    except AppError as error:
        if existing and password is None:
            mark_auth_failure(existing["id"], error.__cause__ or error, error.message)
        raise
    if not existing and db.account_username_taken(csu_username):
        raise AppError(_USERNAME_BOUND_MESSAGE, status=409, expose=True)

    fields = {
        "enabled": (existing["enabled"] if existing else 1) if payload.get("enabled") is None
        else (0 if payload["enabled"] is False else 1),
        "dkdz": (existing or {}).get("dkdz") or "",
        "jd": (existing or {}).get("jd"),
        "wd": (existing or {}).get("wd"),
        "updated_at": _now_iso(),
    }

    session = _session_fields(probe)
    if probe is not None and _recovering_from_bad_credentials(existing, payload):
        fields["enabled"] = 1

    if existing:
        db.update_account(existing["id"], {
            **fields,
            **({"password_enc": encrypt_secret(password)} if password else {}),
            **session,
        })
        account_id = existing["id"]
    else:
        created = _insert_account({
            "user_id": user["id"], "csu_username": csu_username,
            "password_enc": encrypt_secret(password), **fields, **session, "created_at": _now_iso(),
        })
        account_id = created["id"]

    return {"account_id": account_id, "verify": {"ok": True, **(probe or {})}}


def update(user: dict, account_id: int, payload: dict, *, ip: str | None = None) -> dict:
    account = db.get_account(user["id"], account_id)
    if not account:
        raise AppError("账号不存在", status=404, expose=True)

    fields: dict = {"updated_at": _now_iso()}
    if payload.get("enabled") is not None:
        fields["enabled"] = 1 if payload["enabled"] else 0

    if payload.get("password"):
        password = str(payload["password"])
        probe = _probe(account["csu_username"], password, account,
                       entry=ENTRY_UPDATE, user_id=user["id"], ip=ip)
        fields["password_enc"] = encrypt_secret(password)
        fields.update(_session_fields(probe) or {"auth_error": ""})
        if _recovering_from_bad_credentials(account, payload):
            fields["enabled"] = 1
    db.update_account(account_id, fields)
    if "enabled" in fields and fields["enabled"] != account["enabled"]:
        _log_enabled_change(user, account, bool(fields["enabled"]), "api")
    return {"account_id": account_id}


def set_enabled(user: dict, account_id: int, enabled: bool, *, source: EnabledChangeSource) -> bool:
    """修改账号启用状态，并在状态发生变化时记录审计日志。"""
    account = db.get_account(user["id"], account_id)
    if not account:
        return False
    value = 1 if enabled else 0
    if account["enabled"] == value:
        return True
    db.update_account(account_id, {"enabled": value, "updated_at": _now_iso()})
    _log_enabled_change(user, account, enabled, source)
    return True


def _log_enabled_change(user: dict, account: dict, enabled: bool,
                        source: EnabledChangeSource) -> None:
    log_account_enabled_changed(user_id=user["id"], account_id=account["id"],
                                username=account.get("csu_username") or "",
                                enabled=enabled, source=source)


def delete(user: dict, account_id: int) -> bool:
    """删除账号并记录审计日志。"""
    account = db.get_account(user["id"], account_id)
    if not account or not db.delete_account(user["id"], account_id):
        return False
    log_event("account.deleted", account_id=account_id, user_id=user["id"],
              csu_username_tail=(account.get("csu_username") or "")[-4:])
    return True
