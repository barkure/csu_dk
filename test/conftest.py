"""测试统一用临时数据目录，并硬堵所有真实网络请求。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="csu-dk-test-"))
# 必须覆盖（不是 setdefault）两个数据目录别名：否则继承的外部配置会把测试写进真实数据目录。
os.environ["CSU_DK_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["CSU_DK_TEST_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["CSU_DK_ENV_FILE"] = str(_TEST_DATA_DIR / ".env.absent")
for _secret in (
    "TENCENT_SES_SECRET_ID", "TENCENT_SES_SECRET_KEY",
    "TENCENT_SES_VERIFICATION_CODE_TEMPLATE_ID", "TENCENT_SES_CREDENTIAL_INVALID_TEMPLATE_ID", "MAIL_FROM",
):
    os.environ.pop(_secret, None)

from app import checkin as _checkin  # noqa: E402 - 必须在上面设好环境变量之后再导入

REAL_CAS_LOGIN = _checkin.cas_login


@pytest.fixture()
def real_login(monkeypatch):
    monkeypatch.setattr(_checkin, "cas_login", REAL_CAS_LOGIN)


@pytest.fixture(autouse=True)
def _ensure_master_key():
    from app import config as cfg

    cfg.ensure_master_key()


@pytest.fixture(autouse=True)
def _disable_login_rate_limits(monkeypatch):
    from app import checkin

    monkeypatch.setattr(checkin, "_global_attempts", None)
    monkeypatch.setattr(checkin, "_attempt_gap", None)


@pytest.fixture(autouse=True)
def _reset_login_state():
    from app import checkin, exits

    checkin.reset_login_state()
    checkin.reset_verifications()
    exits.reset()
    yield
    checkin.reset_login_state()
    checkin.reset_verifications()
    exits.reset()


@pytest.fixture(autouse=True)
def _clean_learned_buildings():
    from app import buildings

    path = buildings._learned_path()
    path.unlink(missing_ok=True)
    buildings.reload()
    yield
    path.unlink(missing_ok=True)
    buildings.reload()


@pytest.fixture(autouse=True)
def _disable_jitter(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.config, "checkin_jitter_meters", 0)


@pytest.fixture(autouse=True)
def _forbid_real_school_requests(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("测试里不允许真的请求学校（会触发风控）")

    monkeypatch.setattr("app.checkin.cas_login", boom)
