"""帮助与非法参数不会启动服务或初始化数据目录。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(("args", "exit_code"), [
    (["-h"], 0),
    (["--help"], 0),
    (["--unknown"], 2),
    (["unknown"], 2),
    (["all", "--help"], 2),
    (["db-check", "extra"], 2),
])
def test_arguments_exit_before_importing_config(tmp_path, args, exit_code):
    data_dir = tmp_path / "data"
    env = {**os.environ, "CSU_DK_DATA_DIR": str(data_dir),
           "CSU_DK_ENV_FILE": str(tmp_path / ".env.absent"), "CSU_DK_PORT": "invalid"}

    result = subprocess.run([sys.executable, "-m", "app", *args],
                            cwd=Path(__file__).resolve().parent.parent, env=env,
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == exit_code, result.stderr
    assert "用法：python -m app" in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not data_dir.exists()
