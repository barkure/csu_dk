"""数据库结构检查。"""
from __future__ import annotations

import re
import sqlite3

import pytest

from app import db, dbtools


def test_check_passes_on_current_structure():
    assert dbtools.check() == (True, [])


def test_init_is_idempotent():
    assert dbtools.init() in ("已存在，结构核对通过", "已按最新结构创建")
    ok, missing = dbtools.check()
    assert ok and missing == []


def test_unique_username_trigger_allows_legacy_duplicates(tmp_path):
    schema_without_trigger = re.sub(
        r"CREATE TRIGGER IF NOT EXISTS trg_accounts_unique_username(?:_update)?.*?END;\s*",
        "",
        db._SCHEMA,
        flags=re.DOTALL,
    )
    conn = sqlite3.connect(tmp_path / "legacy.db", isolation_level=None)
    try:
        conn.executescript(schema_without_trigger)
        conn.executemany(
            "INSERT INTO users (id,email,created_at) VALUES (?,?,?)",
            [(1, "one@example.com", "now"), (2, "two@example.com", "now"),
             (3, "three@example.com", "now")],
        )
        for user_id in (1, 2):
            conn.execute(
                """INSERT INTO accounts
                   (user_id,csu_username,password_enc,enabled,dkdz,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (user_id, "999333001", "encrypted", 1, "", "now", "now"),
            )

        db._ensure_schema(conn)

        with pytest.raises(sqlite3.IntegrityError,
                           match=re.escape("accounts.csu_username already bound")):
            conn.execute(
                """INSERT INTO accounts
                   (user_id,csu_username,password_enc,enabled,dkdz,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (3, "999333001", "encrypted", 1, "", "now", "now"),
            )

        # 历史重复行不改学号时仍可维护，但不能把其他账号改成重复学号。
        conn.execute("UPDATE accounts SET dkdz = ? WHERE user_id = ?", ("升华8栋", 1))
        conn.execute(
            """INSERT INTO accounts
               (user_id,csu_username,password_enc,enabled,dkdz,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (3, "999333002", "encrypted", 1, "", "now", "now"),
        )
        with pytest.raises(sqlite3.IntegrityError,
                           match=re.escape("accounts.csu_username already bound")):
            conn.execute("UPDATE accounts SET csu_username = ? WHERE user_id = ?",
                         ("999333001", 3))
    finally:
        conn.close()


def test_db_check_command_reports_incompatible_database(tmp_path):
    import subprocess
    import sys
    from pathlib import Path as PathClass

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_schema = re.sub(r",?\s*auth_error\s+TEXT NOT NULL DEFAULT ''", "", db._SCHEMA)
    old_schema = re.sub(r"CREATE INDEX IF NOT EXISTS idx_accounts_auth_error[^;]*;", "", old_schema)
    conn = sqlite3.connect(data_dir / "csu_dk.db", isolation_level=None)
    conn.executescript(old_schema)
    conn.close()

    project_root = PathClass(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "app", "db-check"],
        cwd=project_root,
        capture_output=True,
        text=True,
        env={
            "CSU_DK_TEST_DATA_DIR": str(data_dir),
            "CSU_DK_ENV_FILE": str(tmp_path / ".env.absent"),
            "PATH": "/usr/bin:/bin",
        },
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "请删除旧数据库并重新启动" in output
    assert "Traceback" not in output
