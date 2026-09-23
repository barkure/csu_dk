"""数据目录配置：双别名优先级、空值拒绝、测试目录隔离。

只重建 Settings() 验证配置行为，不重新 import 应用模块（避免触发生产目录副作用）。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings


def test_production_alias_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("CSU_DK_DATA_DIR", str(tmp_path / "prod"))
    monkeypatch.delenv("CSU_DK_TEST_DATA_DIR", raising=False)
    assert Settings().data_dir == tmp_path / "prod"


def test_test_alias_alone(monkeypatch, tmp_path):
    monkeypatch.delenv("CSU_DK_DATA_DIR", raising=False)
    monkeypatch.setenv("CSU_DK_TEST_DATA_DIR", str(tmp_path / "test"))
    assert Settings().data_dir == tmp_path / "test"


def test_production_alias_wins_when_both_set(monkeypatch, tmp_path):
    monkeypatch.setenv("CSU_DK_DATA_DIR", str(tmp_path / "prod"))
    monkeypatch.setenv("CSU_DK_TEST_DATA_DIR", str(tmp_path / "test"))
    assert Settings().data_dir == tmp_path / "prod", "两者并存时生产别名优先"


def test_default_data_dir_is_project_data(monkeypatch):
    monkeypatch.delenv("CSU_DK_DATA_DIR", raising=False)
    monkeypatch.delenv("CSU_DK_TEST_DATA_DIR", raising=False)
    assert Settings().data_dir == PROJECT_ROOT / "data"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_data_dir_is_rejected(monkeypatch, value):
    monkeypatch.setenv("CSU_DK_DATA_DIR", value)
    with pytest.raises(ValueError):
        Settings()


def test_inherited_data_dir_env_cannot_take_over_test_dir(tmp_path):
    """模拟外部已配置「生产目录」：test/conftest.py 仍须把测试钉在自己的临时目录。

    用子进程完整走一遍 conftest 的启动路径，两个别名都指向模拟目录；
    验证配置与 DB/密钥路径指向子进程自己的测试目录，且模拟目录一个字节都没变。
    """
    fake_production = tmp_path / "fake-production"
    fake_production.mkdir()
    sentinel = fake_production / "sentinel.txt"
    sentinel.write_text("不要动我", encoding="utf-8")
    perms_before = stat.S_IMODE(fake_production.stat().st_mode)
    fake_test_alias = tmp_path / "fake-test-alias"
    fake_test_alias.mkdir()

    script = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import conftest\n"
        "from app import config as cfg\n"
        "print(cfg.config.data_dir)\n"
        "print(cfg.DB_PATH)\n"
        "print(cfg.MASTER_KEY_PATH)\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "CSU_DK_DATA_DIR": str(fake_production),
        "CSU_DK_TEST_DATA_DIR": str(fake_test_alias),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT / "test"),
        env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    data_dir, db_path, key_path = (Path(line) for line in lines[-3:])
    assert data_dir.is_absolute()

    for wrong in (fake_production, fake_test_alias):
        assert data_dir != wrong, "继承的外部数据目录不能接管测试"
        assert db_path.parent != wrong and key_path.parent != wrong
    assert db_path.parent == data_dir, "DB 必须落在测试目录"
    assert key_path.parent == data_dir, "master.key 必须落在测试目录"

    assert list(fake_production.iterdir()) == [sentinel], "模拟生产目录必须一个文件都没多"
    assert sentinel.read_text(encoding="utf-8") == "不要动我"
    assert stat.S_IMODE(fake_production.stat().st_mode) == perms_before, "模拟生产目录权限不能被改动"
    assert list(fake_test_alias.iterdir()) == [], "测试别名指向的模拟目录也不能被写"
