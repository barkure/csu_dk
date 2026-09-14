"""数据库工具：建库、查库、把旧库数据导入新库。

数据升级的方式是"按最新结构建新库 + 导入旧数据"，所以这里不保留迁移链，
只提供三件事：建库、核对结构、导入并校验。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config as cfg
from . import db
from .crypto import decrypt_secret

# 导入顺序：先父后子（users → accounts → records/sessions）
_TABLES = (
    ("users", ("id", "email", "created_at", "last_login_at")),
    ("accounts", (
        "id", "user_id", "csu_username", "password_enc", "enabled", "window_start", "window_end",
        "jd", "wd", "dkdz", "casual", "token", "cookies", "token_at", "next_run_at",
        "last_run_at", "last_status", "last_message", "needs_reauth", "auth_error",
        "created_at", "updated_at",
    )),
    ("sessions", ("token_hash", "user_id", "created_at", "expires_at", "user_agent")),
    ("login_codes", ("id", "email", "code_hash", "expires_at", "used", "attempts", "created_at")),
    ("records", ("id", "account_id", "run_at", "trigger", "status", "message", "dksj")),
)


def init() -> str:
    """建库：新文件按最新结构一次建好；已存在的库只做结构核对。"""
    existed = cfg.DB_PATH.exists() and any(db._structure(db._conn).values())
    db._ensure_schema()
    return "已存在，结构核对通过" if existed else "已按最新结构创建"


def check() -> tuple[bool, list[str]]:
    """核对结构，返回 (是否通过, 缺失项)。"""
    missing = db.missing_structure(db._reference_structure(), db._structure(db._conn))
    return not missing, missing


def _source_tables(path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return db._structure(conn)
    finally:
        conn.close()


def import_from(source: Path) -> dict:
    """把旧库的数据导入当前库（当前库必须是空库或同结构库）。

    密文字段原样搬运：能用同一把 data/master.key 解开，账号不用重新录密码。
    """
    if not source.exists():
        raise SystemExit(f"找不到来源库：{source}")
    if source.resolve() == cfg.DB_PATH.resolve():
        raise SystemExit("来源库就是当前库，不需要导入")

    db._ensure_schema()
    missing = db.missing_structure(db._reference_structure(), _source_tables(source))
    if missing:
        raise SystemExit(f"来源库结构不符（缺少 {'、'.join(missing)}），请先把它的数据导出成当前结构")

    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    source_conn.row_factory = sqlite3.Row
    report: dict[str, int] = {}
    try:
        # 导入失败时整体回滚
        with db.transaction():
            for table, columns in _TABLES:
                rows = source_conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                report[table] = len(rows)
                # 重建目标表
                db._exec(f"DELETE FROM {table}")
                if not rows:
                    continue
                placeholders = ", ".join("?" * len(columns))
                db._conn.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows],
                )
    finally:
        source_conn.close()

    return {**report, **verify()}


def verify() -> dict:
    """导入后校验：密文能否用当前密钥解开、账号与记录数是否对得上。"""
    accounts = db.all_accounts_raw()
    unreadable: list[str] = []
    for account in accounts:
        for column in ("password_enc", "token", "cookies"):
            value = account.get(column)
            if not value:
                continue
            try:
                decrypt_secret(str(value))
            except Exception:  # noqa: BLE001 - 校验用：解不开就记下来
                unreadable.append(f"{account['csu_username']}.{column}")

    return {
        "accounts": len(accounts),
        "records": db._one("SELECT COUNT(*) AS n FROM records")["n"],
        "users": db._one("SELECT COUNT(*) AS n FROM users")["n"],
        "unreadable": len(unreadable),
        "detail": unreadable,
    }
