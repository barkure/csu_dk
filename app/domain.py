"""领域类型。"""
from __future__ import annotations

from enum import StrEnum
from typing import NotRequired, TypedDict


class AuthError(StrEnum):
    """账号认证故障类型 —— 页面上的账号状态就是它加上"正常"。"""

    NONE = ""
    BAD_CREDENTIALS = "bad_credentials"
    LOCKED = "locked"
    OTHER = "other"


class CheckinStatus(StrEnum):
    SUCCESS = "success"
    SKIPPED = "skipped"      # 学校回"今日已打卡"
    WAITING = "waiting"
    FAILED = "failed"


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class AccountRow(TypedDict):
    """accounts 表一行（密文字段在读出来时已解密）。"""

    id: int
    user_id: int
    csu_username: str
    password_enc: str
    enabled: int
    jd: float | None
    wd: float | None
    dkdz: str
    casual: str | None
    token: str | None
    cookies: str | None
    token_at: str | None
    last_run_at: str | None
    last_status: str | None
    last_message: str | None
    auth_error: str
    created_at: str
    updated_at: str


class UserRow(TypedDict):
    id: int
    email: str
    created_at: str
    last_login_at: str | None


class RecordRow(TypedDict):
    id: int
    account_id: int
    run_at: str
    trigger: str
    status: str
    message: str | None
    dksj: str | None


class DkStatusData(TypedDict, total=False):
    """智慧学工 dkStatus 的 data。"""

    sfydk: bool          # 是否已打卡
    dksj: str            # 打卡时间
    kdk: bool            # 可否打卡
    bkyy: str            # 不可打卡原因
    dkbc: str            # 打卡班次


class LocationData(TypedDict, total=False):
    """位置校验返回的 data。"""

    canDk: bool
    yxMc: str            # 楼栋名
    pcMi: float | int    # 距打卡点的米数
    msg: str
    reason: str


class SchoolResponse(TypedDict, total=False):
    """业务接口通用外壳。"""

    code: str
    message: str
    data: dict


class SessionPayload(TypedDict):
    token: str
    casual: str | None
    cookies: str


class CheckinResult(TypedDict):
    status: str
    message: str
    dksj: NotRequired[str | None]
    paused_until: NotRequired[float]
