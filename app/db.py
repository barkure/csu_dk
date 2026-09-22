"""SQLite 存储。单连接由全局锁保护。"""
from __future__ import annotations

import pathlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from . import config as cfg
from .clock import local_now, to_local_iso
from .crypto import decrypt_secret, encrypt_secret
from .domain import AuthError

_conn = sqlite3.connect(cfg.DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode = WAL")
_conn.execute("PRAGMA foreign_keys = ON")

_lock = threading.RLock()
_txn_depth = 0


@contextmanager
def transaction() -> Iterator[None]:
    """把多条写操作合成一个事务（与 _lock 一起保证本进程内不会交错）。"""
    global _txn_depth
    with _lock:
        if _txn_depth == 0:
            _conn.execute("BEGIN IMMEDIATE")
        _txn_depth += 1
        try:
            yield
        except BaseException:
            _txn_depth -= 1
            if _txn_depth == 0:
                _conn.execute("ROLLBACK")
            raise
        _txn_depth -= 1
        if _txn_depth == 0:
            _conn.execute("COMMIT")


_SCHEMA = (pathlib.Path(__file__).with_name("schema.sql")).read_text()
_ADDITIVE_TABLES = frozenset({"verifications"})


def _structure(conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {name: {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')} for name in names}


def missing_structure(expected: dict[str, set[str]], actual: dict[str, set[str]]) -> list[str]:
    """列出缺少的表和字段。"""
    tables = sorted(set(expected) - set(actual))
    columns = [f"{table}.{column}" for table, names in expected.items() if table in actual
               for column in sorted(names - actual[table])]
    return tables + columns


def unexpected_structure(expected: dict[str, set[str]], actual: dict[str, set[str]]) -> list[str]:
    return [f"{table}.{column}" for table, columns in actual.items() if table in expected
            for column in sorted(columns - expected[table])]


def _reference_structure() -> dict[str, set[str]]:
    """从 DDL 生成期望结构。"""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA)
        return _structure(reference)
    finally:
        reference.close()


def _ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    conn = conn if conn is not None else _conn

    existing = _structure(conn)
    if existing:
        expected = _reference_structure()
        missing = [item for item in missing_structure(expected, existing)
                   if item not in _ADDITIVE_TABLES]
        unexpected = unexpected_structure(expected, existing)
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"缺少 {'、'.join(missing)}")
            if unexpected:
                details.append(f"多出 {'、'.join(unexpected)}")
            raise RuntimeError(
                f"数据库结构与本版本不符（{'；'.join(details)}）。"
                "请删除旧数据库并重新启动。"
            )

    conn.executescript(_SCHEMA)


_ensure_schema()

_SECRET_COLUMNS = ("token", "casual", "cookies")
_UPDATABLE = {
    "password_enc", "enabled", "jd", "wd", "dkdz",
    "casual", "token", "cookies", "token_at",
    "last_run_at", "last_status", "last_message", "auth_error", "updated_at",
}


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        return _conn.execute(sql, params)


def _one(sql: str, params: tuple = ()) -> dict | None:
    with _lock:
        row = _conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def now_iso() -> str:
    return to_local_iso(local_now(cfg.config.tz))


def find_user_by_email(email: str) -> dict | None:
    return _one("SELECT * FROM users WHERE email = ?", (email,))


def find_user_by_id(user_id: int) -> dict | None:
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def upsert_user(email: str, created_at: str) -> dict:
    existing = find_user_by_email(email)
    if existing:
        return existing
    _exec("INSERT INTO users (email, created_at) VALUES (?, ?)", (email, created_at))
    created = find_user_by_email(email)
    if not created:
        raise RuntimeError(f"创建用户失败：{email}")
    return created


def touch_user_login(user_id: int, moment: str) -> None:
    _exec("UPDATE users SET last_login_at = ? WHERE id = ?", (moment, user_id))


def insert_login_code(email: str, code_hash: str, expires_at: str, created_at: str) -> int:
    cursor = _exec(
        "INSERT INTO login_codes (email, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (email, code_hash, expires_at, created_at),
    )
    return int(cursor.lastrowid)


def delete_login_code(code_id: int) -> bool:
    return _exec("DELETE FROM login_codes WHERE id = ?", (code_id,)).rowcount > 0


def purge_codes_older_than(cutoff_iso: str) -> int:
    """验证码历史用于计算每日限额。"""
    return _exec("DELETE FROM login_codes WHERE created_at <= ?", (cutoff_iso,)).rowcount


def count_recent_codes(email: str, since_iso: str) -> int:
    row = _one(
        "SELECT COUNT(*) AS count FROM login_codes WHERE email = ? AND created_at >= ?",
        (email, since_iso),
    )
    return int(row["count"]) if row else 0


def latest_login_code(email: str) -> dict | None:
    return _one("SELECT * FROM login_codes WHERE email = ? AND used = 0 ORDER BY id DESC LIMIT 1", (email,))


def bump_code_attempts(code_id: int) -> None:
    _exec("UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?", (code_id,))


def consume_login_code(code_id: int, max_attempts: int) -> bool:
    """原子消费未使用且未超次数的验证码。"""
    return _exec(
        "UPDATE login_codes SET used = 1 WHERE id = ? AND used = 0 AND attempts < ?",
        (code_id, max_attempts),
    ).rowcount == 1


def insert_session(token_hash: str, user_id: int, created_at: str, expires_at: str, user_agent: str = "") -> None:
    _exec(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?, ?)",
        (token_hash, user_id, created_at, expires_at, user_agent),
    )


def find_session(token_hash: str) -> dict | None:
    return _one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_session(token_hash: str) -> None:
    _exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def purge_expired_sessions(now: str) -> int:
    return _exec("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount


def trim_sessions(user_id: int, keep: int) -> int:
    """按 rowid 保留最新会话，避免 created_at 同秒并列。"""
    return _exec(
        """DELETE FROM sessions WHERE user_id = ? AND rowid NOT IN (
             SELECT rowid FROM sessions WHERE user_id = ? ORDER BY rowid DESC LIMIT ?
           )""",
        (user_id, user_id, keep),
    ).rowcount


def purge_old_records(cutoff_iso: str) -> int:
    return _exec("DELETE FROM records WHERE run_at <= ?", (cutoff_iso,)).rowcount


def upsert_verification(account_id: int, run_at: str, trigger: str, next_at: str,
                        created_at: str) -> None:
    _exec(
        """INSERT INTO verifications (account_id, run_at, trigger, next_at, rounds, created_at)
           VALUES (?, ?, ?, ?, 0, ?)
           ON CONFLICT(account_id) DO UPDATE SET
             run_at = excluded.run_at, trigger = excluded.trigger,
             next_at = excluded.next_at, rounds = 0, created_at = excluded.created_at""",
        (account_id, run_at, trigger, next_at, created_at),
    )


def get_verification(account_id: int) -> dict | None:
    return _one("SELECT * FROM verifications WHERE account_id = ?", (account_id,))


def due_verifications(now_iso: str, limit: int) -> list[dict]:
    return _all("SELECT * FROM verifications WHERE next_at <= ? ORDER BY next_at LIMIT ?",
                (now_iso, limit))


def bump_verification(account_id: int, next_at: str) -> None:
    _exec("UPDATE verifications SET rounds = rounds + 1, next_at = ? WHERE account_id = ?",
          (next_at, account_id))


def delete_verification(account_id: int) -> None:
    _exec("DELETE FROM verifications WHERE account_id = ?", (account_id,))


def purge_verifications(cutoff_iso: str) -> int:
    return _exec("DELETE FROM verifications WHERE created_at <= ?", (cutoff_iso,)).rowcount


def clear_verifications() -> int:
    return _exec("DELETE FROM verifications").rowcount


def count_accounts(user_id: int) -> int:
    row = _one("SELECT COUNT(*) AS count FROM accounts WHERE user_id = ?", (user_id,))
    return int(row["count"]) if row else 0


def account_username_taken(csu_username: str) -> bool:
    """是否已有账号绑定该学号。"""
    return _one("SELECT 1 FROM accounts WHERE csu_username = ?", (csu_username,)) is not None


def _seal(row: dict) -> dict:
    out = dict(row)
    for key in _SECRET_COLUMNS:
        value = out.get(key)
        if value not in (None, "") and not str(value).startswith("v1."):
            out[key] = encrypt_secret(str(value))
    return out


def _open(row: dict | None) -> dict | None:
    if not row:
        return None
    out = dict(row)
    for key in _SECRET_COLUMNS:
        value = out.get(key)
        if isinstance(value, str) and value.startswith("v1."):
            try:
                out[key] = decrypt_secret(value)
            except Exception:  # noqa: BLE001 - 解不开就当作没有登录态，下次自动重登
                out[key] = None
    return out


def list_accounts(user_id: int) -> list[dict]:
    return _all("SELECT * FROM accounts WHERE user_id = ? ORDER BY id", (user_id,))


def get_account(user_id: int, account_id: int) -> dict | None:
    return _open(_one("SELECT * FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id)))


def get_account_by_id(account_id: int) -> dict | None:
    """拿锁后重读账号，避免用排队前的旧快照。"""
    return _open(_one("SELECT * FROM accounts WHERE id = ?", (account_id,)))


def get_account_notification_target(account_id: int) -> dict | None:
    return _one(
        """SELECT a.csu_username, a.auth_error, a.enabled, a.user_id, u.email
           FROM accounts a JOIN users u ON u.id = a.user_id
           WHERE a.id = ?""",
        (account_id,),
    )


def get_account_by_username(user_id: int, csu_username: str) -> dict | None:
    return _open(_one("SELECT * FROM accounts WHERE user_id = ? AND csu_username = ?", (user_id, csu_username)))


def all_accounts_raw() -> list[dict]:
    """不解密：启动自检要在密钥可能缺失时清点账号。"""
    return _all("SELECT * FROM accounts ORDER BY id")


def set_auth_error(account_id: int, kind: AuthError | str) -> bool:
    """记录或清除账号的认证故障。"""
    kind = AuthError(kind)
    cursor = _exec(
        """UPDATE accounts SET auth_error = ?, updated_at = ?
            WHERE id = ?""",
        (kind, now_iso(), account_id),
    )
    return cursor.rowcount > 0


def insert_account(row: dict) -> dict | None:
    payload = {"token": "", "casual": None, "cookies": None, "token_at": None,
               "auth_error": "", "dkdz": "", "jd": None, "wd": None}
    payload.update(row)
    payload = _seal(payload)
    _exec(
        """INSERT INTO accounts (user_id, csu_username, password_enc, enabled,
                                 jd, wd, dkdz, token, casual, cookies, token_at, auth_error,
                                 created_at, updated_at)
           VALUES (:user_id, :csu_username, :password_enc, :enabled,
                   :jd, :wd, :dkdz, :token, :casual, :cookies, :token_at, :auth_error,
                   :created_at, :updated_at)""",
        payload,
    )
    return get_account_by_username(row["user_id"], row["csu_username"])


def update_account(account_id: int, fields: dict) -> None:
    keys = [key for key in fields if key in _UPDATABLE]
    if not keys:
        return
    sealed = _seal(fields)
    assignments = ", ".join(f"{key} = :{key}" for key in keys)
    params = {key: sealed[key] for key in keys}
    params["id"] = account_id
    _exec(f"UPDATE accounts SET {assignments} WHERE id = :id", params)


def delete_account(user_id: int, account_id: int) -> bool:
    return _exec("DELETE FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id)).rowcount > 0


def all_enabled_accounts() -> list[dict]:
    rows = _all("SELECT * FROM accounts WHERE enabled = 1 AND auth_error = ''")
    return [_open(row) for row in rows]


def add_record(account_id: int, run_at: str, trigger: str, status: str,
               message: str = "", dksj: str | None = None) -> None:
    _exec(
        "INSERT INTO records (account_id, run_at, trigger, status, message, dksj) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, run_at, trigger, status, message or "", dksj),
    )


def acquire_scheduler_lease(owner: str, now_iso_text: str, stale_before_iso: str) -> bool:
    """获取空闲、已有或过期的调度租约。"""
    with transaction():
        row = _one("SELECT owner, heartbeat_at FROM scheduler_lease WHERE id = 1")
        if row is None:
            _exec("INSERT INTO scheduler_lease (id, owner, heartbeat_at) VALUES (1, ?, ?)",
                  (owner, now_iso_text))
            return True
        if row["owner"] == owner or row["heartbeat_at"] <= stale_before_iso:
            _exec("UPDATE scheduler_lease SET owner = ?, heartbeat_at = ? WHERE id = 1",
                  (owner, now_iso_text))
            return True
        return False


def release_scheduler_lease(owner: str) -> None:
    """释放调度租约。"""
    _exec("DELETE FROM scheduler_lease WHERE owner = ?", (owner,))


def heartbeat_scheduler_lease(owner: str, now_iso_text: str) -> bool:
    """续租；返回 False 说明租约已被别的进程接管，本进程应停止调度。"""
    cursor = _exec("UPDATE scheduler_lease SET heartbeat_at = ? WHERE id = 1 AND owner = ?",
                   (now_iso_text, owner))
    return cursor.rowcount == 1


def list_records(account_id: int, limit: int = 30) -> list[dict]:
    return _all("SELECT * FROM records WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit))


def has_record(account_id: int, run_at: str, status: str) -> bool:
    """同一个账号同一秒同一结果的记录只该有一条（回传结果时用来去重）。"""
    rows = _all("SELECT 1 FROM records WHERE account_id = ? AND run_at = ? AND status = ? LIMIT 1",
                (account_id, run_at, status))
    return bool(rows)


def delete_user(email: str) -> None:
    """级联删掉用户及其账号/会话/记录（e2e 收尾用）。"""
    _exec("DELETE FROM users WHERE email = ?", (email,))
