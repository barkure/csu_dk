"""SQLite 存储。FastAPI 是多线程的，所以用**单连接 + 一把全局锁**，不折腾连接池。

事务：连接跑在 autocommit 模式（isolation_level=None），需要跨语句原子性时用 transaction()，
它同时持有全局锁并把多条写操作包成一个 BEGIN IMMEDIATE ... COMMIT。
"""
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


# ---------- 结构与建表 ----------
# 无迁移链：按 schema.sql 建库或校验结构
_SCHEMA = (pathlib.Path(__file__).with_name("schema.sql")).read_text()



def _structure(conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {name: {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')} for name in names}


def missing_structure(expected: dict[str, set[str]], actual: dict[str, set[str]]) -> list[str]:
    """列出实际库相对期望结构缺了什么（缺表记表名，缺列记 表.列）。"""
    missing = sorted(set(expected) - set(actual))
    missing += [f"{table}.{column}" for table, columns in expected.items() if table in actual
                for column in sorted(columns - actual[table])]
    return missing


def _reference_structure() -> dict[str, set[str]]:
    """用同一份 DDL 在内存库里建一遍，得到"期望结构"（省得再维护一份字段清单）。"""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA)
        return _structure(reference)
    finally:
        reference.close()


def _ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    conn = conn if conn is not None else _conn

    # 先校验，避免索引 DDL 掩盖缺列错误
    existing = _structure(conn)
    if existing:
        missing = missing_structure(_reference_structure(), existing)
        if missing:
            raise RuntimeError(
                f"数据库结构与本版本不符（缺少 {'、'.join(missing)}）。"
                "请按最新结构重建数据库后再导入旧数据；加密字段要用配套的 data/master.key。"
            )

    conn.executescript(_SCHEMA)


_ensure_schema()

_SECRET_COLUMNS = ("token", "casual", "cookies")
_UPDATABLE = {
    "password_enc", "enabled", "window_start", "window_end", "jd", "wd", "dkdz",
    "casual", "token", "cookies", "token_at", "next_run_at",
    "last_run_at", "last_status", "last_message", "needs_reauth", "auth_error", "updated_at",
}


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """单条语句：autocommit；在 transaction() 内则由它统一提交。"""
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


# ---------- 用户 ----------

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


# ---------- 验证码 ----------

def insert_login_code(email: str, code_hash: str, expires_at: str, created_at: str) -> int:
    cursor = _exec(
        "INSERT INTO login_codes (email, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (email, code_hash, expires_at, created_at),
    )
    return int(cursor.lastrowid)


def delete_login_code(code_id: int) -> bool:
    return _exec("DELETE FROM login_codes WHERE id = ?", (code_id,)).rowcount > 0


def purge_codes_older_than(cutoff_iso: str) -> int:
    """保留 24 小时：每邮箱每日上限要靠历史行计数，删早了就数不到。"""
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
    """原子消费验证码：只有"未使用且尝试次数没超"才置为已用，返回是否抢到。

    两个并发请求同时提交同一个（正确的）验证码时，只有一条 UPDATE 的 rowcount 是 1，
    另一个拿到 False —— 否则"读 used=0 → 校验 → 标记 used"这条链路会双双通过。
    """
    return _exec(
        "UPDATE login_codes SET used = 1 WHERE id = ? AND used = 0 AND attempts < ?",
        (code_id, max_attempts),
    ).rowcount == 1


# ---------- 会话 ----------

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
    """会话是 14 天有效的钥匙，只保留最近几个。

    必须按 rowid 排序：created_at 只到秒，同一秒内的多次登录会并列，
    按它排序就可能把刚建的那个（最新的）删掉。
    """
    return _exec(
        """DELETE FROM sessions WHERE user_id = ? AND rowid NOT IN (
             SELECT rowid FROM sessions WHERE user_id = ? ORDER BY rowid DESC LIMIT ?
           )""",
        (user_id, user_id, keep),
    ).rowcount


def purge_old_records(cutoff_iso: str) -> int:
    return _exec("DELETE FROM records WHERE run_at <= ?", (cutoff_iso,)).rowcount


def count_accounts(user_id: int) -> int:
    row = _one("SELECT COUNT(*) AS count FROM accounts WHERE user_id = ?", (user_id,))
    return int(row["count"]) if row else 0


# ---------- 打卡账号 ----------

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


def get_account_by_username(user_id: int, csu_username: str) -> dict | None:
    return _open(_one("SELECT * FROM accounts WHERE user_id = ? AND csu_username = ?", (user_id, csu_username)))


def all_accounts_raw() -> list[dict]:
    """不解密：启动自检要在密钥可能缺失时清点账号。"""
    return _all("SELECT * FROM accounts ORDER BY id")


def set_auth_error(account_id: int, kind: AuthError | str) -> bool:
    """记录/清除账号的认证故障。

    故障类型是状态的唯一依据；needs_reauth 由它派生（历史遗留，调度器用它排除账号）。
    记故障时清掉排期（免得界面显示一个不会执行的下次时刻），清除时保留原排期。
    """
    kind = AuthError(kind)          # 未知取值直接抛错，别把脏值写进库
    cursor = _exec(
        """UPDATE accounts SET auth_error = ?, needs_reauth = ?,
                  next_run_at = CASE WHEN ? = '' THEN next_run_at ELSE NULL END,
                  updated_at = ?
            WHERE id = ?""",
        (kind, 1 if kind else 0, kind, now_iso(), account_id),
    )
    return cursor.rowcount > 0


def insert_account(row: dict) -> dict | None:
    payload = {"token": "", "casual": None, "cookies": None, "token_at": None,
               "needs_reauth": 0, "auth_error": ""}
    payload.update(row)
    payload = _seal(payload)
    _exec(
        """INSERT INTO accounts (user_id, csu_username, password_enc, enabled, window_start, window_end,
                                 jd, wd, dkdz, token, casual, cookies, token_at, needs_reauth,
                                 created_at, updated_at)
           VALUES (:user_id, :csu_username, :password_enc, :enabled, :window_start, :window_end,
                   :jd, :wd, :dkdz, :token, :casual, :cookies, :token_at, :needs_reauth,
                   :created_at, :updated_at)""",
        payload,
    )
    return get_account_by_username(row["user_id"], row["csu_username"])


def update_account(account_id: int, fields: dict) -> None:
    keys = [key for key in fields if key in _UPDATABLE]
    if not keys:
        return
    sealed = _seal(fields)
    # 列名来自白名单，值使用参数绑定
    assignments = ", ".join(f"{key} = :{key}" for key in keys)
    params = {key: sealed[key] for key in keys}
    params["id"] = account_id
    _exec(f"UPDATE accounts SET {assignments} WHERE id = :id", params)


def delete_account(user_id: int, account_id: int) -> bool:
    return _exec("DELETE FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id)).rowcount > 0


def due_accounts(now: str) -> list[dict]:
    """needs_reauth = 0 才排期：密码失效的账号继续尝试只会拿废密码撞学校风控。"""
    rows = _all(
        """SELECT * FROM accounts
            WHERE enabled = 1 AND needs_reauth = 0
              AND (next_run_at IS NULL OR next_run_at <= ?)
            ORDER BY next_run_at IS NULL DESC, next_run_at""",
        (now,),
    )
    return [_open(row) for row in rows]


def all_enabled_accounts() -> list[dict]:
    rows = _all("SELECT * FROM accounts WHERE enabled = 1 AND needs_reauth = 0")
    return [_open(row) for row in rows]


# ---------- 记录 ----------

def add_record(account_id: int, run_at: str, trigger: str, status: str,
               message: str = "", dksj: str | None = None) -> None:
    _exec(
        "INSERT INTO records (account_id, run_at, trigger, status, message, dksj) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, run_at, trigger, status, message or "", dksj),
    )


# ---------- 调度器租约 ----------

def acquire_scheduler_lease(owner: str, now_iso_text: str, stale_before_iso: str) -> bool:
    """抢调度租约：多个进程时只有第一个能跑调度（其余进程退化为纯 HTTP 服务）。

    心跳过期（stale_before_iso 之后没动静）就允许接管，避免进程被 kill 后租约卡死。
    """
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
    """正常退出时让出租约：否则下次启动要等心跳过期（两分钟）才能接管调度。"""
    _exec("DELETE FROM scheduler_lease WHERE owner = ?", (owner,))


def heartbeat_scheduler_lease(owner: str, now_iso_text: str) -> bool:
    """续租；返回 False 说明租约已被别的进程接管，本进程应停止调度。"""
    cursor = _exec("UPDATE scheduler_lease SET heartbeat_at = ? WHERE id = 1 AND owner = ?",
                   (now_iso_text, owner))
    return cursor.rowcount == 1


def list_records(account_id: int, limit: int = 30) -> list[dict]:
    return _all("SELECT * FROM records WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit))


def delete_user(email: str) -> None:
    """级联删掉用户及其账号/会话/记录（e2e 收尾用）。"""
    _exec("DELETE FROM users WHERE email = ?", (email,))
