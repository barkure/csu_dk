"""配置：pydantic-settings 读环境变量（自动加载 .env），类型与范围都在这里校验。

密钥读/写刻意分开：解密路径永不生成新密钥，只有人主动提交新凭据（写入）时才允许生成。
"""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from email_validator import EmailNotValidError, validate_email
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .errors import ConfigError, MasterKeyInvalidError, MasterKeyMissingError
from .permissions import harden_dir, harden_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 配置文件；测试可通过 CSU_DK_ENV_FILE 隔离
ENV_FILE = os.environ.get("CSU_DK_ENV_FILE", str(PROJECT_ROOT / ".env"))
KEY_BYTES = 32


@dataclass(frozen=True)
class Limit:
    window_ms: int
    max: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CSU_DK_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数据目录；别名仅供测试隔离
    data_dir: Path = Field(default=PROJECT_ROOT / "data", validation_alias="CSU_DK_TEST_DATA_DIR")
    host: str = "127.0.0.1"
    port: int = Field(default=8443, ge=1, le=65535)
    tz: str = "Asia/Shanghai"

    session_days: int = Field(default=14, ge=1, le=3650)
    code_minutes: int = Field(default=10, ge=1, le=1440)
    code_cooldown_seconds: int = Field(default=60, ge=1, le=86_400, validation_alias="CSU_DK_CODE_COOLDOWN")
    email_daily_max: int = Field(default=5, ge=1, le=1000)
    allowed_emails: Annotated[tuple[str, ...], NoDecode] = ()
    max_accounts_per_user: int = Field(default=5, ge=1, le=100, validation_alias="CSU_DK_MAX_ACCOUNTS")
    max_sessions_per_user: int = Field(default=10, ge=1, le=100, validation_alias="CSU_DK_MAX_SESSIONS")
    record_retention_days: int = Field(default=180, ge=1, le=3650)

    cas_concurrency: int = Field(default=1, ge=1, le=8)
    relogin_cooldown_seconds: int = Field(default=60, ge=1, le=86_400, validation_alias="CSU_DK_RELOGIN_COOLDOWN")

    scheduler_interval: int = Field(default=20, ge=1, le=3600, validation_alias="CSU_DK_SCHED_INTERVAL")
    maintenance_interval: int = Field(default=600, ge=1, le=86_400)
    token_ttl_seconds: int = Field(default=86_400, ge=1, le=604_800, validation_alias="CSU_DK_TOKEN_TTL")
    ip_freeze_cooldown_seconds: int = Field(default=3600, ge=1, le=604_800,
                                            validation_alias="CSU_DK_IP_FREEZE_COOLDOWN")
    catchup_minutes: int = Field(default=120, ge=1, le=1440)
    max_window_hours: int = Field(default=6, ge=1, le=24, validation_alias="CSU_DK_MAX_WINDOW")
    window_margin_minutes: int = Field(default=60, ge=0, le=1440, validation_alias="CSU_DK_WINDOW_MARGIN")
    default_window_start: str = "20:00"
    default_window_end: str = "22:30"

    trust_proxy: bool = False
    cookie_secure: bool = False

    rl_ip_window: int = Field(default=86_400, ge=1)
    rl_ip_max: int = Field(default=5, ge=1)
    rl_global_window: int = Field(default=86_400, ge=1)
    rl_global_max: int = Field(default=200, ge=1)
    rl_verify_window: int = Field(default=1800, ge=1)
    rl_verify_max: int = Field(default=15, ge=1)

    # 腾讯云邮件推送
    tencent_ses_secret_id: str = Field(default="", validation_alias="TENCENT_SES_SECRET_ID")
    tencent_ses_secret_key: str = Field(default="", validation_alias="TENCENT_SES_SECRET_KEY")
    tencent_ses_region: str = Field(default="ap-hongkong", validation_alias="TENCENT_SES_REGION")
    tencent_ses_template_id: int = Field(default=0, ge=0, validation_alias="TENCENT_SES_TEMPLATE_ID")
    mail_from: str = Field(default="", validation_alias="MAIL_FROM")


    @field_validator("tencent_ses_template_id", mode="before")
    @classmethod
    def _blank_template_id(cls, value: object) -> object:
        # 空模板 ID 视为未配置
        return 0 if value in ("", None) else value

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (tuple, list)):
            items = [str(item).strip() for item in value]
        else:
            items = [item.strip() for item in str(value or "").split(",")]
        cleaned = []
        for item in items:
            if not item:
                continue
            try:
                cleaned.append(validate_email(item, check_deliverability=False).normalized.lower())
            except EmailNotValidError as error:
                raise ValueError(f"CSU_DK_ALLOWED_EMAILS 里有非法邮箱：{item}（{error}）") from error
        return tuple(cleaned)

    @field_validator("tz")
    @classmethod
    def _known_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as error:
            raise ValueError(f"CSU_DK_TZ 不是合法时区：{value}") from error
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Settings:
        if not self.host.strip():
            raise ValueError("CSU_DK_HOST 不能为空")
        if self.window_margin_minutes >= self.max_window_hours * 60:
            raise ValueError("CSU_DK_WINDOW_MARGIN 不能大于等于 CSU_DK_MAX_WINDOW（会把窗口压到最小 30 分钟）")
        for name in ("rl_ip_window", "rl_global_window", "rl_verify_window"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 至少 1 秒")
        return self

    # 限流配置
    @property
    def request_ip(self) -> Limit:
        return Limit(self.rl_ip_window * 1000, self.rl_ip_max)

    @property
    def request_global(self) -> Limit:
        return Limit(self.rl_global_window * 1000, self.rl_global_max)

    @property
    def verify_ip(self) -> Limit:
        return Limit(self.rl_verify_window * 1000, self.rl_verify_max)


try:
    config = Settings()
except Exception as error:
    raise ConfigError(f"配置有问题，已拒绝启动：\n  - {error}") from error

DATA_DIR = config.data_dir
DB_PATH = DATA_DIR / "csu_dk.db"
MASTER_KEY_PATH = DATA_DIR / "master.key"

harden_dir(DATA_DIR, 0o700)


def validate_key(text: str) -> tuple[bool, str | None]:
    raw = str(text or "").strip()
    if not raw:
        return False, "文件为空"
    try:
        size = len(base64.b64decode(raw, validate=True))
    except Exception:  # noqa: BLE001 - binascii 抛什么类型不稳定，这里只关心能否解开
        return False, "不是合法的 base64"
    if size != KEY_BYTES:
        return False, f"应为 {KEY_BYTES} 字节（base64 44 字符），实际解出 {size} 字节"
    return True, None


@dataclass(frozen=True)
class MasterKeyInfo:
    exists: bool
    valid: bool
    detail: str | None
    key: str | None


def inspect_master_key() -> MasterKeyInfo:
    if not MASTER_KEY_PATH.exists():
        return MasterKeyInfo(False, False, "文件不存在", None)
    harden_file(MASTER_KEY_PATH, 0o600)
    raw = MASTER_KEY_PATH.read_text().strip()
    valid, detail = validate_key(raw)
    return MasterKeyInfo(True, valid, detail, raw if valid else None)


_cached_key: str | None = None


def read_master_key() -> str:
    global _cached_key
    if _cached_key:
        return _cached_key
    info = inspect_master_key()
    if not info.exists:
        raise MasterKeyMissingError(MASTER_KEY_PATH)
    if not info.valid or not info.key:
        raise MasterKeyInvalidError(MASTER_KEY_PATH, info.detail)
    _cached_key = info.key
    return _cached_key


def ensure_master_key() -> bool:
    """返回是否新建。密钥内容非法时拒绝覆盖：那可能是恢复错的密钥。"""
    global _cached_key
    info = inspect_master_key()
    if info.exists and info.valid and info.key:
        _cached_key = info.key
        return False
    if info.exists:
        raise MasterKeyInvalidError(MASTER_KEY_PATH, info.detail)

    key = base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()
    MASTER_KEY_PATH.write_text(key)
    harden_file(MASTER_KEY_PATH, 0o600)
    _cached_key = key
    return True


def reset_master_key_cache() -> None:
    global _cached_key
    _cached_key = None
