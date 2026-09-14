"""数据库结构与结果合并。"""
from __future__ import annotations

import re
import sqlite3

from app import config as cfg
from app import db, dbtools
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret


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


def test_merge_results_round_trip():
    user = db.upsert_user("merge@example.com", to_local_iso(local_now(cfg.config.tz)))
    account = db.insert_account({
        "user_id": user["id"], "csu_username": "980000001", "password_enc": encrypt_secret("x"),
        "enabled": 1, "dkdz": "",
        "jd": None, "wd": None,
        "created_at": "2026-09-14T20:00:00", "updated_at": "2026-09-14T20:00:00",
    })
    payload = {"accounts": {str(account["id"]): {
        "last_run_at": "2026-09-14T21:00:00", "last_status": "success", "last_message": "打卡成功",
        "dkdz": "升华24栋", "token": "tok-from-laptop", "casual": "cas-from-laptop",
        "cookies": "[]", "token_at": "2026-09-14T21:00:05", "auth_error": "",
        "records": [{"run_at": "2026-09-14T21:00:00", "trigger": "manual", "status": "success",
                     "message": "打卡成功", "dksj": "2026-09-14 21:00:01"}],
    }}}

    applied = dbtools.merge_results(payload)
    assert applied == {"accounts": 1, "records": 1, "sessions": 1, "skipped": 0}
    saved = db.get_account_by_id(account["id"])
    assert saved["last_status"] == "success" and saved["dkdz"] == "升华24栋"
    assert saved["token"] == "tok-from-laptop", "登录态也要带回来，省得服务器再登一次"

    again = dbtools.merge_results(payload)
    assert again["records"] == 0, "同一条记录回传两次不该重复插入"
    assert len(db.list_records(account["id"], 10)) == 1


def test_merge_results_skips_unknown_account():
    applied = dbtools.merge_results({"accounts": {"999999": {"last_status": "success", "records": []}}})
    assert applied["skipped"] == 1
