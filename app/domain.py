"""领域类型：状态、故障类型、触发方式，以及数据库行与学校接口返回值的形状。

字面量散在各处时，改一个值要全仓库找；这里集中定义，出错在类型检查阶段就能看见。
"""
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
    WAITING = "waiting"      # 还没到打卡时段
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
    window_start: str
    window_end: str
    jd: float | None
    wd: float | None
    dkdz: str
    casual: str | None
    token: str | None
    cookies: str | None
    token_at: str | None
    next_run_at: str | None
    last_run_at: str | None
    last_status: str | None
    last_message: str | None
    needs_reauth: int
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


# ---------- 学校接口的返回形状（只声明我们用到的字段）----------

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


class WindowPlan(TypedDict):
    """添加账号时"提交即验证"探测到的信息。"""

    location: LocationData
    address: str
    session: SessionPayload
    window: tuple[str | None, str | None]


class SessionPayload(TypedDict):
    token: str
    casual: str | None
    cookies: str


class CheckinResult(TypedDict):
    status: str
    message: str
    dksj: NotRequired[str | None]
