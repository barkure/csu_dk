"""数据库结构检查。"""
from __future__ import annotations

import re
import sqlite3

from app import db, dbtools


def test_check_passes_on_current_structure():
    assert dbtools.check() == (True, [])


def test_init_is_idempotent():
    assert dbtools.init() in ("已存在，结构核对通过", "已按最新结构创建")
    ok, missing = dbtools.check()
    assert ok and missing == []


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
