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
from .redaction import scrub_detail

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
        # 分类保留原始异常，对外只显示脱敏文本。
        raise AppError(f"验证失败，未保存：{scrub_detail(str(error))}", status=400, expose=True) from error


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


def create_or_update(user: dict, payload: dict, *, ip: str | None = None,
                     source: EnabledChangeSource = "api") -> dict:
    """按用户检查账号配额，已有账号与打卡共用账号锁。"""
    with lock_for(f"add:{user['id']}"):
        return _create_or_update_locked(user, payload, ip=ip, source=source)


def _create_or_update_locked(user: dict, payload: dict, *, ip: str | None,
                             source: EnabledChangeSource) -> dict:
    csu_username = str(payload.get("csuUsername") or "").strip()
    if not csu_username:
        raise BadRequestError("缺少学号")

    existing = db.get_account_by_username(user["id"], csu_username)
    if existing:
        with lock_for(f"account:{existing['id']}"):
            account = _must_account(user, existing["id"])
            _update_locked(account, payload, ip=ip, source=source, verify_saved_password=True)
        return {"account_id": existing["id"], "verify": {"ok": True}}

    password = str(payload["password"]) if payload.get("password") else None
    if not password:
        raise BadRequestError("请填写密码")
    if db.count_accounts(user["id"]) >= cfg.config.max_accounts_per_user:
        raise BadRequestError(f"最多只能托管 {cfg.config.max_accounts_per_user} 个账号，请先删除不用的")

    probe = _probe(csu_username, password, None, entry=ENTRY_CREATE, user_id=user["id"], ip=ip)
    if db.account_username_taken(csu_username):
        raise AppError(_USERNAME_BOUND_MESSAGE, status=409, expose=True)

    now = _now_iso()
    created = _insert_account({
        "user_id": user["id"], "csu_username": csu_username, "password_enc": encrypt_secret(password),
        "enabled": int(payload.get("enabled") is not False), "dkdz": "",
        **_session_fields(probe), "created_at": now, "updated_at": now,
    })
    return {"account_id": created["id"], "verify": {"ok": True}}


def update(user: dict, account_id: int, payload: dict, *, ip: str | None = None) -> dict:
    with lock_for(f"account:{account_id}"):
        _update_locked(_must_account(user, account_id), payload, ip=ip, source="api")
    return {"account_id": account_id}


def _must_account(user: dict, account_id: int) -> dict:
    account = db.get_account(user["id"], account_id)
    if not account:
        raise AppError("账号不存在", status=404, expose=True)
    return account


def _update_locked(account: dict, payload: dict, *, ip: str | None, source: EnabledChangeSource,
                    verify_saved_password: bool = False) -> None:
    fields: dict = {}
    if payload.get("enabled") is not None:
        fields["enabled"] = int(bool(payload["enabled"]))

    password = str(payload["password"]) if payload.get("password") else None
    if password or verify_saved_password:
        try:
            probe = _probe(account["csu_username"], password, account,
                           entry=ENTRY_UPDATE, user_id=account["user_id"], ip=ip)
        except AppError as error:
            if password is None:
                cause = error.__cause__ or error
                mark_auth_failure(account["id"], cause, str(cause))
            raise
        if password:
            fields["password_enc"] = encrypt_secret(password)
        fields.update(_session_fields(probe) or {"auth_error": ""})
        if probe is not None and _recovering_from_bad_credentials(account, payload):
            fields["enabled"] = 1
    _save_changes(account, fields, source)


def _save_changes(account: dict, fields: dict, source: EnabledChangeSource) -> None:
    changes = {key: value for key, value in fields.items() if value != account.get(key)}
    if not changes:
        return
    db.update_account(account["id"], {**changes, "updated_at": _now_iso()})
    if "enabled" in changes:
        log_account_enabled_changed(user_id=account["user_id"], account_id=account["id"],
                                    username=account.get("csu_username") or "",
                                    enabled=bool(changes["enabled"]), source=source)


def set_enabled(user: dict, account_id: int, enabled: bool, *, source: EnabledChangeSource) -> bool:
    return _change_enabled(user, account_id, enabled, source=source)


def toggle_enabled(user: dict, account_id: int, *, source: EnabledChangeSource) -> bool:
    return _change_enabled(user, account_id, None, source=source)


def _change_enabled(user: dict, account_id: int, enabled: bool | None, *, source: EnabledChangeSource) -> bool:
    with lock_for(f"account:{account_id}"):
        account = db.get_account(user["id"], account_id)
        if not account:
            return False
        value = int(not account["enabled"] if enabled is None else enabled)
        _save_changes(account, {"enabled": value}, source)
        return True


def delete(user: dict, account_id: int) -> bool:
    """删除账号并记录审计日志。"""
    with lock_for(f"account:{account_id}"):
        account = db.get_account(user["id"], account_id)
        if not account or not db.delete_account(user["id"], account_id):
            return False
        log_event("account.deleted", account_id=account_id, user_id=user["id"],
                  csu_username_tail=(account.get("csu_username") or "")[-4:])
        return True
