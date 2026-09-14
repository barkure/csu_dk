"""启动自检。必须在调度器启动**之前**跑完：
调度会解密登录态，而解密路径不会生成密钥 —— 顺序反了的话"密钥丢失"就永远不会被发现。
"""
from __future__ import annotations

from . import config as cfg
from . import db
from .crypto import decrypt_secret
from .errors import MasterKeyInvalidError
from .log import log_event, warn_block
from .permissions import harden_dir, harden_files


def _can_decrypt(account: dict) -> bool:
    try:
        decrypt_secret(account["password_enc"])
        return True
    except Exception:  # noqa: BLE001 - 解不开就算坏的，这里只做判定
        return False


def _credential_problem(accounts: list[dict]):
    key = cfg.inspect_master_key()
    if not accounts:
        return key, None, [], cfg.ensure_master_key()
    if not key.exists:
        return key, f"加密密钥不存在：{cfg.MASTER_KEY_PATH}", accounts, False
    if not key.valid:
        warn_block([
            f"⚠️  加密密钥内容非法：{cfg.MASTER_KEY_PATH}（{key.detail}）",
            "    已拒绝启动。放回正确的密钥，或先把它移走再重启。",
        ])
        raise MasterKeyInvalidError(cfg.MASTER_KEY_PATH, key.detail)
    affected = [account for account in accounts if not _can_decrypt(account)]
    reason = f"{len(affected)}/{len(accounts)} 个账号的密文无法解密（密钥不匹配或数据损坏）" if affected else None
    return key, reason, affected, False


def run_startup_checks() -> dict:
    report = {"chmod": [], "key_created": False, "flagged": [], "key_missing": False}

    if harden_dir(cfg.DATA_DIR, 0o700):
        report["chmod"].append(f"{cfg.DATA_DIR} → 0700")
    report["chmod"] += [
        str(path) for path in harden_files([
            cfg.DB_PATH,
            cfg.DB_PATH.with_name(cfg.DB_PATH.name + "-wal"),
            cfg.DB_PATH.with_name(cfg.DB_PATH.name + "-shm"),
            cfg.MASTER_KEY_PATH,
        ])
    ]

    accounts = db.all_accounts_raw()
    key, reason, affected, created = _credential_problem(accounts)
    report["key_missing"] = not key.exists
    report["key_created"] = created

    if reason:
        for account in affected:
            kind = account.get("auth_error") or "other"
            if db.set_auth_error(account["id"], kind):
                report["flagged"].append(account["csu_username"])

        warn_block([
            f"⚠️  {reason}",
            f"    受影响账号：{'、'.join(report['flagged']) or '—'}（保持暂停，需人工重新提交密码）",
            "    恢复：到网页重新提交一次密码" if key.exists
            else "    恢复：放回 master.key 后重启，或到网页重新提交密码",
        ])
        log_event("startup.credentials_unavailable", reason=reason, flagged=len(report["flagged"]),
                  total=len(accounts), key_exists=bool(key.exists))

    loopback = cfg.config.host in ("127.0.0.1", "localhost", "::1")
    if not cfg.config.allowed_emails and not loopback:
        warn_block([
            f"⚠️  未设置 CSU_DK_ALLOWED_EMAILS 且监听 {cfg.config.host}：任何能收到邮件的人都能注册并触发学校登录",
            "    要限制就在 .env 里加：CSU_DK_ALLOWED_EMAILS=你的邮箱",
        ])
        log_event("startup.open_registration", host=cfg.config.host)

    if report["key_created"]:
        print(f"[startup] 已生成加密密钥 {cfg.MASTER_KEY_PATH}", flush=True)
        log_event("startup.master_key_created")
    if report["chmod"]:
        print(f"[startup] 已修正权限：{'、'.join(report['chmod'])}", flush=True)
        log_event("startup.permissions_fixed", paths=len(report["chmod"]))

    log_event("startup.checks_done", accounts=len(accounts),
              key_exists=cfg.inspect_master_key().exists, flagged=len(report["flagged"]))
    return report
