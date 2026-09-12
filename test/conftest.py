"""测试统一用临时数据目录，并硬堵所有真实网络请求。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("CSU_DK_TEST_DATA_DIR", tempfile.mkdtemp(prefix="csu-dk-test-"))
# 测试不读项目的 .env：那里有真实的发信密钥，会让用例真的发出邮件
os.environ["CSU_DK_ENV_FILE"] = str(Path(os.environ["CSU_DK_TEST_DATA_DIR"]) / ".env.absent")
for _secret in ("TENCENT_SES_SECRET_ID", "TENCENT_SES_SECRET_KEY", "TENCENT_SES_TEMPLATE_ID", "MAIL_FROM"):
    os.environ.pop(_secret, None)
Path(os.environ["CSU_DK_TEST_DATA_DIR"]).mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _ensure_master_key():
    """接口测试不走 lifespan，所以这里保证有一把可用的密钥。"""
    from app import config as cfg

    cfg.ensure_master_key()


@pytest.fixture(autouse=True)
def _forbid_real_school_requests(monkeypatch):
    """任何真的去登录学校的调用都直接失败 —— 学校有 IP 风控，一次都不该试。"""
    def boom(*_args, **_kwargs):
        raise AssertionError("测试里不允许真的请求学校（会触发风控）")

    monkeypatch.setattr("app.checkin.cas_login", boom)
    monkeypatch.setattr("app.checkin.probe_window", boom)
    monkeypatch.setattr("app.accounts.probe_window", boom)
