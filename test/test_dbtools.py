"""db 工具：建库、核对结构、把旧库数据导入新库并校验。"""
from __future__ import annotations

import re
import sqlite3

from app import config as cfg
from app import db, dbtools
from app.clock import local_now, to_local_iso
from app.crypto import encrypt_secret


def build_source(path, *, password_enc: str | None = None, email: str = "old@example.com",
                 username: str = "255000001") -> None:
    """按最新结构建一个"旧库"，塞进一个用户、一个账号、一条记录。"""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(db._SCHEMA)
        now = to_local_iso(local_now(cfg.config.tz))
        conn.execute("INSERT INTO users (id, email, created_at) VALUES (1, ?, ?)", (email, now))
        conn.execute(
            """INSERT INTO accounts (id, user_id, csu_username, password_enc, enabled, window_start,
                                     window_end, jd, wd, dkdz, needs_reauth, auth_error,
                                     created_at, updated_at)
               VALUES (7, 1, ?, ?, 1, '20:00', '22:30', 112.936833, 28.157238,
                       '升华8栋', 0, 'locked', ?, ?)""",
            (username, password_enc or encrypt_secret("school-password"), now, now),
        )
        conn.execute(
            "INSERT INTO records (account_id, run_at, trigger, status, message)"
            " VALUES (7, ?, 'schedule', 'success', '打卡成功')",
            (now,),
        )
    finally:
        conn.close()


def test_check_passes_on_current_structure():
    assert dbtools.check() == (True, [])


def test_init_is_idempotent():
    assert dbtools.init() in ("已存在，结构核对通过", "已按最新结构创建")
    ok, missing = dbtools.check()
    assert ok and missing == []


def test_import_round_trip_keeps_data_and_state(tmp_path):
    """导入后账号、记录、故障状态都在，密文能用当前密钥解开（不用重录密码）。"""
    source = tmp_path / "old.db"
    build_source(source)

    report = dbtools.import_from(source)

    assert report["users"] == 1 and report["accounts"] == 1 and report["records"] == 1
    assert report["unreadable"] == 0, report["detail"]

    account = db.get_account_by_id(7)
    assert account is not None
    assert account["csu_username"] == "255000001"
    assert account["auth_error"] == "locked", "故障状态要一起搬过来"
    assert db.list_records(7)[0]["status"] == "success"


def test_import_reports_ciphertext_that_does_not_decrypt(tmp_path):
    """用别的密钥加密的密文导进来会解不开：要如实报出来，而不是装作成功。"""
    source = tmp_path / "other-key.db"
    build_source(source, password_enc="v1.AAAA.BBBBCCCCDDDD")

    report = dbtools.import_from(source)

    assert report["accounts"] == 1
    assert report["unreadable"] == 1 and "255000001.password_enc" in report["detail"]


def test_import_rejects_incompatible_source(tmp_path):
    """来源库结构不符时明确报错（配合"按新结构建库再导数据"的流程）。"""
    source = tmp_path / "weird.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")   # 缺一大堆列
    conn.commit()
    conn.close()

    try:
        dbtools.import_from(source)
    except SystemExit as error:
        assert "来源库结构不符" in str(error)
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("结构不符时应当直接拒绝")


def test_import_is_one_transaction(tmp_path):
    """导入途中失败要整体回滚：不能留下"导入了一半"的库。"""
    import pytest

    # 目标库里先放一份数据（要验证失败后它还在）
    now = to_local_iso(local_now(cfg.config.tz))
    db.upsert_user("keep@example.com", now)

    source = tmp_path / "bad.db"
    build_source(source, email="never-imported@example.com", username="255000999")
    # 让来源库里的记录指向一个不存在的账号：来源连接没开外键校验所以塞得进去，
    # 导入到开了外键的目标库时会在 records 这一步失败（此时 users/accounts 已经搬完了）。
    conn = sqlite3.connect(source, isolation_level=None)
    conn.execute(
        "INSERT INTO records (account_id, run_at, trigger, status) VALUES (999, ?, 'schedule', 'failed')",
        (now,),
    )
    conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        dbtools.import_from(source)

    assert db.find_user_by_email("keep@example.com") is not None, "失败后原数据必须还在"
    assert db.find_user_by_email("never-imported@example.com") is None, "半导入的数据不能留下"


def test_db_check_command_reports_incompatible_database(tmp_path):
    """真实启动链：结构不符时退出码 1、给出提示、没有 traceback。

    这条必须用子进程跑 —— 结构核对是在导入 app.db 时执行的，
    以前 CLI 还没轮到友好输出就先抛异常了。
    """
    import subprocess
    import sys
    from pathlib import Path as PathClass

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 造一个"缺 auth_error 列"的库
    old_schema = re.sub(r",?\s*auth_error TEXT NOT NULL DEFAULT ''", "", db._SCHEMA)
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
    assert "请按最新结构重建数据库" in output
    assert "Traceback" not in output


def test_import_from_empty_source_clears_target(tmp_path):
    """导入是"重建"：来源表为空时，目标表也要被清空，不能留下旧数据。"""
    now = to_local_iso(local_now(cfg.config.tz))
    keep = db.upsert_user("stale@example.com", now)
    db.insert_account({
        "user_id": keep["id"], "csu_username": "255000777", "password_enc": encrypt_secret("pw"),
        "enabled": 1, "window_start": "20:00", "window_end": "22:30", "jd": 112.9, "wd": 28.1,
        "dkdz": "", "created_at": now, "updated_at": now,
    })

    source = tmp_path / "empty.db"
    conn = sqlite3.connect(source)
    conn.executescript(db._SCHEMA)          # 结构对，但一张表都没数据
    conn.close()

    report = dbtools.import_from(source)

    assert report["users"] == 0 and report["accounts"] == 0
    assert db.find_user_by_email("stale@example.com") is None, "空来源也要清空目标"
    assert db.all_accounts_raw() == []


def test_db_import_without_path_exits_cleanly(tmp_path):
    """--from 后面没给路径：退出码 2 + 用法提示，不能是 IndexError traceback。"""
    import subprocess
    import sys
    from pathlib import Path as PathClass

    result = subprocess.run(
        [sys.executable, "-m", "app", "db-import", "--from"],
        cwd=PathClass(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        env={
            "CSU_DK_TEST_DATA_DIR": str(tmp_path / "data"),
            "CSU_DK_ENV_FILE": str(tmp_path / ".env.absent"),
            "PATH": "/usr/bin:/bin",
        },
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "需要 --from 旧库路径" in output and "用法" in output
    assert "Traceback" not in output
