"""测试统一用临时数据目录，并硬堵所有真实网络请求。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("CSU_DK_TEST_DATA_DIR", tempfile.mkdtemp(prefix="csu-dk-test-"))
os.environ["CSU_DK_ENV_FILE"] = str(Path(os.environ["CSU_DK_TEST_DATA_DIR"]) / ".env.absent")
for _secret in ("TENCENT_SES_SECRET_ID", "TENCENT_SES_SECRET_KEY", "TENCENT_SES_TEMPLATE_ID", "MAIL_FROM"):
    os.environ.pop(_secret, None)
Path(os.environ["CSU_DK_TEST_DATA_DIR"]).mkdir(parents=True, exist_ok=True)

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
    from app import checkin

    checkin.reset_login_state()
    yield
    checkin.reset_login_state()


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
def _forbid_real_school_requests(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("测试里不允许真的请求学校（会触发风控）")

    monkeypatch.setattr("app.checkin.cas_login", boom)
