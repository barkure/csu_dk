"""数据库检查与结果合并。"""
from __future__ import annotations

from . import config as cfg
from . import db
from .clock import local_now, to_local_iso


def init() -> str:
    """建库：新文件按最新结构一次建好；已存在的库只做结构核对。"""
    existed = cfg.DB_PATH.exists() and any(db._structure(db._conn).values())
    db._ensure_schema()
    return "已存在，结构核对通过" if existed else "已按最新结构创建"


def check() -> tuple[bool, list[str]]:
    expected = db._reference_structure()
    actual = db._structure(db._conn)
    differences = db.missing_structure(expected, actual) + db.unexpected_structure(expected, actual)
    return not differences, differences


_RESULT_FIELDS = ("last_run_at", "last_status", "last_message", "jd", "wd", "dkdz")
_SESSION_FIELDS = ("token", "casual", "cookies", "token_at", "auth_error")


def _newer(candidate, current) -> bool:
    return bool(candidate) and (not current or str(candidate) > str(current))


def merge_results(payload: dict) -> dict:
    """合并外部执行产生的账号状态、登录态和记录。"""
    applied = {"accounts": 0, "records": 0, "sessions": 0, "skipped": 0}
    for account_id, item in (payload.get("accounts") or {}).items():
        account = db.get_account_by_id(int(account_id))
        if not account:
            applied["skipped"] += 1
            continue
        fields: dict = {}
        if _newer(item.get("last_run_at"), account.get("last_run_at")):
            fields.update({key: item[key] for key in _RESULT_FIELDS if key in item})
        if _newer(item.get("token_at"), account.get("token_at")):
            fields.update({key: item[key] for key in _SESSION_FIELDS if key in item})
            applied["sessions"] += 1
        if fields:
            fields["updated_at"] = to_local_iso(local_now(cfg.config.tz))
            db.update_account(int(account_id), fields)
            applied["accounts"] += 1
        for record in item.get("records") or []:
            run_at, status = str(record.get("run_at") or ""), str(record.get("status") or "")
            if not run_at or db.has_record(int(account_id), run_at, status):
                continue
            db.add_record(int(account_id), run_at, str(record.get("trigger") or "manual"),
                          status, str(record.get("message") or ""), record.get("dksj"))
            applied["records"] += 1
    return applied
